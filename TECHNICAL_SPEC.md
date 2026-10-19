# 智能体动态记忆管理系统：统一技术规范与成员协作说明

> **版本：v2.2**
> **课程：Software Requirements Engineering**  
> **项目：Dynamic Memory Management for AI Agents**  
> **适用范围：** 五名成员的技术开发、模块交接、测试和最终验收  
> **技术开发及成员模块交付截止：2026 年 11 月 1 日（第三周周日）晚上**

本文件说明课程要求、采用的技术路线、仓库结构、成员职责、共同接口和验收条件。它规定模块之间必须一致的行为与交付，不代替成员对各自算法和实现方式的研究。文中标注“课程要求”的内容来自课程项目说明；“团队方案”是当前采用的实现路线。跨模块契约在正文中统一规定；实验配置使用明确的初始数值，正式测试前以经过开发集验证并记录的配置为准。

v2.2 在 v2.1 基础上仅补充 IC-001、IC-002、IC-003、IC-012 四项跨模块接口执行约定。**v2.2 是技术文档版本，现有公共 DTO 的 schema_version 继续为 2.1**；既有 SourceRef、SourceTurn、MemoryRecord、QueryRequest 和 QueryResponse 的字段和含义保持不变。此前 v2.0→v2.1 的迁移规则继续有效。新增约定是后续开发与合同测试的确定依据；文档修订不表示新增接口已经在代码中实现。

---

# 一、项目需求与建设目标

## 课程背景与需要解决的问题

长期、多轮对话中的人物状态、计划、经历和偏好会发生变化。只用关键词或向量相似度检索，可能把旧状态与新状态一起返回；若不判断它们的时间与关系，最终回答就可能使用已经失效的信息。

**课程要求**：基于 LoCoMo 长期对话构建有原始证据的结构化 Memory，按问题检索相关历史，判断 `RETAIN`、`UPDATE`、`SUPERSEDE`、`CONFLICT` 等处理关系，依据问题和时间条件选取有效 Memory，生成回答，并在 Web 中展示相关记忆、状态解析过程与来源。正式实验应完成 QA F1、证据记忆检索 Recall@K、动态状态更新判断准确率，并与 Full Text、Vector RAG 比较。

| 必须具备的能力 | 用户或评测者应能验证的结果 |
|---|---|
| 导入原始对话 | 正确区分 `sample_id`、session、speaker、`dia_id` 与会话时间 |
| 结构化记忆 | 导出及重新导入 Memory JSON；每条事实可定位到真实对话轮次 |
| 相关记忆检索 | 按指定样本和查询意图检索相关 Memory，给出稳定顺序、候选数量和来源；支持当前状态与历史经历等不同问题范围 |
| 动态状态判断 | 保留历史；区分状态进展、明确替代和无法消解的冲突 |
| 证据约束问答 | 输出答案或说明不足/不确定，不引用不存在的轮次 |
| Web 交互 | 无需先提问即可浏览 Memory；提问后查看候选、关系、有效项及原文 |
| 对照与评测 | 三路方法可复现；逐题记录预测、证据、评分及失败原因 |
| 工程与文档 | 可运行源码、统一契约、测试、实验产物、成员工作及验收记录 |

## 数据范围与使用边界

课程主实验采用官方 10 段 LoCoMo 长对话中的 **4 段开发对话 + 2 段独立测试对话**。团队已暂定按冻结的原始数据文件顺序使用：

| 分组 | 对话顺序 | 用途 |
|---|---|---|
| `main_dev` | 1–4 | 课程主开发集；方法设计、Prompt、参数选择与测试 |
| `extra_dev` | 5–8 | 补充开发与扩展检查；与主开发结果分开报告 |
| `test` | 9–10 | 独立测试；正式配置冻结后运行，不能根据其 Gold 调参 |

1–8 是允许用于开发的对话池，但正式课程主实验必须仍可独立呈现 **1–4 + 9–10** 的结果。此处的“开发/训练”不表示需要训练或微调基础语言模型。正式实现须按数据快照中的原始顺序获取真实 `sample_id`，把 ID、划分及数据文件校验值保存到 `configs/data_split.yaml`；不允许在运行时仅依赖数组下标。

业务链的原始事实输入仅来自 `conversation`。LoCoMo 的 `qa.question` 可作为问题输入；`qa.answer`、`qa.evidence`、课程或团队的状态 Gold 仅由独立评测程序读取，不得进入 Memory 抽取、检索、关系解析或答案生成。官方辅助 `observation`、`session_summary`、`event_summary` 不作为主实验抽取输入。图像轮次采用**文本可用信息方案**：原始 `SourceTurn.text` 保持不变，若存在 `blip_caption` 则另存为可选 `image_caption`（来源标识 `caption_origin="blip_caption"`）；B、A 及其余消费者通过唯一的 `render_visible_turn` 方法按一致格式附上 `[image_caption: …]` 文本，不从 `img_url` 下载或分析图像。证据仍回指原始 `dia_id`；若关键结论依赖 caption，记录 `caption_used` 或等价的运行追踪，UI 不将机器描述伪装为说话者原话。相关问答题照常进入测试并统计，图像不可见带来的限制在报告中说明。

## 项目边界和交付

**团队方案**：实现一套单机可运行的研究原型，采用 Python 核心服务、结构化文件、样本内轻量检索与 Streamlit Web。**暂定 DeepSeek API** 辅助事实抽取、复杂语义判断和回答生成；Embedding 由成员 C 根据检索表现及资源开销选择，在本地运行。课程不要求本地部署大型语言模型，也不要求训练基础模型。基础版对**冻结的历史快照**离线构建 Memory 和索引，在线按问题判断历史状态与有效证据；不要求每收到一条新消息就实时增量抽取、更新用户画像或索引。新增会话的自动增量维护属于可选扩展。

必须交付可运行 Web、完整源代码、Memory JSON、三路实验结果与分析、软件需求与项目报告、最终展示材料及必要的过程记录。图数据库、多跳图推理、复杂记忆压缩、生产级并发部署以及额外 Agent 框架均不是本版基础架构的前提。

---

# 二、总体技术方案与模块设计

## 系统工作方式

**离线数据构建**在首次导入或显式重建时执行，**在线问题处理**只消费已经生成的 Memory 与索引。用户在 Web 中切换页面或连续提问，不应重新调用 DeepSeek 抽取整个对话。

```text
离线构建
LoCoMo conversation
      → [B] 规范化对话 / 原始轮次索引
      → [B] 结构化 Memory 抽取、规范化、质量检查和导出
      → [C] 按样本构建并持久化检索索引

在线查询
用户问题（含可选显式时间）
      → [C] 查询条件分析 / 检索与历史候选补充
      → 最终 CandidateSet ──┬─→ [C] Vector RAG：候选上下文→统一生成器
                            └─→ [D] 状态关系与有效记忆→统一生成器
      → [A] 统一业务入口组装 QueryResponse
      → [E] Streamlit 展示  /  [A] 离线评测读取业务输出

独立基线
用户问题 + 同一样本的原始对话
      → [A] Full Text 上下文→同一答案生成器
      → 同一 QueryResponse 结构

评测 Gold：只由 [A] 评分层在预测保存之后读取，不进入上述推理流程
```

这是**业务数据流**，不是 Python 模块必须互相 import 的方向。代码依赖采用共享 `contracts` / `llm` 基础模块，`pipeline` 调用各业务模块，Web 与评测器调用 `pipeline`；`llm` 不反向依赖业务流程。A 负责组合服务运行环境（模型适配器、数据仓库、索引、统一配置），避免模块互相调用造成循环依赖或隐式共享状态。

## Memory 构建：事实、来源与时间

B 从原始会话的可见轮次提取**原子事实**，以便后续按主体、属性、时间和事实性质比较。每条 Memory 必须有自然语言内容、规范化实体及属性、真实 SourceRef 与必要的时间信息。相关字段不确定时保留 `null`/`unknown`，不能由模型补造事实或日期。

需要明确区分两个独立维度：

| 维度 | 允许的最小类型 | 含义与例子 |
|---|---|---|
| `fact_kind` | `event`、`state`、`unknown` | “3月16日开业”是发生过的事件；“目前经营店铺”是可能持续的状态 |
| `assertion_type` | `fact`、`plan`、`hypothetical`、`uncertain`、`unknown` | “已经入职”与“计划入职”不能视为同样的已实现状态 |
| `polarity` | `positive`、`negative`、`unknown` | “已在A工作”与“没有在A工作”必须区分 |

这些字段表示**原始表述所断言的性质**，不是让 B 替 D 计算状态转移。撤销计划、离职、关店等应保留为具有明确来源的新事实；跨会话的新旧关联由 D 判断。人名、代词、同义属性只在有证据时归并，保留原句用于核查。允许 B 根据开发数据选择窗口切分、Prompt 和抽取方法，但不能跳过字段校验和证据回查。

事实去重规则是：先规范化字段，在**同一来源会话、同一语义事实且时间、断言性质一致**的范围内合并窗口重复项、汇总真实 `source_refs`；再生成最终 `memory_id`。跨会话相同陈述不自动合并，不同时间阶段或互相矛盾的事实不可覆盖。抽取失败的会话/窗口应单独记录，不能把不完整结果标作全部完成；保存 `extraction_window_id`、窗口覆盖轮次、校验失败原因、合并前后条数及 `partial_build` 标志。

