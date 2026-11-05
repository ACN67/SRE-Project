# 自动事实抽取与状态解析

`main-2` 增加了不调用远端模型的 Memory 构建路径。它保留既有窗口规划、
checkpoint、公开 Schema、审核发布、来源回读和索引边界，只替换三个需要
语义模型的位置：

1. 窗口内事实抽取；
2. 发布前事实审核；
3. Dynamic 状态关系与逐 Memory 决策。

最终自然语言答案仍由配置的 QA 模型生成。上述三个阶段不读取 Gold，也不
产生 HTTP 请求。

## 事实抽取

`src/dynamic_memory/memory/auto_extraction.py` 以轮次和句为单位生成候选
事实。`content` 保存原始句子，并在前面加上同一来源轮次的说话者；`entity`、
`attribute`、`assertion_type`、`polarity`、`recorded_at`、`event_time` 和
`source_refs` 均由确定性规则生成。计划、假设、不确定表述、否定、明确日期、
相对日期和图片 caption 分别标记。若规则不能可靠确定实体或字段，就保留
原始句子并将对应字段写为 `null`。

抽取器实现了现有 `LLMRuntime` 所需的适配器接口，因此窗口边界、schema
校验和 checkpoint 身份保持不变。自动运行使用 mock backend，运行时预算和
产物中记录的 `network_calls` 必须为 `0`。严格发布产物记录
`backend=deterministic`、`resolved_model_id=null`，以及
`thinking/reasoning_effort=not_applicable`，不会冒充 DeepSeek 构建。

## 自动审核

`src/dynamic_memory/memory/auto_review.py` 对每条草稿重新读取其精确引用的
轮次，并检查：

- 是否只引用一个来源；
- 事实词项是否能在同一原始句中回查；
- 说话者、日期、时间和图片 caption 是否一致；
- 否定、未来计划和不确定语气是否一致。

审核身份固定为 `auto-extractive-v1`，权限版本绑定策略源码哈希。验证发布
入口会重新计算审核结果，因此修改批准文件不能把 `unclear` 事实改成批准。
无法由局部原文可靠证明的事实保持 `unclear`，不会发布。

## Dynamic 状态解析

`src/dynamic_memory/reasoning/auto_resolver.py` 默认由
`DynamicServicePorts` 使用。解析器按问题实体和属性提示先排除无关候选，
再将同主体的 residence、work 和 planning 属性归入稳定状态组。它对事件
时间和记录时间做时区一致排序，识别显式变化、计划转事实、否定冲突和截止
时间，并保留全部候选的 `RETAIN`、`EXCLUDE` 或 `UNRESOLVED` 决策。

需要人工判断的冲突失败关闭为 `UNRESOLVED`。原来的模型状态解析器仍可通过
`DynamicServicePorts(..., resolver_mode="llm")` 显式启用，不在默认路径。

## 检索

`c-local-index-v7-state-hybrid` 在本地 BGE 向量之外加入确定性 BM25 混合
重排。Memory 文本不改写；索引投影包含来源说话者、已审核实体、属性、原始
时间表达和原文。候选集优先覆盖不同证据轮次，并可在已审核 Memory 中扩展
有限数量的相邻轮次。状态实体、属性与时间判断保留在 D 的解析器中。
Vector RAG 与 Dynamic 仍使用同一份有序 CandidateSet，所以这一优化不需要
额外模型调用。

纯向量消融继续保留未经 BM25 重排的向量名次，不会与主检索结果混用。

## 单样本构建

以下命令完成抽取、自动审核、严格发布和本地 BGE 索引，不产生远端模型调用：

```powershell
$env:PYTHONPATH = 'src;.'
python scripts/auto_build_sample.py `
  --sample conv-26 `
  --state-dir results/stage6_auto_conv26_extract `
  --review-dir results/stage6_auto_conv26_review `
  --artifact-root C:\path\to\auto-artifacts `
  --index-root C:\path\to\auto-indexes `
  --model-root C:\path\to\bge-small-en-v1.5 `
  --model-revision 5c38ec7c405ec4b44b94cc5a9bb96e735b38267a `
  --summary-dir results/stage6_auto_conv26_publish
```

输出目录必须尚不存在或为空，发布不会覆盖旧结果。结构核验可运行
`scripts/audit_auto_structural.py`；它先保存 Vector/Dynamic 预测，再读取
课程 Gold 计算证据召回。状态核验使用
`scripts/audit_auto_state.py`，先验证预测文件，再把已审核的开发集状态标注
映射到新 Memory。

状态标注最初绑定旧 Memory 候选集，重新映射只能覆盖其能够由原文、实体、
属性与本地向量可靠定位的子集。报告时分别给出新端到端候选覆盖率和
candidate-conditioned 决策准确率；不得把旧候选集上的整题状态准确率直接
当作新自动链路的成绩。

## 边界

规则抽取和局部审核不是通用的自然语言语义证明。它们降低延迟和费用，但
可能漏掉需要跨句指代或领域知识的事实。报告必须同时给出自动审核覆盖率、
结构召回和状态指标，并保留 `INTEGRATION_PENDING` 直到最终 QA 已按新产物
重跑。自动路径不需要供应商凭证，权重和其余运行产物继续放在仓库之外。
