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
  - [x] **生成侧评测集扩大到 58 条** —— 12 → 58（51 可回答 + 7 不可回答），换成 174 篇真实语料后指标终于不再满分：
        要点覆盖率 0.98 / 忠贞度 0.997 / 引用准确率 0.98 / 拒答率 1.00（7/7）。
        剩余偏差均可定位到具体用例（2 条「超出资料推断」、1 条引用错配）。
  - [x] **指标本身的修正**（实测发现）—— `要点覆盖率` 对「同义改写」无解：
        ① 生成加 markdown 修饰导致 `` `df` 命令 `` 判不中「df命令」→ `_normalize` 改为剥离标点（+单测）；
        ② 文档写「拥有者」、模型答「所有者」—— 子串匹配本质无解，**特意保留一个盲区样本并在评测集里标注 note**。
        结论写进 README：要点覆盖率是哨兵指标，不是质量判据。
  - [ ] **生成侧加难** —— 补「资料部分相关 / 跨文档多跳」（多跳需先改评测脚本：命中判定现在是 any-of 语义）。
- [x] **评测集与运行台分离** —— 评测集是**单一事实来源**（`eval/*.jsonl`），离线脚本（零依赖、可进 CI）是主入口。
      曾接过 LangSmith 做实验对比，**因其收费已移除**（代码/配置/文档全清）。

## P1 · 加分项

- [x] **图片 OCR（多模态第一步）** —— jpg/png/webp/bmp/gif → 视觉模型（Qwen-VL via DashScope 兼容口）
      OCR/描述 → Markdown 入索引；未配视觉模型 Key 时明确 400。配置：`VISION_API_KEY` / `VISION_MODEL` / `VISION_BASE_URL`。
- [ ] **多模态（剩余）** —— PDF/Word 内嵌图片 OCR、音频 ASR、视频理解；补齐后才可写完整「多模态」。
- [x] **调参实验（首轮）** —— `scripts/eval_retrieval.py`（免重建：minScore / topK / rerank / hybrid-top-k / rrf-c）
      + `scripts/sweep_chunk_size.py`（chunk size，重建索引后评测，跑完恢复原尺寸）。
      实测：minScore 0.4 → recall@5 0.28（过杀）；chunk 256 → 负例误召 0.25，512/1024 一致。结论写进 README。
- [x] **扩评测集** —— 55 条（49 正 + 6 负），覆盖 5 篇文档（差旅 / 发布 / 考勤 / 申论 / 简历）；新增 26 题。
- [x] **扩库到真实规模（公开语料）** —— `scripts/fetch_oss_corpus.py` 抓 `jaywcjlove/linux-command`
      （MIT、37k stars、160 篇中文命令手册）→ 知识库 14 → **174 篇 / 467 块**（12 倍）。
      为什么用公开语料：合成语料经不起「这 14 篇是不是你自己写的」；而命令手册天然成簇、互为强干扰。
      · `scripts/validate_eval_set.py`：**机械校验地面真值**（标题存在 + 要点能在文档中找到），防「标题打错→指标骗人」
      · 评测集 87 → **132 条**（+39 条 OSS 题，总难例 51 条）
      · 实测：`hard hit@1` 终于分开（纯混合 0.824 vs rerank 0.941）；失败可解释（`at` 抢 `batch`）
- [x] **chunk size 扫描（三档齐 + 重复实验）** —— 三点**单调**：`hit@1` 0.992(256,883块) / 0.975(512,467块) / 0.967(1024,274块)；
      256 与 512 各重复一次，**逐位一致**（冻结查询向量 + 确定性分块）。
      结论：方向性效应真实（块越小匹配越精确），但幅度只 1~2 条题（120 条），
      而 256 的索引是 512 的 1.9 倍 → **保持 512，权衡记录在 README**。
- [ ] **chunk size 定案（需更大样本）** —— 评测集扩到几百条 / 多组问句重采样，看效应是否稳定。
- [x] **重建知识库回统一尺寸** —— 改用 robust reindex（每篇独立会话 + 重试）后完成：467 块 / 174 篇 / 0 失败。
- [x] **修复 reindex 的共享会话缺陷**（本轮实测暴露）—— 原先 174 篇共用一个 DB 会话，PG 一崩就把后面 **74 篇**全拖死；
      改每篇独立会话 + 重试 3 次后，同条件下只失败 1 篇。
- [x] **扩库（合成干扰语料）** —— 无真实文档，造 8 篇与现有文档组成**强干扰对**（会议费用 vs 差旅、
      数据库变更 SOP vs 发布 SOP、干扰简历 vs 梁多强、行测 vs 申论、考勤制度 vs 考勤表）：
      `eval/corpus/*.md` + `scripts/seed_demo_corpus.py`（幂等导入 / `--wipe` 回滚）。
      实测：5 → 14 篇后指标不再饱和（纯混合难题 hit@1 0.76 vs rerank 0.90）；评测集扩到 87 条（含 15 条交叉混淆题）。
- [x] **调参实验（扩展，扩库后重测）** —— `hybrid_top_k {5,10,20,40}` 完全无影响；`rrf_c=10` 略好
      （recall 1.00 vs 0.988）但**只差 1 条题**，不足以支撑结论 —— 仍记为「无差异」。