**Memory 质量检查分为两层**：自动校验结构、ID 和引用存在；在开发集跨多个 session 抽查约 30–50 条（含状态变化、计划、否定、多人指代与 caption）核查原文是否支持该事实、关键事实是否遗漏、主体与事件时间是否正确。记录 `supported/unsupported/unclear` 以及遗漏或时序错误；同一 LLM 的自评只作辅助，不能替代抽样人工复核。发现问题后按抽取版本重建 Memory、ID 与索引。

## 检索：相关性与历史候选

C 在单一 `sample_id` 内建立可重建的向量检索索引，以 Embedding 相似度排序为起点；可以按主体、属性或历史线索补充旧状态候选，但由 C **统一完成**，D 不直接扫描整库。Embedding 型号、大小由 C 根据开发集 Recall@K、内存、加载速度、推理速度等表现选择，正式实验前冻结模型、版本和文本编码方式。

C 还承担**服务于检索的共享查询条件分析**：从问题及显式参数中识别主体、属性和时间线索，并输出 `QueryAnalysis`。这不是对 Memory 有效性的最终判断；D 根据同一 `QueryAnalysis` 和候选证据决定实际状态。

最终输出唯一 `CandidateSet`，包含首轮检索数量 `base_top_k`、总候选上限 `max_candidates`、检索器版本指纹、稳定排名和每条候选的来源。**初始检索预算采用 `base_top_k=8`、`max_candidates=12`**；扩展的去重、排序与截断由 C 实现并记录，所有方法遵守第六部分的统一实验参数。若开发集证实需要调整，C 记录比较依据，并将正式使用值同步至统一配置，不由不同方法各设一套。**Vector RAG 与 Dynamic 在主对照中必须接收同一个最终 CandidateSet（ID、顺序、候选预算一致）**，才能主要考察状态解析带来的差异。纯向量 Top-K 作为**补充消融**独立保存（默认 K=8，仅使用 Embedding 排序，不执行历史扩展），不改变课程三路主实验；原始 Top-K 与最终候选分别记录以便解释历史扩展收益。C 对同分候选规定确定性的打破平局规则（至少以 `memory_id` 为末级键），`load_candidates` 必须按 `rank` 还原顺序，不得依赖集合或字典的遍历顺序。保留 `initial_topk_ids`、`expanded_ids`、`final_candidate_ids` 或等价日志，并记录被 Token 预算排除的候选。

## 动态状态：关系、决策和历史有效性

D 处理两层不同的结果：

- **Memory 之间的关系 `RelationEdge`**：`UPDATE` 表示同一事项有证据的阶段进展或补充；`SUPERSEDE` 表示有明确证据的新状态替代旧状态在目标属性上的当前有效性；`CONFLICT` 表示同一主体、属性与相关时间内无法消解的互斥主张。`UPDATE/SUPERSEDE` 方向始终为旧 Memory → 新 Memory；`CONFLICT` 在语义上对称，序列化时将两个 ID 按字典序固定为 `from/to`。边有 `scope_attribute` 描述作用属性，同一端点与同一属性下相同关系不重复；不同作用属性允许不同边。
- **本次查询的决定 `MemoryDecision`**：对 CandidateSet 中每条 Memory 恰好输出一个 `RETAIN`、`EXCLUDE` 或 `UNRESOLVED`。`RETAIN` 表示**在本问题的查询范围内**选作有效回答证据（不必是当前最新状态）；`EXCLUDE` 仅表示本次不使用，不删除历史；`UNRESOLVED` 表示相关性或有效性无法确定。**`effective_memory_ids` 必须严格等于全部 `RETAIN` 对应的 ID**。实际送入模型的上下文如因 Token 预算进一步缩减，应另记 `used_memory_ids`，不能偷改有效集合。

时间和有效性须遵循以下最小规则：

1. `recorded_at` 是对话记录时间；`event_time` 是**事件或断言所指的时间**，二者不得混用。日期精度与相对表达保存在 `TimeSpan`，使用左闭右开区间 `[start, end_exclusive)`；未知时间不能填机器日期。
2. **事件发生区间不等于状态有效期**。例如“3月开业”可能支持后续经营状态，但不能仅因开业事件发生在3月就推断“4月已关店”，也不能永久推断一直营业。D 根据明确的开始、持续、取消、结束、替代和查询时间证据构造本次有效性判断；派生有效范围可以留在解析内部或推理日志，无须给每个 Memory 存永久的 `active/inactive`。
3. 时间模式 `latest_known` 表示以原始对话结束时为**已知历史的截止界限**，不是电脑当前时间，也不意味着所有问题都只取最新状态；是否选取最新状态取决于 `query_intent`。历史查询考虑明确的事实生效与失效信息；若 `as_of` 晚于对话已知范围，不能没有依据地声称未来状态必然持续。
4. 相对时间以原始会话时间为锚点；时间含糊、先后不可比或仅有计划时，不得自动当成已实现状态；同一人可以同时拥有兼职和全职等并存属性。
5. 只对**影响当前问题目标**的未解冲突阻断确定回答；不相关的冲突不应导致整题拒答。历史旧状态始终保留，可服务于历史问题。
6. **依据查询意图选择证据**：`current_state` 取最新可证的有效状态；`historical_state` 按指定时点；`state_history` 可同时 RETAIN 多个过去阶段；`event_fact` 保留事件及时间证据；`general_fact` 不强制解释为互斥状态。否定计划不抹除过去曾有计划的事实；同一属性不必互斥（例如兼职与全职）；事后追述以有依据的事件时间而不是简单以记录先后判断；缺少关键旧候选时记录 `candidate_incomplete`，D 不绕开 C 扫全库。

D 的状态解析不应给不存在的候选生成关系边，也不依赖评测 Gold。简明可执行测试应覆盖申请→面试→录用、A工作→离职→B工作、兼职并存、计划后取消、开店后关闭、相同时间冲突、事件与记录时间不一致、未知时间、无证据和多主体隔离。

## Query 时间条件和不确定性

`QueryRequest` 保留用户原问题及可选显式 `as_of`。C 的共享 `QueryAnalysis` 必须**分别表示查询意图 `query_intent` 与时间约束 `time_mode`**；前者决定需要当前状态、历史阶段、事件还是一般事实，后者决定时间界限和解析情况。D 使用同一分析结果选择本次的有效 Memory，Full Text 和 Vector RAG 也必须遵守一致的问题解释。

| `query_intent` | 含义 | 举例 |
|---|---|---|
| `current_state` | 当前或对话末端的最新已知状态 | “现在在哪里工作？” |
| `historical_state` | 指定过去某一时点的状态 | “2023年5月在哪里工作？” |
| `state_history` | 历次状态、曾经的经历或变化轨迹 | “曾经在哪里工作过？” |
| `event_fact` | 事件是否发生、发生时间、先后顺序 | “什么时候离职？” |
| `general_fact` | 无须强行做状态替代的一般事实或开放域问题 | “最喜欢哪部电影？” |
| `unknown` | 意图无法可靠确定 | 保持原问题，必要时说明不确定 |

约定如下：

- 问题明确提及历史日期、且请求未提供 `as_of`：采用能准确解析的自然语言日期；明确的时间范围不能擅自缩成单一天，若系统暂无法可靠解释范围对答案的作用则保留 `TIME_PARSE_UNCERTAIN`，不得假装查过整个区间。
- 显式 `as_of` 与问题中的时间一致：按该日期；二者明确矛盾：标记 `QUERY_TIME_CONFLICT`，`time_issue` 保留两侧原始时间值，不静默覆盖一方。
- 问题和显式参数都没有指定日期：`time_mode=latest_known` 只表示**已知对话截至末端的时间界限**，并不等于 `query_intent=current_state`；例如询问“曾经”时仍须保留历史阶段，询问“何时”时仍须使用历史事件。
- `as_of=YYYY-MM-DD` 对**指定时点的状态**默认按该日结束时判断；事件发生时间问题不据此推断持续状态。无法证明时间有效性时返回 `time_uncertain`，不得伪造确定结果。
- 意图未知时不以 `latest_known` 为理由直接排除旧事实；保留能支持原问题的相关证据，必要时标记不确定。

业务状态区分 `answered`、`no_evidence`、`time_uncertain`、`unresolved_conflict`、`error`。前三种不确定情况各保留理由与可用证据，其中 `time_uncertain` 表示**存在相关事实但时间依据不足或输入时间冲突**，不同于没有任何证据；`error` 用于模型、数据或契约错误。E 与 A 评分程序不得自行把不同状态解释成“确认回答”。

## 证据约束的 QA 和三路方法

统一模型 Adapter 由 A 维护，当前暂定调用 DeepSeek API。B 使用它进行 Memory 抽取；D 可以用于复杂关系语义辅助；A/C/D 的三种答案生成路径共享**同一个基础问答模型和尽可能一致的生成设置**。基础方案明确采用**非思考模式**、`temperature=0`（所选模型支持时）；由于 DeepSeek 思考模式不响应 `temperature`，不得依赖默认模式来实现低温度对照。结构化抽取及关系分析使用模型支持的 JSON 输出模式，并在 Prompt 明确要求 JSON；答案生成使用英文简短回答。各模块可使用适合任务的 Prompt，但其版本、实际上下文及调用开销需记录。DeepSeek 的输出必须由本地程序进行 Schema 与 ID 校验，不能信任模型凭空输出的来源编号。具体 Token、超时和重试上限见第六部分。

