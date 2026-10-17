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
