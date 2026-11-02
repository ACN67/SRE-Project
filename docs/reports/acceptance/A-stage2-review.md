# A 分支第 2 阶段体系化审查（2026-10-31）

## 身份和范围

- 分支 `A`；开始和结束 HEAD 均为 `02923f7bb8d5b61ff3c64ae7619c768a01aeaba6`。
- 开始时已有六个未提交文件：`evaluation/metrics/qa_scoring_manifest.py`、`scripts/check_dev_runbook.py`、`src/dynamic_memory/pipeline/query_runner.py`、`tests/unit/test_dev_runbook_check.py`、`tests/unit/test_pure_vector_ablation.py`、`tests/unit/test_qa_summary_breakdown.py`。均保留；本阶段新增改动仅在 A 的两个 runner 和两份单元测试，以及本报告。
- 全部运行均为本地 synthetic fixture；未联网、未调用真实模型或外部 API，未读取正式 Gold，未运行正式开发集或测试集。

## 已确认缺陷：运行控制文件可经符号链接回读

| 缺陷 | 根因 | 修改文件 | 反例 | 修复后证据 |
| --- | --- | --- | --- | --- |
| 三路及纯向量消融的 `verify_*run` 可跟随 `run_manifest.json` 或 `run_manifest.sha256` 的符号链接，读取运行目录外的控制文件。 | 两个回读函数限制了运行目录，却未检查目录中的两个控制文件是否为符号链接。 | `evaluation/runners/three_method_runner.py`、`evaluation/runners/pure_vector_ablation.py`；对应 `tests/unit/test_three_method_batch_runner.py`、`tests/unit/test_pure_vector_ablation.py`。 | 先增加 4 个参数化负面用例，再分别运行 `-k linked_control_file`：两组各 `2 failed`，退出码 1，报错 `DID NOT RAISE EvaluationInputError`。测试以 `Path.is_symlink` 注入链接状态，避免 Windows 创建链接权限差异。 | 回读前检查两个文件，命中则返回 `RUN_PATH_INVALID`。两份测试文件合跑 `50 passed`，退出码 0；完整回归 `679 passed`，退出码 0。 |

该检查针对已确认的回读缺陷；并发替换文件的原子性不在本轮合成测试的证明范围。

## 七个审查面与证据

| 审查面 | 本轮证据与结论 |
| --- | --- |
| 共享输入与隔离 | `evaluation/question_manifest.py` 通过 `inspect_snapshot` 和原始字节 SHA 冻结问题身份；`test_eval_question_identity.py` 覆盖顺序、官方 ID、重复和错误拆分。`test_gold_isolation.py` 的 receipt/哈希/身份负面用例，以及 `test_three_method_batch_runner.py::test_real_a14_synthetic_manifest_exposes_no_gold_to_batch`，证明预测先于 Gold 且合成批处理不读取 Gold。`RunProtocol` 的 snapshot、split、身份字段由 `_validate` 检查；缺失或不匹配被拒。 |
| 时间与错误语义 | `test_shared_query_conditions.py` 覆盖一次分析、冲突早停、解析不确定、分析错误；`test_run_query_dispatch.py` 覆盖真实空候选的 `no_evidence`、`time_uncertain`、未知异常的 `error`；`test_gold_isolation.py::test_missing_gold_is_separate_from_failed_prediction` 独立覆盖 Gold 缺失。 |
| 三路公平性 | `test_three_method_batch_runner.py::test_one_analysis_three_methods_order_saved_and_verified` 逐题一次分析、两次独立检索并比较观察到的候选；`test_batch_candidate_comparability.py` 验证候选不同时不可称可比较。`compare_candidates` 保留 `mismatch`、`unverified`、`not_comparable`，并只在可核对身份及候选一致时记 `identical`。`pure_vector_topk` 由独立 runner 保存，`METHODS` 仍仅三路。 |
| Gold 与结果口径 | `evaluation/gold_loader.py` 先验证持久化预测，再打开含 Gold 的快照；`test_qa_score_summary.py` 验证错误计 0 和独立分母；`test_qa_summary_breakdown.py` 验证 `category × sample_id`、原因分布、评分记录完整性、原子发布和不覆盖。Gold 缺失不伪造类别或得分。 |
| LLM 资源安全 | `test_llm_execution.py` 覆盖超时边界、有限重试、并发和预算预检；`test_llm_recording.py` 覆盖缓存身份、损坏拒绝、事务和 Fake 抛异常时不重试隐式提供者；`test_mock_llm.py` 覆盖确定性 Mock。同步注入 adapter 的单次调用超时由 adapter 自身负责，`GuardedLLMAdapter` 只限制并发许可等待。 |
| 可复核性 | 两个 runner 使用新运行目录、原子发布和 SHA 回读；`test_three_method_batch_runner.py::test_third_save_failure_preserves_first_two_without_completion` 证明失败文件仍在且不标完成；相邻篡改与重算哈希用例拒绝不一致记录。评分产物的完成标记、并发发布和中断反例由 `test_qa_summary_breakdown.py` 覆盖。新缺陷的红绿输出如上，可定位到具体控制文件。 |
| 安全与兼容 | `test_three_method_batch_runner.py` 覆盖运行根和路径穿越；新增 4 个链接反例覆盖控制文件。`tests/contract` 的公共 DTO 严格验证与回读共 92 通过；`test_service_web_binding.py` 和 `tests/web` 的合成 WebBindings 契约通过。未改公共 Schema、Web、B/C/D/E 代码或配置。仓库没有归档旧版 DTO 字节 fixture，因此“任意历史版本文件回读”仍待集成确认。 |

