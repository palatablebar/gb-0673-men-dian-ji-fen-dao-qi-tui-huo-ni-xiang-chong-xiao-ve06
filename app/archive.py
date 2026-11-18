import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核'),'migration_status':result.get('migration_status','未发布')},ensure_ascii=False,indent=2))
        evidence={'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']}
        archive.writestr('evidence.json',json.dumps(evidence,ensure_ascii=False,indent=2))
        if result.get('migration'):
            archive.writestr('migration.json',json.dumps({'migration_status':result.get('migration_status','未发布'),'migration':result['migration']},ensure_ascii=False,indent=2))
    return buffer.getvalue()
