# TODO / 待完善

> 面向「可展示 + 面试可深挖」的完善清单，按性价比排序。
> 勾选项为已完成。

## P0 · 最该做（面试硬指标 / AI 岗深度）

- [x] **README** —— 从空文件补成完整仓库门面（定位 / 架构图 / 快速启动 / 核心设计 / API / 边界）。
- [x] **单元测试（pytest）** —— `backend/tests/`，26 个纯逻辑用例（mock 外部依赖，不连 PG/ES/Neo4j）：
  - [x] RRF 融合（`search.hybrid_search`，mock 两路召回 + PG）
  - [x] 分块器 `chunker`（块序号 / 稳定 ID / heading 继承）
  - [x] `to_eval`（retry 前缀映射）、`_to_plan`（意图→工具开关）、`summarize_hits`（截断回归）
  - [x] `normalize_entity_name` / `split_graph_keywords` / `merge_graph_hits` / `_evidence_snippet`
  - [x] `_pack_retrieve_result`（全局编号 / 事件）、citation 解析 `[n]`
  - [x] 可见性：`empty_scope` / `neo4j_access_params` / `neo4j_document_access_where`
  - [x] `grade_hits`（注入假判定器：空召回跳过 / 切题 / 不切题 / 失败放行）
  - [x] RBAC 权限矩阵（mock PG 会话：ADMIN 追加操作权限 / 直接+角色合并去重 / 非法 uid）
  - [x] 入参校验（超长字段被拦 → HTTP 422，不落库变 500）
- [x] **CI（GitHub Actions）** —— `.github/workflows/ci.yml`：后端 `ruff + mypy(0 issue) + pytest`，前端 `tsc -b + build`；PR / push 自动跑。
- [x] **RAG 评测集（检索侧）** —— `eval/rag_eval.jsonl`（29 条：25 正 + 4 负）+ `scripts/eval_retrieval.py`；
      指标 `recall@k` / `hit@1` / `MRR` / 负例空召回率；支持扫参数（top-k / min-score / rerank / hybrid-top-k / rrf-c）。
      实测：`minScore=0.4` → recall@5 0.28（过杀）、`0.1` → 1.00 且负例全空；关 rerank → 负例空召回率 0.0（噪声进上下文）。
  - [ ] 生成指标 `faithfulness` / 引用准确率（需 LLM 判定）
  - [ ] 评测集扩到 30~50 条 + 更多文档（当前 KB 仅 2 篇公开文档，区分度有限）

## P1 · 加分项

- [x] **图片 OCR（多模态第一步）** —— jpg/png/webp/bmp/gif → 视觉模型（Qwen-VL via DashScope 兼容口）
      OCR/描述 → Markdown 入索引；未配视觉模型 Key 时明确 400。配置：`VISION_API_KEY` / `VISION_MODEL` / `VISION_BASE_URL`。
- [ ] **多模态（剩余）** —— PDF/Word 内嵌图片 OCR、音频 ASR、视频理解；补齐后才可写完整「多模态」。
- [ ] **调参实验** —— `chunk 512/64`、`minScore=0.1` 均为默认值；做 chunk size / topK 扫描，用数据支撑选型。
- [ ] **成本与限流** —— LLM 调用无预算控制；加每用户/每会话调用上限 + token/成本日志。
- [ ] **可观测性** —— LangSmith 已接但无面板；加请求级 latency / 命中率 / 成本统计。

## P2 · 工程完善

- [ ] **增量索引 / 文档去重** —— 当前重复发布触发全量重建；做按 `content hash` 跳过未变文档。
- [ ] **知识图谱实体链接** —— 抽取概率性导致同物异名（已归一书名号）；可加别名/同义词归并，降低漏匹配。
- [ ] **KG 抽取稳定性** —— LLM 重抽实体集有方差；可加「多次抽取取并集」或更明确的必抽类型约束。
- [ ] **生产部署** —— 目前仅本地 compose；补 nginx + uvicorn workers + 迁移 + 健康检查（或 k8s）。

## Chore · 收尾

- [ ] **注释润色** —— 去迁移残留引用后，少数注释略生硬（个别空 `#`、悬空标点），逐文件润色。
- [ ] **文档对齐** —— 已删 `架构.md` / `需求.md` / `简历素材-my-rag.md`；保留的模块文档随代码演进同步。
- [ ] **提交规范** —— 按 `feat/fix/docs/chore` 拆分提交，避免大杂烩。
