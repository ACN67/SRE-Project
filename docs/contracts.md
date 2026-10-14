# 公共合同 v2.1

唯一可执行 DTO 定义位于 `src/dynamic_memory/contracts/models.py`，完整语义见 `TECHNICAL_SPEC.md` 第五章。主要对象为 SourceRef、SourceTurn、TimeSpan、MemoryRecord、QueryRequest、QueryAnalysis、CandidateItem、CandidateSet、RelationEdge、MemoryDecision、ResolutionResult、AnswerResult、QueryResponse。独立跨模块对象要求 `schema_version="2.1"`。

`QueryAnalysis.query_intent` 和 `time_mode` 是独立字段。SourceTurn 原文与 image_caption 分开，统一用 `render_visible_turn` 显示。Memory ID 使用 `contracts.memory_id` 中的规范化 canonical JSON SHA-256 算法。CandidateSet 从 rank 1 连续排序、去重并受预算约束。ResolutionResult 的有效 ID 恰好是 RETAIN ID。Full Text 无 retrieved/resolution；Vector RAG 有 retrieved、无 resolution；Dynamic 正常同时有两者。

需要候选上下文的校验由 `validate_resolution_against_candidates` 执行；需要完整 Memory 和原始轮次索引的引用校验由 `validate_response_context` 执行。结构与 ID 校验不能证明语义事实受原文支持，后续成员须人工抽查。Gold 不在业务 DTO 中。

当前接口实现范围仅为 DTO、确定性工具与 synthetic Mock。真实服务接口仍由 A/B/C/D/E 后续交付。
