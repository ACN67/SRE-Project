# SRS 3.1～3.7：A/B/C/D/E 实现与回归追踪

依据：[正式 SRS](references/Milestone3.md)、`TECHNICAL_SPEC.md` v2.2。下表的「合成已验」仅指本地固定 Mock/FixedEncoder/SyntheticQA 的工程控制流。2026-11-03 本轮原始运行命令、日志、状态目录和 SHA 列于桌面 `gpt.md`；这些不是正式课程成绩。

| SRS | 入口 | 永久测试 | 本轮实证/状态 |
|---|---|---|---|
| 3.1 冻结数据、原文来源、错误隔离 | `data/snapshot.py`, `data/loader.py`, `data/repository.py` | `test_abce_long_history.py::test_twenty_memories_expand_and_survive_all_abce_consumers`; `test_source_repository.py` | 20 条合成会话经真实 Loader，来源逐条回查；合成已验。真实图像语义未验。 |
| 3.2 事实、构建诊断、严格发布 | `memory/build.py`, `contracts/build.py`, `memory/artifact_store.py`, `integration/artifacts.py` | `test_build_report_public.py`; `test_memory_build.py`; `test_abce_long_history.py`; `test_abce_repairs.py::test_corrupt_current_preserves_error_and_full_text` | `conv-26/30/41/42` 均完成真实抽取、v3 审核、严格 Memory 发布和回读；Memory 数分别为 762/600/909/857。其余语料与审核精度未验。 |
| 3.3 当前/历史问题、初始 Top-K 与历史扩展 | `retrieval/query_analysis.py`, `retrieval/vector_retrieval.py`, `retrieval/shared_retrieval.py` | `test_abce_long_history.py`; `test_shared_retrieval.py::test_same_retriever_identity_new_index_content_invalidates_cache` | 四段 main_dev 均已建立并回读真实 BGE 索引；全部 757 题完成运行，Evidence Recall@5/@10 为 0.489441/0.539006。 |
| 3.4 关系、RETAIN/UPDATE/SUPERSEDE/CONFLICT | `reasoning/resolver.py`, `qa/dynamic.py`, `reasoning/service_ports.py` | `tests/dynamic`; `stage6_evaluate_state.py` | D 模型驱动端口、逐候选决策、关系与来源校验已实现；四段共 757 题真实 Dynamic 路径完成。ACN67 在 11 道真实开发题、112 个 Memory 映射上完成团队标注复核，整题状态更新准确率 0.727273、关系准确率 0.833333、决策准确率 0.953488。课程官方状态 Gold 仍未下发。 |
| 3.5 证据约束问答、故障/时间状态 | `services/dynamic_memory_service.py`, `pipeline/query_runner.py`, `baselines/full_text.py` | `test_abce_repairs.py` 的坏指针、过期产物、发布切换；`test_abce_long_history.py` | 同一来源、候选、used IDs 与证据引用合成已验；757 题真实问答无执行错误。完整结果和限制见 `docs/STAGE6_MAIN_DEV_FINAL.md`。 |
| 3.6 Web 浏览、问答、状态与失效 | `web/app.py`, `web/query.py`, `web/bindings.py` | `test_abce_long_history.py` 的真实工厂 AppTest；`tests/web` | 支持 Memory 浏览、原始轮次回查、三路问答、状态解析展示，以及当前样本全部 Memory 的 JSON 下载。四段真实 Memory 已通过 AppTest；正式人工浏览器验收未验。 |
| 3.7 同题评测、逐题失败分母、Gold 隔离 | `scripts/stage6_dev_three_method.py`, `scripts/stage6_evaluate_dev.py`, `evaluation/predictions.py` | `tests/unit/test_stage6_dev_three_method.py`; `test_abce_repairs.py::test_public_cli_prepares_every_main_dev_question_and_preserves_failure_count` | 全部 757 道 main_dev 题完成 Full Text / Vector RAG / Dynamic，预测先锁定后评分并形成冻结清单；独立 test 仍未运行。 |

非功能要求：唯一公共 IC-002 DTO 由 `test_build_report_public.py` 守护；索引缓存与生成末尾身份由 `test_abce_repairs.py`、`test_shared_retrieval.py` 守护；D 的共享 Runtime、生产工厂、C provenance 和完整对话 cutoff 已接入。全部 `main_dev` 三路实验、团队状态标注子集和回归已经完成并冻结；课程官方状态 Gold、独立 test 和后续系统兼容性修复仍是验收项。