- [x] **加难评测题** —— 新增 17 条近义改写难题（刻意避开目标文档词汇，让 BM25 失效），集子 72 条（66 正 + 6 负）。
      实测（关 rerank）：难题 hit@1 仅 **0.88** vs 简单题 1.00；开 rerank 拉到 1.00 —— 指标终于有区分度。
      副产品：暴露兜底门槛 0.37 是对简单题**过拟合**（难题正确命中向量分最低 0.28）→ 改 0.35。
- [x] **跨文档多跳题** —— 新增 16 条（对比/并列类，如 `df 和 du 分别看什么？`），并在评测脚本里区分语义：
      单跳是 any-of，多跳是 **all-of**，两者**分开统计**、不混算；多了「全命中率 / 部分命中率 / 文档召回率」
      三个指标 + 「缺了哪篇」的诊断输出。
      实测：全命中率 **0.938** < 单跳 recall 0.992（多跳确实更难）；部分命中率 1.000，
      说明失败都是「只找齐一半」而非「完全找不到」。
      🔍 副产品结论：多跳指标在**有无 rerank 下完全一致** —— 多跳衡量的候选集完整性由**召回层**决定，
      rerank 只改候选内部顺序。即「召回管找齐、rerank 管排序」。
- [ ] **生成侧多跳** —— 生成侧目前仍都是单跳，可补「需要两篇文档才能答全」的用例。
- [ ] **扫 embedding 模型** —— 换 embedding 需全量重建索引（同 chunk 扫描套路）。
- [x] **rerank 降级门控** —— rerank 不可用时不再直接返回 RRF，改用**向量余弦相似度**兜底
      （`RAG_MIN_VECTOR_SCORE=0.37`）：负例空召回率 0.00 → 0.83、recall 不掉。
      阈值来自实测（正例期望文档 ≥0.381 / 负例全候选 5/6 ≤0.355）；剩余 1 条硬负例 0.418，向量分分不开。
- [x] **评测脚本提示降级** —— `eval_retrieval.py` 统计两类告警：`RerankerService`（rerank 挂）与
      `HybridSearchService`「向量化失败」（向量挂）；任一发生则显式提示「本次指标不可信」。
- [x] **修复兜底门控的组合失效**（实测发现）—— rerank 与**向量通道同时挂**时，`vectorScore` 全为 0，
      兜底门控会把候选**全部滤空**，整体召回从 0.94 直接变成 **0.00**（比不兜底还差）。
      已修：向量通道无数据时不做数据驱动的判断，退回 RRF 原序（+ 单测）。
- [x] **reranker 换型 + 阈值重校** —— 换 `qwen3.7-text-rerank`（原 `gte-rerank-v2` 仅免费额度可用，耗尽即 403）；
      `minScore` 由 gte 时代的 `0.1` 重校为 **`0.7`**（55 条实测安全区间 0.55~0.84，取中点）
      → recall@5 1.00 且负例空召回率 1.00。教训：阈值与 reranker 模型强耦合，不重校就漏噪声。
- [x] **成本与限流** —— `app/services/llm_budget.py`：
      · 成本核算：usage handler 挂在 **LLM 构造器**（8 个构造点 + 3 个派生器；挂 invoke 会漏新代码），
        轮次内 agent 主循环 + 工具子调用累加到同一 `ContextVar`，结束打结构化日志（token 真实 / 金额估算）。
      · 限流：Redis 计数每用户 / 每会话每日轮次，超限在**推流前** 429；Redis 挂则 fail-open。
      · 验证：14 例单测 + 本地 mock OpenAI 兼容服务端到端（不花钱）。日志前缀「本轮 LLM 用量」。
- [x] **并发压测（补生产数字）** —— `scripts/load_test.py`（mock 掉 LLM/Embedding，独立实例，采样 `pg_stat_activity`）。
      实测：池 5+10 时**并发 20 即 5/20 失败**（p50 0.5s→31s，= pool_timeout）。
      但**根因不是池小**：峰值 31 条连接中 **20 条 `idle in transaction`**，最后一条 SQL 是权限查询。
      根因 = FastAPI 生成器依赖 `Depends(get_db_session)` 活到响应结束 → 权限事务占住整条链路（含 LLM）。
      修法：权限守卫改短会话 + 后台摘要把 LLM 移出会话块 → **60/100 并发零失败，峰值连接 31→2**。
      教训：先查「连接持有时间」，再谈「池容量」。
- [x] **压测拈出的 3 个真 bug** —— 结构化输出未判 `None` ×2（改写 / 切题 / KG，会 500）、
      `graph_build` 的 `logger.info("...%d", len)`（日志永远不打印）。
