# SRS 3.1～3.7：ABCE 实现与回归追踪

依据：[正式 SRS](references/Milestone3.md)、`TECHNICAL_SPEC.md` v2.2。下表的「合成已验」仅指本地固定 Mock/FixedEncoder/SyntheticQA 的工程控制流。2026-11-03 本轮原始运行命令、日志、状态目录和 SHA 列于桌面 `gpt.md`；这些不是正式课程成绩。

| SRS | 入口 | 永久测试 | 本轮实证/状态 |
|---|---|---|---|
| 3.1 冻结数据、原文来源、错误隔离 | `data/snapshot.py`, `data/loader.py`, `data/repository.py` | `test_abce_long_history.py::test_twenty_memories_expand_and_survive_all_abce_consumers`; `test_source_repository.py` | 20 条合成会话经真实 Loader，来源逐条回查；合成已验。真实图像语义未验。 |
| 3.2 事实、构建诊断、严格发布 | `memory/build.py`, `contracts/build.py`, `memory/artifact_store.py`, `integration/artifacts.py` | `test_build_report_public.py`; `test_memory_build.py`; `test_abce_long_history.py`; `test_abce_repairs.py::test_corrupt_current_preserves_error_and_full_text` | 20 条 Mock 事实、完整/部分/失败/零窗口合同及严格发布；合成已验。事实蕴含与人工审核未验。 |
| 3.3 当前/历史问题、初始 Top-K 与历史扩展 | `retrieval/query_analysis.py`, `retrieval/vector_retrieval.py`, `retrieval/shared_retrieval.py` | `test_abce_long_history.py`; `test_shared_retrieval.py::test_same_retriever_identity_new_index_content_invalidates_cache` | 20 条 Memory 触发初始 8、扩展 4、预算排除 8；当前/历史意图、同分顺序合成已验。语义召回未验。 |
| 3.4 关系、RETAIN/UPDATE/SUPERSEDE/CONFLICT | D 模块与 A 预留端口 | 既有 Fake D 仅 ABI 测试 | **D_DEFERRED**；本轮不修改或验证真实 D。 |
| 3.5 证据约束问答、故障/时间状态 | `services/dynamic_memory_service.py`, `pipeline/query_runner.py`, `baselines/full_text.py` | `test_abce_repairs.py` 的坏指针、过期产物、发布切换；`test_abce_long_history.py` | 同一来源、候选、used IDs 与证据引用合成已验；错误不退化为 answered/no_evidence。真实答案准确率未验。 |
| 3.6 Web 浏览、问答、状态与失效 | `web/app.py`, `web/query.py`, `web/bindings.py` | `test_abce_long_history.py` 的真实工厂 AppTest；`tests/web` | 20 条记忆浏览、来源、Vector 问答合成已验；异常与恢复见本轮集成日志。真实浏览器人工验收未验。 |
| 3.7 同题评测、逐题失败分母、Gold 隔离 | `scripts/offline_integration.py`, `evaluation/runners/non_d_offline.py`, `evaluation/predictions.py` | `test_abce_repairs.py::test_public_cli_prepares_every_main_dev_question_and_preserves_failure_count`; `test_abce_long_history.py` | 公开 CLI 四样本三种非 D 方法预测 SHA 回读、缺索引 3+1 分母；长历史两题候选对比合成已验。Dynamic 正式第三路、真实 Gold/F1/Recall 未验。 |

非功能要求：唯一公共 IC-002 DTO 由 `test_build_report_public.py` 守护；索引缓存与生成末尾身份由 `test_abce_repairs.py`、`test_shared_retrieval.py` 守护；全量回归、Git 改动范围和证据目录见本轮桌面报告。正式生产组合仍属 GAP-002 后续工作。
