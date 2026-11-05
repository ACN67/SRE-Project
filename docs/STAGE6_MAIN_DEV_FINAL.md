# main_dev 三路实验与配置冻结

日期：2026-11-05
状态：`MAIN_DEV_CONFIGURATION_FROZEN` / `INTEGRATION_PENDING`

本报告记录 DeepSeek 三路实验、100 题分层验证、全部 757 道 `main_dev`
问题运行、评分和冻结结果。独立测试集 `conv-49`、`conv-50` 没有运行，
测试 Gold 未参与提示、阈值或方案选择。事实抽取和语义审核没有在本轮重跑。

## 实验协议

- 模型：`deepseek-flash`
- Thinking：`enabled`
- Reasoning effort：`high`
- API `max_tokens`：省略
- 每个样本的运行内并发：1
- 自动重试：0
- 样本间：4 个独立进程并行
- 三种方法逐题使用相同的查询分析和有序 CandidateSet
- 每个方法的预测文件先落盘并通过 SHA 回读，评分阶段随后才打开 Gold
- Full Text 使用独立 benchmark profile 的 200K token 输入预算
- Vector RAG 与 Dynamic 使用最多 12 条候选和 12 条证据上下文

固定题集不是调参依据。100 题验证清单由样本比例配额和类别分层生成，
排除此前的 16 道固定诊断题，SHA256 为
`274ed0b8b691b64becebbbfc119db09122785a8b90e6d1ad52edce92599e8d7c`。
样本配额为 `26/14/26/34`，类别 1–5 配额为 `14/17/4/42/23`。

## 本轮修正

- 用 Unicode 文本估算器替换 UTF-8 字节数估算，避免非 ASCII 文本被严重高估。
- 为 benchmark 单独提供 200K QA 输入预算，通用运行仍保持 32K。
- QA 使用最短答案提示，并要求类别 5 只选择一个官方拒答或干扰选项。
- Vector RAG 和 Dynamic 的上下文同时包含抽取事实与实际来源轮次，保留说话人、
  会话、日期、标题和图像说明。
- Web 记忆浏览页可把当前样本的全部 Memory 直接下载为统一 JSON 数组。
- Dynamic 对普通事实题使用全部候选；只有状态、变更、取消、as-of 或冲突问题
  才调用状态解析器。
- 相对时间不再在状态解析前直接失败；显式时间冲突仍立即失败。
- 模型漏掉候选决策时，仅将遗漏项补为 `UNRESOLVED`，不会伪造证据或答案。
- Dynamic 状态解析器可补齐缺失的协议常量 `schema_version=2.1`；其他 JSON
  字段、候选 ID、来源和状态仍严格校验。
- 数字型 LoCoMo Gold 规范化为十进制文本；所有非回答状态统一映射为
  `No information available` 供官方评分器识别。
- Vector/Dynamic 的上下文上限与配置中的候选上限统一为 12。

## 100 题分层验证

100 题清单在同一次三路比较中运行。下表为官方 LoCoMo 五类综合分。

| 方法 | QA 综合分 | Text F1 | 已回答 | No evidence | Time uncertain | Error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Full Text | 0.740117 | 0.500674 | 100 | 0 | 0 | 0 |
| Vector RAG | 0.598680 | 0.398680 | 100 | 0 | 0 | 0 |
| Dynamic | 0.570406 | 0.360203 | 94 | 5 | 1 | 0 |

Vector RAG 与 Dynamic 使用相同的检索结果：

| 方法 | Evidence Recall@5 | Evidence Recall@10 |
| --- | ---: | ---: |
| Full Text | N/A | N/A |
| Vector RAG | 0.480640 | 0.518519 |
| Dynamic | 0.480640 | 0.518519 |

## 动态状态更新准确率

课程尚未下发独立的动态状态 Gold。为完成离线验收，本轮从真实
`main_dev` Dynamic 候选中选取 11 道当前状态、状态历程或可回答性题，
由 ACN67 查看问题、Memory 内容、原始轮次和问题时间后完成
`team_annotated` 标注及复核。该子集包含 112 个 Gold item 映射、
129 个逐 Memory 决策和 6 个明确状态关系。

