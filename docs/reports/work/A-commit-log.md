# A 分支开发提交记录

本文件持续记录 A 分支实际开发的功能提交。每条记录随对应代码和测试一起提交；日期与计划提交信息可从 Git 历史反查最终 SHA，未提交前不预填 SHA。后续 A-02 至 A-26 依序追加，不覆盖既有记录。

## A-01（2026-10-14）

- Commit Message：`实现统一配置加载与校验`。
- 功能及文件：`src/dynamic_memory/pipeline/settings.py` 新增六份 YAML 的类型化离线加载、错误定位、安全 Mock 默认与交叉预算校验；`tests/unit/test_settings.py` 新增正常、异常和注入目录测试；`README.md` 增加已实现的加载示例。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q`：25 passed（退出码 0）；`.\.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke`：synthetic smoke PASS（退出码 0）；README 配置加载示例输出 `mock 8`（退出码 0）。
- 过程记录：本条随 A-01 源码及测试一起提交；对应 SHA 以 Git 历史为准。

## A-02（2026-10-15）

- Commit Message：`实现统一LLM Adapter接口`。
- 功能及文件：`src/dynamic_memory/llm/protocol.py`、`llm/__init__.py` 定义并导出供应商无关的请求、响应、生成参数、用量和 `LLMAdapter.generate` 协议；`tests/unit/test_llm_protocol.py` 验证合法/非法输入输出及局部 Fake 注入；`docs/architecture.md` 说明已实现边界。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q` 为 44 passed（退出码 0）；既有 Mock smoke 为 PASS（退出码 0）；独立导入及局部 Fake 演示输出 `True synthetic`（退出码 0）。
- 限制：未实现生产 Mock、DeepSeek Provider 或实际模型调用；真实 Provider 的结束原因映射及业务 JSON Schema/来源核验留待后续任务。本条随 A-02 代码和测试一同提交，实际 Commit SHA 以 Git 历史为准。

## A-03（2026-10-15）

- Commit Message：`确定性 Mock LLM Adapter`。
- 功能及文件：`src/dynamic_memory/llm/mock.py`、`llm/__init__.py` 实现并导出稳定指纹、离线 Mock、严格预设匹配；`tests/unit/test_mock_llm.py` 覆盖三任务、注入、异常及跨进程稳定性；`docs/architecture.md` 说明使用边界。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q tests\unit\test_mock_llm.py`：8 passed（退出码 0）；完整 `pytest -q`：52 passed（退出码 0）；既有 `mock_smoke`：PASS（退出码 0）。跨进程结果由定向测试启动三个独立 Python 进程核对一致。
- 限制：输出均为 synthetic，不代表真实模型、Token 用量、事实抽取或问答；DeepSeek Provider 与业务 JSON Schema 校验属后续任务。

## A-04（2026-10-15）

- Commit Message：`实现DeepSeek API Adapter`。
- 功能及文件：`src/dynamic_memory/llm/deepseek.py`、`llm/__init__.py` 实现并导出显式启用的 Chat Completions Provider、请求参数映射、严格响应检查及脱敏错误分类；`tests/unit/test_deepseek_llm.py` 用内存 Fake 覆盖三任务和失败路径；`docs/architecture.md` 说明接口与后续边界。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q tests\unit\test_deepseek_llm.py`：27 passed（退出码 0）；完整 `pytest -q`：79 passed（退出码 0）；既有 `mock_smoke`：PASS（退出码 0）。全部测试离线，无真实 Provider 请求。
- 限制：正式模型 ID 尚待全组确认；本轮不验证真实账号/API、业务 JSON Schema 或计费指标。统一重试、并发及输入预算控制留待后续任务。

## A-05（2026-10-16）

