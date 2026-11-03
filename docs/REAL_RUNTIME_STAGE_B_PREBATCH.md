# 阶段 B：真实批量授权前的审核与预算安全交接

日期：2026-11-03。本文档不授予 DeepSeek 付费调用，也不是人类审核批准文件。

## 当前阶段状态

冻结的 main_dev/conv-30 有 63 个完整确定性窗口。已成功的真实 High Pilot 只有 1 个窗口、6 条草稿。剩余 62 窗尚无真实结果，独立事实审核尚未签署，正式 MemoryArtifact、Manifest 和对应真实 BGE 索引均不存在。现有 synthetic-0 / 63 Fake SDK / 62 synthetic Memory / BGE CPU 索引回读是离线功能证据，不能代替正式 B 数据。

## 1. 全局费用账本的核验与防重放补丁

B-R4 的授权 user-chat-20261103-pilot01 消费 1/1，新 Pilot user-chat-20261103-final-pilot01 消费 1/1。Codex 报告称预算账本与授权注册标记曾存在，但独立远程检查发现预期的 Windows Local AppData 下 SRE-Project-BudgetLedgers 与 SRE-Project-AuthorizationRegistry 两个目录均不存在，而私有检查点及成功调用的 Token 汇总仍存在。原因未知，不可以声称旧 ledger 已经恢复，更不能将此当作允许重试的证据。

新 batch_budget.py 的实际运行语义：
- 两个已消费授权 ID 即使预算目录、注册标记全丢也始终拒绝。
- REAL pilot/batch 模式下，没有预先独立登记的计数为零的授权标记，Runner 会 fail-closed 并返回 BUDGET_ENROLLMENT_REQUIRED，不会自动初始化零次账本。offline_test 模式保留原有行为。
- 独立登记只能使用 BatchBudgetLedger.enroll()，必须在用户明确批准了一个新的授权 ID、模型、窗口、价格和最大请求数后运行；Runner 不会调用它。
- 一次新请求先持久将标记计数从 0 提升至 1，再写尝试账本，然后才发出网络调用。崩溃/删除一侧记录/回滚计数应 fail-closed。更换 state-dir 不改变真实授权的注册位置。
- 这是 Windows 本地文件级的操作性保证，不是对拥有全文件系统权限的攻击者的密码学防篡改。所有副本均消失时，不得自动按新授权运行，必须人工对账及重新、独立授权。
- 上一轮报告中的原全局账本文件确切消失机制仍无法由现存日志解释；不要推断曾经实际账单为零。

### 新独立批准后的登记命令（现在不执行）

以下仅供以后用户先批准并创建新的有效独立 BatchApproval v2 文件后使用，命令本身为零 HTTP：

    python -B -m scripts.stage_b_enroll_authorization --mode batch --approval-file "<独立新批准JSON>" --authorization-id "<用户本次确认的新ID>" --state-dir "<Windows TEMP下的独立私有目录>" --confirm-separate-user-approval

必须得到 ENROLLED_ZERO_ATTEMPTS、network_calls=0，再通过原 stage_b_repair_runner 的 live 入口运行，不能额外使用 SDK 或绕过 Guarded 禁重试。

注意：当前真实授权代码冻结了 2026-11-03 UTC 对服务端默认输出上限和价格的核验；下一 UTC 日期的真实调用需要重新核验官方文档、更新明确的验证日期和对应指纹，重新发行审批 JSON。不得延长、编辑或复用已经消耗的批准。离线 Fake 回归不受这项日期冻结的影响。

## 2. 真实 Pilot 六条草稿的审核建议（非真人签署）

队列：
C:\Users\Keshu\AppData\Local\Temp\SRE_STAGE_B_FINAL_06fb5d3223d8\real_pilot\private_review\queue.json

队列指纹：sha256:938b885a4447cafed9cb9722da14e5c1e6c8c54cb08852bf1bbc11b9c8d245d8

根据原始 SourceRef 建议保留 #1（Gina 的努力意向）、#3（Jon 自述积极）、#4（Jon 自述继续向前）、#5（Jon 自述相信能够成功）、#6（Gina 鼓励 Jon）。#2 只来自 Gina 的鼓励预测，不能作为 Jon 未来已被证明成功的事实，建议拒绝。原自动校验全部为 unclear，不能自动转化为真人 approved。

非权威逐条证据说明和完整 draft/source 指纹见：
C:\Users\Keshu\Documents\SRE-Project-PreBatch-20261103-2330\review_recommendations.md

在用户或指定真正的人类审核者核对并正式确认 5 留 1 的建议之前，不能创建或导入冒名 reviewer authority/ReviewApproval。即使旧 Pilot 6 条审核完成，也不代表后续 63 窗事实的独立审核已完成。

## 3. 费用准备（不是授权）

官方当前文档：
https://api-docs.deepseek.com/quick_start/pricing/
https://api-docs.deepseek.com/api/create-chat-completion/

DeepSeek deepseek-flash，thinking=enabled，reasoning_effort=high，不传 max_tokens，服务端默认生成上限 65536 tokens（包含 reasoning）。完整上下文 1M，实际请求来源仍由冻结的 63 个窗口决定，不裁剪可见对话。

- 63 窗实际待发送 Prompt 的 UTF-8 bytes+framing 估算总量 200957，最小 1968、最大 3507；它不是精确供应商计费 Token。
- 成功 Pilot：prompt 549、completion 5808、reasoning 5265、final 543；cache hit 384、miss 165；按峰时价格估算 USD 0.007021404。
- 若机械地用一个 Pilot 倍乘 63，峰时总额约 USD 0.44235，当前官方非峰时按半价约 USD 0.22117。样本量为 1，这不是可靠的正式费用承诺，其他窗口的推理长度及失败率均可能不同。
- 当前 Runner 按上下文最宽容上限每窗预留 934464 输入与 65536 生成。峰时 input-miss USD 0.30/百万、output USD 1.20/百万，单窗保守容量 USD 0.3589824，63 窗总预留 USD 22.6158912。该数由于输入容量严重高于真实短窗口输入而明显偏保守，属于最坏容量预算而非预测账单。
- DeepSeek 当前公布峰时为周一至周五 01:00–04:00、06:00–10:00 UTC，其他时段半价（中国节假日例外）。批准的安全预算应使用峰时价格，真实扣费以供应商记录为准。
- 不通过“调用结束后记录 Token”冒充调用前 USD 硬限；若要批准比上述严格容量预留更低的硬限，需先建立供应商级强制限额或改变生成策略，不能在忽略 64K 默认输出上限的情况下直接声明有硬费用保证。

## 4. 完整 B 发布与阶段 C 前置条件

完整 63/63 真实合法提取结果 + 逐窗逐事实真人审查（允许明确 reject/unclear 的剔除）+ strict ReviewedMemoryArtifactStore/ArtifactPorts 正式发布并回读 + 真实 BGE 固定权重 384 维索引保存/回读和合法候选检索，这四项必须全部完成，才能将相应真实路径交给阶段 C。不能用 synthetic-0 代替。

默认 Mock、禁 API、零自动重试、独立数据源和 Gold 隔离继续保留。所有付费 API、审核批准和后续完整批量运行均需新的明确用户授权。