| 方法 | 送入回答模型的数据 | 状态解析 | 本次答案的证据来源 |
|---|---|---|---|
| `full_text` | 同一样本的原始历史对话（按冻结的上下文上限处理） | 不进行 | **实际送入模型**的原始对话轮次；允许没有可核实的引用，但不能虚构 |
| `vector_rag` | C 的候选 Memory，经上下文预算截断后的部分 | 不进行 | 实际用于回答上下文的候选 Memory 来源 |
| `dynamic` | C 的同一候选 → D 关系与有效性 → 选定回答上下文 | 进行 | 实际用于回答的有效 Memory 来源；未解冲突说明可引用冲突双方 |

共享 CandidateSet 不代表三路模型输入 Token 一样。应分别记录被检索到的 ID、实际输入生成器的 `used_memory_ids` / `context_source_refs`、截断情况和 Token 用量；引用只能来自实际送入模型的来源。Full Text 如因上下文窗口超限而截断，必须说明规则，不能仍声称“完整全文”。若输入证据不足，回答应明确不确定，而非由模型利用常识杜撰对话人物的私人经历。**开放域问答**允许利用模型已知的一般知识进行有依据的推理，但所有关于对话人物的私人经历、状态、关系与行为主张必须由对话或可见 caption 支持；三路使用同样的事实与常识边界，不添加在线搜索。记录抽取 Memory 的原文支持、Memory 进入上下文、答案引用实际证据、结论由证据支持四个不同检查层次。

## Web 与离线评测的共用核心

E 以 Streamlit 直接调用统一 Python service；不要求另起 FastAPI 服务。服务提供样本及 Memory 列表、单条记忆详情、原始轮次回查，以及 `run_query`。UI 和离线评测器都调用同一真实业务路径；评测器仅在业务预测完成后加载 Gold 和评分。

系统预处理一次、索引按版本缓存、在线按问题调用；Streamlit 重新渲染页面或切换样本不得触发重新抽取或错误复用其他样本缓存。Memory 本身保留历史记录；默认关系输出属于本次查询，不必搭建持久关系图数据库。

---

# 三、仓库结构与文件管理

仓库目录对应上述模块边界，保持源代码、配置、原始数据、评测 Gold 和生成结果分离。目录树是目标结构，只有实际完成的脚本才能写入 README 的可运行命令。

```text
DynamicMemory/
├── README.md
├── TECHNICAL_SPEC.md                 # 本文
├── pyproject.toml
├── .env.example                       # 变量名；不包含真实密钥
├── .gitignore
├── configs/
│   ├── data_split.yaml               # 冻结数据快照及实际 sample_id
│   ├── models.yaml                   # A：DeepSeek、生成设置、Mock、API 限制
│   ├── extraction.yaml               # B：抽取范围/版本与窗口策略
│   ├── retrieval.yaml                # C：Embedding、检索与候选预算
│   ├── reasoning.yaml                # D：关系、时间与不确定性配置
│   └── evaluation.yaml               # A：题集、K、评分和正式运行设置
├── docs/
│   ├── srs.md                         # 软件需求规格说明
│   ├── architecture.md                # 架构与流程说明
│   ├── contracts.md                   # 公共接口的已确认定义
│   ├── decisions/                     # 影响跨模块的方案记录
│   └── reports/
│       ├── work/                      # 个人工作报告
│       └── acceptance/                # 复核/验收报告
├── process/                           # 必要的团队过程和版本协作材料
├── data/
│   ├── README.md                      # 数据来源、许可与获取方式
│   ├── raw/                           # LoCoMo 原始文件（依许可管理）
│   ├── processed/                     # 会话索引、Memory JSON/JSONL、manifest
│   └── annotations/                   # 标注来源登记；仅评测路径访问 Gold
├── src/dynamic_memory/
│   ├── contracts/                     # 共享 DTO / Schema / 枚举
│   ├── data/                          # B：解析、SourceTurn与数据白名单
│   ├── memory/                        # B：抽取、规范化、去重与导出
│   ├── retrieval/                     # C：查询分析、索引、候选、Vector RAG
│   ├── reasoning/                     # D：时间/状态解析、关系和决策
│   ├── qa/                            # D：Dynamic QA
│   ├── llm/                           # A：共享 DeepSeek/Mock Adapter
│   ├── pipeline/                      # A：Runtime初始化、统一 run_query
│   └── web/                           # E：Streamlit与证据浏览
├── evaluation/
│   ├── gold/                          # A：仅评分使用的 Gold Loader、映射及标注来源
│   ├── baselines/                     # A：Full Text；Vector RAG 调用 C 接口；纯向量消融
│   ├── metrics/                       # A：QA 评分适配（answer_adapter.py）、检索与状态指标
│   ├── runners/                       # A：批量实验入口
│   └── analysis/                      # A：分项结果、错误归因
├── scripts/                           # 已实现的离线构建/评测/启动入口
├── tests/
│   ├── fixtures/                      # 统一 Mock 原始轮次、Query 意图、Gold 及评分样例
│   ├── unit/                          # 各模块单测
│   ├── contract/                      # 跨模块 Schema、引用及错误合同
│   ├── integration/                   # 模块对接与共享服务
│   └── e2e/                           # 数据→问答→Web、评测路径
├── results/
│   ├── main_dev/
│   ├── extra_dev/
│   └── test/
└── .github/workflows/                 # 必要的自动检查（可采用轻量配置）
```

## 文件与数据的统一管理方式

- **唯一 Schema 来源**：`src/dynamic_memory/contracts/` 定义所有跨模块类型；`docs/contracts.md` 是其可读说明。成员不在私有模块复制另一套同名 DTO。
- **业务数据白名单与 Query ID**：B 的 `ConversationSample` 仅包含原始对话与必要元信息，不把 `qa.answer`、Gold evidence、Gold 关系装入可被下游访问的字典。官方 QA 有稳定原始题号时沿用；否则使用冻结的 `sample_id` 与**该样本原始 QA 数组位置**生成 `query_id`（从 0 开始且不因过滤重新编号），不能只以题目文本作为唯一键；Web 交互提问使用 `interactive_<UUID>`，不进入官方 Gold 对照。测试 Gold 由 `evaluation/gold/` 独立读取；仅在预测保存后按冻结的 `query_id`/`gold_record_key` 合并评分。
- **生成物与版本**：Memory 用 JSONL 进行内部逐条处理、JSON 数组作为课程导出格式；同一批次附 `manifest.json`（数据校验值、抽取配置和版本、样本、条数、失败窗口、`partial_build` 状态）。索引记录 `retriever_fingerprint`，由 Memory 摘要、Embedding 版本、模板、参数推导；不匹配时重建或拒绝加载。公共 Schema 由 2.0 升至 2.1 后，旧产物须显式迁移并重新校验，或重新生成；缓存键包含查询 ID、时间条件、意图解析版本、数据/模型/Prompt/索引指纹及方法。
- **可靠落盘**：构建先写临时文件、验证成功后替换；失败不覆盖上一份有效 Memory。模型、索引和问答缓存必须带配置版本，避免换模型仍读取旧结果。
- **安全与可复现**：API Key 放环境变量或本地未追踪配置；源数据、模型缓存、大型可再生结果按许可与体积管理。运行记录保存 Git SHA、配置、数据/索引/Prompt 版本、显式思考模式、调用时间、输入/输出 Token、耗时、调用次数、费用（可取得时）及错误。按抽取、查询分析、动态解析和 QA 分开统计；失败不能进入成功缓存，必要时设置批次用量/费用软预警。
- **运行入口**：提供独立的数据构建、索引构建、统一实验、Web 启动方式；实际命令由开发者运行验证后写入 README，不把本规范的函数声明视为已存在的脚本。

---

# 四、成员职责、工作目标与个人验收

| 成员 | 核心职责 | 主要交接对象 |
|---|---|---|
| **A 常珂舒（组长）** | 工程框架、公共契约组织、共享模型 Adapter、主流程、Full Text、统一评测与集成 | 全组；E和评分程序 |
| **B 吴宇轩** | LoCoMo 数据规范化、结构化 Memory、来源回查与导出 | C；A/E 只读查询 |
| **C 郭柏村** | 查询条件分析、Embedding检索、候选扩展、Vector RAG | D与A |
| **D 杨林涛** | 时间与状态解析、关系判断、有效性选择、Dynamic QA | A/E |
| **E 刘星宇** | Streamlit Web、Memory浏览、状态/证据可视化 | 消费A的统一服务和B的只读来源服务 |

个人验收以**可运行结果、真实测试和合格输入输出**为依据，不以“已完成代码编写”代替验收。成员应交付各自模块的源码、模块说明、测试及工作记录，与上下游完成至少一次真实开发数据或可核验 Mock 的接口对接。

## A：统一工程、基线与评测

**工作目标**：使业务功能有一个稳定入口，三路方法可以在同一运行配置下比较，并保留可复算的结果。组长除集成协调外，承担可独立验收的技术实现。

**具体工作**：建立私有仓库及 Python 工程；组织统一 DTO、配置与 Mock fixture；实现可替换的 DeepSeek/Mock Adapter 和 Runtime 初始化；编排 `run_query`；实现 Full Text；建立稳定官方 QA ID/Gold Loader、独立业务答案评分适配器、关系/决策/有效集合分层评分、三路批量 Runner、纯向量消融入口、分题型统计与失败记录；提供给 E 的统一服务入口。不得在 A 的 Runner 里复制 B/C/D 的核心算法。

**个人验收**：统一 Mock 链路可运行，三个 `method` 均输出合法 QueryResponse；Full Text 不依赖 Memory 索引，说明实际上下文截断；LoCoMo 1–5 类 QA、Recall 和状态关系/决策/有效集合评分在手算 fixture 上通过；缺 Gold 时明确 N/A，执行失败不被当作正确拒答，候选覆盖与评分分母可复核；保存逐题预测、运行配置与错误记录。能在不是组长个人电脑的环境按说明启动相同服务。