- Commit Message：`实现LLM JSON结构化校验`。
- 功能及文件：`src/dynamic_memory/llm/structured_output.py` 新增供应商无关的显式 Pydantic Schema 校验入口与脱敏错误分类；`llm/__init__.py` 导出；`tests/unit/test_structured_output.py` 覆盖公共 DTO、局部抽取草稿、JSON 完整性、协议一致性及失败脱敏；`docs/architecture.md` 说明使用边界。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q tests\unit\test_structured_output.py`：32 passed（退出码 0）；完整 `pytest -q`：111 passed（退出码 0）；`mock_smoke`：PASS（退出码 0）；`git diff --check`：退出码 0。全部离线，无真实 Provider 请求。
- 限制：本层只验证已返回 JSON 的结构和现有 DTO 约束，额外要求版本化公共 DTO 显式携带 `schema_version="2.1"`；不验证事实、来源存在、候选集合或 Memory ID 与内容的哈希对应，不修复或重试模型输出。A-06 Prompt 模板管理留待下一轮。

## A-06（2026-10-17）

- Commit Message：`实现统一Prompt模板管理`。
- 功能及文件：`src/dynamic_memory/llm/prompts.py` 新增不可变模板、受限占位符渲染、精确版本查找、稳定模板指纹与按配置构建 LLMRequest；`llm/__init__.py` 导出；`tests/unit/test_prompts.py` 覆盖模板合同、变量原样保留、跨进程指纹、参数映射、QA 公平输入和脱敏失败；`docs/architecture.md` 提供三个初始模板的可运行示例及 B/D 扩展约定。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q tests\unit\test_prompts.py`：33 passed（退出码 0）；`.\.venv\Scripts\python.exe -m pytest -q`：144 passed（退出码 0，原有 111 项无回归）；`.\.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke`：PASS（退出码 0）；架构文档 Python 示例实际运行 PASS（退出码 0）；`git diff --check`：退出码 0。测试均使用 synthetic 数据，无真实 API 请求。
- 限制：三个内置模板均为 `demo-v1` 演示/联调版本，不代表已冻结的正式实验 Prompt；上下文选择、Gold 排除、来源事实核验由业务模块负责，安全提示不能证明完全抵御 Prompt Injection。本层不计数或截断 Token、不实施抽取/状态算法、不写日志缓存；A-07 模型重试、并发与预算执行留待后续任务。

## A-07（2026-10-17）

- Commit Message：`实现LLM调用重试并发与预算控制`。
- 功能及文件：`src/dynamic_memory/llm/execution.py` 增加可注入的 `GuardedLLMAdapter`、有限重试白名单、执行器共享的在途并发许可、调用前预算/输入估算核验、实际用量与未知尝试摘要及脱敏错误；`llm/__init__.py` 导出；`tests/unit/test_llm_execution.py` 使用 Fake、Mock 与线程事件覆盖重试、并发、预算及错误路径；`docs/architecture.md` 说明边界与可运行离线示例。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q tests\unit\test_llm_execution.py`：46 passed（退出码 0）；完整 `pytest -q`：190 passed（退出码 0，原 144 项无回归）；`mock_smoke`：PASS（退出码 0）；架构文档 A-07 离线示例运行 PASS（退出码 0）。无真实 Provider 请求。
- 限制：无可信输入 Token 估算时严格模式调用前拒绝，宽松模式标明未核验；估算不等于真实分词数，未知用量不填零。`API_STATUS` 无结构化状态码、`INVALID_RESPONSE` 无空 JSON 精细分类，默认不重试；并发限制只在同一执行器实例及进程内共享，Provider 单次超时仍由其自身负责。本层不裁剪业务上下文、不写日志缓存或计算真实费用。A-08 调用日志与缓存留待下一轮。

## A-08（2026-10-18）

- Commit Message：`实现LLM调用日志与缓存`。
- 功能及文件：`src/dynamic_memory/llm/recording.py` 新增严格版本化的 `CallIdentity`、组合缓存键、内存/显式 SQLite 存储、成功响应校验与重放、脱敏逻辑调用事件及同键 single-flight；`llm/__init__.py` 导出；`tests/unit/test_llm_recording.py` 用 synthetic Fake、线程事件与 `tmp_path` 验证身份隔离、成功与失败、事务回滚、损坏检测和离线重放；`docs/architecture.md` 说明 API、敏感缓存路径及运行边界。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q tests\unit\test_llm_recording.py`：44 passed（退出码 0）；完整 `pytest -q`：234 passed（退出码 0，原 190 项无回归）；`mock_smoke`：PASS（退出码 0）；架构文档 A-08 离线示例运行 PASS（退出码 0）；`git diff --check`：退出码 0。无真实 API 请求。
- 限制：默认只保存内存日志与缓存；SQLite 须显式指定被忽略的 `results/` 或临时路径，原始响应可能含私人信息。single-flight 只在同一执行器实例内有效；不跨进程锁定同键请求。指纹与版本身份由 A-09/业务层提供，未推断 Gold 或业务事实；QA 缓存不证明回答正确，JSON 无业务 validator 不入缓存。本层不计算真实费用、没有在线或跨进程并发验证。A-09 Runtime 与依赖注入留待下一轮。

