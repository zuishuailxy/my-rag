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
  - [x] 评测集扩到 72 条（66 正 + 6 负，5 篇文档），含 17 条难题。
  - [x] **生成指标 `faithfulness` / 引用准确率** —— `app/services/answer_grader.py`（LLM 判分）
        + `eval/gen_eval.jsonl`（12 条）+ `scripts/eval_generation.py`（含确定性的要点覆盖率/引用越界/拒答率）。
        实测：要点覆盖率 / 引用率 / 拒答率 / 忠贞度 / 引用准确率均 1.00。
        ⚠️ 首轮引用准确率 0.80 是**判分器口径错**（把 `[1]` 当成小节而非整块 chunk），人工核对后修正 prompt → 1.00。
  - [ ] **生成侧评测集扩大 + 加难** —— 当前 12 条全满分，区分度有限；补「资料部分相关 / 多跳」用例。
- [x] **LangSmith 评测台** —— `scripts/eval_generation.py --push-dataset`（检索集 72 + 生成集 12）+ `--langsmith`
      （跑 experiment 拿对比视图）；评测器与离线脚本**共用同一份**代码与评测集。
      约定：离线脚本（零依赖、可进 CI）是主入口，LangSmith 只负责演示与对比。

## P1 · 加分项

- [x] **图片 OCR（多模态第一步）** —— jpg/png/webp/bmp/gif → 视觉模型（Qwen-VL via DashScope 兼容口）
      OCR/描述 → Markdown 入索引；未配视觉模型 Key 时明确 400。配置：`VISION_API_KEY` / `VISION_MODEL` / `VISION_BASE_URL`。
- [ ] **多模态（剩余）** —— PDF/Word 内嵌图片 OCR、音频 ASR、视频理解；补齐后才可写完整「多模态」。
- [x] **调参实验（首轮）** —— `scripts/eval_retrieval.py`（免重建：minScore / topK / rerank / hybrid-top-k / rrf-c）
      + `scripts/sweep_chunk_size.py`（chunk size，重建索引后评测，跑完恢复原尺寸）。
      实测：minScore 0.4 → recall@5 0.28（过杀）；chunk 256 → 负例误召 0.25，512/1024 一致。结论写进 README。
- [x] **扩评测集** —— 55 条（49 正 + 6 负），覆盖 5 篇文档（差旅 / 发布 / 考勤 / 申论 / 简历）；新增 26 题。
- [x] **调参实验（扩展）** —— `hybrid_top_k` / `rrf_c` 扫描完成：55 条上指标完全一致（recall@5 / hit@1 / MRR 均 1.00）
      —— 库太小没区分度，**记为结论而非跳过**（避免为了有结论而编调参）。
- [x] **加难评测题** —— 新增 17 条近义改写难题（刻意避开目标文档词汇，让 BM25 失效），集子 72 条（66 正 + 6 负）。
      实测（关 rerank）：难题 hit@1 仅 **0.88** vs 简单题 1.00；开 rerank 拉到 1.00 —— 指标终于有区分度。
      副产品：暴露兜底门槛 0.37 是对简单题**过拟合**（难题正确命中向量分最低 0.28）→ 改 0.35。
- [ ] **跨文档多跳题** —— 需「必须同时命中 2 篇文档」的判定（当前 `recall` 是 any-of 语义），要先改评测脚本。
- [ ] **扫 embedding 模型** —— 换 embedding 需全量重建索引（同 chunk 扫描套路）。
- [x] **rerank 降级门控** —— rerank 不可用时不再直接返回 RRF，改用**向量余弦相似度**兜底
      （`RAG_MIN_VECTOR_SCORE=0.37`）：负例空召回率 0.00 → 0.83、recall 不掉。
      阈值来自实测（正例期望文档 ≥0.381 / 负例全候选 5/6 ≤0.355）；剩余 1 条硬负例 0.418，向量分分不开。
- [x] **评测脚本提示 rerank 不可用** —— `eval_retrieval.py` 统计 `RerankerService` 告警，降级时显式提示。
- [x] **reranker 换型 + 阈值重校** —— 换 `qwen3.7-text-rerank`（原 `gte-rerank-v2` 仅免费额度可用，耗尽即 403）；
      `minScore` 由 gte 时代的 `0.1` 重校为 **`0.7`**（55 条实测安全区间 0.55~0.84，取中点）
      → recall@5 1.00 且负例空召回率 1.00。教训：阈值与 reranker 模型强耦合，不重校就漏噪声。
- [ ] **成本与限流** —— LLM 调用无预算控制；加每用户/每会话调用上限 + token/成本日志。
- [ ] **可观测性** —— LangSmith 已接（tracing + eval experiment）；仍缺请求级 latency / 命中率 / 成本统计面板。

## P2 · 工程完善

- [ ] **增量索引 / 文档去重** —— 当前重复发布触发全量重建；做按 `content hash` 跳过未变文档。
- [ ] **知识图谱实体链接** —— 抽取概率性导致同物异名（已归一书名号）；可加别名/同义词归并，降低漏匹配。
- [ ] **KG 抽取稳定性** —— LLM 重抽实体集有方差；可加「多次抽取取并集」或更明确的必抽类型约束。
- [ ] **生产部署** —— 目前仅本地 compose；补 nginx + uvicorn workers + 迁移 + 健康检查（或 k8s）。

## Chore · 收尾

- [ ] **注释润色** —— 去迁移残留引用后，少数注释略生硬（个别空 `#`、悬空标点），逐文件润色。
- [ ] **文档对齐** —— 已删 `架构.md` / `需求.md` / `简历素材-my-rag.md`；保留的模块文档随代码演进同步。
- [ ] **提交规范** —— 按 `feat/fix/docs/chore` 拆分提交，避免大杂烩。
