# D：动态状态解析与证据约束问答

D 现已提供实际模型驱动的 `StateResolver.resolve`、`DynamicAnswerer.answer_dynamic` 和 `DynamicServicePorts`，保持 Schema 2.1 及 A 的现有函数签名。实现只消费 C 的有序最终候选，不扫描整库、不读取 Gold、不重建 B/C 产物。复杂语义由共享 LLMRuntime 调用状态模型判断，确定性校验负责拒绝结构、引用及可证明的时序错误。

## 运行

Python 3.11+，在项目根目录执行：

```sh
python -m pip install -e '.[dev,web,eval,api]'
python scripts/run_dynamic.py --smoke
python -m pytest tests/dynamic tests/contract -q
```

`--smoke` 使用明确标识的预设完成文本，只检查真实 D 端口、有效集筛选及 QA 引用链；不具备自然语言语义推理能力，也不是正式成绩。结构示例见 `examples/dynamic_request.synthetic.json`，其中记忆 ID 由公共哈希算法生成。

使用实际 B/C 的已验证候选快照时，把它们连同共享查询分析和真实版本身份写入与示例相同结构的 JSON，再显式调用：

```sh
python scripts/run_dynamic.py --input verified-query.json --api-enabled --model-id MODEL_ID --output new-result.json
```

API Key 通过 `DEEPSEEK_API_KEY` 环境变量提供；不会打印。输入必须是上游已经校验的同一问题、同一样本、同一索引的候选快照。CLI 不代替 B 的证据核查、C 的索引验真或 A 的原始轮次校验。示例中的合成身份不能当成生产身份。输出文件以独占创建模式写入，已存在的文件不会被覆盖。CLI 使用保守 UTF-8 字节估算约束输入预算，可能提早拒绝较长请求；这个估算不是服务端精确 token 数。

## A/C/E 接线

```python
from dynamic_memory.reasoning import DynamicProvenance, DynamicServicePorts

# runtime 为 A 创建并共享的 LLMRuntime；不要在每题创建模型。
# provenance_for_sample(sample_id) 必须读取本次已验证 B/C 身份，
# 返回 DynamicProvenance(data_fingerprint, query_analysis_version,
#                      git_sha, run_kind, retriever_fingerprint)。
# 缺失或过期产物必须拒绝，不能返回测试占位身份。
d = DynamicServicePorts(
    runtime,
    provenance_factory=provenance_for_sample,
    conversation_cutoff=cutoff_for_sample,
    on_diagnostic=save_diagnostic,
)
runner = QueryOrchestrator(
    # 其他 A/B/C 端口保持既有接线。
    resolve=d.resolve,
    answer_dynamic=d.answer_dynamic,
    # ...
)
```

`cutoff_for_sample(sample_id)` 返回完整原始对话的截止 datetime 或 None；不能用检索到的少量记忆日期冒充全对话截止日期。不提供 cutoff 时，模型会被明确告知它未知。`latest_known` 从不使用计算机日期。

D 的两个端口必须接收**同次调用**的 request、analysis、完整有序候选；主对照中它与 Vector RAG 使用相同 CandidateSet。上游负责保持同次索引版本，D 的无状态端口不会单独锁定整个业务事务。D 只接收公共 list[MemoryRecord]，无法自行证明 C 的索引真伪；这些检查继续由 A/C 负责。E 在工厂实际注入 D 后才能设置 `dynamic_available=True`；默认离线 ABCE 工厂维持原有非 D 行为。

独立使用可实例化 `StateResolver(runtime, provenance)` 和 `DynamicAnswerer(runtime, provenance)`。显式合成测试必须传入 `synthetic_test_mode=True` 及自定义 synthetic_adapter；普通 MockLLMAdapter 的占位输出始终不能成为业务答案。

## 行为和边界

- 关系与逐条决策分开：UPDATE/SUPERSEDE/CONFLICT 描述记忆间关系；RETAIN/EXCLUDE/UNRESOLVED 是本题决策。每个候选恰好一条决定，有效集合严格等于全部 RETAIN。
- 状态模型按查询意图处理当前、历史、历次经历、事件和一般事实；提示词明确规定计划/取消、否定、兼职并存、事件时间与记录时间、未知时间、属性范围及无关冲突的处理。
- 每条关系必须引用两端真实候选的来源；关系不能越界、自环、重复，CONFLICT 两端按 ID 排序。已知且可比较的事件区间能证明方向颠倒时，拒绝结果。
- 相关未解冲突的双方都必须 UNRESOLVED；无关冲突不进入 unresolved_conflict_ids，因此不会单独阻断答案。UNRESOLVED 必须解释原因。
- 时间解析失败、输入时间冲突、相关未解冲突和无有效证据走明确不确定路径，不调用 QA。其他未定候选采用保守拒绝确认策略：时间相关问题返回 time_uncertain，一般问题返回 no_evidence，并保留原因。这些状态不代表模型/契约异常。
- QA 只收到按原 rank 排列的有效 Memory，不包含被排除的记忆或其关系证据。引用只能来自实际上下文；空引用、伪造来源、越界引用、重复 JSON 键、异常结束和无效格式均拒绝。引用存在不等于语义蕴含，事实正确性仍需真实样本审核。
- 使用共享运行层的预算、超时、有限重试、并发、缓存、记录和模型参数。超预算明确失败，不悄悄截掉候选、决策或有效证据；自动跨组分批尚未实现。诊断区分 resolve/answer，保留候选/上下文差异、查询指纹、缓存命中和调用用量摘要。
- 输入/输出校验失败不写入有效缓存。模型语义判断仍可能出错；结构校验不等于状态准确率通过。正式模型、提示词、Embedding 与质量指标仍需真实开发集冻结和验收。

## 验证证据

`tests/dynamic/test_dynamic.py`：决策完整性、历史证据保留、有效集筛选、冲突/时间/无证据、引用范围、非法模型输出、预算、缓存与异常脱敏。

`tests/dynamic/test_service_integration.py`：真实 B Builder、C Index、A Orchestrator、D 实现及 E WebBindings；两样本并发，断言 Vector RAG 与 Dynamic 的最终候选一致。模型输出仍为显式合成预设，不声称真实语义准确率。

测试环境和最终数量见 `D-validation.md`。未读取 Gold、未发起真实模型调用、未生成正式 LoCoMo 分数。