## B：LoCoMo 数据与结构化 Memory

**工作目标**：让原始对话转化为可信、可追溯、可导出并可重新加载的 Memory，确保历史变化未在抽取阶段丢失。

**具体工作**：解析样本、会话、speaker、轮次及时间；提供 `get_turn`、Memory 查询和导出/导入能力所需的数据访问；通过共享 Adapter 抽取原子事实；识别 `fact_kind`、`assertion_type`、`polarity`、实体、属性、事件时间；规范化与同会话重复事实合并；生成最终 ID 和 manifest；抽样核对事实支持、重要事实覆盖、主体及时间正确性；保证原始 text 与 caption 分开保存并统一渲染。

**个人验收**：分组与真实 sample ID 正确；无 Gold 泄漏；字段不确定时不编造；JSON/JSONL 可回读，来源能够定位；重跑可核对ID和变化原因；不同会话状态不会误合并；用真实开发样本检查抽取遗漏、错误断言、时间错误、caption 来源和失败窗口；提供可复核的抽样原文、判定与错误分布。交付 C 可直接加载的 Memory 与合同样例。

## C：Memory 检索与 Vector RAG

**工作目标**：准确、稳定地召回当前查询相关及必要的历史 Memory，形成 D 可直接消费的最终候选，并提供不做状态解析的对照方法。

**具体工作**：根据开发集选择本地 Embedding；实现样本内索引、独立 `query_intent`/时间约束分析、首轮排序与候选扩展；以正文规定的检索预算为初始设置，记录必要的效果比较、排序规则与索引指纹；输出 `QueryAnalysis` 与 `CandidateSet`；按最终候选组织 Vector RAG 回答上下文，通过共享 Adapter 生成回答，不调用 D 的 `resolve`。

**个人验收**：跨样本隔离生效，排序可复现；索引过期可检测；相同候选提供给 Vector RAG 与 Dynamic；在模拟与真实样例中能够检查旧/新状态共同进入候选；`base_top_k/max_candidates`、排序及截断能复核；候选载入按 rank，纯向量消融与最终候选可区分；Embedding 型号与性能/资源选择有开发集测试依据；与 B、D 完成接口对接。

## D：动态状态解析与证据约束 QA

**工作目标**：在只使用 C 最终候选的前提下，判断关系、时间有效性和本次可用事实，生成可追溯回答或明确不确定性。

**具体工作**：消费共享 QueryAnalysis 的查询意图与时间条件；实现当前/历史/事件/一般事实的证据范围选择、事件与状态的时间处理、计划/事实/否定识别后的状态判定、`RelationEdge`、逐条 `MemoryDecision`、`effective_memory_ids`；在复杂语义处调用共享 LLM Adapter；组织 Dynamic 的实际回答上下文并限制引用范围。

**个人验收**：新旧状态、当前与历史经历/事件查询、计划取消、持续状态结束、兼职并存、同属性冲突及无关冲突、未知时间、无证据等案例有预期结果；每个候选恰好一条决定；全部 RETAIN ID 等于有效集合；关系端点和答案来源不越界；未解决问题不伪装为确认答案；与 C/A/E 在真实开发样本中跑通。

## E：Web、Memory 浏览与解释性展示

**工作目标**：使用户能够浏览既有 Memory、提出问题、理解为什么某条记忆被使用或排除，并回查原始轮次。

**具体工作**：基于 Streamlit 展示样本列表、Memory 列表与详情、时间与来源；通过统一 `run_query` 显示不同方法的答案、实际候选、状态边、有效集合、冲突与不确定性；提供原始轮次跳转、加载/失败/空结果状态。关系展示可采用表格和时间线，不要求复杂图形框架。

**个人验收**：尚未提问时能浏览 Memory；任一来源引用可定位原文；Full Text 无 CandidateSet、Vector RAG 无状态结果和 Dynamic 有状态结果都能正确显示；Mock 与真实服务均能联通；异常与证据不足清楚区分；Web 不读取 Gold、不重复实现检索和状态判断，也不会因为页面刷新重复抽取。

---

# 五、跨模块数据接口与共同开发约定

这是**所有成员共同遵守的唯一跨模块契约说明**。实现以 `src/dynamic_memory/contracts/` 中的 DTO/Schema 为准，文档示例用于解释语义而非已运行的产物。公共接口调整须同步修改 Schema、`docs/contracts.md`、fixtures、合同测试以及受影响的调用方。

## 共同的基本约束

- 所有**独立跨模块传输或落盘的主要业务对象**（`MemoryRecord`、`QueryRequest`、`QueryAnalysis`、`CandidateSet`、`ResolutionResult`、`AnswerResult`、`QueryResponse`）使用 `schema_version: "2.1"`；嵌套的 `SourceRef`、`MemoryDecision` 等通过父对象版本约束。公共示例与真实导出不可混用旧版本字段。**从 v2.0 到 v2.1 的新增字段为契约变更**：不兼容的旧 `QueryAnalysis` 必须重建或按明确迁移规则补齐 `query_intent`，不能静默以 `current_state` 作为所有旧问题的默认值；所有受影响的 DTO、fixtures、JSON 示例与合同测试一并更新。
- SourceRef 由 `(sample_id, session_id, dia_id)` 联合定位；单条 Memory 的 `source_refs` 至少一条，且初期属于**同一原始 session**；跨 session 关系由 D 表示。所有引用必须在原始索引中确实存在。
- Memory 不存永久 `active/inactive`；查询后的状态由 `ResolutionResult` 表达，不回写为静态事实。
- Gold 不能通过业务 DTO 或共享 LLM Prompt 传入 B/C/D；测试样本也不能跨 conversation 混入检索。
- 示例中的人物、文本和短 ID 均是虚构 fixture；正式数据的ID必须由统一哈希算法生成，不能复制示例占位 ID。

### 数据容器与来源仓库合同（IC-001，A/B）

公共容器由 A 在 `contracts/` 定义，B 在 `data/` 实现解析和仓库能力，跨模块不得私建同名 DTO：

- `ConversationSession`：`session_id: str`、`turns: list[SourceTurn]`，保持原始轮次顺序。
- `ConversationSample`：`sample_id: str`、`source_order_1_based: int`、`sessions: list[ConversationSession]`；序号只用于核对源文件顺序，**不得替代真实 sample_id**。
- `DatasetBundle`：`snapshot_sha256: str`、`samples: list[ConversationSample]`、`split_by_sample_id: dict[str,str]`，划分值只能为 `main_dev/extra_dev/test`，且与冻结的 `configs/data_split.yaml` 完全一致。
- `SourceRepository`：`list_samples()`、`get_sample(sample_id)`、`get_turn(sample_id, ref: SourceRef)`；来源联合键为 `(sample_id,session_id,dia_id)`，不得重复或越样本读取。A 在统一服务中注入 B 的仓库，E 不直接读取 B 私有目录。

`load_conversations(path, split_path) -> DatasetBundle` 保留原签名，正式读取必须核验快照 SHA256、真实样本 ID、划分和来源唯一性；配置中快照/样本 ID 尚未冻结时拒绝正式读取并报 `SPLIT_UNFROZEN`，开发测试应使用单独的 synthetic 冻结 fixture。业务对象只暴露 `conversation` 及必要来源元信息，不允许包含 `qa.answer`、`qa.evidence`、Gold、`observation`、`session_summary`、`event_summary`。失败采用带稳定 `.code` 的脱敏异常：`DATA_FILE_NOT_FOUND`、`DATA_FORMAT_INVALID`、`DATA_SNAPSHOT_MISMATCH`、`SPLIT_MISMATCH`、`DUPLICATE_SOURCE_REF`、`SAMPLE_NOT_FOUND`、`SOURCE_NOT_FOUND`、`SAMPLE_MISMATCH`；不得伪造轮次或打印原始数据。

## 时间、事实与 ID 约定

| 数据类型 | 核心字段与解释 |
|---|---|
| `SourceRef` | `session_id`, `dia_id`；样本由所属对象的 `sample_id` 指定 |
| `TimeSpan` | `raw: str|null`, `start: str|null`, `end_exclusive: str|null`, `precision: instant/day/month/year/unknown`, `resolution: explicit/relative/unknown` |
| `MemoryRecord` | `schema_version`, `memory_id`, `sample_id`, `content`, `entity`, `attribute`, `value`, `fact_kind`, `assertion_type`, `polarity`, `recorded_at`, `event_time`, `source_refs`, `extractor_version` |
| `ConversationSample` | `sample_id`、规范化会话/轮次及必需元信息，**不含任何 Gold** |
| `SourceTurn` | `sample_id`, `session_id`, `dia_id`, `speaker`, `text`, `recorded_at`, `image_caption: str|null`, `caption_origin: str|null`；原始 `text` 不可篡改，caption 可选且来源须标明 |

B 统一提供 `render_visible_turn(turn: SourceTurn) -> str`：保留原 `text`，若存在 caption 则按固定标签追加 `[image_caption: …]`；A 的 Full Text、B 的抽取和 E 的来源展示使用同一可见文本格式，不各自拼接。

`recorded_at` 为合法 ISO 时间或 `null`；无法确认时不做猜测。`TimeSpan` 为日历精度的事件/断言时间范围，区间左闭右开；例如精确到日的 `2023-10-20` 表示 `[2023-10-20T00:00:00, 2023-10-21T00:00:00)`，不是持续状态自动失效的期限。无时区的 LoCoMo 会话时间按同一样本内部的本地时间轴比较，不强加北京时间；显式不同时区需转换后再比较。

