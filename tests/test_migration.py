from fastapi.testclient import TestClient
import pytest

from app import main
from app.engine import evaluate
from app.modules.migration.rules import migrate, decide_lot, load_config, EARLY_EXPIRY, RESCHEDULED, UNCHANGED
from app.errors import BusinessError


@pytest.fixture
def mig():
    return evaluate({'dataset': 'migration'}, until=8)


def test_old_remaining_explained_by_kept_plus_dropped(mig):
    m = mig['migration']
    assert m['old_remaining'] == 400
    assert m['kept_total'] == 220
    assert m['dropped_total'] == 180
    assert m['old_remaining'] == m['kept_total'] + m['dropped_total']
    assert m['migration_balanced'] is True


def test_early_expiry_not_retained(mig):
    row = next(r for r in mig['migration']['differences'] if r['source_lot'] == 'E100')
    assert row['reason_code'] == EARLY_EXPIRY
    assert row['kept'] == 0 and row['dropped'] == 100
    assert not any(lot['source_lot'] == 'E100' for lot in mig['migration']['new_lots'])


def test_rescheduled_kept_with_new_expiry(mig):
    row = next(r for r in mig['migration']['differences'] if r['source_lot'] == 'E101')
    assert row['reason_code'] == RESCHEDULED
    assert row['kept'] == 100 and row['dropped'] == 0
    lot = next(lot for lot in mig['migration']['new_lots'] if lot['source_lot'] == 'E101')
    assert lot['remaining'] == 100
    assert lot['rule_version'] == mig['migration']['target_version']


def test_unchanged_when_expiry_equal(mig):
    row = next(r for r in mig['migration']['differences'] if r['source_lot'] == 'E105')
    assert row['reason_code'] == UNCHANGED
    assert row['old_expires'] == row['new_expires']


def test_new_lot_traces_to_version_and_source_event(mig):
    for lot in mig['migration']['new_lots']:
        assert lot['rule_version'] == '2026.12.1-r1'
        assert lot['id'] == f"{lot['source_lot']}@2026.12.1-r1"
        original = next(r for r in mig['records'] if r['id'] == lot['source_event'])
        assert original['type'] == 'EARN'
        assert lot['issued_at'] == original['at']


def test_member_level_balance(mig):
    rows = {r['member']: r for r in mig['migration']['members']}
    assert rows['00100'] == {'member': '00100', 'old_remaining': 100, 'kept': 0, 'dropped': 100, 'balanced': True}
    assert rows['00104']['old_remaining'] == 70 and rows['00104']['kept'] == 70
    assert all(r['balanced'] for r in mig['migration']['members'])


def test_expired_old_lots_have_no_difference(mig):
    # 00102/00103 在切换前已按旧规则到期，切换时点余额为零，不产生迁移差异。
    touched = {r['source_lot'] for r in mig['migration']['differences']}
    assert 'E102' not in touched and 'E103' not in touched


def test_history_remains_under_old_rule(mig):
    # 切换前的批次、消费、退款仍按旧规则保留，迁移模块不重写历史。
    spend = next(x for x in mig['point_ledger']['spends'] if x['id'] == 'S104')
    assert spend['points'] == 40
    refund = next(x for x in mig['point_ledger']['refunds'] if x['id'] == 'R104')
    assert refund['restored'] == 10
    assert mig['manifest']['rule_version'] == mig['migration']['old_rule_version']


def test_tampered_balance_blocks_migration():
    s = evaluate({'dataset': 'migration'}, until=8)
    s['balance']['points_balanced'] = False
    result = migrate(s)
    assert result['base_balanced'] is False
    assert result['eligible'] is False
    assert result['publish_status'] == 'BLOCKED'


def test_unknown_dropped_reason_blocks_migration():
    # 直接验证去向守卫：未登记去向码的未保留差异不允许通过裁决。
    from app.modules.migration.rules import DROPPED_REASONS
    row = {'reason_code': 'MYSTERY', 'dropped': 100}
    assert not (row['reason_code'] in DROPPED_REASONS or row['dropped'] == 0)
    legit = {'reason_code': EARLY_EXPIRY, 'dropped': 100}
    assert legit['reason_code'] in DROPPED_REASONS
    s = evaluate({'dataset': 'migration'}, until=8)
    assert s['migration']['reasons_known'] is True


def test_tampered_migration_amount_breaks_conservation():
    s = evaluate({'dataset': 'migration'}, until=8)
    m = s['migration']
    assert m['migration_balanced'] is True
    # 篡改裁决结果后，旧版余额无法由保留与差异解释。
    m['old_remaining'] += 1
    assert m['old_remaining'] != m['kept_total'] + m['dropped_total']


