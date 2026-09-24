"""团队管理 DTO 与响应模型

字段逐字对齐 CreateTeamDto / UpdateTeamDto / QueryTeamDto；
camelCase 别名由 alias_generator=to_camel 处理（与 permission.py 同款约定）。

：()，超长会撞 PG 22001 → 500，
此处按 init.sql kh_team 列宽补 max_length（teamName 100 / teamCode 50 /
description 500）；必填的 teamName 补 min_length=1（对齐 role.py 同款处理）。

bigint 列：请求体里 leaderId / parentId 是雪花字符串；
响应体里 id / leaderId / parentId 一律转字符串（全项目约定）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

__all__ = [
    "CreateTeamRequest",
    "QueryTeamRequest",
    "TeamDetailResponse",
    "TeamMemberResponse",
    "TeamPageResponse",
    "TeamResponse",
    "TeamTreeResponse",
    "UpdateTeamRequest",
]


class CreateTeamRequest(BaseModel):
    """创建团队请求体"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={
            "example": {
                "teamName": "前端开发组",
                "teamCode": "FRONTEND_TEAM",
                "description": "前端开发",
                "parentId": "8000000000000000001",
                "leaderId": "1000000000000000001",
                "sort": 1,
                "status": 1,
            }
        },
    )

    # 团队名称（展示用）
    team_name: str = Field(..., min_length=1, max_length=100, description="团队名称（1-100 字符）")
    # 团队编码
    team_code: str | None = Field(default=None, max_length=50, description="团队编码（可选）")
    description: str | None = Field(default=None, max_length=500, description="描述（可选）")
    # 负责人 → kh_user.id（雪花字符串；存在性校验在服务层）
    leader_id: str | None = Field(default=None, description="负责人用户 ID（雪花，可选）")
    # 父团队 ID（雪花字符串），不传 / '0' 视为根
    parent_id: str | None = Field(default=None, description="父团队 ID（雪花，不传为根团队）")
    sort: int | None = Field(default=None, description="排序，默认 0")
    # status 无取值约束（0 禁用 1 启用），默认 1
    status: int | None = Field(default=None, description="0 禁用 1 启用，默认 1")


class UpdateTeamRequest(BaseModel):
    """更新团队请求体：status 无取值约束（传 0 即禁用）；leaderId / parentId 由服务层校验存在性
。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        json_schema_extra={"example": {"teamName": "前端开发组（改）", "status": 0}},
    )

    team_name: str | None = Field(
        default=None, min_length=1, max_length=100, description="团队名称"
    )
    team_code: str | None = Field(default=None, max_length=50, description="团队编码")
    description: str | None = Field(default=None, max_length=500, description="描述")
    leader_id: str | None = Field(default=None, description="负责人用户 ID（雪花）")
    parent_id: str | None = Field(default=None, description="父团队 ID（雪花）")
    sort: int | None = Field(default=None, description="排序")
    status: int | None = Field(default=None, description="0 禁用 1 启用")


class QueryTeamRequest(BaseModel):
    """团队分页查询"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    keyword: str | None = Field(default=None, description="模糊匹配团队名称 / 编码")
    status: int | None = Field(default=None, description="按状态筛选：0 禁用 1 启用")
    page: int = Field(default=1, ge=1, description="页码，默认 1")
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数，默认 20，上限 100")


# ---------------- 响应模型 ----------------


class TeamResponse(BaseModel):
    """团队响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    id: str
    team_name: str
    team_code: str | None = None
    description: str | None = None
    leader_id: str | None = None
    parent_id: str
    sort: int
    status: int
    created_at: datetime
    updated_at: datetime
    deleted: bool


class TeamDetailResponse(TeamResponse):
    """团队详情响应"""

    member_count: int = 0


class TeamPageResponse(BaseModel):
    """团队分页响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    items: list[TeamResponse]
    total: int
    page: int
    page_size: int


class TeamTreeResponse(TeamResponse):
    """团队树节点"""

    children: list["TeamTreeResponse"] = Field(default_factory=list)


class TeamMemberResponse(BaseModel):
    """团队成员响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    user_id: str
    member_role: str
    username: str
    real_name: str | None = None
