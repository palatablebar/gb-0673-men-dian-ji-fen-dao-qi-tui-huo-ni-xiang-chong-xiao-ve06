from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.errors import BusinessError
from app.web import render
from app import store
router = APIRouter()


def _db():
    # 与 web.py 一致：运行时解析，便于测试通过 main.DB_PATH 切换临时库。
    from app.main import DB_PATH
    return DB_PATH


@router.get('/migration')
def page(request: Request):
    return render(request, 'migration')


@router.post('/api/migration')
def calculate(payload: dict = Body(default={})):
    return evaluate(payload, until=8)


@router.post('/api/runs/{run_id}/migration/publish')
def publish(run_id: str):
    result = store.get(_db(), run_id)
    migration = result.get('migration')
    if not migration:
        raise BusinessError('INCOMPLETE_RUN', '批次尚未执行规则迁移')
    if not migration['eligible']:
        # 对账未通过或切换失败：保留旧版余额，拒绝发布。
        raise BusinessError('MIGRATION_BLOCKED', '对账未通过或基线不平衡，保留旧版余额，拒绝发布')
    # 幂等发布：已发布批次重复调用直接返回，不重复计入任何差异。
    return store.publish_migration(_db(), run_id)
