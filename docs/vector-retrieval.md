# C-04 / C-05：向量检索与历史候选扩展

本阶段把问题编码成向量，在同一个样本内计算余弦相似度，默认取前 8 条记忆，再按已提供的主体/属性补充历史候选，最终最多 12 条，返回公共 Schema 2.1 `CandidateSet`。尚未实现回答生成，也未接入 A 的真实运行器。正式模型尚未冻结，统一配置的 `real_retrieval_implemented` 保留原值，待完整运行器接入与真实数据验证后再统一开启。

## 使用

复用 C-03 的完整 `MemoryArtifact`、`LocalIndexEncoder` 和 `RetrievalSettings`：

```python
from dynamic_memory.retrieval import IndexManager, VectorRetriever, analyze_query

# artifact 必须来自 B 的严格 loader；encoder/settings 与建索引时一致。
index = IndexManager(encoder).build_index(artifact, settings)
retriever = VectorRetriever(encoder, settings)
analysis = analyze_query(request)
candidates = retriever.retrieve(request, analysis, index)
memories = retriever.load_candidates(candidates, index)
```

`retrieve(request, analysis, index)` 和 `load_candidates(candidates, index)` 为绑定实例方法，便于 A 注入同一个编码器和配置；不使用全局模型或隐式下载。读取已保存的索引后可直接检索，无须重新编码记忆。

## 排序与边界

- 先校验请求/分析、query_id、sample_id、索引、模型和参数身份，再编码问题。分析不携带问题正文摘要，调用方必须针对同一份 request 新建或正确缓存 analysis，不能只靠重复的 query_id 证明正文相同。
- 文档、问题向量均须有限、同维、单位化。点积即余弦相似度，浮点舍入越界截到 [-1, 1]。分数不是置信度或正确率。
- 分数降序，同分按 memory_id 升序；按配置 base_top_k 截取，默认 8。不足时返回实际数量，不设相关性阈值，负分也参与排序。
- items 按 rank 连续编号，origin 为 semantic，source_refs 取自对应 Memory。每次返回独立对象，不会修改索引。
- 空索引返回合法空 CandidateSet，不调用问题编码。编码失败抛错误，不能伪装为空检索。
- 明确时间冲突抛 QUERY_TIME_CONFLICT，交由 A 生成 time_uncertain 响应；unknown 时间和实体/属性提示不作硬过滤，不提前剔除旧状态。
- load_candidates 校验公开契约、样本、索引指纹、预算、ID、有限分数和来源，再按 rank 返回完整 Memory。按公开契约，乱序 items、重复 ID、缺失/重复 rank 均拒绝，不擅自修补。该入口校验候选来源，不重新编码问题或证明分数确由该问题计算。
- 首轮 semantic 候选保留原顺序；其后追加 history_expansion 候选。因此最终 rank 不等同于全体分数降序，score 始终保留原余弦值。

## 历史扩展规则与共享快照

以首轮候选中非空的 `(entity, attribute)` 为锚点，只做去首尾空白和大小写统一，不从正文猜别名、代词或同义属性。缺少任一字段不建立锚点。扫描同一样本中其余 Memory，仅补充相同字段对的记录；不递归扩展，不覆盖旧事实，不根据 recorded_at、event_time、as_of、fact_kind、assertion_type 或 polarity 排除记录或判断有效性。计划、取消、否定、假设等都作为待判断证据交给 D。

扩展池按“该字段对在首轮中最早的 rank → 相似度降序 → memory_id 升序”排序，去除首轮已有 ID，取剩余预算。预算只保证上限，不保证大量历史下每个关键事件都被召回；缺失结构字段和锚点排位偏差也会影响召回，需要真实开发集测量。

```python
snapshot = retriever.retrieve_with_trace(request, analysis, index)
vector_input = snapshot.candidates
dynamic_input = snapshot.candidates
assert vector_input == dynamic_input
pure_vector_ablation = snapshot.pure_vector
```

应检索一次，把同一 snapshot 的候选分别交给两路。属性每次返回独立副本，避免一方修改影响另一方；不是让两路各自执行检索。快照为 C 内部不可变记录，不新增公共 DTO。纯向量补充消融独立使用 optional_pure_vector_top_k（默认 8），全部标记 semantic，其 CandidateSet 的 base_top_k/max_candidates 均为该 K。

快照保存 initial_topk_ids、expanded_ids、final_candidate_ids、排序后的 expansion_pool_ids 和预算截断的 budget_excluded_ids；query_fingerprint 绑定完整请求、分析和索引身份。budget_excluded_ids 仅表示候选条数预算，回答阶段 Token 预算的排除记录仍须由 C-06 单独实现。调用方可将这些字段写入被 Git 忽略的结果目录，不应把问题和私人记忆写入公开日志。

检索策略版本升级为 `c-local-index-v2-history`，计入索引指纹。C-03 的 v1 索引文件会因旧实现版本被拒绝（INDEX_CORRUPT），需要重建；不静默复用旧索引/候选缓存。

## 离线演示（Git Bash）

模型已存在于 `.venv/models/all-MiniLM-L6-v2` 时：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m dynamic_memory.retrieval.retrieval_smoke
```

输入是两条明确标记的手工合成记忆；输出真实模型的分数、Top-1 和三组候选 ID。演示同时断言保存重载后的候选完全一致。生成文件放在被 Git 忽略的 `results/retrieval-smoke/`。这不是 LoCoMo 效果评估，也不能代替 B 严格磁盘来源核验。

## 错误与后续接入

沿用 `RetrievalIndexError.code`：索引/模型过期 INDEX_STALE；跨样本 SAMPLE_MISMATCH；查询无效 QUERY_INVALID；分析无效 QUERY_ANALYSIS_INVALID；请求/分析不一致 QUERY_ANALYSIS_MISMATCH；时间冲突 QUERY_TIME_CONFLICT；编码失败 EMBEDDING_FAILED；向量无效 EMBEDDING_OUTPUT_INVALID；候选无效 CANDIDATES_INVALID；候选来源不符 CANDIDATE_SOURCE_MISMATCH。底层模型异常不向上泄漏问题正文或路径。

A 的错误码映射、真实运行器注入和 B/C/D 联调仍待完成；未修改其他成员业务实现。当前线性扫描适用于样本内轻量检索，真实规模上的性能和召回率需另测。