## A-09（2026-10-18）

- Commit Message：`实现Runtime初始化与依赖注入`。
- 功能及文件：`src/dynamic_memory/pipeline/runtime.py` 新增显式 `create_runtime`、只读引用的 `LLMRuntime`、`PreparedCall`、请求/身份绑定及执行预检；只装配一个底层 Adapter、一个 A-07 Guarded 和一个 A-08 Recorded，默认 Mock，DeepSeek 需独立显式授权与凭据或受控 Fake Client。`tests/integration/test_llm_runtime.py` 验证共享实例、Prompt/JSON、三路 QA、身份隔离、缓存、并发、离线 Fake 和失败路径；`docs/architecture.md` 增加可运行示例与接入边界。
- 测试：定向 `pytest -q tests\integration\test_llm_runtime.py`：10 passed（退出码 0）；完整 `pytest -q`：244 passed（退出码 0，原有 234 项无回归）；`mock_smoke`：PASS（退出码 0）；文档 A-09 示例输出 `mock 2`（退出码 0）；`git diff --check`：退出码 0。全部离线，无真实 API 请求。
- 限制：这只是共享 LLM 基础设施的 Runtime；B/C/D 的真实数据、索引、状态和证据核验及 A 的正式 `run_query` 尚未接入。Mock/Fake 的回答、用量和身份皆为 synthetic，不能作为业务或在线验证。数据/索引指纹、Schema、查询范围和已存在的 Git SHA 必须由上游显式传入；JSON 缺业务 validator 不成功缓存。


## G-01 / G-02 公共接口兼容修复（2026-10-19，非 A-10 编号任务）

- Commit Message：`修复空JSON统一重试与MemoryArtifact索引交接`。
- G-01：DeepSeek Provider 将 JSON 任务下的空内容明确分类为 `EMPTY_JSON`；A-07 统一 Guarded 执行器与 `TIMEOUT/NETWORK/RATE_LIMIT` 共享 `max_retries=2` 的最多三次调用预算，文本空白/`{}`/非空无效 JSON/截断与 Schema 校验失败均不重试。A-08 原有日志/缓存记录真实尝试次数与失败码，失败结果不会缓存。
- G-02：公共 `contracts.artifacts` 新增 `MemoryManifest`、`MemoryArtifact`、`FailedWindow` 及稳定内存记录集合/manifest 指纹，验证版本、样本、记录数、Memory ID、窗口状态与摘要一致性；正式 `build_index` 规范改为接收 `MemoryArtifact` 而非裸列表。B 的严格磁盘/来源/发布校验 loader 与 C 的实际索引仍由相应模块实现。
- 测试：新增离线 G-01 Provider→执行器→A-08 缓存/审计链专项测试18项与 G-02 结构及拒绝 partial 的合同测试22项；完整 pytest 284 passed（原有244项无回归）、Mock Smoke PASS、git diff --check 通过。没有真实 DeepSeek 调用，不把 Mock/Fake 当作业务事实。
- 兼容：维持技术规范 v2.2 与业务 Schema 2.1，不更改已存在的 MemoryRecord/SourceRef/QueryResponse 字段；B/D 不再自行叠加 JSON 空内容重试，C 不能以裸列表作为正式索引入口。