- [ ] **⚠️ 本机 PG 反复崩溃（阻塞类问题）** —— 日志显示 `server process exited with exit code 2`
      → 自动 recovery，一小时内可发生多次；`checkpoint` 的 write 耗时高达 29~54s（I/O 极慢）。
      **不是本项目代码引起**（09-16/17/18 已先于本项目改动发生过），但随语料/流量增长而变频繁。
      已核实：PG 自身配置很保守（`shared_buffers=128MB`），且 compose **未给任何容器设内存上限**。
      建议：① 调大 Docker Desktop 内存（当前 VM 仅 7.7GB / 16 容器）；② 给 ES/Kibana/Neo4j 设 `mem_limit`；
      ③ 数据库卷改挂到 Docker volume（而非 macOS 目录），避开慢 I/O。
- [ ] **本机资源预算（实测教训）** —— Docker VM 仅 7.7GB / 16 容器；Langfuse 栈又占 ~2.6GB，
      导致 **PostgreSQL 被 OOM 杀掉并 crash recovery**（日志里 09-16/09-17/09-18 已先于压测发生过，
      说明本来就临界，Langfuse 只是推高了）。已把 Langfuse 改为按需启停。
      可选：调大 Docker 内存，或给 PG/ES 设 `mem_limit`/`shared_buffers`。
- [ ] **多 worker 压测** —— 当前结论是单进程；多 worker 需按 `(pool_size+max_overflow) × workers < max_connections` 重算并重测。
- [ ] **成本告警 / 降级演练** —— 目前只有事后日志：加「单日成本超阈值告警」；
      已因欠费中断两次（`Arrearage`），值得把「外部额度耗尽」纳入演练。
- [x] **评测可复现性**（实测发现）—— 同一评测集连跑 `hit@1` 在 0.889/0.901 之间跳。
      排除法定位：纯关键词稳定 → 篒在向量通道；提高 HNSW `ef_search` 到 200 **仍飘**；
      直测 embedding API —— **同一文本两次调用 1024 维中 1017 维不同**（服务端浮点非确定），这是根因。
      修法：查询向量缓存 + 落盘冻结（`eval/query_vectors.json`）→ 连跑 4 次完全一致；
      顺带收益：重复查询省一次嵌入调用；`--no-rerank` 离线消融不再依赖付费 API。
- [x] **确定性 tie-break** —— `search.py` 里 ES 同分与 RRF 同分均按 `chunk_id` 定序（非根因，但本就该做）。
- [x] **sweep 开跑前 embedding 预检** —— 重建中途失败会留下混合尺寸索引，而恢复也依赖嵌入 → 宁可提前中止。
- [x] **README「设计取舍」章节** —— 把三级消融 / 扩库动机 / 组合故障 bug / 可复现性四个故事写进去。
- [x] **可观测性（Langfuse 自托管）** —— trace 下钻 + 成本看板；`docker-compose.langfuse.yml` 独立栈
      （含 ClickHouse，v4 的 OLAP 主存储）。接入点在 `llm_budget.usage_callbacks()`，未配 Key 则不加载。
      已实证：trace 入库 + 本地成本估算与 Langfuse `cost_details` 完全一致（0.000398 元）。
- [ ] **评测集集成到 Langfuse** —— 检索侧**不值得**（不产生 trace，无可关联）；生成侧有边际收益，
      但要守住「jsonl 是唯一事实来源」。等 trace 用熟后再评估。
- [ ] **成本监控** —— 已因欠费两次中断（`Arrearage`），需加余额/用量告警与实践中的降级演练。

## P2 · 工程完善

- [ ] **增量索引 / 文档去重** —— 当前重复发布触发全量重建；做按 `content hash` 跳过未变文档。
- [ ] **知识图谱实体链接** —— 抽取概率性导致同物异名（已归一书名号）；可加别名/同义词归并，降低漏匹配。
- [ ] **KG 抽取稳定性** —— LLM 重抽实体集有方差；可加「多次抽取取并集」或更明确的必抽类型约束。
- [ ] **生产部署** —— 目前仅本地 compose；补 nginx + uvicorn workers + 迁移 + 健康检查（或 k8s）。
- [ ] **补演示截图**（阻塞在“需要人工在浏览器里操作”）—— 项目目前零视觉证据，
      都是“说了但没看到”。启动：`./scripts/dev_up.sh`（已会复用已在跑的进程）。
      建议按这个顺序截 6 张，每张都要能当证据：
      1. **登录页**（证明前端不是壳）
      2. **对话页ing**——流式过程中的思考 + 工具调用卡片 + 底部 `[n]` 引用来源
      3. **检索页**——混合检索命中与各处得分（证明 RRF/rerank 真的在跑）
      4. **知识图谱全景**（证明 Neo4j 那一路不是摆设）
      5. **文档审核流 / 权限页**（证明企业向的 RBAC 与审核链路完整）
      6. **终端一张**——`eval_retrieval.py` 输出，证明 README 里的数字是跑出来的
      截完放进 README 的「🖥 演示」一节（已留占位注释）。

## Chore · 收尾

- [ ] **注释润色** —— 去迁移残留引用后，少数注释略生硬（个别空 `#`、悬空标点），逐文件润色。
- [ ] **文档对齐** —— 已删 `架构.md` / `需求.md` / `简历素材-my-rag.md`；保留的模块文档随代码演进同步。
- [ ] **提交规范** —— 按 `feat/fix/docs/chore` 拆分提交，避免大杂烩。
