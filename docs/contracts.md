# 公共合同 v2.1

主要业务 DTO 位于 `src/dynamic_memory/contracts/models.py`，Memory 产物 DTO 位于 `src/dynamic_memory/contracts/artifacts.py`；公共类型统一由 `contracts/` 导出，完整语义见 `TECHNICAL_SPEC.md` 第五章。主要对象为 SourceRef、SourceTurn、TimeSpan、MemoryRecord、QueryRequest、QueryAnalysis、CandidateItem、CandidateSet、RelationEdge、MemoryDecision、ResolutionResult、AnswerResult、QueryResponse。独立跨模块对象要求 `schema_version="2.1"`。

`QueryAnalysis.query_intent` 和 `time_mode` 是独立字段。SourceTurn 原文与 image_caption 分开，统一用 `render_visible_turn` 显示。Memory ID 使用 `contracts.memory_id` 中的规范化 canonical JSON SHA-256 算法。CandidateSet 从 rank 1 连续排序、去重并受预算约束。ResolutionResult 的有效 ID 恰好是 RETAIN ID。Full Text 无 retrieved/resolution；Vector RAG 有 retrieved、无 resolution；Dynamic 正常同时有两者。

需要候选上下文的校验由 `validate_resolution_against_candidates` 执行；需要完整 Memory 和原始轮次索引的引用校验由 `validate_response_context` 执行。结构与 ID 校验不能证明语义事实受原文支持，后续成员须人工抽查。Gold 不在业务 DTO 中。

当前已实现公共 DTO/验证工具、Mock 与可显式启用的 DeepSeek Adapter，以及 A-05～A-09 的 LLM 基础设施和 Runtime；B/C/D/E 的完整业务服务尚待集成。

当前**技术规范为 `TECHNICAL_SPEC.md` v2.2，业务 DTO 的 Schema 仍为 2.1**。v2.2 新增 IC-001、IC-002、IC-003、IC-012 四项数据容器、构建诊断、manifest 与只读状态的跨模块执行合同；G-02 的产物公共类型已实现；IC-001/IC-002 数据及构建结果类型、B 的严格加载、C 的实际索引和 A/E 的统一服务仍需按合同开发，并同步更新 fixtures 与合同测试。

**G-01：** A 的 `GuardedLLMAdapter` 对 JSON 模式空白 Provider 内容统一分类为 `EMPTY_JSON`，与超时/网络/限流共享最多三次 Provider 尝试；`{}`、非空无效 JSON、截断和业务 Schema 失败直接失败。B/D 不叠加重试，失败不写入成功缓存。

**G-02：** 公共 `contracts.artifacts` 已提供 `MemoryManifest` / `MemoryArtifact` / `memory_set_fingerprint`，正式索引签名固定为 `build_index(artifact: MemoryArtifact, settings: RetrievalSettings)`。B 严格 loader 才能确认磁盘哈希、来源与发布指针；公共对象的结构校验不代表 B/C 业务已实现。C 正式索引拒绝 partial、保留并核对 `source_manifest_fingerprint` 与实际 Embedding/检索身份；裸 Memory 列表只用于内部编码。
