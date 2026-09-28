"""
健康检查接口

用 @public() 豁免鉴权：容器探活 / 负载均衡健康检查不会带 token，
被守卫拦成 401 会让服务被误判为不可用。

**语义：本端点是 liveness（存活），不是 readiness（就绪）** —— 
即使数据库不可用也返回 200，原因有两个：
  1. 进程确实活着且可服务（非 DB 接口都正常），返回 5xx 会让编排层无谓地重启它；
  2. 应用已是**降级启动**（PG 挂了也不会退出），就绪与否应该看下层的 `database` 字段。
编排层/负载均衡请用 `database == "ok"` 做 readiness 判定。
"""
from app.api.auth_route import auth_router, public
from app.db.session import db_state

router = auth_router()


@router.get("", summary="健康检查")
@public()
async def health_check():
    """返回服务基础状态 + 依赖（数据库）状态

    降级时给出原因，便于直接定位（而不用去翻日志）。
    """
    return {
        "status": "ok",
        "service": "knowledge-hub-backend",
        "database": "ok" if db_state.ready else "unavailable",
        "databaseError": db_state.last_error,
    }
