# A 分支 A-only 最终验收记录

## 范围与判定

- 本轮基线：分支 `A`，HEAD `02923f7bb8d5b61ff3c64ae7619c768a01aeaba6`。开始时 `git status --short --branch` 显示 10 个已修改的跟踪文件和 4 个未跟踪文件（本页、`A-stage2-review.md`、`A-stage3-review.md`、`tests/integration/test_a_standalone_flow.py`）；均为本轮前已有的工作树内容。本轮仅原位修订本页，保留其他改动；Git 索引为空。
- 本页逐项核对 A-01…A-18 的**现有实现和离线测试**。下表的“已验证”仅指本轮完整测试覆盖的 A 自身合同、Mock/Fake 或 synthetic fixture 行为；不表示真实数据、真实模型或跨分支生产组合已通过。
- 统一执行证据 `T1`：2026-11-01 在仓库根目录运行指定命令 `& 'C:\Users\Keshu\OneDrive\Desktop\历史文件\大三上\软件需求工程\SRE-Project\.venv\Scripts\python.exe' -m pytest -q`，随后读取 `$LASTEXITCODE`；退出码 **0**；关键输出 `685 passed in 19.23s` 和 `PYTEST_EXIT_CODE=0`；**685 通过、0 失败、耗时 19.23 秒**。输出的进度达到 `[100%]`。表内每个测试节点均由 `T1` 执行，故各行命令/退出码均为 `T1 / 0`。本轮逐一核对表中 18 个编号，以及所列实现文件、测试文件和具名测试函数均存在。既往提交记录和阶段报告只用于追溯任务与文件，不充当本轮通过数。

## A-01…A-18 证据矩阵

| 编号 | 状态 | 实现证据 | 本轮测试名（均由 T1 执行，退出码 0） | 验证边界 |
| --- | --- | --- | --- | --- |
| A-01 | 已验证 | `src/dynamic_memory/pipeline/settings.py` | `tests/unit/test_settings.py::test_repository_defaults_and_pending_values` | 六份配置的离线加载；未冻结外部选型。 |
| A-02 | 已验证 | `src/dynamic_memory/llm/protocol.py` | `tests/unit/test_llm_protocol.py::test_three_tasks_use_loaded_budgets_and_undecided_model` | LLM 协议合同，不是线上 Provider。 |
| A-03 | 已验证 | `src/dynamic_memory/llm/mock.py` | `tests/unit/test_mock_llm.py::test_three_tasks_use_one_protocol_and_safe_defaults` | 确定性 Mock，输出为 synthetic。 |
| A-04 | 已验证 | `src/dynamic_memory/llm/deepseek.py` | `tests/unit/test_deepseek_llm.py::test_import_and_construction_are_offline_and_disabled_by_default` | Provider 的离线行为；未请求真实 API。 |
| A-05 | 已验证 | `src/dynamic_memory/llm/structured_output.py` | `tests/unit/test_structured_output.py::test_returns_business_model_without_mutating_response` | JSON/DTO 结构校验，不证明回答事实。 |
| A-06 | 已验证 | `src/dynamic_memory/llm/prompts.py` | `tests/unit/test_prompts.py::test_default_templates_render_build_and_work_with_offline_mock` | 演示模板；正式 Prompt 未冻结。 |
| A-07 | 已验证 | `src/dynamic_memory/llm/execution.py` | `tests/unit/test_llm_execution.py::test_three_tasks_use_same_executor_policy_and_mock` | 重试、并发及预算的离线策略；无真实用量。 |
| A-08 | 已验证 | `src/dynamic_memory/llm/recording.py` | `tests/unit/test_llm_recording.py::test_miss_hit_and_log_use_current_not_historical_usage` | 本地记录/缓存合同；未验证线上计费。 |
| A-09 | 已验证 | `src/dynamic_memory/pipeline/runtime.py` | `tests/integration/test_llm_runtime.py::test_default_shared_runtime_and_three_prompt_budgets` | 注入式 Runtime；默认 Mock。 |
| A-10 | 已验证 | `src/dynamic_memory/contracts/data.py` | `tests/contract/test_dataset_containers.py::test_public_exports_schema_and_order_preserved_across_roundtrip` | 公共数据容器；正式 B 加载另行接入。 |
| A-11 | 已验证 | `src/dynamic_memory/pipeline/query_conditions.py` | `tests/unit/test_shared_query_conditions.py::test_one_analyzer_call_snapshot_reused_for_three_methods` | 共享查询分析端口；真实分析器待接入。 |
| A-12 | 已验证 | `src/dynamic_memory/pipeline/query_runner.py` | `tests/unit/test_run_query_dispatch.py::test_default_dynamic_calls_one_analyzer_and_c_then_d_in_order` | 三路调度的 Fake 端口；C/D 业务实现待接入。 |
| A-13 | 已验证 | `src/dynamic_memory/baselines/full_text.py` | `tests/unit/test_full_text_baseline.py::test_complete_source_prompt_runtime_and_a12_integration` | Full Text 基线的 synthetic 接线；无正式问答成绩。 |
| A-14 | 已验证 | `evaluation/question_manifest.py`、`evaluation/predictions.py`、`evaluation/gold_loader.py` | `tests/unit/test_gold_isolation.py::test_save_verify_then_load_gold_by_unique_key` | 保存/核验后读取 synthetic Gold；未读取真实 Gold。 |
| A-15 | 已验证 | `evaluation/metrics/official_qa.py`、`evaluation/metrics/qa_summary.py` | `tests/unit/test_official_qa_scoring.py::test_normalization_order_and_porter_stems` | 五类 QA 评分实现的离线测试；课程口径未确认。 |
| A-16 | 已验证 | `evaluation/metrics/evidence_recall.py`、`evaluation/metrics/state_metrics.py` | `tests/unit/test_evidence_turn_recall.py::test_ranked_memory_k_and_deduplicated_turn_units`；`tests/unit/test_state_metrics.py`（T1 全文件） | synthetic 证据/状态指标；正式证据格式与状态 Gold 待确认。 |
| A-17 | 已验证 | `evaluation/runners/three_method_runner.py` | `tests/unit/test_three_method_batch_runner.py::test_one_analysis_three_methods_order_saved_and_verified` | synthetic 三路持久化与回读；无真实开发集运行。 |
| A-18 | 已验证 | `src/dynamic_memory/services/dynamic_memory_service.py`、`src/dynamic_memory/contracts/service_status.py` | `tests/unit/test_dynamic_memory_service.py::test_six_methods_complete_and_defensive_copies`；`tests/contract/test_build_status_service.py`、`tests/integration/test_service_web_binding.py`（T1 全文件） | 只读服务/IC-012 与 E 绑定的 Fake 合同；正式 B/C/D/E 组合未验证。 |

