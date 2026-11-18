from datetime import timedelta
from app.coerce import instant
from app.config import RULE_VERSION
from app.errors import BusinessError
from app.modules.consumption.rules import replay

# 差异去向码：未保留（提前到期作废）与保留（按新规则重定到期）。
EARLY_EXPIRY = 'EARLY_EXPIRY'
RESCHEDULED = 'RESCHEDULED'
UNCHANGED = 'UNCHANGED'
DROPPED_REASONS = {EARLY_EXPIRY}


def load_config(context):
    cfg = context.get('migration')
    if not isinstance(cfg, dict):
        raise BusinessError('MIGRATION_CONFIG', '迁移配置缺失')
    try:
        cutover = instant(cfg['cutover_at'])
        target = str(cfg['target_version'])
        days = int(cfg['validity_days'])
    except (KeyError, TypeError, ValueError):
        raise BusinessError('MIGRATION_CONFIG', '迁移配置缺失或无效')
    if not target or days <= 0:
        raise BusinessError('MIGRATION_CONFIG', '迁移配置缺失或无效')
    return cutover, target, days, str(cfg.get('description', ''))


def decide_lot(lot, cutover, target, days):
    """对切换时点仍有余额的旧批次逐条裁决，返回(新批次或None, 差异去向行)。"""
    issued_at = instant(lot['at'])
    old_expires = instant(lot['expires'])
    carried = int(lot['remaining'])
    new_expires = issued_at + timedelta(days=days)
    common = {
        'member': lot['member'], 'source_lot': lot['id'], 'source_event': lot['id'],
        'line': lot.get('line', 0), 'old_expires': old_expires.isoformat(),
        'new_expires': new_expires.isoformat(), 'carried': carried,
        'target_version': target,
    }
    if new_expires <= cutover:
        row = {**common, 'reason_code': EARLY_EXPIRY, 'disposition': '提前到期作废，不进入新版',
               'kept': 0, 'dropped': carried}
        return None, row
    if new_expires == old_expires:
        reason, detail = UNCHANGED, '新旧到期一致，原样保留'
    elif new_expires < old_expires:
        reason, detail = RESCHEDULED, '新规则缩短有效期，余额保留并按新到期管理'
    else:
        reason, detail = RESCHEDULED, '新规则延长有效期，余额保留并按新到期管理'
    new_lot = {
        'id': f"{lot['id']}@{target}", 'member': lot['member'], 'source_lot': lot['id'],
        'source_event': lot['id'], 'issued': carried, 'remaining': carried,
        'expires': new_expires.isoformat(), 'at': cutover.isoformat(),
        'issued_at': issued_at.isoformat(), 'rule_version': target, 'line': lot.get('line', 0),
    }
    row = {**common, 'reason_code': reason, 'disposition': detail,
           'kept': carried, 'dropped': 0}
    return new_lot, row


def migrate(state):
    cutover, target, days, description = load_config(state['context'])
    # 旧版只重放到切换时点：切换前的批次、消费、退款按旧规则原样追溯，不重写历史。
    old = replay(state['records'], state['lots'], cutover.isoformat())
    surviving = [lot for lot in old['lots'] if lot['remaining'] > 0]
    new_lots, rows = [], []
    for lot in sorted(surviving, key=lambda r: (r['member'], r['expires'], r['at'], r['id'])):
        new_lot, row = decide_lot(lot, cutover, target, days)
        if new_lot is not None:
            new_lots.append(new_lot)
        rows.append(row)
    old_remaining = sum(int(r['remaining']) for r in old['lots'])
    kept = sum(r['kept'] for r in rows)
    dropped = sum(r['dropped'] for r in rows)
    # 会员级守恒：每个会员旧版在切余额都能解释为保留+差异。
    member_rows = []
    old_by_member, kept_by_member, dropped_by_member = {}, {}, {}
    for lot in old['lots']:
        old_by_member[lot['member']] = old_by_member.get(lot['member'], 0) + int(lot['remaining'])
    for row in rows:
        kept_by_member[row['member']] = kept_by_member.get(row['member'], 0) + row['kept']
        dropped_by_member[row['member']] = dropped_by_member.get(row['member'], 0) + row['dropped']
    for member in sorted(old_by_member):
        member_rows.append({
            'member': member, 'old_remaining': old_by_member[member],
            'kept': kept_by_member.get(member, 0), 'dropped': dropped_by_member.get(member, 0),
            'balanced': old_by_member[member] == kept_by_member.get(member, 0) + dropped_by_member.get(member, 0),
        })
    balanced = old_remaining == kept + dropped and all(r['balanced'] for r in member_rows)
    # 每条未保留差异必须落在已登记的作废去向码内，去向不明即裁决失败。
    reasons_known = all(
        r['reason_code'] in DROPPED_REASONS or r['dropped'] == 0 for r in rows
    )
    # 基线（切换前对账与退货核验）不平衡时，迁移不得发布。
    base_balanced = state['balance']['row_balanced'] and state['balance']['points_balanced'] and not state['refund_issues']
    eligible = balanced and base_balanced and reasons_known
    return {
        'cutover_at': cutover.isoformat(), 'target_version': target,
        'validity_days': days, 'description': description,
        'old_rule_version': RULE_VERSION,
        'old_remaining': old_remaining, 'new_remaining': kept,
        'dropped_total': dropped, 'kept_total': kept,
        'member_balanced': balanced, 'base_balanced': base_balanced,
        'migration_balanced': balanced, 'reasons_known': reasons_known,
        'eligible': eligible,
        'publish_status': '待发布' if eligible else 'BLOCKED',
        'new_lots': new_lots, 'differences': rows, 'members': member_rows,
    }


def apply(state):
    result = migrate(state)
    state['migration'] = result
    state['views']['migration'] = {
        'title': '规则迁移', 'rows': result['differences'],
        'metrics': {
            '旧版可用': result['old_remaining'], '新版可用': result['new_remaining'],
            '未保留差异': result['dropped_total'], '发布状态': result['publish_status'],
        },
    }