整题状态更新正确要求同时满足：

- 所有标注关系正确，且没有未标注的多余关系；
- 所有逐 Memory 的 `RETAIN / EXCLUDE / UNRESOLVED` 决策正确；
- 最终有效 Memory 集合与 Gold 完全相同。

| 指标 | 结果 |
| --- | ---: |
| 整题状态更新准确率 | 0.727273（8/11） |
| 关系标签准确率 | 0.833333（5/6） |
| 逐 Memory 决策准确率 | 0.953488（123/129） |
| 有效集合 Exact Match | 0.727273（8/11） |
| 有效集合 Macro F1 | 0.845455 |

失败的 3 道题为：

- `conv-30/qa_conv-30_70`：保留了过多泛化的“business”记忆，增加了 1 条
  未标注关系，有效集合范围过宽；
- `conv-41/qa_conv-41_101`：关系标签正确，但把旧计划 Memory 也放进了当前
  有效集合；
- `conv-42/qa_conv-42_249`：对抗题最终选择拒答，但仍保留了“不玩游戏”这一
  泛化记忆，导致有效集合不精确。

该结果可以证明动态状态评测链路可执行，但不是课程官方状态准确率，不能
替代教师后续提供的 Gold。标注文件 SHA256 为
`403ea2e7ba53acb206381652e9ad6896e4bb040917e4dcbc12dadf49b64d5c79`。

该验证共发出 305 次 HTTP 请求，全部完成，无重试、无失败。使用 DeepSeek
峰值单价和实际缓存命中/未命中 token 估算：

- Prompt：4,288,383 tokens
- Completion：104,374 tokens，其中 reasoning 95,529 tokens
- Cache hit / miss：4,070,371 / 218,012 tokens
- 估算费用：USD 0.2150746，约 CNY 1.5485
- 平均单次延迟：2.6409 秒；最大延迟：43.8584 秒

对比候选上下文为 8 与 12 的两轮结果，固定在相同 100 题、相同模型配置、
相同检索候选上，Vector RAG 从 0.574232 提升到 0.598680；Dynamic 从
0.566744 到 0.570406。由于温度固定为 0 时服务端仍出现可见输出波动，
这些差值作为开发集证据，不宣称统计显著性。

## 全部 757 道 main_dev 问题

| 样本 | 题数 | Full Text | Vector RAG | Dynamic |
| --- | ---: | ---: | ---: | ---: |
| conv-26 | 199 | 0.732195 | 0.617964 | 0.614033 |
| conv-30 | 105 | 0.748581 | 0.605663 | 0.597954 |
| conv-41 | 193 | 0.797199 | 0.653975 | 0.618757 |
| conv-42 | 260 | 0.681875 | 0.549591 | 0.520100 |
| **全部** | **757** | **0.733758** | **0.601955** | **0.580745** |

按类别汇总：

| 方法 | C1 多跳/列表 | C2 时间 | C3 开放推断 | C4 单跳 | C5 对抗 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full Text | 0.615963 | 0.703354 | 0.378142 | 0.754168 | 0.861272 |
| Vector RAG | 0.319507 | 0.652072 | 0.334226 | 0.526529 | 0.930636 |
| Dynamic | 0.305234 | 0.655350 | 0.292084 | 0.489391 | 0.919075 |

Vector RAG 与 Dynamic 的 Evidence Recall 相同：

| K | Recall | 可评分题数 / 总数 |
| --- | ---: | ---: |
| 5 | 0.489441 | 752 / 757 |
| 10 | 0.539006 | 752 / 757 |

五个题目的官方 evidence 字段为空或含无法唯一定位的来源，因此不进入
Evidence Recall 分母；这些题仍进入 QA 评分和总题数。

运行状态：

| 方法 | Answered | No evidence | Time uncertain | Unresolved conflict | Error |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full Text | 757 | 0 | 0 | 0 | 0 |
| Vector RAG | 757 | 0 | 0 | 0 | 0 |
| Dynamic | 696 | 54 | 7 | 0 | 0 |