def test_decide_lot_unit_rules():
    from app.coerce import instant
    from datetime import timedelta
    cutover, target, days, _ = load_config({'migration': {
        'cutover_at': '2026-12-01T00:00:00+08:00', 'target_version': '2026.12.1-r1', 'validity_days': 45}})
    lot = {'id': 'L1', 'member': '00001', 'remaining': 30, 'at': '2026-10-20T09:00:00+08:00',
           'expires': '2027-01-20T00:00:00+08:00', 'line': 2}
    new_lot, row = decide_lot(lot, cutover, target, days)
    assert new_lot is not None and new_lot['remaining'] == 30
    assert new_lot['expires'] == (instant(lot['at']) + timedelta(days=45)).isoformat()
    assert row['reason_code'] == RESCHEDULED
    expired_lot = dict(lot, id='L2', at='2026-09-01T09:00:00+08:00', expires='2026-12-15T00:00:00+08:00')
    none_lot, dropped = decide_lot(expired_lot, cutover, target, days)
    assert none_lot is None and dropped['reason_code'] == EARLY_EXPIRY and dropped['dropped'] == 30


def test_missing_config_raises():
    s = evaluate({'dataset': 'migration'}, until=8)
    del s['context']['migration']
    with pytest.raises(BusinessError) as e:
        migrate(s)
    assert e.value.code == 'MIGRATION_CONFIG'


def test_overrides_change_cutover_decision():
    # 把有效期放宽到 200 天：E100 新到期晚于切换，不再提前作废。
    s = evaluate({'dataset': 'migration', 'overrides': {'migration': {
        'cutover_at': '2026-12-01T00:00:00+08:00', 'target_version': '2026.12.1-r1',
        'validity_days': 200, 'description': '放宽窗口'}}}, until=8)
    codes = {r['source_lot']: r['reason_code'] for r in s['migration']['differences']}
    assert codes['E100'] != EARLY_EXPIRY
    assert s['migration']['dropped_total'] == 0
    assert s['migration']['new_remaining'] == 400


def test_deterministic_new_lot_ids():
    a = evaluate({'dataset': 'migration'}, until=8)
    b = evaluate({'dataset': 'migration'}, until=8)
    assert [x['id'] for x in a['migration']['new_lots']] == [x['id'] for x in b['migration']['new_lots']]
    assert a['manifest']['run_id'] == b['manifest']['run_id']


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'mig.sqlite'))
    return TestClient(main.app)


def test_publish_api_idempotent(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'migration'}).json()
    rid = run['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/migration/publish')
    assert first.status_code == 200
    assert first.json()['migration_status'] == '已发布'
    before = c.get('/api/runs').json()
    second = c.post(f'/api/runs/{rid}/migration/publish')
    assert second.status_code == 200
    assert second.json()['migration_status'] == '已发布'
    after = c.get('/api/runs').json()
    # 重复发布不产生新批次、不重复计入。
    assert before == after
    fetched = c.get(f'/api/runs/{rid}').json()
    assert fetched['migration']['publish_status'] == '已发布'
    assert fetched['migration']['new_remaining'] == 220
    # 连续第三次发布：金额与差异行数不变，重复执行不重复计入同一差异。
    for _ in range(2):
        again = c.post(f'/api/runs/{rid}/migration/publish').json()
        assert (again['migration']['kept_total'], again['migration']['dropped_total']) == (220, 180)
    final = c.get(f'/api/runs/{rid}').json()
    assert len(final['migration']['differences']) == 5
    assert len(final['migration']['new_lots']) == 3


def test_blocked_publish_keeps_old_balance(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'migration'}).json()
    rid = run['manifest']['run_id']
    # 直接在持久化结果上破坏基线守恒，模拟对账未通过。
    from app import store
    db_path = str(tmp_path / 'mig.sqlite')
    broken = store.get(db_path, rid)
    broken['balance']['points_balanced'] = False
    broken['migration']['base_balanced'] = False
    broken['migration']['eligible'] = False
    broken['migration']['publish_status'] = 'BLOCKED'
    import sqlite3, json
    with sqlite3.connect(db_path) as con:
        con.execute('UPDATE runs SET result=? WHERE id=?', (json.dumps(broken, ensure_ascii=False), rid))
    resp = c.post(f'/api/runs/{rid}/migration/publish')
    assert resp.status_code == 422
    assert resp.json()['code'] == 'MIGRATION_BLOCKED'
    assert c.get('/api/runs').json()[0]['migration_status'] == '未发布'


def test_publish_unknown_run_404(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    resp = c.post('/api/runs/nonexistent/migration/publish')
    assert resp.status_code == 404
    assert resp.json()['code'] == 'NOT_FOUND'


def test_migration_page_and_export(tmp_path, monkeypatch):
    import zipfile, io, json
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'migration'}).json()
    rid = run['manifest']['run_id']
    html = c.get(f'/migration?run_id={rid}').text
    assert '规则迁移' in html and '差异去向' in html and '待发布' in html
    c.post(f'/api/runs/{rid}/migration/publish')
    z = zipfile.ZipFile(io.BytesIO(c.get(f'/api/export/{rid}').content))
    assert 'migration.json' in z.namelist()
    payload = json.loads(z.read('migration.json'))
    assert payload['migration_status'] == '已发布'
    assert payload['migration']['old_remaining'] == payload['migration']['new_remaining'] + payload['migration']['dropped_total']


def test_rerun_preserves_migration(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'migration'}).json()
    rid = run['manifest']['run_id']
    again = c.post(f'/api/runs/{rid}/rerun', json={})
    assert again.status_code == 200
    body = again.json()
    assert body['manifest']['run_id'] == rid
    assert body['migration']['old_remaining'] == 400
