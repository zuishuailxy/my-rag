"""
文档审核 DTO（对应 TS dto/review.dto.ts）

- QueryReviewTasksDto → ReviewTaskQuery
- ReviewDecisionDto   → ReviewDecision（通过 / 驳回共用）

请求体字段为 camelCase（reviewComment），响应同样输出 camelCase。

审核人信息**不在请求体里**（TS ReviewDecisionDto 只有 reviewComment，注释写明
「审核人信息从 JWT 取，不在 body 传」）：路由层从 @CurrentUser() 取 userId 与
`realName ?? username`，再传给 DocumentReviewService（参数必填）。

⚠️ 刻意偏离 TS 一处：status 用 Literal 而非自由字符串。TS 只有 @IsString()（无 @IsIn），
传入非枚举值时 listTasks 三个分支都不命中 → 不加 WHERE → 返回全部记录。此处由
Pydantic 直接 422 拦截，不把该缺陷带过来。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# 审核任务筛选值（TS 的联合字面量类型；路由层直接用它做查询参数校验）
ReviewTaskStatus = Literal["pending", "approved", "rejected"]


class ReviewTaskQuery(BaseModel):
    """审核任务列表查询参数（对应 TS QueryReviewTasksDto）"""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )

    # pending=待审 / approved=已通过 / rejected=已驳回
    # 不传（或传 pending）都按待审处理，与 TS `query.status === 'pending' || !query.status` 一致
    status: ReviewTaskStatus | None = Field(
        default=None, description="审核状态过滤（默认 pending）"
    )

    # 页码，从 1 开始（TS 只有 @Min(1)，无上限，此处同样不设 le）
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")

    # 每页条数（TS @Min(1) @Max(100)，与项目 DocumentQuery 约定一致）
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数")


class ReviewDecision(BaseModel):
    """审核通过 / 驳回请求体（对应 TS ReviewDecisionDto）

    只有一个字段：审核意见。审核人信息从 JWT 取（TS 注释：「审核人信息从 JWT 取，
    不在 body 传」），因此这里**不要**再加 reviewerId / reviewerName —— 前端伪造
    审核人身份的空间就是从这种 DTO 里漏出来的。

    「驳回意见必填」由 DocumentReviewService.reject_review 校验并抛
    400「驳回意见不能为空」（与 TS 一致，TS 也未在 DTO 层标必填）。
    """

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        # 覆盖 Swagger 自动示例：驳回场景的合法报文（通过时该字段可省略）
        json_schema_extra={"example": {"reviewComment": "内容需补充后重新提交"}},
    )

    # 审核意见（驳回时必填，由服务层校验）
    review_comment: str | None = Field(default=None, description="审核意见")


class ReviewTaskItem(BaseModel):
    """审核任务响应（对应 kh_document_review 一行；bigint 转字符串，键名 camelCase）"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 审核记录 ID
    id: str
    # 被审文档 ID
    document_id: str
    # 审核人 ID（待审时为 NULL）
    reviewer_id: str | None = None
    # 审核人姓名
    reviewer_name: str | None = None
    # 审核结果：NULL=待审 / 1=通过 / 2=驳回
    review_result: int | None = None
    # 审核意见
    review_comment: str | None = None
    # 提审前的文档状态（0 草稿 / 1 已发布 / 3 待审核）
    before_status: int
    # 审核完成时间
    reviewed_at: datetime | None = None
    # 提交审核时间
    created_at: datetime


class ReviewTaskListResponse(BaseModel):
    """审核任务分页响应"""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    # 当前页数据
    items: list[ReviewTaskItem]
    # 总条数
    total: int
    # 页码
    page: int
    # 每页条数
    page_size: int
