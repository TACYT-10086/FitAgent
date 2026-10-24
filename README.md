# FitAgent: 场馆智能运营 Agent

面向游泳馆/健身馆的智能体，支持会员、教练、管理员三角色，通过工具调用完成预约、取消、查询，并通过 Agentic RAG 回答场馆规则。

## 技术栈
- Python / PostgreSQL / Milvus
- DeepSeek API (Agent 大脑)
- Ollama (本地 Embedding，bge-m3)
- Agent Loop：手写轻量级 ReAct，不用 LangChain
- LangGraph：仅作为 Agent Chat UI 的托管适配层，核心 Agent Loop 仍手写

## 当前进度 (第 4 周)
- [x] PostgreSQL 8 张业务表 + 模拟数据
- [x] Milvus 向量化知识库 (20 条文档)
- [x] 朴素 RAG 基线检索 (Top-1 命中验证)
- [x] Agentic Query-Transformed RAG (rewrite / multi_query / hyde + 评分重试)
- [x] 评测体系 (朴素 RAG vs Agentic RAG 对比，产出 `eval/rag_eval.json`)
- [x] 业务工具调用与权限控制 (7 个工具；身份由会话注入，硬权限矩阵拦截越权并留痕)
- [x] 手写轻量级 ReAct Agent Loop，包含硬权限校验、JSON 兜底和多轮澄清
- [x] 真实 trace 记录 (`trace/trace.jsonl`，一行一次工具调用，含耗时与越权请求标记)
- [x] Web 交互层：基于 LangGraph 适配器接入 Agent Chat UI，支持网页端多轮对话
- [x] 记忆持久化：引入 LangGraph PostgresSaver，实现基于 thread_id 的跨会话状态存储
- [x] 可观测性：集成 LangSmith 运行追踪，实现 Agent 节点与工具调用可视化

> 评测现状（诚实记录）：按 `hit@1` + MRR 的严格口径，**朴素 RAG 100%、Agentic 90%**
> —— 查询改写在这份语料上不但没带来增益，反而**变差了**。详见下方「评测」一节。

## 测试

全部真连真实服务：PostgreSQL、Milvus、Ollama。无 mock、无打桩。

- 数据层：PostgreSQL 连接、`db/seed.py` 灌数与数据正确性
- 向量层：Milvus 连接、`rag/ingest.py` 入库、`rag/retriever.py` 检索
- 权限层：member / coach / admin 三类越权拦截、白名单外参数拒绝、被拒调用零数据库副作用
- 工具层：`book_class` 真写库、`query_schedule` 只返回未来场次、`get_member_balance` 由流水派生消费额、满员拒绝

```powershell
pytest -v                                                                 # 全量
pytest tests/test_db.py tests/test_seed.py tests/test_permissions.py -v   # 数据库与权限
pytest tests/test_tools.py -v                                             # 业务工具
```

唯一的跳过是 `tests/test_policy.py` 里 2 条带 `llm` 标记的用例：真打 DeepSeek、要花钱且结果不确定，默认跳过，`$env:RUN_LLM_TESTS=1` 才放行。其余一律真跑。

`tests/test_tools.py::test_search_policy_end_to_end_real_api` 会真实调用 DeepSeek（约 7 秒），刻意不带 `llm` 标记，每次运行都跑。写库用例 `try/finally` 自清理，跑完数据库回到 seed 原状。

## 评测

对比朴素 RAG 与 Agentic RAG 在 10 条真实会员问题上的表现：

```bash
python -m eval.rag_eval                  # 完整跑：模型自选策略（约 40–50 次 DeepSeek 调用）
python -m eval.rag_eval --no-strategy-llm # 固定 rewrite 策略，省调用、结果可复现
python -m eval.rag_eval --top-k 1         # 调小 k 可提高区分度（见下）
```

- 题目与标注：`eval/queries.py`（ground-truth 标题与 `db/seed.py::KNOWLEDGE_DOCS` 逐字一致）
- 产物：`eval/rag_eval.json`（含 config / summary / 逐题明细）

指标口径（本步不生成最终答案，只评检索结果）：

| 指标 | 含义 |
|---|---|
| `hit@k`（准确率） | top-k 里命中任一 ground-truth 即该 query 记 1，取平均 |
| `recall@k`（召回率） | 被覆盖的 ground-truth 占该题全部 ground-truth 的比例 |
| `MRR@k` | 第一个命中的 ground-truth 的倒数排名，对排序退化敏感 |

`--top-k` 是**指标口径**，`--probe-k` 是**检索深度**（默认 10）。两者分开的原因：只按 top-k 检索的话，排在前 k 名之外的排名信息全丢了，MRR 就只能知道「没进前 k」，算不出真实倒数排名。

### 实测结论：查询改写在这份语料上是负收益

`top_k=1` + MRR 的严格口径下（候选深度 10）：

| 模式 | 准确率 hit@1 | 召回率 | MRR@1 | 平均耗时 |
|---|---|---|---|---|
| 朴素 RAG | **100%** | 100% | **1.000** | 2209 ms |
| Agentic | **90%** | 90% | **0.900** | 7357 ms |

- 朴素 RAG 在 **10/10 题上把 ground-truth 排在第 1 位**。`bge-m3` 的中文语义表征强到足以自己吃掉口语化差异，原先预期「难」的 q06–q10 全部第一位命中。
- Agentic 唯一失手的是 **q08**（「我朋友能拿我的卡来锻炼吗？」）：HyDE 生成的假想文档把 `会员卡类型与价格` 顶到了第 1 位，ground-truth `会员卡能否转让` 掉到第 2 位。
- 代价是实打实的：**慢约 3.3×**（2.2s → 7.4s），外加每 query 约 3–5 次 DeepSeek 调用。

归因（重要，别误读）：**这个负收益来自「用假想文档替换原问题」，不是来自重试循环**。逐题明细里所有 agentic 轮次都是 `1轮` —— 因为候选深度 10 时，评分总能在里面找到至少一篇相关文档，`sufficient` 恒为真，**换策略重试那条路径基本没被触发**。也就是说这份评测实测的是 transform 部分，不是 agentic 的决策部分。

假说：原问题本身已带足主题词，而 HyDE 的假想文档会引入话题漂移（它会对没把握的细节编造内容，比如替 q08 生成了卡种说明）。

要让评测真正测到 agentic 的决策价值，还需要：更大的语料、答案需跨文档综合的问题、以及语料里根本没有答案（应触发重试并最终拒答）的问题。目前**尚未**做这一步。