**Memory ID 唯一算法**：

1. 先完成字段规范化、同会话重复抽取合并、来源去重和稳定排序；所有字符串统一 Unicode NFC、去首尾空白、连续空白压缩为单空格，`null` 保持 JSON null。`source_refs` 按 `(session_id, dia_id)` 升序排序。
2. Canonical payload **只含**下面固定字段，按键名排序序列化：`sample_id`, `content`, `entity`, `attribute`, `value`, `fact_kind`, `assertion_type`, `polarity`, `recorded_at`, `event_time`, `source_refs`。`event_time` 内的时间值统一成前述格式；不包含 `extractor_version`、`schema_version` 或 `memory_id`。
3. 使用 `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`，对 UTF-8 字节求 SHA-256，取前 20 个十六进制字符，得到 `mem_<20hex>`。完全相同 canonical payload 重复出现时去重；不同 payload 若产生相同 ID，应拒绝并报告。
4. 同一 Memory 增加来源、修正时间、改变抽取内容或规范化方式**可能改变 ID**。manifest 记录抽取版本及数据指纹，后续关系引用和索引必须重建/失效，不许声称跨版本 ID 永久不变。

### `MemoryRecord` JSON 示例

```json
{
  "schema_version": "2.1",
  "memory_id": "mem_0123456789abcdefabcd",
  "sample_id": "demo_001",
  "content": "Caroline passed the adoption agency interviews.",
  "entity": "Caroline",
  "attribute": "adoption_stage",
  "value": "interviews_passed",
  "fact_kind": "event",
  "assertion_type": "fact",
  "polarity": "positive",
  "recorded_at": "2023-10-22T09:55:00",
  "event_time": {
    "raw": "last Friday",
    "start": "2023-10-20T00:00:00",
    "end_exclusive": "2023-10-21T00:00:00",
    "precision": "day",
    "resolution": "relative"
  },
  "source_refs": [{"session_id": "session_19", "dia_id": "D19:1"}],
  "extractor_version": "demo-fixture"
}
```

此处 `memory_id` 是**格式示意占位**，不是对上面的 JSON 执行哈希得出的值；合同 fixture 应由实际 ID 计算函数生成并校验。若 `entity/attribute/value` 无法可靠解析，可用 `null`；`fact_kind/assertion_type/polarity` 不明时应按 Schema 的 `unknown` 分支处理并保留原文，不可硬填事实。Schema 应允许对应枚举的 `unknown`，但对已有证据的明确事实不得滥用未知值以规避解析。

## 查询、候选与动态解析对象

| 类型 | 最小字段 | 一致性条件 |
|---|---|---|
| `QueryRequest` | `schema_version`, `query_id`, `sample_id`, `question`, `as_of` | `as_of` 可空；问题必须非空，UI 与 Runner 同构 |
| `QueryAnalysis` | `schema_version`, `query_id`, `sample_id`, **`query_intent`**, `entity_hints`, `attribute_hints`, `time_mode`, `resolved_as_of`, `time_issue` | 意图取 `current_state/historical_state/state_history/event_fact/general_fact/unknown`；时间模式独立取 `latest_known/as_of/unknown/conflict`，不得将“未给日期”解释成“只回答当前状态” |
| `CandidateItem` | `memory_id`, `rank`, `score`, `source_refs`, `origin` | `rank` 从 1 连续且唯一；`origin=semantic/history_expansion`；分数是排序信号，非正确概率 |
| `CandidateSet` | `schema_version`, `query_id`, `sample_id`, `base_top_k`, `max_candidates`, `retriever_fingerprint`, `items` | 最终唯一候选；ID 不重复；条数不超预算，来源须真实 |
| `RelationEdge` | `from_memory_id`, `to_memory_id`, `relation`, `scope_attribute`, `evidence_refs`, `reason` | 端点不同并属于 CandidateSet；`UPDATE/SUPERSEDE/CONFLICT`，不允许 `RETAIN` 自环 |
| `MemoryDecision` | `memory_id`, `action`, `reason` | `RETAIN/EXCLUDE/UNRESOLVED`；每个候选正好一条 |
| `ResolutionResult` | `schema_version`, `query_id`, `relations`, `decisions`, `effective_memory_ids`, `unresolved_conflict_ids`, `uncertainty` | 有效集合**等于**所有 RETAIN ID；冲突 ID 是 Memory ID，且均在候选中 |
| `AnswerResult` | `schema_version`, `status`, `answer`, `used_memory_ids`, `context_source_refs`, `evidence_refs`, `uncertainty`, `error` | 所有来源真实，引用不能超出实际送入生成器的上下文；状态见下文 |
| `QueryResponse` | `schema_version`, `query_id`, `sample_id`, `question`, `method`, `query_analysis`, `retrieved`, `resolution`, `answer_result` | 三路方法共享格式，但按方法允许不同字段为 `null` |

`QueryRequest.query_id` 由统一数据层构造：原始 QA 优先使用稳定官方ID，否则 `qa_{sample_id}_{original_qa_index}`；Web 使用 `interactive_<UUID>`。原始 QA 索引在冻结快照中从 0 计且不得按筛选后的列表重新编号。评分程序只用官方 QA 映射键读取 Gold，不以问题文本匹配。

为保持统一，业务返回的 `QueryResponse` 不必复制 `QueryRequest.as_of`；它可通过 `query_analysis` 的最终时间条件表达。`query_analysis` 在输入解析失败时可为 `null`，此时应有明确的 `answer_result.status=error` 或时间不可判定状态。`time_issue` 统一采用可空对象 `{code, message, explicit_as_of, question_time}`，后两字段可空，冲突时必须保留两侧原始时间信息。`QUERY_TIME_CONFLICT` 或 `TIME_PARSE_UNCERTAIN` 是业务语义，不是模型崩溃；本版明确支持日期型 `as_of`，无法可靠解释的自然语言区间不得偷偷归并为单个日期。

**`CandidateSet.items` 按最终 rank 升序保存**。扩展候选去重、分数转换、稳定同分排序及最终截断规则由 C 实现并记录；`load_candidates` 必须按最终 `rank` 返回，不按字典/集合迭代顺序返回；无候选时 `items=[]` 是合法成功检索，不应伪造候选。关系对象 `scope_attribute` 可为规范化属性名；无法判断作用范围则不应构造确定关系。`UPDATE`/`SUPERSEDE` 为旧→新；`CONFLICT` 两端按 ID 排序，防止反向重复。不同属性的关系可共存，重复的 `(from,to,relation,scope_attribute)` 禁止输出。

**决策与答案状态**：

- `effective_memory_ids` 与所有 `action=RETAIN` 的 ID 集合严格相等；同一 ID 不可既 RETAIN 又 UNRESOLVED。
- `unresolved_conflict_ids` 是因本次问题相关冲突无法确认而涉及的 Memory ID 集合，不是边 ID，属于 CandidateSet；无关冲突可以展示但不使答案整体失败。
- `status=answered`：`answer` 为非空文本，`error=null`；对于 Vector RAG / Dynamic 需有真实可核验证据，不能编造来源。
- `status=no_evidence`：没有足以回答的相关证据；`status=time_uncertain`：相关事实存在但无法确定目标时点/显式条件矛盾；`status=unresolved_conflict`：相关证据互斥且无法消解。这些状态允许说明性文字或 `answer=null`，`uncertainty` 应注明原因。
- `status=error`：数据、模型、索引或契约执行异常，`answer=null` 且 `error={code,message}` 非空；不与正常业务不确定性混同。

### 三路方法的可空字段及证据规则

| `method` | `retrieved` | `resolution` | 允许的 `used_memory_ids` 与 `evidence_refs` |
|---|---|---|---|
| `full_text` | `null` | `null` | `used_memory_ids=[]`；证据来自**实际送入模型**的原始轮次，允许空引用但不得伪造 |
| `vector_rag` | 正常为 `CandidateSet` | `null` | 实际送入模型的候选 Memory ID 子集；证据来自这些 Memory 的来源 |
| `dynamic` | 正常为 `CandidateSet` | 正常为 `ResolutionResult` | 确认回答时实际使用有效 Memory ID 子集；未解冲突说明可包含冲突双方，证据须来自实际上下文 |

若检索前发生错误，Vector RAG / Dynamic 的 `retrieved` 可以为 `null`；若解析前发生错误，Dynamic 的 `resolution` 可以为 `null`，但必须同时返回 `status=error` 与明确诊断。另若查询分析已明确判定时间条件冲突而停止后续检索，可以返回 `status=time_uncertain`、`retrieved=null`、`resolution=null`，并在 `query_analysis.time_issue` 记载原因；此情形不得与模型执行错误混淆。正常的空检索则返回空 `CandidateSet`，不是 `null`。三种方法的 `context_source_refs` 均表示最终送入回答模型的原始来源集合；对于 Full Text 是实际保留的轮次，对于 RAG/Dynamic 是所用 Memory 的来源。`evidence_refs` 必须是 `context_source_refs` 的子集；断言已证实但引用未送入模型的轮次为合同错误。

`QueryAnalysis` 的意图与时间约束应供 C 和 D 使用，且 Full Text 也遵守同一个已解析条件，不能对基线单独人工补充历史日期。`QueryResponse` 中记录 `query_analysis` 供 E/评分器一致显示/诊断。对于截断造成的未知，保留实际上下文和原因，而不是把“召回了”当成“模型看到了”。

