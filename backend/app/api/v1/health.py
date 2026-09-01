"""
健康检查接口

用 @public() 豁免鉴权：容器探活 / 负载均衡健康检查不会带 token，
被守卫拦成 401 会让服务被误判为不可用。
"""
from app.api.auth_route import auth_router, public

router = auth_router()


@router.get("", summary="健康检查")
@public()
async def health_check():
    """返回服务基础状态"""
    return {"status": "ok", "service": "knowledge-hub-backend"}
