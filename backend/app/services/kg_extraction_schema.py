"""
通用知识图谱抽取 Schema（对应 TS kg-extraction.schema.ts）

@see docs/kg-extraction-schema.md

- KG_ENTITY_TYPES / KG_RELATION_TYPES：实体 / 语义关系枚举
  （Neo4j 边类型仍为 RELATED_TO，relation 存为边属性）
- normalize_entity_type / normalize_relation_type：LLM 返回类型规范到枚举，
  未知值走兜底（CONCEPT / RELATED_TO）
- KgExtractionLlmOutput：LLM 结构化输出（对齐 TS zod schema，供
  with_structured_output 使用，工具名 extract_knowledge_graph）
- build_extraction_system_prompt：系统提示词（与 TS 逐字一致）
"""
from pydantic import BaseModel, Field

# 核心 + 扩展实体类型
KG_ENTITY_TYPES = (
    "PERSON",
    "ORGANIZATION",
    "CONCEPT",
    "DOCUMENT",
    "PROCESS",
    "PRODUCT",
    "LOCATION",
    "TIME",
    "POLICY",
    "RESOURCE",
)

# 实体间语义关系（Neo4j 边属性 relation；边类型仍为 RELATED_TO）
KG_RELATION_TYPES = (
    "HAS_PART",
    "BELONGS_TO",
    "RELATED_TO",
    "DEFINES",
    "REQUIRES",
    "USES",
    "RESPONSIBLE_FOR",
    "PARTICIPATES_IN",
    "LOCATED_IN",
    "OCCURS_AT",
    "CAUSES",
    "CONFLICTS_WITH",
)

_ENTITY_TYPE_SET = frozenset(KG_ENTITY_TYPES)
_RELATION_TYPE_SET = frozenset(KG_RELATION_TYPES)


def normalize_entity_type(raw: str | None) -> str:
    """将 LLM 返回的类型规范到实体枚举，未知值走兜底 CONCEPT"""
    upper = (raw or "").strip().upper()
    if upper in _ENTITY_TYPE_SET:
        return upper
    return "CONCEPT"


def normalize_relation_type(raw: str | None) -> str:
    """将 LLM 返回的类型规范到关系枚举，未知值走兜底 RELATED_TO"""
    upper = (raw or "").strip().upper()
    if upper in _RELATION_TYPE_SET:
        return upper
    return "RELATED_TO"


class KgEntityLlmOutput(BaseModel):
    """LLM 结构化输出：实体（对齐 TS kgExtractedEntitySchema）"""

    name: str = Field(description="文中原文实体名")
    # string 而非 enum：模型常返回中文类型或额外字段，交给 normalize_entity_type 归类
    type: str | None = Field(default=None, description="实体类型")
    description: str | None = Field(default=None, description="简短描述，可空")
    aliases: list[str] | None = Field(default=None, description="别名")


class KgRelationLlmOutput(BaseModel):
    """LLM 结构化输出：关系（对齐 TS kgExtractedRelationSchema）"""

    source: str = Field(description="起点实体 name，必须是已抽取实体")
    target: str = Field(description="终点实体 name，必须是已抽取实体")
    # 用 string 而非 enum：模型常写 type 或自造类型（如 APPLIES_TO），
    # 整段 enum 校验失败会丢整块抽取
    relation: str | None = Field(default=None, description="关系类型，字段名必须是 relation")
    type: str | None = Field(default=None, description="兼容：模型误把关系类型写成 type 时读取")
    weight: float | None = Field(default=None, ge=0, le=1, description="置信度 0-1")


class KgExtractionLlmOutput(BaseModel):
    """LLM 结构化输出：单个 chunk 的抽取结果（对齐 TS kgExtractionResultSchema）"""

    entities: list[KgEntityLlmOutput] = Field(description="实体列表")
    relations: list[KgRelationLlmOutput] = Field(description="关系列表")


def build_extraction_system_prompt(max_entities: int, max_relations: int) -> str:
    """构建 LLM system prompt（与 TS buildExtractionSystemPrompt 逐字一致）"""
    return f"""你是知识图谱构建专家。请严格从文档片段中抽取知识实体和关系。

## 抽取规则
1. 只抽取文中明确提到的、有实际意义的实体，不要臆测
2. 不要抽取过于泛化的词（如「系统」「功能」「数据」「问题」）
3. 实体名使用文中原文；别名放入 aliases
4. 关系必须有文中依据：**优先抽同一句内共现的实体对**，其次相邻句；不要跨段硬凑。
   source/target 必须是已抽取实体的 name
5. 每个片段最多 {max_entities} 个实体、{max_relations} 个关系
6. 无法归类时用实体类型 CONCEPT、关系类型 RELATED_TO

## 实体类型
- PERSON: 人物、角色（如 张三、审核员）
- ORGANIZATION: 组织、部门（如 研发中心、财务部）
- CONCEPT: 术语、概念（如 分布式事务、试用期）
- DOCUMENT: 文档、规范（如 《员工手册》）
- PROCESS: 流程、活动（如 入职流程、发布流程）
- PRODUCT: 产品、系统（如 知识库、CRM、Redis）
- LOCATION: 地点（如 北京、会议室 A）
- TIME: 时间、周期（如 2026-Q1、每周一）
- POLICY: 政策、制度条款
- RESOURCE: 文件、工具、设备（如 Docker、培训课件）

## 关系类型
- HAS_PART: 组成、包含
- BELONGS_TO: 归属
- RELATED_TO: 泛关联（兜底）
- DEFINES: 定义、解释
- REQUIRES: 前置条件、依赖
- USES: 使用
- RESPONSIBLE_FOR: 负责
- PARTICIPATES_IN: 参与
- LOCATED_IN: 位于
- OCCURS_AT: 发生于（时间）
- CAUSES: 导致、因果
- CONFLICTS_WITH: 冲突、例外

只返回 JSON，不要 markdown 或其它说明。关系对象的字段名是 relation（不要写成 type）。示例：
{{"entities":[{{"name":"财务部","type":"ORGANIZATION","description":"","aliases":[]}}],"relations":[{{"source":"财务部","target":"差旅报销","relation":"RESPONSIBLE_FOR","weight":0.8}}]}}"""