## 独立待办状态

- `BCDE_INTEGRATION=DEFERRED`：B 的正式 Memory 加载、C 的正式索引/检索与问答、D 的正式解析/问答、E 的正式组合均须由各负责分支接线并联调；A-12、A-17、A-18 的 synthetic/Fake 测试不替代该工作。接口边界见 `docs/contracts.md` 的 IC-012 和 `docs/architecture.md` 的 A-12/A-17/A-18 说明。
- `FORMAL_DEVELOPMENT_RUN=NOT_RUN`：本轮未运行真实开发集，也未调用真实模型或外部 API；本轮测试结果不构成正式业务成绩。
- `COURSE_GOLD=UNCONFIRMED`：课程 Gold 与最终评分口径未核实；A-14～A-16 的合成 Gold 测试不能确立课程成绩。参见 `docs/architecture.md` 的 A-14～A-16 边界。

本轮不包含真实开发集运行、跨分支联调或任何正式业务成绩。未运行在线模型、下载依赖、读取真实 Gold；未改 `src/`、`evaluation/`、`configs/`、模板或 `data/`。本页是文档与 A-only 测试验收记录，不宣告跨模块集成或正式实验完成。

前一阶段报告 `docs/reports/acceptance/A-stage3-review.md` 记录的全新 Python 3.11 环境缺少依赖、无法离线复跑问题，本轮未尝试解决；上述 `T1` 使用仓库现有 `.venv`，不将其视为全新环境验收证据。

## 本阶段验收条目

1. **报告及逐项证据：已验证。** 本文件的矩阵完整列出 A-01…A-18；每行给出实现文件、具名测试或测试文件、统一执行命令 `T1` 及退出码 0。证据边界列说明 Fake、Mock 和 synthetic fixture 的适用范围。
2. **范围声明：已验证。** 上述三个独立待办状态和范围说明明确记录：未运行真实开发集、未做跨分支联调、没有正式业务成绩。
3. **本轮自检：已验证。** `T1` 为指定的独立完整测试命令，退出码 0，关键输出 `685 passed in 19.23s`；685 通过、0 失败。
