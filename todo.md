# TODO / 待完善

> 面向「可展示 + 面试可深挖」的完善清单，按性价比排序。
> 勾选项为已完成。

## P0 · 最该做（面试硬指标 / AI 岗深度）

- [x] **README** —— 从空文件补成完整仓库门面（定位 / 架构图 / 快速启动 / 核心设计 / API / 边界）。
- [ ] **单元测试（pytest）** —— 当前只有 43 个真实依赖 E2E 冒烟，缺纯逻辑单测。建议覆盖：
  - [ ] RRF 融合（`search.py`）
  - [ ] 分块器 `chunker`（边界 / overlap）
  - [ ] `to_eval` / `grade_hits` / `_to_plan`（纯函数）
  - [ ] `merge_graph_hits` / `_evidence_snippet` / `normalize_entity_name`
  - [ ] `_pack_retrieve_result`（全局编号 / 事件）
  - [ ] citation 解析 `[n]`、权限矩阵 `roles/permissions`
  - [ ] 入参校验（超长 → 400 而非 500）
- [ ] **CI（GitHub Actions）** —— `ruff + mypy + pytest` + 关键冒烟；PR/推送自动跑。
- [ ] **RAG 评测集** —— 量化检索/问答质量（AI 岗最能体现深度）：
  - [ ] 构造 30~50 条 `(问题, 期望文档/答案)`
  - [ ] 检索指标 `recall@k` / `MRR`
  - [ ] 生成指标 `faithfulness` / 引用准确率
  - [ ] ablation：关键词 only vs 向量 only vs 混合；有/无 rerank；不同 `minScore` / chunk size
  - [ ] 把结论写进 README（如「混合+rerank 使 recall@5 从 X → Y」）

## P1 · 加分项

- [ ] **多模态** —— 目前仅 5 种文本类解析器；补图片 OCR（PaddleOCR）或表格解析，才可写「多模态」。
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
