# 架构说明

离线：B 规范化对话及抽取 Memory，C 构建索引。在线：C 分析查询并检索，D 解析状态与问答，A 组装统一业务服务，E 展示。Gold 仅在 A 的独立评分路径读取。详见 `TECHNICAL_SPEC.md` 第二章。上述真实模块尚未实现。

## 共享模型调用接口（A-02）

`dynamic_memory.llm` 提供 `LLMMessage`、`GenerationSettings`、`LLMRequest`、`LLMResponse`、`TokenUsage` 和同步 `LLMAdapter.generate(request) -> LLMResponse` 协议。抽取与状态语义判断请求使用 JSON 输出模式；问答请求使用文本模式。生成预算、温度、思考模式和可空模型 ID 由调用方显式提供，可从已加载的 `AppSettings` 取得。响应保留原始输出；JSON 模式还解析为对象，并拒绝非法或截断的 JSON。用量、结束原因和模型 ID 可以为未知值。`validate_response_for_request` 可检查响应模式及已知模型 ID 是否与请求一致。

这些类型仅定义内部调用合同，不读取密钥或发起模型请求。B/D 仍需按公共业务 DTO 验证模型输出的事实、来源与 ID。

## 离线确定性 Mock（A-03）

`dynamic_memory.llm.MockLLMAdapter` 实现上述 `generate` 协议。默认 JSON 输出仅含 `mock`、`synthetic`、任务类型与请求 SHA-256 指纹；QA 返回明确标为 Synthetic Mock 的非事实文本。它不回显输入、不推断事实，`usage`、`finish_reason`、实际模型 ID 保持未知。

测试可用 `request_fingerprint(request)` 作为键，向 `MockLLMAdapter({key: LLMResponse(...)}, strict=True)` 注入内存中的合成响应。严格模式遇到未配置的请求会抛出 `MockPresetNotFound`；默认模式则返回通用合成结果。预设仍经响应与请求合同校验。此 Mock 只用于离线联调；真实 Provider 接口见下节，业务 JSON Schema/来源验证仍待后续任务。

## DeepSeek Chat Completions Provider（A-04）

`DeepSeekLLMAdapter` 实现同一 `LLMAdapter` 协议，但默认 `api_enabled=False`，不会发起请求。只有调用方显式启用并提供非空模型 ID 后才能调用；真实 SDK 路径还要求显式传入凭据，SDK `openai` 为可选依赖，导入 `dynamic_memory.llm` 无需安装。测试可以注入纯内存 Fake Client。Provider 不读取环境密钥，也不从 `model_id: null` 推断正式型号。

单次调用使用 `client.chat.completions.create`、官方 `https://api.deepseek.com` 基础 URL、`stream=False`，将输出预算映射为 `max_tokens`，用 `extra_body={"thinking":{"type":"disabled"}}` 明确关闭思考模式；JSON 任务额外传 `response_format={"type":"json_object"}`。调用方须在提示词中明确要求 JSON。返回的原始 `message.content`、可用用量、结束原因及型号映射到 A-02 响应；异常只暴露分类诊断，不携带密钥或请求正文。SDK 自动重试设为零，项目统一重试、并发和输入预算控制留待 A-07；具体业务 JSON Schema 可由下述 A-05 入口显式校验，SourceRef 和 ID 的上下文核验仍由业务模块完成。

## JSON 结构化输出校验（A-05）

Provider 返回 `LLMResponse` 后，调用方显式调用 `validate_structured_output(request, response, schema)`，其中 `schema` 是当前任务的 Pydantic 模型类；成功返回该类的实例。A-02 负责 JSON 语法及顶层对象，A-05 重新核对原始输出与缓存、拒绝重复键和非有限数值，并执行业务 Schema 字段与跨字段验证。公共 DTO 使用 `contracts.models`，本入口额外要求继承 `VersionedModel` 的对象在 JSON 中显式提供 `schema_version: "2.1"`，尽管 DTO 自身有默认版本。

B 的抽取窗口可以自行提供局部 `{"items": [...]}` 草稿 Schema，后续再核验来源、规范化事实并生成正式 `MemoryRecord`。A-05 不证明原文支持事实，不核对来源存在或规范 Memory ID 与内容的哈希对应；需要候选和原文上下文的验证仍由业务流程调用 `contracts.validation` 等工具完成。QA 文本模式不进入此校验函数。Mock 和 DeepSeek 的默认输出及调用行为不变，校验失败抛出带固定 `code` 的脱敏异常，不自动修复或重试。
