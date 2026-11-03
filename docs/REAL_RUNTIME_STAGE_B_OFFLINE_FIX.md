# 阶段 B｜真实批次 SCHEMA_INVALID 离线修复与安全恢复设计

日期：2026-11-04。本文档记录**纯离线代码修复**；不构成新的 DeepSeek 费用授权、重试授权或真人审核批准。

## 1. 真实故障与保护现状

用户批准的原 63 窗抽取在第一个实际窗口 window_7ca1d05cd445edbfed21b9e9 返回 finish_reason=stop，但随后被严格 AtomicFactDraftBatch schema 拒绝（SCHEMA_INVALID）。它不是 TRUNCATED。账本授权 ID 为 user-chat-20261103-batch63-01，确切消费 1/63 次，统计 prompt 963、completion 7764（其中 reasoning 6400），单次费用估算 0.009568068 美元，0/63 合法 checkpoint。已经消费的这一窗口不能自动重试；另外 62 窗尚未发送。

历史账本、旧审批文件和用户隐私内容均不应修改、清空、复制到 Git 或伪造。旧结果中并未保留完整的拒绝 JSON，因此本轮无法从既有日志确定是缺少哪个字段或某个字段值错误。**不要把新 Fake 测试暴露的错误模式声称为旧真实请求的已确认根因。**

## 2. 本次完成的失败诊断（仅在之后新授权的调用中生效）

- StructuredOutputError 对 Pydantic ValidationError 只保留前 8 条严格脱敏的 field/type 和总数。字段路径来自静态白名单，未知 extra key 一律写成 unknown_field；错误种类来自允许的固定类别，其他归类 other_validation_error。绝不输出实际 value、ctx、input、raw_output、reasoning_content、密钥或用户对话。
- RecordingError / WindowExtractionError 向上传递相同的纯结构化诊断；原始正常成功日志保持旧结构。
- 新产生的 SCHEMA_INVALID 会在本次私有 Windows TEMP 状态目录下生成 private_diagnostics/window_<id>.json，只记录 run_fp、call_fp、window_id、validation_error_count、validation_errors、稳定错误码。CLI 同时可输出短小的脱敏字段列表。
- 每个失败窗口仍被 ledger.record 标记为 failed，保留已消费次数和可得的真实 Token/finish_reason。成功的 AtomicFactDraftBatch 合同、来源边界和证据审核没有放松，也没有对失败 JSON 做“宽松解析/自动修复”。

## 3. 显式跳过与可恢复处理

运行入口新增 **--continue-past-failed**（默认关闭），并向内部 run_plan 增加 continue_past_failed=False。

- 只在 batch/offline_test 中允许，pilot 禁止。必须是显式 opt-in，不能作为默认网络行为。
- 跳过仅针对同一 run_fingerprint、同一 window_id、同一 call_fingerprint 下**已经记账失败**的窗口；ledger 保存固定 error_code 后，只有 SCHEMA_INVALID、JSON_INTEGRITY、SOURCE_OUT_OF_WINDOW、RECORDED_AT_MISMATCH 等**结构化/证据问题**属于可跳过类别。
- AUTH、API_STATUS、INSUFFICIENT_BALANCE、RATE_LIMIT、TIMEOUT、spent_unknown、以前没有可信分类的旧失败、仅有伪造 checkpoint、已成功但缺失 checkpoint 等一律拒绝跳过或停止。防止额度不足时继续烧掉余下全部窗口。
- 启用显式跳过后，可继续抽取尚未消费的**其他**窗口，并回读相同运行身份下已完成的合法 checkpoint；不会重发已消费的窗口。中途停止仍可按以前的状态恢复。
- 任何窗口被跳过，最终状态必为 **INCOMPLETE_SPENT_FAILURES**、published=false，列出失败窗口 ID 与完整度；**不生成**真实审核队列，不发布 MemoryArtifact/Manifest，不启动 BGE 正式索引，绝不把 62/63 冒充 63/63。
- 没有显式跳过选项时，保留旧行为：第一个失败即停止，随后同一已消费窗口返回 BUDGET_ATTEMPT_SPENT，0 新 HTTP。

## 4. 新旧费用授权隔离

本次修改了 runner.py、llm/recording.py、llm/structured_output.py、memory/extraction.py、memory/batch_budget.py，且将 recording.py 和 structured_output.py 纳入 CODE_FILES 的代码指纹计算。真实 run_fingerprint 已变化。旧用户批准的审批文件绑定了旧代码指纹：**不能用旧授权继续发起任何调用**；即使尚剩 62 次名义额度，也不能跨已变更代码偷用它。

注意同一 run_fingerprint 的 checkpoint 不可直接混入另一代码版本的正式 MemoryArtifact。现有显式跳过只解决“在新的运行身份中、某些窗口失败之后继续其他窗口，同时保持不完整状态”的开发和安全问题，不提供跨代码版本的付费成功检查点迁移。真正恢复旧窗口并发布完整真实 B，仍需明确的新运行规划与用户单独批准；不能以本离线修改充当自动付费许可。

费用账本版本保持不变，新增的 error_code 字段可选，旧条目缺少字段时只允许保守地停止，**不能因为信息不足而当作可跳过的错误**。旧账本 SHA、审批 SHA、原始数据 SHA 和 Git HEAD 应保持不变。

## 5. 离线测试验收

- 新增 tests/integration/test_stage_b_offline_repair.py：Fake SDK 模拟缺字段、未知 extra key、错误来源类型、错误枚举、非法日期/SourceRef；确认只记录字段路径/错误类别及实际 usage，任何敏感文本不泄漏到公开结果、私有诊断摘要和 recorder 事件。
- 模拟第三窗口 schema 失败、前两个有效、剩下 60 个有效；断点恢复后只有 62/63 完成，账本 63 次（1 次失败），永不生成审核队列或正式产物。
- 模拟第一窗口失败时显式跳过，后续暂停/恢复；模拟 provider 402 余额失败不得继续消费、spent_unknown 崩溃不能跳过、无错误类别的旧账本失败不能跳过、已付费 extracted checkpoint 丢失后 fail-closed。
- 自动验证录入旧 run_fingerprint 的审批与当前运行不匹配，SDK 0 次调用。
- 最终全量 pytest 必须基于**最后一次生产代码修改之后**的源码重跑，记录 exit code 和数量；不能只引用上一次 Codex 的 1205 passed。

**当前不存在用户对此次修复后新版本的 DeepSeek 付费批准。所有真实调用在新审批之前严格禁止。**
