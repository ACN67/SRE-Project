# 公共合同 v2.1

主要业务 DTO 位于 `src/dynamic_memory/contracts/models.py`，IC-001 数据容器位于 `contracts/data.py`，Memory 产物 DTO 位于 `contracts/artifacts.py`；公共类型统一由 `contracts/` 导出，完整语义见 `TECHNICAL_SPEC.md` 第五章。主要对象为 SourceRef、SourceTurn、TimeSpan、MemoryRecord、QueryRequest、QueryAnalysis、CandidateItem、CandidateSet、RelationEdge、MemoryDecision、ResolutionResult、AnswerResult、QueryResponse。独立跨模块对象要求 `schema_version="2.1"`。

`QueryAnalysis.query_intent` 和 `time_mode` 是独立字段。SourceTurn 原文与 image_caption 分开，统一用 `render_visible_turn` 显示。Memory ID 使用 `contracts.memory_id` 中的规范化 canonical JSON SHA-256 算法。CandidateSet 从 rank 1 连续排序、去重并受预算约束。ResolutionResult 的有效 ID 恰好是 RETAIN ID。Full Text 无 retrieved/resolution；Vector RAG 有 retrieved、无 resolution；Dynamic 正常同时有两者。

需要候选上下文的校验由 `validate_resolution_against_candidates` 执行；需要完整 Memory 和原始轮次索引的引用校验由 `validate_response_context` 执行。结构与 ID 校验不能证明语义事实受原文支持，后续成员须人工抽查。Gold 不在业务 DTO 中。

当前已实现公共 DTO/验证工具、Mock 与可显式启用的 DeepSeek Adapter，以及 A-05～A-09 的 LLM 基础设施和 Runtime；B/C/D/E 的完整业务服务尚待集成。

当前**技术规范为 `TECHNICAL_SPEC.md` v2.2，业务 DTO 的 Schema 仍为 2.1**。v2.2 新增 IC-001、IC-002、IC-003、IC-012 四项数据容器、构建诊断、manifest 与只读状态的跨模块执行合同；G-02 的产物公共类型与 IC-001 公共数据容器已实现；IC-002 构建结果类型、B 的严格加载、C 的实际索引和 A/E 的统一服务仍需按合同开发，并同步更新 fixtures 与合同测试。

**IC-001：** `contracts.data` 唯一定义并由 `dynamic_memory.contracts` 导出 `ConversationSession(session_id, turns: list[SourceTurn])`、`ConversationSample(schema_version="2.1", sample_id, source_order_1_based, sessions)`、`DatasetBundle(schema_version="2.1", snapshot_sha256, samples, split_by_sample_id)`。`source_order_1_based` 从 1 开始，仅记录原文件位置，不能替代 `sample_id`。会话、轮次和样本列表保留传入顺序；同一样本的 session ID、同一 session 的 dia ID、Bundle 的 sample ID 各自唯一，轮次的 sample/session ID 必须与父对象匹配。不同 session 可复用 dia ID，完整来源键仍为 `(sample_id, session_id, dia_id)`。划分值限定为 `main_dev/extra_dev/test`，映射键必须恰好等于 Bundle 的 sample ID 集合；快照字段为 64 位小写 SHA-256 十六进制摘要。规范未规定最小会话/轮次/样本数量，因此公共容器允许空列表与对应的空划分映射；正式数据是否满足冻结配置，由 B 的 loader 另行严格核验。

三个容器只接收明示字段，`extra=forbid`；既有 `SourceTurn` 的原文、空文本和可空时间保持原样，caption 在容器内仅接受来源标识 `blip_caption`，展示复用 `render_visible_turn`。`qa.answer`、`qa.evidence`、`observation`、`session_summary`、`event_summary` 和任意未审查 metadata 不得附在业务容器。三个容器启用 Pydantic `hide_input_in_errors=True`，避免 `str(ValidationError)` 回显不合法的原始输入；但 `errors(include_input=True)` 仍可能暴露敏感值，上层不可直接记录该结构或原始异常输入。结构验证不读取实际文件，也不验证快照字节、冻结划分、来源轮次存在或文本事实；B-02 的解析/Gold 剥离与 B-03 的 SourceRepository 负责后续边界。

