# C-06：Vector RAG 回答

`VectorAnswerer` 将按 rank 加载的 Memory 交给 A 的 `LLMRuntime`，复用 `QA_PROMPT`、模型配置、调用记录、缓存及执行预算。它不调用 D，不构造状态关系，不改变检索候选。

```python
from dynamic_memory.retrieval import VectorAnswerer, VectorAnswerProvenance

# index 为当前完整已验证索引；runtime 由 A 初始化。
provenance = VectorAnswerProvenance(
    data_fingerprint=verified_data_fingerprint,
    query_analysis_version=analysis_version,
    git_sha=source_git_sha,
    run_kind="development",
)
answerer = VectorAnswerer(runtime, provenance, index, on_diagnostic=save_diagnostic)
memories = retriever.load_candidates(snapshot.candidates, index)
answer = answerer.answer_vector_rag(request, analysis, memories)
# answerer 也可作为三参数 callable 注入 A。
```

不从环境隐式读取 API Key、不下载模型、不创建真实 runtime。真实调用必须由上层显式配置。默认 Mock 的占位文字禁止作为业务答案；测试需显式 synthetic_test_mode=True 且注入合成 Adapter。

## 输入、上下文和预算

- 重新验证请求、分析与 Memory，检查 query_id/sample_id、重复 ID，并将每条 Memory 与绑定索引的记录完整比较；必须是 load_candidates 已按 rank 排列的输入。三参数端口没有 CandidateSet，无法单独证明这些 ID 是该问题的检索结果，调用方须传同一检索快照。
- Memory 以完整 JSON 输入，包含正文、主体/属性、事实性质、时间和真实来源。问题与完整 QueryAnalysis 一并输入，保留时间不确定原因。
- 使用 runtime Guard 的同一 TokenEstimator，计算全部 system/user 消息预算，而非仅正文长度。超限时从尾部整条删除，保留高 rank 前缀，不截断半条事实。
- 通过 on_diagnostic 保存 query_id、sample_id、query_fingerprint、retriever_fingerprint，以及 included_memory_ids、dropped_memory_ids、输入估计值、预算是否已验证及上限。查询指纹绑定完整请求、分析和索引，计算规则与 RetrievalSnapshot 一致；同一 ID 改题或改时间条件也可区分，不保存问题或记忆正文。身份取自每次调用的校验快照，无共享“当前问题”状态。正常回答、空候选、时间冲突和未知/超限预算的现有诊断记录均带身份。它是本次回答的 Token 排除记录，与检索扩展的条数预算排除分开。相同输入重复执行有相同指纹；需要区分每次执行时由记录端额外保存运行 ID/时间戳。
- strict_budget=True 时没有估计值即停止；非严格模式允许估计未知，但明确记录未验证。所有记忆都无法放入时报 INPUT_BUDGET_EXCEEDED，不冒充 no_evidence。
- 空列表直接 no_evidence；明确时间冲突直接 time_uncertain；两者均不调用模型。

## 文本输出协议与引用

共享 QA 任务仍为 text 模式，C 在问题附加的格式要求中请求以下 JSON 文本，不更改公共 DTO 或共享 Prompt 定义：

```json
{
  "status": "answered",
  "answer": "An answer supported by supplied memories.",
  "evidence_refs": [{"session_id": "synthetic_session", "dia_id": "synthetic_turn"}],
  "uncertainty": null
}
```

status 也可为 no_evidence/time_uncertain/unresolved_conflict，此时 answer 必须为 null，uncertainty 必须非空。未完成输出、普通无引用文本、重复 JSON 键、额外字段、空答案、重复或越界引用均拒绝；answered 必须至少一条引用。引用只能来自预算保留的 Memory。

used_memory_ids 和 context_source_refs 由代码按实际上下文生成，模型不能自行声明输入了哪些记忆；evidence_refs 取自校验后的模型输出。校验作为 runtime validator 执行，失败结果不进入成功缓存。引用存在与进入上下文不等于事实语义已获支持，仍须真实开发集人工核查。

**与 A/D 的待对齐点：** 当前 A 分支 Full Text 是自由文本，尚未采用这个文本引用封装。C 的附加格式要求会影响输入 Token，真实三路实验前须统一回答输出约定并记录 Prompt/模板身份，不能直接把当前不同格式视作已冻结的公平实验条件。

## 错误、缓存及边界

失败抛出脱敏的 VectorAnswerError.code，由 A 的调度器转换成合法 error 响应。主要错误包括 QUERY_INVALID、ANALYSIS_INVALID、SAMPLE_MISMATCH、QUERY_ANALYSIS_MISMATCH、CANDIDATES_INVALID、INPUT_BUDGET_UNKNOWN/EXCEEDED、INVALID_ESTIMATE、IDENTITY_INVALID、SYNTHETIC_ANSWER_DISALLOWED、DIAGNOSTIC_FAILED、MODEL_FAILED。模型执行和 validator 失败统一映射 MODEL_FAILED，不暴露问题/密钥/路径；结构校验不通过时不返回伪造答案。

调用身份使用 method=vector_rag，带数据、索引、查询分析版本、Git SHA 和 C 文本封装 schema 身份；实际请求、时间条件、候选内容也进入 A 的缓存键。provenance 由上层提供已验证值，不从当前机器日期推断。runtime 的调用记录保留 Token 用量与缓存状态。

本轮验证仅使用合成 Adapter，没有真实付费调用；A 真实运行器绑定、三路输出格式统一、真实 B 数据引用回查和回答质量评估仍待完成。