## 本轮自检原文与结果

工作目录：`C:\Users\Keshu\OneDrive\Desktop\历史文件\大三上\软件需求工程\SRE-Project`。

| 命令原文 | 退出码 | 关键输出 |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_three_method_batch_runner.py -k linked_control_file`（修复前） | 1 | `2 failed, 17 deselected` |
| `.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_pure_vector_ablation.py -k linked_control_file`（修复前） | 1 | `2 failed, 29 deselected` |
| `.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_three_method_batch_runner.py tests/unit/test_pure_vector_ablation.py` | 0 | `50 passed in 2.67s` |
| `.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_eval_question_identity.py tests/unit/test_three_method_batch_runner.py tests/unit/test_pure_vector_ablation.py tests/unit/test_qa_summary_breakdown.py tests/unit/test_run_query_dispatch.py tests/unit/test_shared_query_conditions.py tests/unit/test_llm_execution.py tests/unit/test_llm_recording.py tests/unit/test_dynamic_memory_service.py tests/unit/test_dev_runbook_check.py` | 0 | `265 passed in 6.46s` |
| `.\.venv\Scripts\python.exe -m pytest tests/contract -q` | 0 | `92 passed in 0.35s` |
| `.\.venv\Scripts\python.exe -m pytest tests/web -q` | 0 | `33 passed in 9.24s` |
| `.\.venv\Scripts\python.exe -m pytest -q` | 0 | `679 passed in 19.18s` |
| `git diff --check` | 0 | 无 whitespace 错误；Git 仅提示工作副本 LF/CRLF 转换。 |

首次 A 专项命令误写不存在的 `tests/unit/test_eval_prediction_persistence.py`，退出码 1、`no tests ran`；随后用实际文件列表修正并得到上表 265 通过，不将首次命令算作测试通过。

## 待集成确认

- B 的正式 Memory 落盘及其严格加载、C 的真实索引与检索、D 的状态解析、E 的实际服务工厂和正式 Web 闭环均未在本轮验证；合成 Fake 只证明注入契约。
- `RunProtocol` 的指纹属于合成端口声明，真实提供者身份与真实候选共享需跨模块复核。正式 Gold、课程评分口径、模型与 Embedding 选择仍未冻结。
- 任意历史版本 DTO 文件回读缺少归档字节 fixture；第二轮已用现有公共 Schema 2.1 构造旧式省略默认字段的合法/非法合成 JSON，并实测回读与拒绝。此证据仅覆盖所构造的形状，真实历史文件仍待集成确认。

## 第 2 轮补充审查（迁移到 v3 执行）

本轮起始及结束分支为 `A`，HEAD 均为 `02923f7bb8d5b61ff3c64ae7619c768a01aeaba6`。启动时 `git status --short` 有 9 个已修改文件和本报告 1 个未跟踪文件，全部保留。本轮仅新增 `tests/contract/test_contracts.py` 的合成旧式 DTO 回读/拒绝测试，并更新本报告；未修改公共 Schema、B/C/D/E 归属代码或配置。以下所有证据均是本地 synthetic_fixture/Fake，不能代替正式跨分支集成。

### 七面可定位证据

| 审查面 | 实际源码位置、测试名称与关键断言 |
| --- | --- |
| 共享输入与隔离 | `src/dynamic_memory/data/repository.py:80` 的 `SourceRepository.snapshot_sha256`、`evaluation/question_manifest.py` 的 `load_question_manifest` 绑定快照与题目身份；`evaluation/predictions.py:99,132` 与 `evaluation/gold_loader.py:36` 验证预测 receipt 后才加载 Gold。`tests/unit/test_eval_question_identity.py::test_explicit_verified_official_id_and_collisions` 拒绝重复身份；`tests/unit/test_gold_isolation.py::test_missing_receipt_file_hash_and_mismatched_identity_fail_before_gold` 拒绝缺失或不一致证明；`tests/unit/test_three_method_batch_runner.py::test_real_a14_synthetic_manifest_exposes_no_gold_to_batch` 断言批处理没有 Gold。`RunProtocol` 在 `evaluation/runners/three_method_runner.py` 校验快照、拆分、索引声明，缺失声明不能作为可比运行。 |
| 时间与错误语义 | `src/dynamic_memory/pipeline/query_runner.py:256,345` 将一次分析与各路执行分开。`tests/unit/test_shared_query_conditions.py::test_conflict_builds_same_valid_early_stop_for_three_methods` 断言冲突早停；`test_time_parse_uncertainty_is_not_guessed_or_early_stopped` 保留解析不确定；`tests/unit/test_run_query_dispatch.py::test_empty_candidates_have_real_candidate_set_and_correct_status` 验证 `no_evidence`，`test_business_uncertainty_statuses_are_not_collapsed_to_no_evidence` 验证 `time_uncertain`，`test_missing_port_and_unknown_exception_are_sanitized_errors` 验证 `error`；`tests/unit/test_gold_isolation.py::test_missing_gold_is_separate_from_failed_prediction` 独立验证 Gold 缺失。 |
| 三路公平性 | `evaluation/runners/three_method_runner.py:141,218` 比较真实候选并逐题共享 prepared analysis；`tests/unit/test_three_method_batch_runner.py::test_one_analysis_three_methods_order_saved_and_verified` 断言分析调用一次和三路顺序；`tests/unit/test_batch_candidate_comparability.py::test_same_fingerprint_cannot_hide_candidate_difference` 与 `test_equal_missing_and_unverified` 覆盖 `mismatch`、`unverified` 和不可比较。`evaluation/runners/pure_vector_ablation.py:65` 单列消融；`tests/unit/test_pure_vector_ablation.py::test_normal_deterministic_run_and_strict_readback` 验证独立产物。 |
| Gold 与结果口径 | `evaluation/gold_loader.py:36` 先核验已保存预测；`evaluation/metrics/qa_scoring_manifest.py:156,185` 保存和验证评分证明。`tests/unit/test_qa_score_summary.py::test_saved_entrypoint_exact_scores_and_denominators` 验证错误按 0 计与主分母独立；`tests/unit/test_qa_summary_breakdown.py::test_manual_multisample_multicategory_fixture` 验证 `category × sample_id` 和原因分布，`test_scoring_integrity_readback_needs_no_gold` 断言回读不触发 Gold。 |
| LLM 资源安全 | `src/dynamic_memory/llm/` 的 executor、recording 与 Mock 端口；`tests/unit/test_llm_execution.py::test_retry_limits_preserve_last_error_and_attempt_count`、`test_task_limits_and_estimator_preflight`、`test_error_releases_permit_and_wait_timeout_is_observable` 覆盖有限重试、预算和等待超时；`tests/unit/test_llm_recording.py::test_cache_corruption_and_version_mismatch_never_replay`、`test_log_write_failure_is_observable_and_never_retries_provider` 覆盖缓存和注入异常 Fake 不隐式重调；`tests/unit/test_mock_llm.py::test_repeated_calls_instances_and_order_are_identical` 验证确定性。同步 provider 自身执行超时由注入 adapter 承担。 |
| 可复核性 | `evaluation/runners/three_method_runner.py:218,270` 和 `evaluation/runners/pure_vector_ablation.py:65,108` 使用独立运行目录、哈希和严格回读。`tests/unit/test_three_method_batch_runner.py::test_third_save_failure_preserves_first_two_without_completion` 断言前两份失败现场保留且无完成标记；`test_verify_run_detects_byte_and_manifest_tamper_and_missing_file` 拒绝篡改；`tests/unit/test_qa_summary_breakdown.py::test_scoring_concurrent_publication_has_one_winner`、`test_scoring_interrupted_publish_fails_closed` 验证不覆盖且中断可定位。 |
| 安全与兼容 | `tests/unit/test_three_method_batch_runner.py::test_reject_test_split_wrong_snapshot_and_existing_directory`、`test_verify_run_rejects_linked_control_file` 与 `tests/unit/test_pure_vector_ablation.py::test_ablation_readback_rejects_linked_control_file` 覆盖运行根、目录/控制文件路径约束；`tests/unit/test_qa_summary_breakdown.py::test_scoring_orphan_sidecar_and_traversal_rejected` 覆盖路径穿越。新增 `tests/contract/test_contracts.py:52,87` 构造旧式省略默认字段 Schema 2.1 JSON：合法 `MemoryRecord`/`QueryResponse` 回读、身份和默认字段断言通过；2.0 版本、额外 Gold 字段、空来源、非法 ID 被拒。`tests/integration/test_service_web_binding.py::test_existing_web_bindings_accepts_one_synthetic_service` 与 `tests/web` 保持 WebBindings 合成契约。无归档历史字节 fixture，因此仅证明这些合成旧式形状，真实旧文件仍待集成确认。 |

### 本轮缺陷与差异口径

本轮没有发现新的可独立复现的 A 代码缺陷。上一轮的符号链接控制文件缺陷已按上表负面测试和源码回读覆盖，本轮没有重复修改。旧式 DTO 原先只有通常的序列化回读证据；本轮补充 `tests/contract/test_contracts.py:52,87`，其反例为版本 2.0、额外 Gold 字段、空来源、非法 ID，合同套件 `97 passed`。这是证据缺口补充，不宣称修复公共 Schema。

`changed` 若按“本轮新修改的已跟踪文件”计算，只包含 `tests/contract/test_contracts.py`；若某工具对其自身 patch 返回空 `changed`，也不能推断工作树干净。`git status --short` 读取的是相对 HEAD 的**全部**未提交改动，启动时已有 9 个已修改文件和本报告 1 个未跟踪文件；最终为 10 个已修改文件和本报告 1 个未跟踪文件。`git diff --name-only` 只列已跟踪文件，`git ls-files --others --exclude-standard` 单独列本报告。两者统计对象不同，并无文件丢失。

### 第 2 轮独立自检

工作目录：`C:\Users\Keshu\OneDrive\Desktop\历史文件\大三上\软件需求工程\SRE-Project`。所有命令在本轮执行，以下数字不引用上轮结果。

| 命令原文 | 退出码 | 关键输出 |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_eval_question_identity.py tests/unit/test_three_method_batch_runner.py tests/unit/test_pure_vector_ablation.py tests/unit/test_qa_summary_breakdown.py tests/unit/test_run_query_dispatch.py tests/unit/test_shared_query_conditions.py tests/unit/test_llm_execution.py tests/unit/test_llm_recording.py tests/unit/test_dynamic_memory_service.py tests/unit/test_dev_runbook_check.py` | 0 | `265 passed in 6.17s` |
| `.\.venv\Scripts\python.exe -m pytest -q tests/contract` | 0 | `97 passed in 0.55s`，含新增 5 个合成旧式 DTO 用例 |
| `.\.venv\Scripts\python.exe -m pytest -q tests/web` | 0 | `33 passed in 9.21s` |
| `.\.venv\Scripts\python.exe -m pytest -q` | 0 | `684 passed in 19.40s` |
| `& 'C:\Users\Keshu\OneDrive\Desktop\历史文件\大三上\软件需求工程\SRE-Project\.venv\Scripts\python.exe' -m pytest -q` | 0 | 按指定解释器绝对路径复跑：`684 passed in 18.93s` |
| `git diff --check` | 0 | 无 whitespace 错误；仅有 Git LF/CRLF 工作副本提示 |

正式 B Memory 落盘、C/D 实现、E 工厂、真实旧版 DTO 文件与正式 Gold 均未接入，原因是本阶段没有可用的跨分支接口或历史字节样本，且授权明确禁止真实数据、模型和外部 API。本轮未执行 git add/commit/push/pull/merge/rebase/reset。