**G-01：** A 的 `GuardedLLMAdapter` 对 JSON 模式空白 Provider 内容统一分类为 `EMPTY_JSON`，与超时/网络/限流共享最多三次 Provider 尝试；`{}`、非空无效 JSON、截断和业务 Schema 失败直接失败。B/D 不叠加重试，失败不写入成功缓存。

**G-02：** 公共 `contracts.artifacts` 已提供 `MemoryManifest` / `MemoryArtifact` / `memory_set_fingerprint`，正式索引签名固定为 `build_index(artifact: MemoryArtifact, settings: RetrievalSettings)`。B 严格 loader 才能确认磁盘哈希、来源与发布指针；公共对象的结构校验不代表 B/C 业务已实现。C 正式索引拒绝 partial、保留并核对 `source_manifest_fingerprint` 与实际 Embedding/检索身份；裸 Memory 列表只用于内部编码。


## IC-012：统一只读服务状态（A-18）

公共 BuildStatus 在 contracts/service_status.py 单一来源定义，由 dynamic_memory.contracts 导出；为新增的 Schema 2.1 兼容类型，不更改既有 DTO 字段或版本。字段为 schema_version=2.1、dataset_state（unloaded/ready/load_error）、memory_state（not_built/complete/partial/failed/stale/corrupt）、partial_build、build_id、error_code。严格、冻结、禁止额外字段并隐藏输入。dataset 非 ready 时只能是 not_built、无 build_id，错误码分别为 DATA_NOT_LOADED / DATA_LOAD_FAILED。ready+not_built 的错误码为 BUILD_NOT_FOUND；ready+complete 需要 build_id、partial_build=False、error_code=None，即使 Memory 数量为 0 也合法；partial/failed 要有 build_id、partial_build=True、各自错误码 ARTIFACT_INCOMPLETE / MEMORY_BUILD_FAILED；stale/corrupt 需 build_id、partial_build=False、对应 ARTIFACT_STALE / ARTIFACT_CORRUPT。矛盾组合拒绝。

DynamicMemoryService 以构造时注入的 SourceRepository、只读 build_status 端口、load_verified_artifact 端口、index_readiness 端口及 A-12 QueryOrchestrator 组成。get_build_status 是只读诊断；complete 还必须通过产物门槛。接口缺失会报告稳定不可用错误，不能由字符串 complete 或任意 MemoryArtifact 自动证明生产构建。VerifiedMemoryLoad 是信任边界：formal 的 b_verified_loader 声明必须由 B 严格落盘 loader 在核验发布指针、字节哈希、原始来源和快照后提供；本轮仅使用 synthetic_fixture。A 在返回列表前独立复验公共 MemoryArtifact、完整构建、快照/样本/build ID/manifest 指纹、每条 Memory ID 和同样本真实来源，并返回防御性拷贝。磁盘摘要与 B current build 发布指针只能由 B loader 证明，A 内存复验不替代该证明。IndexReadiness 只是注入的只读索引状态/来源清单身份检查，C 正式索引装载仍应由同一组合根接线到 QueryOrchestrator；本轮未证明真实 C 索引。

A-18 独立审查修复：服务必须通过 A-12 的公开 `QueryOrchestrator.with_source_lookup(service_sources.get_sample)` 派生独立调度实例，覆盖外部原调度器可能绑定的其它样本读取端口；只保证实际进入 A-12 get_sample 的原始来源与服务自己的 SourceRepository 相同，不宣称能静态证明任意业务回调或真实索引的语义来源。服务的 `run_query` 先运行一次 `prepare_query`：分析错误和合法时间冲突走 `run_prepared` 的原有早停结果，不能被缺少 Memory / Index 覆盖；正常查询再验证 B 产物和 C 索引后执行同一 PreparedQuery。早停与正常返回均检查公共响应身份，正常路径还检查本服务来源与 Memory 引用。
