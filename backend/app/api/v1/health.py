"""
健康检查接口
"""
from fastapi import APIRouter

router = APIRouter()


@router.get("", summary="健康检查")
async def health_check():
    """返回服务基础状态"""
    return {"status": "ok", "service": "knowledge-hub-backend"}