全部 757 题共发出 2,317 次 HTTP 请求，全部完成：

| Method | Calls | Prompt tokens | Completion tokens | Reasoning tokens |
| --- | ---: | ---: | ---: | ---: |
| Full Text | 757 | 27,556,198 | 254,640 | 249,337 |
| Vector RAG | 757 | 2,466,904 | 192,054 | 187,643 |
| Dynamic | 803 | 2,558,386 | 634,974 | 558,007 |
| **合计** | **2,317** | **32,581,488** | **1,081,668** | **994,987** |

按报告给出的当前峰值单价和实际缓存命中/未命中 token 估算：

- Cache hit / miss：31,980,165 / 601,323 tokens
- 输入费用：USD 0.3722779
- 输出费用：USD 1.2980016
- 合计：USD 1.6702795，约 CNY 12.0260
- 平均单次延迟：3.4321 秒；最大延迟：111.9207 秒
- 四样本并行运行墙钟时间：约 84 分 57 秒

## 结果判断

Full Text 在本次所有样本和总集上得分最高。它直接读取完整对话，是强上界，
但 757 题产生约 2,756 万 prompt tokens。

Vector RAG 的总分比 Dynamic 高 2.12 个百分点。Dynamic 的非回答策略使
61 题在第 1–4 类中按零分进入主分母；同一策略对类别 5 大多是有利的，
Dynamic 的类别 5 得分为 0.919075。Dynamic 未能在综合分上证明优于
Vector RAG。

主要瓶颈是证据覆盖而不是运行稳定性：Recall@10 仅为 0.539006，类别 3
需要隐式推断，类别 1 需要完整列举，类别 4 中大量答案依赖未抽出的关系或
背景。继续直接调整 QA 文案的收益有限，下一轮若追求明显提升，应优先
重做事实粒度与关系覆盖，并增加查询时双通道召回、回源核验和去噪。

## 冻结身份

- Freeze bundle：
  `results/main_dev_freeze_20261105_final_v2.json`
- Freeze bundle SHA256：
  `ac2a17be2bb059f7b07a40317df8c1dc4a7a6011d366b10ccef941a3040a0768`
- Aggregate：
  `results/main_dev_full_live_v4_aggregate.json`
- Prediction code fingerprint：
  `sha256:e2c1c4b5d4fa95e5b580d21fc5f8dcd952b3634993bce98c1a9044d23c964a28`
- Final tree code fingerprint：
  `sha256:09e40943e0d2fefab275f789407aed6ef6f17f8d6b24df5113652bc237e94d15`
- Dataset：
  `sha256:79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`
- Runtime config：
  `sha256:0aef170bf898e2f9363356aa995bb61674b23820b4ac2efd609243460377500c`
- Execution policy：
  `sha256:73e6fcf73eaa9c4fe6cd97e14af283120e2f8f66e6cfbf78b805fead55064697`
- Embedding model revision：
  `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`
- Benchmark QA prompt：
  `sha256:00a262e9f7652b7888c7e31bbea2e3abe29e5232fb8ab22877d455cfb882237a`
- Dynamic resolver prompt：
  `sha256:8a97389660cba97e10bb002d7fe7548e92fedb9cd81dfb978365f6e2436fef14`

冻结清单还逐样本保存 Artifact build ID、Memory set、审核、来源、索引及三种
方法的预测 SHA。该清单是后续正式测试的输入身份依据。

## 回归与限制

- `python -m pytest -q`：`1347 passed`
- `python -m py_compile`：聚合与冻结工具通过
- `git diff --check`：通过，仅有 Windows 换行提示
- 预测、调用账本和评分结果全部位于被 Git 忽略的 `results/`
- 没有执行 `conv-49`、`conv-50`
- 没有重跑事实抽取或语义审核
- 团队已报告状态关系、决策和有效集合的开发集结果；课程官方状态 Gold 仍未下发
- 类别 3 和隐式关系覆盖仍是后续兼容性修复重点

因此，本轮的工程链路、开发集实验与配置冻结已完成；真实独立测试和系统
联调验收仍未完成。