## A-10（2026-10-19）

- Commit Message：`实现公共对话数据容器`。
- 功能及文件：`src/dynamic_memory/contracts/data.py` 唯一定义 IC-001 的 `ConversationSession`、`ConversationSample`、`DatasetBundle`，复用 `SourceTurn` 和公共基类，校验同层唯一性、父子来源身份、划分集合与快照摘要；`contracts/__init__.py` 导出；`tests/contract/test_dataset_containers.py` 使用纯 synthetic fixture 覆盖保序、回读、空结构、Gold 字段拒绝及异常路径，并对三个公共容器启用 `hide_input_in_errors=True`、验证格式化校验错误不会回显被拒绝的 Gold 标记；`docs/contracts.md` 写明结构合同、脱敏边界与 B 加载器职责。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q tests\contract\test_dataset_containers.py`：26 passed（含 4 项异常脱敏测试，退出码 0）；完整 `pytest -q`：310 passed（原有 284 项无回归，退出码 0）；`mock_smoke`：PASS（退出码 0）；`git diff --check`：退出码 0。全部离线，不读取真实数据、Gold 或调用模型 API。
- 限制：公共容器只检查结构与字段白名单；B-02 负责原始 LoCoMo 解析和 Gold 剥离，B-03 负责 SourceRepository，快照文件 SHA 和冻结配置一致性由 B 的严格加载器核验。v2.2 未设置列表最小数量，故空 session、sample 和 Bundle 在结构层合法；不代表正式数据可为空。

## A-11（2026-10-20）

- Commit Message：`实现共享查询条件处理入口`。
- 功能及文件：`src/dynamic_memory/pipeline/query_conditions.py` 新增可注入 `QueryAnalyzer`、`prepare_shared_query` 和防御性快照 `PreparedQuery`，每个请求分析一次，复验 Schema 2.1/ID/显式日期，并在序列化前检查 QueryRequest、QueryAnalysis、嵌套 TimeIssue 原始字段白名单以拒绝 model_copy 注入的未知字段，统一三路时间冲突或错误早停；`tests/unit/test_shared_query_conditions.py` 使用 synthetic Fake 覆盖六种意图、时间一致性、脱敏失败与隔离；`docs/architecture.md` 增加接入示例和边界。
- 测试：专项 `pytest -q tests\unit\test_shared_query_conditions.py`：35 passed（含 8 项未知字段/嵌套字段的回归测试，退出码 0）；完整 `pytest -q`：345 passed（退出码 0）；`mock_smoke`：PASS（退出码 0）；文档 synthetic Fake 示例输出 `historical_state conflict`（退出码 0）；`git diff --check`：退出码 0。所有测试均离线，无 C 正式分析、真实模型或 Gold。
- 限制：本轮仅有 A 侧查询条件接线，没有 C 正式分析器，也不实现检索、状态推理或三路答案。原计划 A-10 的统一只读业务访问未完成；B-03 的来源仓库未合入 A，A-12 的正式三路调度仍待单独任务。

## A-12（2026-10-23）

- 拟提交信息：`实现run_query三路查询调度`。
- 功能及文件：`src/dynamic_memory/pipeline/query_runner.py` 新增显式注入端口的 `QueryOrchestrator`，提供 `run_query`、`prepare_query`、`run_prepared`；复用 A-11 单次分析/早停，Full Text 只读原始样本，Vector RAG 与 Dynamic 共享 C 检索/候选装载路径，D 解析通过校验后才进入 Dynamic 回答。逐层复验公共 DTO、Memory ID、候选来源、解析决定/关系证据与最终响应，失败使用稳定脱敏码；`tests/unit/test_run_query_dispatch.py` 覆盖三路 synthetic Fake 调度、调用顺序、空候选、异常与边界；`docs/architecture.md` 说明端口职责与离线示例。
- 测试：A-12 专项 `pytest -q tests/unit/test_run_query_dispatch.py`：24 passed；A-11 专项：35 passed；排除 Web 的离线完整回归 `pytest -q --ignore=tests/web`：437 passed；Mock smoke：PASS；文档 synthetic 示例输出 `full_text no_evidence`；`git diff --check`：通过，以上退出码均为 0。完整 `pytest -q` 在收集两项 Web 测试时因当前 `.venv` 缺少可选依赖 `streamlit` 中断，退出码 1，未获得完整套件通过结论；本轮未安装依赖。
- 限制：C 正式查询分析/检索/Vector QA、D 正式解析/Dynamic QA、B 严格产物与匹配索引读取适配、A 正式 Full Text 生成器及统一只读服务尚待接入；测试均为 synthetic，不是 LoCoMo 事实或正式三路问答结果。A 调度器信任注入的 `load_verified_index` 端口完成 B/C 文件、来源、完整性和索引版本验证，不将内存 DTO 当作磁盘验证证据。

## A-13（2026-10-24，待用户审阅，未提交）

- 建议提交信息：`实现Full Text问答基线`。
- 功能及文件：`src/dynamic_memory/baselines/full_text.py` 与包导出实现原始可见轮次组织、完整 QA 输入预算估算、最近完整轮次截断、不可变来源诊断、共享 QA Prompt/Runtime/身份记录及脱敏失败；`tests/unit/test_full_text_baseline.py` 用离线 synthetic Fake 覆盖原文、来源、预算、异常与 A-12 注入；`docs/architecture.md` 说明正式接入和限制。
- 测试：A-13 专项 `pytest -q tests/unit/test_full_text_baseline.py`：10 passed；A-11/A-12 回归：59 passed；完整 `pytest -q`：480 passed（原 470 项无回归）；Mock smoke：PASS；`git diff --check`：通过，退出码均为 0。未运行正式 LoCoMo/Gold 或付费模型；Fake 回答不代表事实正确。
- 限制：预算以注入的 TokenEstimator 为估计而非 Provider 精确用量；`qa_shared@demo-v1` 尚非正式冻结 Prompt，未验证真实模型回答的事实性。正式 B/C/D、统一服务/Web、三路评分及 Gold 隔离评测仍待后续接入。

## A-14（2026-10-25，待用户审阅，未提交）

- 建议提交信息：`建立QA身份与Gold隔离评测基础`。
- 功能及文件：`evaluation/question_manifest.py` 提供冻结快照校验、问题白名单、原始 QA 下标及稳定身份；`evaluation/predictions.py` 提供完整预测的临时/忽略目录保存、字节回执与回读验证；`evaluation/gold_loader.py` 仅在预测核验后读取 Gold，区分缺失与格式错误；`evaluation/metrics/answer_adapter.py` 提供五状态固定评分输入。新增三组 synthetic 单元测试，更新 `docs/architecture.md`。
- 测试：题目身份专项 4 passed、Gold 隔离专项 9 passed、答案适配专项 6 passed；完整 `pytest -q`：499 passed（A-13 基线 480 项，新增 19 项）；Mock smoke：PASS；`git diff --check`：通过。所有退出码为 0，未读取真实 LoCoMo 测试 Gold、未调用模型或生成正式分数。
- 审查修复：修正 `evaluation/predictions.py` 对仓库 `results/` 的根目录计算（`parents[1]`），并将预写入后 `os.replace` 改为不可覆盖已有目标的原子硬链接发布；增加真实仓库结果目录/仓库父目录拒绝测试和并发目标先创建时的保留性测试。修复后 Gold 隔离专项 **11 passed**、A-14 三组专项 **21 passed**、完整回归 **501 passed**，Mock smoke：PASS，均无真实 Gold 或付费调用。硬链接文件系统不受支持时返回固定保存失败错误，不改写已存在文件。
- 限制：官方 ID 字段须在外部核实后显式指定；本轮不含官方 QA F1、Recall、状态指标和三路 Runner。`docs/references/Milestone4/` 为用户已授权的预存未跟踪资料，本轮未触碰；下次经用户授权提交时随 A-14 统一纳入。

## A-15（2026-10-25，待用户审阅，未提交）

- 建议提交信息：`实现LoCoMo官方五类QA评分器`。
- 功能及文件：`evaluation/metrics/official_qa.py` 复现固定上游五类 QA 规则；`evaluation/metrics/qa_summary.py` 在 A-14 预测/Gold 门槛后逐题评分并汇总主/条件分母、失败、缺 Gold、类别/样本/方法；`pyproject.toml` 增加有界 `eval` 可选依赖；两组 synthetic 离线专项测试；`docs/architecture.md` 说明固定版本及边界。
- 上游依据：`snap-research/locomo` 的 `task_eval/evaluation.py`，commit `cbfbc1dba6bc53d00625212a0f22d55ffee7c1fc`；一次性动态对照 4 个归一化/词项样例和 5 个类别样例全部一致。未安装或运行完整 BERTScore/ROUGE 模块。
- 测试：`test_official_qa_scoring.py` 13 passed、`test_qa_score_summary.py` 5 passed；完整 `pytest -q` 519 passed（A-14 基线 501，新增 18）；Mock smoke PASS；`git diff --check` 通过，以上退出码均为 0。所有 Gold 和预测均为 synthetic，无真实课程分数。
- 审查修复：发现 GitHub Actions 原安装 `.[dev,web]`，未包含 A-15 的 `eval` 可选依赖；本轮将 `.github/workflows/contracts.yml` 改为 `.[dev,web,eval]`，保障全新 CI 环境能安装 NLTK/regex。修复后本地 A-15 专项 18 passed、全量 519 passed、Mock smoke PASS；正式 CI 结果以后续远端执行为准。

## A-16（2026-10-27，待用户审阅，未提交）

- 建议提交信息：`实现检索证据与动态状态评测指标`。起点大写 A 分支，HEAD `b17893b6d1411982cb5b474a2d38e6b1426cf78f`，工作区干净。
- 功能及文件：`evaluation/metrics/evidence_recall.py` 实现来源仓库核验后的 Evidence-turn Recall@5/@10、逐题状态及方法/样本分母，并在 A-14 保存预测/回执验证后读取 Gold；`evaluation/metrics/state_metrics.py` 实现显式 Gold Memory 身份映射、关系/决定分类及有效集 P/R/F1/Exact Match，区分完整产物中的抽取缺失、候选缺失、身份不明和模型失败。`tests/unit/test_evidence_turn_recall.py` 与 `tests/unit/test_state_metrics.py` 为离线 synthetic 样例；`docs/architecture.md` 记录单位、空集、N/A、混淆矩阵和正式标注边界。
- 验证：证据专项 11 passed、状态专项 5 passed；原完整回归 535 passed，Mock smoke PASS；无真实 Gold、API、下载或课程成绩；正式 `qa.evidence` 格式及状态 Gold/Memory ID 仍待核实。
- 提交前独立审查修复（2026-10-27）：发现同名轮次来自不同冻结快照时仍可被算作合法召回，现为共享 `SourceRepository` 增加只读 `snapshot_sha256` 属性（取自严格验证的 DatasetBundle，不重新哈希原始文件），Evidence-turn Recall 先核对仓库与 Manifest 快照身份，再读取 Gold；不符报 `EVIDENCE_SNAPSHOT_MISMATCH`，未加载时报 `EVIDENCE_SOURCE_UNAVAILABLE`。同时新增 Gold 决策与有效集合的显式一致性检查，避免将 `EXCLUDE` 等误标为有效而继续评分；对已标注的 `RETAIN` / `EXCLUDE` / `UNRESOLVED` 与可用有效集矛盾时报 `STATE_GOLD_INCONSISTENT`，部分标注不擅自补齐。3 项新增回归测试覆盖反例及保存前核验顺序；修复后 A-16 专项 **19 passed**、完整回归 **538 passed**、Mock smoke PASS、`git diff --check` 通过。仅在 A 工作树实现该最小共享读取属性，未修改 B 分支。


## A-17（2026-10-27，待用户审阅，未提交）

- 建议提交信息：实现三路批量预测与可比性核验。起点大写 A 分支，HEAD ee2350a49b4d9951ef045c490219faebc0b9dce9，工作区干净。
- 功能及文件：evaluation/runners/__init__.py、three_method_runner.py 实现一次共享分析、三路预测、实际候选比较、三份 A-14 兼容文件保存及独立验收；tests/unit/test_three_method_batch_runner.py、test_batch_candidate_comparability.py 用离线 synthetic Fake 验证顺序、失败、候选差异与持久化门槛；docs/architecture.md 记录协议及边界。未修改 A-12 调度接口或公共 Schema。
- 验证：Runner 专项 17 passed、候选比较专项 8 passed；完整 pytest 563 passed（起点 538，新增 25）；Mock smoke PASS；git diff --check 通过，以上退出码均为 0。实际 synthetic 三路持久化与独立回读 3/3 文件成功，2 道题候选 identical 2、mismatch/not_comparable/unverified 各 0。没有 Gold、付费 API、正式模型或真实成绩；未提交或推送。


## A-18（2026-10-27，待用户审阅，未提交）

- 建议提交信息：实现统一只读服务与构建状态接口。起点大写 A 分支，HEAD 3d62f27085701cf3fbc50b4fc7004155d91b3e6d，工作区干净。
- 功能及文件：contracts/service_status.py 与 contracts/__init__.py 增量定义并导出 BuildStatus；services/dynamic_memory_service.py 与 services/__init__.py 提供六个只读方法、可信产物/索引状态门槛及脱敏错误；tests/contract/test_build_status_service.py、tests/unit/test_dynamic_memory_service.py、tests/integration/test_service_web_binding.py 用离线 synthetic Fake 验证状态、数据隔离、A-12 调度与 E WebBindings；docs/contracts.md、docs/architecture.md 说明接口与正式依赖。未改既有 Schema 字段、A-12、A-17 或 E 页面。
- 验证：服务专项 14 passed、状态合同专项 20 passed、A→E 合成接入 1 passed、Web 回归 33 passed；完整 pytest 598 passed（起点 563，新增 35）；Mock smoke PASS、git diff --check 通过，以上退出码均为 0。未接入 B/C/D 正式 loader/索引/模型；无真实 Gold、API 或课程成绩。未暂存、提交、推送或合并。
- 独立审查修复（2026-10-27）：一是服务和原 QueryOrchestrator 可以注入两个具有相同 sample_id/SourceRef 但快照不同的来源，误接受异源原文；现在 A-12 新增公开 `with_source_lookup`，A-18 服务构造时创建独立调度器并以自己的 SourceRepository.get_sample 强制绑定原文，外部原有调度器保持不变。二是旧服务先检查 Memory/索引再分析问题，会把可提前返回的 `time_uncertain` 覆盖为 `BUILD_NOT_FOUND`；现在先执行一次 `prepare_query`，错误/时间冲突立即通过 `run_prepared` 返回；正常请求再检查 Memory/索引并复用同一 PreparedQuery。早停与正常响应均复验身份和公共 Schema。新增 3 项 synthetic 反例测试，修复后 A-18 专项 **38 passed**、A-12 调度回归 **24 passed**、Web 回归 **33 passed**、完整 pytest **601 passed**、Mock smoke PASS、`git diff --check` 通过。A 仅能证明服务的 get_sample 与同一个 SourceRepository 绑定；任意注入的真实生成器和 C 索引语义必须由 B/C 可信组合根及指纹核验。本轮未调用真实模型/Gold，未更改 B/C/D/E/main。
