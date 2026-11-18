import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核', migration_status TEXT NOT NULL DEFAULT '未发布')")
    _ensure_columns(con)
    return con

def _ensure_columns(con):
    cols={r['name'] for r in con.execute('PRAGMA table_info(runs)')}
    if 'migration_status' not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN migration_status TEXT NOT NULL DEFAULT '未发布'")

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    result['migration_status']=row['migration_status']
    if result.get('migration') and row['migration_status']=='已发布':
        # 发布态以持久化列为准；未发布时保留本次计算的待发布/BLOCKED。
        result['migration']['publish_status']='已发布'
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status,migration_status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新',409)
    return get(path,run_id)

def publish_migration(path,run_id):
    with connect(path) as con:
        row=con.execute('SELECT migration_status,result FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
        if row['migration_status']!='已发布':
            result=json.loads(row['result'])
            if result.get('migration'): result['migration']['publish_status']='已发布'
            con.execute("UPDATE runs SET migration_status='已发布', result=? WHERE id=?",(json.dumps(result,ensure_ascii=False),run_id))
    return get(path,run_id)
