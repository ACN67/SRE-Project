# 架构说明

离线：B 规范化对话及抽取 Memory，C 构建索引。在线：C 分析查询并检索，D 解析状态与问答，A 组装统一业务服务，E 展示。Gold 仅在 A 的独立评分路径读取。详见 `TECHNICAL_SPEC.md` 第二章。上述真实模块尚未实现。

## 共享模型调用接口（A-02）

`dynamic_memory.llm` 提供 `LLMMessage`、`GenerationSettings`、`LLMRequest`、`LLMResponse`、`TokenUsage` 和同步 `LLMAdapter.generate(request) -> LLMResponse` 协议。抽取与状态语义判断请求使用 JSON 输出模式；问答请求使用文本模式。生成预算、温度、思考模式和可空模型 ID 由调用方显式提供，可从已加载的 `AppSettings` 取得。响应保留原始输出；JSON 模式还解析为对象，并拒绝非法或截断的 JSON。用量、结束原因和模型 ID 可以为未知值。`validate_response_for_request` 可检查响应模式及已知模型 ID 是否与请求一致。

这些类型仅定义内部调用合同，不读取密钥或发起模型请求。B/D 仍需按公共业务 DTO 验证模型输出的事实、来源与 ID。

## 离线确定性 Mock（A-03）

`dynamic_memory.llm.MockLLMAdapter` 实现上述 `generate` 协议。默认 JSON 输出仅含 `mock`、`synthetic`、任务类型与请求 SHA-256 指纹；QA 返回明确标为 Synthetic Mock 的非事实文本。它不回显输入、不推断事实，`usage`、`finish_reason`、实际模型 ID 保持未知。

测试可用 `request_fingerprint(request)` 作为键，向 `MockLLMAdapter({key: LLMResponse(...)}, strict=True)` 注入内存中的合成响应。严格模式遇到未配置的请求会抛出 `MockPresetNotFound`；默认模式则返回通用合成结果。预设仍经响应与请求合同校验。此 Mock 只用于离线联调，真实 DeepSeek Provider 和业务 JSON Schema/来源验证仍待后续任务。
