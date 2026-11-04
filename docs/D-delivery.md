# D：动态状态解析与证据约束问答

D 提供模型驱动的 `StateResolver.resolve`、`DynamicAnswerer.answer_dynamic`
和 `DynamicServicePorts`，保持 Schema 2.1 及 A 的现有函数签名。实现只消费 C
的有序最终候选，不扫描整库、不读取 Gold、不重建 B/C 产物。复杂语义由共享
`LLMRuntime` 调用状态模型判断，确定性校验负责拒绝结构、引用及可证明的时序错误。

## 运行

```powershell
python -m pip install -e '.[dev,web,eval,api]'
python scripts/run_dynamic.py --smoke
python -m pytest tests/dynamic tests/contract -q
```

`--smoke` 使用明确标识的预设完成文本，只检查真实 D 端口、有效集筛选及
QA 引用链；不具备自然语言语义推理能力，也不是正式成绩。结构示例见
`examples/dynamic_request.synthetic.json`。

使用实际 B/C 的已验证候选快照时，把它们连同共享查询分析和真实版本身份
写入与示例相同结构的 JSON，再显式调用：

```powershell
python scripts/run_dynamic.py --input verified-query.json `
  --api-enabled --model-id deepseek-flash --output new-result.json
```

API Key 通过 `DEEPSEEK_API_KEY` 环境变量提供，不会打印。输入必须是上游已经
校验的同一问题、同一样本、同一索引的候选快照。CLI 不代替 B 的证据核查、
C 的索引验真或 A 的原始轮次校验。

## A/C/E 接线

```python
from dynamic_memory.reasoning import DynamicProvenance, DynamicServicePorts

dynamic = DynamicServicePorts(
    runtime,
    provenance_factory=provenance_for_sample,
    conversation_cutoff=cutoff_for_sample,
)
runner = QueryOrchestrator(
    resolve=dynamic.resolve,
    answer_dynamic=dynamic.answer_dynamic,
)
```

`provenance_for_sample(sample_id)` 必须从已验证的 B/C 身份构造
`DynamicProvenance`。`cutoff_for_sample(sample_id)` 返回完整原始对话的截止
时间；不能用检索到的少量记忆日期冒充全对话截止时间。

D 的两个端口接收同次调用的 request、analysis 和完整有序候选。Vector RAG
与 Dynamic 主对照必须使用同一 CandidateSet。D 不单独锁定整个业务事务；
源、Artifact 和索引的一致性继续由 A/C 检查。

生产组合根已让 Full Text、Vector RAG、D 解析与 D 问答共享同一
`LLMRuntime`，并在 `dynamic_memory.integration.production` 中启用
`dynamic_available=True`。

## 行为边界

- 每个候选恰好一条 `RETAIN`、`EXCLUDE` 或 `UNRESOLVED` 决策。
- `effective_memory_ids` 必须严格等于所有 `RETAIN` ID。
- UPDATE/SUPERSEDE 使用事件时间顺序；关系证据必须同时来自两个端点。
- 相关冲突的两端必须保持 UNRESOLVED；无关冲突不阻断答案。
- 计划、假设、取消、否定、兼职并存、事件与状态有效期按 Prompt 约束判断。
- QA 只收到有效 Memory，不包含被排除内容；引用必须是实际上下文来源。
- 输入超预算、结构错误、异常结束和伪造引用均失败关闭。
- High profile 对状态解析与 QA 均省略 API `max_tokens`，内部保留 64K
  输出安全预算。
- 合成测试不代表真实状态判断准确率；正式质量仍需真实模型与开发集验收。
