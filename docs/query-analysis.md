# C：查询分析第一版

查询分析先判断问题想找什么，再把条件交给检索与 D 的状态解析模块。例如，“现在在哪里工作”与“曾经在哪里工作过”需要不同范围的记忆。这里不负责判断哪条记忆有效，也不生成答案。

## 直接试用（Git Bash）

在仓库根目录、已安装项目依赖的环境执行；不需要下载 Embedding 模型，也不需要 API Key：

```bash
.venv/Scripts/python.exe -m dynamic_memory.retrieval.query_analysis_smoke "Where has Sam ever worked?"
.venv/Scripts/python.exe -m dynamic_memory.retrieval.query_analysis_smoke "小王现在在哪里工作？" --entity 小王
.venv/Scripts/python.exe -m dynamic_memory.retrieval.query_analysis_smoke "Where did Sam work on May 1, 2023?" --as-of 2023-06-01
```

第三个例子故意让问题日期与参数日期不同，应输出 `QUERY_TIME_CONFLICT`，保留两边的时间。

## 程序接口

```python
from dynamic_memory.contracts.models import QueryRequest
from dynamic_memory.retrieval import analyze_query, QueryAnalyzer

request = QueryRequest(query_id="q1", sample_id="demo", question="Where has Sam ever worked?")
analysis = analyze_query(request)
assert analysis.query_intent == "state_history"
assert analysis.time_mode == "latest_known"

# 若 B 已提供当前样本的人名列表，可注入以改善中英文匹配。
analyzer = QueryAnalyzer(entity_names=["Sam", "小王"])
analysis = analyzer.analyze(request)
```

`analyze_query(request) -> QueryAnalysis` 保持技术规范的统一入口。`QueryAnalyzer` 的可选人名词表只能来自当前样本，调用方负责选择该样本的词表；没有全局共享实体状态。输出只使用公共 DTO v2.1，不新增 Schema 字段。规则版本为 `QUERY_ANALYSIS_VERSION`（当前 `c-query-rules-v2`），集成方可记录到运行日志，并在规则版本变化时使相关缓存失效。

## 能识别什么

| 问题示例 | query_intent | 时间解释 |
| --- | --- | --- |
| Where does Sam work now? | current_state | latest_known |
| Where did Sam work on 2023-05-01? | historical_state | as_of，解析到该日 |
| Where has Sam ever worked? | state_history | latest_known，仍保留历史经历 |
| When did Sam resign? | event_fact | latest_known，不自行填离职日期 |
| What is Sam's favorite movie? | general_fact | latest_known |
| ??? | unknown | latest_known，不强制按当前状态过滤 |

意图和时间约束分别判断。`latest_known` 表示以已知对话结束处为截止界限，不能当成电脑今天，也不表示只需要最新状态。显式 `as_of` 与问题中的唯一明确日期一致时使用该日，明确不一致时报冲突。

完整日期支持 `YYYY-MM-DD`、`YYYY年M月D日`、`May 1, 2023`、`1 May 2023` 等常见表达。只有年份、月份、多个不同日期、范围/前后条件、相对时间、无效日期或含糊的斜杠日期时，返回 `time_mode=unknown` 和 `TIME_PARSE_UNCERTAIN`，不擅自压缩成某一天。显式日期不掩盖这些未解决的范围或相对时间问题。

v2 修正了日期片段造成的误报：含糊斜杠日期、明确日期混合相对时间时，不用其中解析出的片段证明时间冲突；`by May 1, 2023`、`not on May 1, 2023`、无锚点的 `on Friday` 均保留时间不确定性。清晰的唯一日期与显式参数矛盾时仍报告冲突。首尾空白不改变问题分类，也不修改原始请求。

实体提示：有词表时按词表匹配；无词表时只尝试英文首字母大写的人名/专名，不能可靠识别中文人名、全小写人名、代词或别名。不推断“他”是谁。

属性提示暂含 `employment`、`residence`、`preference`、`education`、`adoption_stage`。这些是帮助检索的线索，尚未与 B 的真实属性词表联调，**不得直接作为排除其他 Memory 的强制过滤条件**。

## 边界和验证

这是固定规则基线，覆盖的是常见表达，不是完整的自然语言理解。复合问题、罕见表达可能误分类；接入真实开发集后需检查并改进。未识别到的时间表达也可能漏检；返回结果不能代替 D 对证据和实际时间有效性的判断。

不使用电脑日期，不读取 Memory、Gold 或外部数据，不调用网络/LLM，不修改请求。无对话时间锚点时，“去年”“昨天”不能确定为具体日期，故保留不确定性。

测试命令：

```bash
.venv/Scripts/python.exe -m pytest tests/unit/test_query_analysis.py -q
```

测试均为合成例子，不代表 LoCoMo 上的分类准确率。后续由 A/C/D 联调：三个方法消费相同分析结果，处理 `time_issue`，不能静默忽略时间冲突或不确定性。