### 查询与候选 JSON 示例

```json
{
  "schema_version": "2.1",
  "query_id": "demo_q01",
  "sample_id": "demo_001",
  "question": "What is Caroline's current adoption stage?",
  "as_of": null
}
```

```json
{
  "query_id": "demo_q01",
  "sample_id": "demo_001",
  "schema_version": "2.1",
  "query_intent": "current_state",
  "entity_hints": ["Caroline"],
  "attribute_hints": ["adoption_stage"],
  "time_mode": "latest_known",
  "resolved_as_of": null,
  "time_issue": null
}
```

```json
{
  "schema_version": "2.1",
  "query_id": "demo_q01",
  "sample_id": "demo_001",
  "base_top_k": 2,
  "max_candidates": 3,
  "retriever_fingerprint": "demo-index-01",
  "items": [
    {"memory_id": "mem_0123456789abcdefabcd", "rank": 1, "score": 0.87, "source_refs": [{"session_id": "session_19", "dia_id": "D19:1"}], "origin": "semantic"},
    {"memory_id": "mem_fedcba9876543210abcd", "rank": 2, "score": 0.76, "source_refs": [{"session_id": "session_13", "dia_id": "D13:1"}], "origin": "history_expansion"}
  ]
}
```

```json
{
  "query_id": "demo_q01",
  "schema_version": "2.1",
  "relations": [
    {
      "from_memory_id": "mem_fedcba9876543210abcd",
      "to_memory_id": "mem_0123456789abcdefabcd",
      "relation": "UPDATE",
      "scope_attribute": "adoption_stage",
      "evidence_refs": [
        {"session_id": "session_13", "dia_id": "D13:1"},
        {"session_id": "session_19", "dia_id": "D19:1"}
      ],
      "reason": "The later stage follows the earlier adoption process."
    }
  ],
  "decisions": [
    {"memory_id": "mem_0123456789abcdefabcd", "action": "RETAIN", "reason": "Latest supported stage."},
    {"memory_id": "mem_fedcba9876543210abcd", "action": "EXCLUDE", "reason": "Earlier stage remains historical evidence."}
  ],
  "effective_memory_ids": ["mem_0123456789abcdefabcd"],
  "unresolved_conflict_ids": [],
  "uncertainty": null
}
```

示例对象可作为初始 Mock 的结构参考；Memory ID 为占位值，不可作为哈希结果的测试期望。合同 fixture 应由真实数据模型与哈希工具生成完整有效引用，并覆盖所有错误分支。

## 构建诊断及 Memory 产物合同（IC-002、IC-003，A/B/C）

**IC-002：部分失败不能伪装成构建成功。** 保留 `build_memories(sample) -> list[MemoryRecord]` 作为**仅完整成功**的严格兼容接口；新增 `build_memories_with_report(sample) -> MemoryBuildResult`，由 B 承担实际执行。`MemoryBuildResult` 至少包含 `sample_id`、`memories`、`build_status: complete/partial/failed`、`partial_build`、`total_windows`、`successful_windows`、`window_diagnostics`、`extractor_version`。每条窗口诊断至少含 `extraction_window_id`、覆盖的原始 `session_id/dia_id`、`status: success/failed`、可空的脱敏 `error_code` 及有据可查的前后记录数量。`total_windows=successful_windows+failed_windows_count`；`partial_build=(failed_windows_count>0)`；无失败为 complete，有成功也有失败为 partial，全部窗口失败为 failed。合法零窗口或抽取零条合法事实属于 complete，可返回空列表。`memories` **仅含通过 v2.1 Schema、Memory ID 和来源校验的记录**。严格接口在存在失败窗口时抛 `MEMORY_BUILD_INCOMPLETE`（全部失败报 `MEMORY_BUILD_FAILED`），不能返回不完整 list。B 复用 A 已有的 Prompt、JSON 校验、重试/预算与日志模块。

**IC-003：清单格式及读取行为固定。** 每次构建附带 `manifest.json`，字段为 `manifest_format_version=1`、`schema_version="2.1"`、`build_id`、`sample_id`、`dataset_snapshot_sha256`、`source_turns_fingerprint`、`extraction_config_fingerprint`、`extractor_version`、`prompt_fingerprint`、`backend`、`resolved_model_id`、`created_at`、`build_status`、`partial_build`、窗口总数/成功数及失败窗口明细、`memory_count`、`memories_jsonl_sha256`、导出 JSON 时的 `memories_json_sha256`、`memory_set_fingerprint`。计数、窗口覆盖、版本和实际文件字节必须一致；指纹和摘要从真实内容计算，不得使用占位值。每次构建先写新 `build_id` 的临时文件，校验通过后提交清单并原子切换当前产物引用；构建失败不得覆盖上一份完整有效结果。

`import_memories(path) -> list[MemoryRecord]` 保持严格：仅接受 manifest、文件校验和、版本、Memory ID、来源引用全部核验通过且 `build_status=complete`、`partial_build=false` 的产物。新增 `load_memory_artifact(path, *, allow_partial=False) -> MemoryArtifact`（含已验证的 manifest 与 memories）；部分产物默认抛 `ARTIFACT_INCOMPLETE`，全失败报 `MEMORY_BUILD_FAILED`，损坏报 `ARTIFACT_CORRUPT`，身份过期报 `ARTIFACT_STALE`。显式 `allow_partial=True` 只用于开发诊断或只读预览，**禁止投入正式索引及三路 QA**。C 建索引时必须校验源 manifest 与数据/Memory/抽取/Prompt 身份，保存 `source_manifest_fingerprint` 和自身 `retriever_fingerprint`；上游产物或 Embedding/检索参数变化时必须重建索引或报 `INDEX_STALE`，模型回答缓存随数据/索引指纹失效。

## 约定的 Python 服务接口

以下仅规定跨模块形态；具体类、参数注入及内部算法由实现者确定，但 Web 与离线 Runner 必须调用同一初始化后的服务实例接口，不依赖隐式全局变量：

```python
# DTO、Runtime 相关类型由 src/dynamic_memory/contracts/ 统一定义

def load_conversations(path: str, split_path: str) -> DatasetBundle: ...
def get_turn(sample_id: str, ref: SourceRef) -> SourceTurn: ...
def render_visible_turn(turn: SourceTurn) -> str: ...
def build_memories(sample: ConversationSample) -> list[MemoryRecord]: ...
def build_memories_with_report(sample: ConversationSample) -> MemoryBuildResult: ...
def export_memories(memories: list[MemoryRecord], path: str) -> None: ...
def import_memories(path: str) -> list[MemoryRecord]: ...
def load_memory_artifact(path: str, *, allow_partial: bool = False) -> MemoryArtifact: ...

def build_index(memories: list[MemoryRecord], settings: RetrievalSettings) -> RetrievalIndex: ...
def analyze_query(request: QueryRequest) -> QueryAnalysis: ...
def retrieve(request: QueryRequest, analysis: QueryAnalysis, index: RetrievalIndex) -> CandidateSet: ...
def load_candidates(candidates: CandidateSet, index: RetrievalIndex) -> list[MemoryRecord]: ...

def resolve(request: QueryRequest, analysis: QueryAnalysis,
            candidates: list[MemoryRecord]) -> ResolutionResult: ...
def answer_dynamic(request: QueryRequest, analysis: QueryAnalysis,
                   resolution: ResolutionResult, candidates: list[MemoryRecord]) -> AnswerResult: ...
def answer_vector_rag(request: QueryRequest, analysis: QueryAnalysis,
                      candidates: list[MemoryRecord]) -> AnswerResult: ...
def answer_full_text(request: QueryRequest, analysis: QueryAnalysis,
                     sample: ConversationSample) -> AnswerResult: ...

# 对 UI 和评测器暴露的统一、已初始化业务服务
class DynamicMemoryService:
    def get_build_status(self, sample_id: str) -> BuildStatus: ...
    def list_samples(self) -> list[str]: ...
    def list_memories(self, sample_id: str) -> list[MemoryRecord]: ...
    def get_memory(self, sample_id: str, memory_id: str) -> MemoryRecord: ...
    def get_turn(self, sample_id: str, ref: SourceRef) -> SourceTurn: ...
    def run_query(self, request: QueryRequest,
                  method: str = "dynamic") -> QueryResponse: ...
```

- `load_conversations` 仅产生业务白名单 DTO；独立 Gold Loader 由 A 在评测路径实现，不出现在业务服务接口中。
- `analyze_query` 属于 C，与 D 共用独立的意图及时间条件；实际时点可无法确定，`time_issue` 不得被静默忽略。三个方法均接收相同解析结果；服务可以在时间条件冲突时直接形成 `time_uncertain` 响应。
- `list_samples/list_memories/get_memory/get_turn` 是 Web 所需**只读业务接口**，由 A 统一暴露，数据由 B 的规范化仓库提供；`import_memories` 用于 B 的离线回读及测试，不通过 Web 随意覆盖线上索引。
- `build_index/retrieve/load_candidates` 属于 C，只有所列候选可供 D 解析；`answer_vector_rag` 属于 C，`answer_dynamic` 属于 D，`answer_full_text` 属于 A。
- 服务在初始化时注入数据仓库、索引、模型 Adapter 和有效配置，可在测试中同时构建两个相互隔离的实例；E 与评测器不自行加载模块私有状态或重新实现业务逻辑。

## 只读服务状态及异常处理（IC-012，A/B/E）

