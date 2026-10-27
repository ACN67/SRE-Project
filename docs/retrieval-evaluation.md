# C 检索效果诊断工具（2026-10-27）

工具位于 `dynamic_memory.retrieval.evaluation`，用于比较纯向量候选与历史扩展后候选对人工相关记忆标注的覆盖。它不是 A 的官方 QA 评分器，不读取课程 Gold，不调用回答模型，不把标注交给查询分析或检索算法。

## 直接运行合成演示

Git Bash：

```bash
.venv/validation311/Scripts/python.exe -m dynamic_memory.retrieval.evaluation_smoke
```

输出：`results/retrieval-evaluation/synthetic-report.json`（Git 忽略）。演示有 11 条手工合成记忆和 3 条相关标签，受控合成向量使旧工作、离职记录排在 Top-8 外。实际结果为纯向量找回 1/3，历史扩展后找回 3/3。这仅证明指标与扩展逻辑可复核，**不是实际 Embedding 的 100% 召回率**。本轮合成耗时约 2.70ms，不能用作真实模型性能结论。

## 真实开发集的调用方式

```python
from dynamic_memory.retrieval.evaluation import RetrievalEvaluationCase, evaluate_retrieval

case = RetrievalEvaluationCase(
    case_id="reviewed-dev-question-001",
    label_kind="team_annotated",
    split="main_dev",
    request=request,
    source_manifest_fingerprint=index.source_manifest_fingerprint,
    relevant_memory_ids=reviewed_relevant_ids,
    annotator=annotator_name,
    reviewer=reviewer_name,
)
report = evaluate_retrieval(
    [case], indexes={index.sample_id: index}, retriever=vector_retriever,
    recall_at_k=settings.recall_at_k,
)
```

必须传不带共享缓存的 VectorRetriever，索引来自已验证产物。同份报告不得混合 synthetic_fixture 与 team_annotated，也不得混合 main_dev/extra_dev；此工具不接受 test 划分来调参。人工标注须注明标注者、复核者。函数不读写文件，调用方自行将标注和结果保存在受忽略目录，避免私人问题/记忆进入 Git。

## 指标含义

- relevant_count：人工标注的相关 Memory 数；hits：候选与相关集合的交集大小；recall=hits/relevant_count。
- micro_recall：所有查询的命中数总和除以相关数总和；macro_recall：有相关标签查询的 recall 平均值。报告明确写出查询数、有效查询数和无相关标签查询数。
- 无相关标签时为 null，不计作满分。纯向量预算默认 8，因此其 Recall@10 为 null，不拿返回的 8 条冒充 Top-10；若需要纯向量 Recall@10，应在独立比较配置里将 optional_pure_vector_top_k 调到至少 10，并重建匹配索引，不改变正式 Top-8 默认配置。
- final_candidates 的 Recall@K 按最终 rank 前 K 项计算；最终顺序保留首轮语义候选，再接历史扩展项，不能解释成“全体按相似度排序的 Top-K”。完整候选 recall 与 Recall@K 分开提供。
- 标签必须匹配当前 source_manifest_fingerprint，且全部相关 ID 存在于该样本索引。失效、错样本、未映射 ID、重复标注、重复 case_id 会在检索前报错。模型/检索执行失败终止本轮，不悄悄丢弃失败题后给出偏高汇总。

这里测的是**相关 Memory 已被抽取并进入索引之后的条件召回**，不能衡量 B 漏抽事实，也不是端到端证据召回。未映射事实应先单独记录 extraction_missing/mapping_missing，处理标注映射后才能使用此工具。函数不判断相关标签语义是否正确，也不能证明回答的事实正确性。

## 耗时与复核信息

每题记录完整分析＋无共享缓存检索的耗时，包括模型身份检查；首题可能包含模型加载。排除索引构建、磁盘读取与回答生成。不应把此值当纯编码耗时，也没有记录内存峰值或模型加载独立耗时。

报告保留查询、标注、源 manifest 与索引指纹，初始/扩展/最终 ID、纯向量 ID 和条数预算排除项，不写入问题或记忆正文。指纹用于检查同一输入版本，不替代妥善保存原始人工标注。

## 本轮验证及边界

9 项新增测试覆盖可手算的 1/3→3/3、预算不足的 Recall@10、无相关标签、陈旧/不存在/重复标签、未复核人工标签、禁止 test 划分、混合报告与 macro/micro 分母。全项目 **624 passed**，Mock smoke PASS，演示退出码 0。

本轮没有使用真实数据、下载模型或调用付费 API。下一步是整理人工开发集标注、在 B 严格产物可用后跑真实比较，并补真实模型加载/内存测量；目前不冻结正式 Embedding 选择。