A 的 `DynamicMemoryService` 新增只读 `get_build_status(sample_id) -> BuildStatus`，至少包含 `dataset_state: unloaded/ready/load_error`、`memory_state: not_built/complete/partial/failed/stale/corrupt`、`partial_build`、可空 `build_id` 及 `error_code`，从 B 已验证的来源仓库和构建产物读取，**不触发构建**。

- `list_samples()` 只有成功读取到空数据集才返回 `[]`；未加载/加载失败分别抛 `DATA_NOT_LOADED` / `DATA_LOAD_FAILED`。`list_memories(sample_id)` 只返回完整有效产物，**完整但确无 Memory** 才返回 `[]`；未构建、部分构建、全部失败、损坏/失效分别报 `BUILD_NOT_FOUND`、`ARTIFACT_INCOMPLETE`、`MEMORY_BUILD_FAILED`、`ARTIFACT_CORRUPT/ARTIFACT_STALE`。`get_memory` 采用同样门槛，完整库中确实不存在 ID 才报 `MEMORY_NOT_FOUND`。
- `get_turn(sample_id,ref)` 仅依赖正常加载的来源仓库，不因 Memory 尚未构建或构建失败而不能回查。未知样本、轮次和越样本访问分别采用 `SAMPLE_NOT_FOUND`、`SOURCE_NOT_FOUND`、`SAMPLE_MISMATCH`。
- `run_query` 的 Full Text 只依赖可用的原始会话；Vector RAG/Dynamic 需要完整 Memory 和匹配版本的索引。数据/构建/索引异常返回合法 `QueryResponse`，其 `answer_result.status="error"`、`answer=null`、`answer_result.error={code,message}`；失败早停时 `retrieved/resolution` 可为 null，**不能以 `no_evidence` 冒充执行错误**。正常运行但证据不足时才使用 `no_evidence`。

E 只调用 A 的统一只读服务，明确展示“未构建 / 部分构建 / 加载失败 / 完整零 Memory / 已就绪”，不得在 Web 重渲染时自动抽取，也不得绕开接口读取 Gold 或未验证的产物。错误码稳定、脱敏。

## 交接、合同测试与接口变更

| 交接 | 输出方 | 使用方 | 应共同验证的内容 |
|---|---|---|---|
| 原始来源索引与 Memory JSON、manifest | B | C、A/E | 来源真实、字段完整、JSON 回读、数据隔离 |
| QueryAnalysis、CandidateSet 与候选加载 | C | D、A | 时间约束一致、同样本、预算、排名、历史召回 |
| ResolutionResult、Dynamic AnswerResult | D | A、E | 关系范围、决定集合、证据引用与不确定性 |
| 共享生成器、Runtime与QueryResponse | A | B/C/D/E、评测器 | 三路输出一致、状态/异常可表示、Mock和真模型接口一致 |
| Gold 与评分统计 | A | 项目报告与验收 | 金标隔离、ID 映射、逐题指标和分母 |
| Memory 浏览和原文只读服务 | A（B 提供仓库能力） | E | 不提问可访问、不越样本、不读取 Gold |

成员交付前先用 `tests/fixtures/` 的 Mock 输入验证本模块，再在开发集上与直接上下游进行真实输出联调。公共类型或接口发生变更时，先确认影响范围，再由提供方、消费方及 A 同步修改类型、示例、测试与说明；禁止为了绕过错误自行复制 Schema 或悄悄增加默认字段。

新增合同测试必须覆盖：真实 sample_id、Gold 白名单、来源唯一性与跨样本拒绝；窗口全成功/部分失败/全部失败/零事实；manifest/文件摘要、partial 默认拒绝、索引指纹失效；只读服务各状态和完整空结果的区分；运行错误不能伪装成 no_evidence。新增 DTO、消费者、fixtures 与 `docs/contracts.md` 须同步实现。本技术文档已确定接口行为，但**不代表新增接口已在当前仓库实现**。

合同测试至少覆盖：合法输入、空数据、无时间、原文来源不存在、跨样本引用、重复 ID、时间冲突、错误时间区间、当前/曾经/何时/一般事实等查询意图、同一问题的不同时间输入、caption 原文保留与统一渲染、候选加载排名、候选之外的关系边、RETAIN 与有效集合不一致、模型无效输出、索引过期，以及不同方法可空字段与证据约束。公共 Schema 改动须同步修改生产者与消费者、JSON fixtures、文档和测试；发生不兼容变更时必须拒绝无迁移的旧产物或整体重建，不能混用版本。通用可诊断错误例如 `SCHEMA_INVALID`、`SOURCE_NOT_FOUND`、`SAMPLE_MISMATCH`、`INDEX_STALE`、`MODEL_TIMEOUT`、`MODEL_OUTPUT_INVALID`；缺少 Gold 是评测侧状态，不应使正常业务查询崩溃。

---

# 六、系统整体要求、测试与最终交付

## 必须通过的功能与质量检查

- **正确性**：在固定样例中能区分进展、替代、冲突和独立事实，历史状态仍可回查；计划、否定、事件/状态与时间不确定性不会被错误转成肯定的当前事实。
- **来源与语义**：引用轮次确实存在，且答案所声称的主要结论受到被引证据支持。仅检验 ID 合法不足以证明回答正确；应在开发集抽样核对内容蕴含和过度推断。
- **自动化测试**：包含单元、Schema/合同、相邻模块集成、端到端、评分函数测试。固定 Mock 不依赖在线 API；另需至少一条真实开发集链路完成对话导入→Memory→检索→状态→QA→Web/Runner。
- **异常处理**：无证据、时间不确定、相关冲突、服务失败有不同状态；错误不静默吞掉，Web不把不确定答案显示成已确认。
- **运行可复现**：保留原始数据和生成物版本、模型及 Prompt/Embedding 配置、API 用量与费用（可取得时）、候选与上下文截断记录、源码版本和逐题结果。云端模型重新调用未必逐字相同；保存结果应可离线重新评分，重推理差异如实记录。
- **资源控制**：离线抽取与索引持久化；模型调用有超时、有限重试、缓存与失败记录。D 先按主体/属性筛选再考虑关系，不把所有候选两两配对都默认作为独立 API 请求。允许中断后复用通过校验的已有样本/问题产物。

## 三路实验与公平比较

**Full Text** 以同一对话原始历史回答；**Vector RAG** 用 B 的结构化 Memory 和 C 的最终候选直接回答；**Dynamic** 用同一最终候选经过 D 的状态解析再回答。三路共同使用统一问答模型 Adapter、相同题集、可比较的生成设置和统一评分程序；各自输入的上下文结构与额外状态解析调用必须记录。Vector RAG 与 Dynamic 的候选 Recall@K 在主对照中应相同：其差别主要反映状态处理与有效上下文选择的作用，不能把相同候选的 Recall 提升归功于 Dynamic。

**补充消融 `pure_vector_topk`**：保留 B 的 Memory 和同一 QA 模型，但由 C 返回不含历史扩展的原始 Embedding Top-8（同分按 Memory ID 稳定排序）直接问答。该结果单列为补充组，**不替代、不重命名**三路课程主实验；若只提交三路主结果则可不运行此消融。记录它与最终扩展候选的 Recall 差异，分别分析“历史扩展的收益”和“同候选条件下 D 的状态解析收益”。

### 统一实验默认参数

以下为**本项目的初始工程配置**，不是 LoCoMo 官方规定的最优值。设置依据是：原始 LoCoMo 论文报告每段对话平均约 16,618 tokens，32K 的 QA 输入上限通常足以容纳平均规模的 Full Text；官方 DeepSeek Chat Completions API 支持非思考模式、`max_tokens`、JSON Output，并说明思考模式下 `temperature` 不生效。实际仍应核验所选模型的可用上下文，开发集调整需记录原因；独立测试集运行前冻结唯一配置。

| 配置项 | 默认数值/处理 | 使用说明 |
|---|---|---|
| `base_top_k` | **8** | C 的首轮语义检索候选数 |
| `max_candidates` | **12** | C 完成历史候选扩展后的总上限；Vector RAG 和 Dynamic 共享 |
| `recall_at_k` | **[5, 10]** | 同时报告 Evidence Recall@5 和 Recall@10；主结果重点呈现 Recall@10，并单列完整候选覆盖率 |
| `extraction_max_input_tokens` | **4096** | B 优先按 session 抽取；过长的 session 继续按不超过此输入预算分窗，窗口重叠 **2 条轮次**，保持 SourceRef |
| `extraction_max_output_tokens` | **4096** | 每个抽取窗口的模型输出上限，JSON 截断必须报错/重试，不得作为有效 Memory |
| `reasoning_max_input_tokens` | **8192** | D 的一次关系解析输入上限；超限时在同主体/属性筛选后压缩或分批，留存候选和实际输入差异 |
| `reasoning_max_output_tokens` | **2048** | D 的结构化关系/决策结果输出上限；不得静默丢边或丢决策 |
| `qa_max_input_tokens` | **32768** | **三路回答共享同一输入上限**，包含 system Prompt、问题及上下文；超限记录实际保留的轮次/Memory和截断原因 |
| `qa_max_output_tokens` | **256** | 三路回答共享，倾向英文精简事实答案，便于统一 F1 |
| `thinking` / `temperature` | **`disabled` / `0`** | 基础方案对抽取、解析、QA 均关闭思考模式；`temperature=0` 仅在模式及型号支持时有效；不另外调节 `top_p` |
| `request_timeout_seconds` | **120** | 单次云端请求超时；不能将超时结果当作无证据 |
| `max_retries` | **2** | 最多两次重试（总计至多三次尝试）；仅对可重试异常/空JSON有限重试，失败保留诊断和已成功缓存 |
| `max_concurrent_api_requests` | **2** | 本地批量调用并发上限，防止测试阶段无意大量并行请求 |

**上下文与公平性约定：** Full Text 在 32K 预算内尽量放入全部原始可见文本；超过限制时按时间顺序保留最近轮次，明确标为 `truncated_full_text` 并保存被排除轮次。Vector RAG / Dynamic 依最终候选排名组织上下文，超限时优先舍弃低优先级项；两者可以因动态状态选择而实际输入不同，但必须保存 `used_memory_ids`、`context_source_refs` 和截断日志。共同候选不等于答案生成器实际看到了全部候选。除 `used_memory_ids/context_source_refs` 外，应保存初始 Top-K、扩展来源、最终候选、上下文截断的ID与原因。Evidence Recall@5/@10按**最终候选 rank**计算；若分析纯向量则另报其自身排序。诊断至少区分“Gold证据是否被B抽取覆盖”“是否被C召回”“是否实际进入QA上下文”，不能把候选命中当成模型使用。

**模型调用约定：** 答案生成模型 ID、非思考模式、温度、输出上限以及请求超时/重试策略在三路中保持一致；D 的额外状态解析调用与成本单独统计。B、D 的结构化输出需设置官方支持的 JSON 模式，Prompt 明示 JSON 格式，检查 `finish_reason`、空输出和 Schema；校验失败不保存为有效结果。对模型输入长度采用安全余量及实际用量检查，不能把估算 token 数视为服务端精确计数。API 不可用时运行 Mock 和缓存测试，不虚构在线评测成绩。

**依据资料：** [LoCoMo ACL 2024 论文](https://aclanthology.org/2024.acl-long.747/)（原始对话规模）；[DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)（参数与模式）；[DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)（`temperature` 的适用范围）；[DeepSeek JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)（结构化输出）。


## 评测与 Gold 对应

| 指标 | 必须遵循的口径 | 特别说明 |
|---|---|---|
| **QA F1** | 使用已冻结版本的 LoCoMo 官方 QA 评分行为或课程实际公布的统一口径 | 保存原始预测与按评分规则处理后的文本；记录分题型和整体逐题平均 |
| **证据 Recall@K** | 有 Gold Memory 单位时评 Memory Recall；仅有 `qa.evidence` 轮次时评 **Evidence-turn Recall@K** | 前 K 条 Memory 的来源轮次先去重；不能把轮次召回误称事实级召回 |
| **动态状态判断** | 按已确认的 Gold 评测单位对照关系与有效Memory | 同时报告必要历史是否召回、关系标签和最终有效集合的表现，不能只评已召回部分 |

**QA 评分依据及适配**：采用冻结版本的 LoCoMo [`task_eval/evaluation.py`](https://github.com/snap-research/locomo/blob/main/task_eval/evaluation.py) 或老师要求的统一评分器，记录源码 Git SHA。官方 `category` 整数与代码的对应是 **1=Multi-hop、2=Temporal、3=Open-domain、4=Single-hop、5=Adversarial**（不要按论文叙述顺序重编）。1按逗号拆分答案并按每个 Gold 子答案的最佳匹配 F1 取平均；2/3/4以官方词项归一化、词干化后的文本 F1 处理，其中类别3先取 Gold 分号前内容；5按照官方拒答短语匹配判定。因包含对抗题规则，汇总项称**LoCoMo 官方 QA 综合得分**，不能全部误称普通 Token F1。参考源码与[类别编号说明](https://github.com/snap-research/locomo/issues/29)；若老师采用不同口径，应以课程评分为准并保留差异说明。

A 在 `evaluation/metrics/answer_adapter.py`（或等价模块）实现 **`AnswerResult` → 评分输入** 的确定性适配，不修改保存的原始预测：`answered` 提交原答案；`no_evidence` 按统一事先固定的拒答文本 `No information available`；`time_uncertain` 固定为 `Unable to determine the time from available evidence`，`unresolved_conflict` 固定为 `Conflicting information in the conversation`，不能依据 Gold 类别事后改写；`error` 作为执行失败，在完整任务主分母中计0分，另行记录执行成功条件下得分，不得当作正确拒答。缺 Gold 单独计 `gold_missing`，不混入可计分分母。对多跳、开放域分号、对抗拒答大小写、`no_evidence` 与 `error` 的差别建立评分 fixture；测试集运行前冻结整个适配逻辑，并确保**先保存预测，后读 Gold**。

**Gold 与自抽取 Memory 的对应**：外部标注的记忆 ID 不保证等于 B 生成的哈希 ID。若课程 Gold 基于相同冻结的 Memory 文件，可直接按 ID 对照；否则 A 在评测侧建立 `gold_item → predicted_memory_id(s)` 的可核验映射，至少保存来源轮次、实体/属性/取值、人工或确定性匹配依据以及一对多/无法匹配情形。映射应由开发样例先检验，必要时抽样复核；测试 Gold 只供冻结后的评分路径访问，不能进入业务模块或用于调参。Gold标注的是轮次时，只能直接得出轮次检索指标，不能凭同轮命中宣称所有事实都命中。

**动态 Gold 评价协议**：明确区分三种单位：（a）`relation_label_metrics` 针对 Memory 对及 `scope_attribute` 的 `UPDATE/SUPERSEDE/CONFLICT`；（b）`decision_metrics` 针对 `(query_id,memory_id)` 的 `RETAIN/EXCLUDE/UNRESOLVED`；（c）`effective_set_metrics` 针对每题最终有效 Memory ID 集合的 Precision/Recall/F1 或 Exact Match。课程 `RETAIN` 的含义需要核对，不能直接作为第四种关系边；`EXCLUDE` 也不自动等于 `SUPERSEDE`。关系负例若采用 `NO_RELATION`，仅属于评测标注类别，不加入业务 RelationEdge 枚举；必须规定参与评价的 Memory 对及属性范围、空集合/无法映射的处理。

**端到端与条件指标分别报告**：相关历史事实未被抽取或关系两端不在 CandidateSet 时记录 `extraction_missing` / `candidate_missing`，不能仅对候选内已出现的关系计算一个偏高的准确率；同时保留候选覆盖条件下的分类指标以定位D的问题。结果记录 `total_gold_units`、`mapped_units`、`candidate_covered_units`、`scored_units`、`model_error_units` 及相应分母和混淆矩阵。团队自建有真实来源的开发样本标注（`team_annotated`）与合成测试（`synthetic_fixture`）分开，保留标注者与复核记录；课程正式 Gold（`course_gold`）不存在时该正式指标仍标 `N/A (gold unavailable)`，不得把内部结果冒充课程准确率。A/D应至少使用一份可人工逐项核查的固定开发样例验证单位映射、分母和评分可复算。

**结果汇总**：保存逐题预测、题目类型、原始证据、方法、来源支持检查和失败原因；至少计算整体逐题平均与分题型结果，并给出按样本分别统计；标明官方 QA 综合得分的评分单位，必要时另列普通文本 F1。服务失败、缺 Gold、证据缺失应分别计数，明确分母；不得只留下成功问题形成虚高成绩。只有两段独立测试对话，不能据此推导广泛用户上的泛化可靠性。

## 统一验收与交付记录

每位成员的工作报告记录：负责模块、代码与接口变更、对应提交 SHA、真实测试命令与运行环境、示例输入输出、已发现问题、实际联调结果；未执行的检查明确标注。验收报告由非主要开发者根据真实代码及测试证据复核，逐条说明已通过、未通过或未验证事项；无法访问 API、数据或日志时不假定成功。报告保存在 `docs/reports/work/` 和 `docs/reports/acceptance/`。

最终交付时应能检查：技术需求可对应到负责人、测试和证据（详细追踪矩阵放入 Milestone 3 的 SRS）；Web 可启动；Memory JSON 可回读且来源可定位；三路对照能够按相同运行协议执行；数据划分与 Gold 隔离正确；动态状态、历史问题、冲突、时间不确定和无证据场景可复现；QA、检索和状态指标有可核验口径；源代码、SRS、项目报告、实验结果、最终展示及过程记录齐备。**仅安排最终验收，不设置中期验收。**

---

# 七、团队需要共同确定的问题

现有需求、技术路线、分工、接口和实验初始参数均按正文执行；本节仅保留需要全组共同确认、会影响整体技术路线或统一接口的决定。具体实现与实验配置按正文由对应成员负责，并以可复现记录为准。

| 共同确认的事项 | 需要作出的决定 | 处理方式 |
|---|---|---|
| **DeepSeek API 所用模型** | 从 DeepSeek 当前可用模型中选择正式使用的型号，确认抽取、状态解析和三路回答是否采用同一型号；三路 QA 必须使用相同基础模型 | A 汇总接口可用性和简短测试结果，全组确认；配置由 A 统一维护 |
| **技术文档与跨模块契约** | 确认本文规定的功能边界、五人分工、主要 DTO、服务接口和交接约定作为共同开发基准 | 全组审阅确认，由 A 同步固化 `docs/contracts.md` 与合同 fixtures；之后跨模块变更需同步更新 |

关于课程是否另行提供动态状态金标，属于外部评测依据的核实工作，由 A、D 依第六部分处理，不将缺失标注或模块参数提升为新的团队方案决策。

**文档依据**：课程项目说明；既有团队技术文档 v1.3；独立审查报告《v1.2审查报告.md》；LoCoMo 官方公开数据及评分代码。此处列出依据不代表已经通过实际系统测试。本规范的核心目标是让需求、模块实现、证据与评测保持一致。
