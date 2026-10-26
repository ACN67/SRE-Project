# C：查询分析第一版工作记录

日期：2026-10-19。分支：`C`。本轮仅修改、测试，未 commit 或 push。

## 实现内容

- `retrieval/query_analysis.py`：实现 `analyze_query(QueryRequest) -> QueryAnalysis`，输出常见查询意图、实体/属性线索和独立时间条件。
- `QueryAnalyzer` 可接收当前样本的人名词表，实例互相隔离；无词表时仅尝试英文专名提示，不猜测代词指代。
- 支持常见中英文完整日期；明确冲突保留两侧时间；相对日期、部分日期、范围、多个日期和无效日期保留 `TIME_PARSE_UNCERTAIN`。
- 规则版本 `c-query-rules-v1` 可供 A 的运行记录使用；未改变公共 DTO v2.1。
- 提供命令行演示、使用说明及 64 个合成测试用例。不调用 LLM、Embedding、网络，不读取 Gold 或 Memory。

## 验证

Windows，CPython 3.12.14，pytest 9.1.1。项目推荐的 Python 3.11 未验证。

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
git diff --check
```

全套 **329 passed**（含新增 64 项查询分析测试）；diff 格式检查通过。普通沙箱访问系统临时目录失败；将临时文件改放 `.venv` 又触发原有 SQLite 缓存目录限制，因此最终保留原有系统临时目录约定、提升权限运行完整测试，不修改原有缓存规则或测试。

执行了 `docs/query-analysis.md` 中的三个命令行示例，均正常退出：

- `Where has Sam ever worked?` → `state_history` + `latest_known`。
- `小王现在在哪里工作？`，词表含“小王” → `current_state` + `employment`，实体提示为“小王”。
- 问题含 `May 1, 2023`，参数 `as_of=2023-06-01` → `conflict` + `QUERY_TIME_CONFLICT`，两侧原始时间保留。

## 限制与交接

固定规则只覆盖常见表达，复杂自然语言和未覆盖的时间表达可能误判/漏检，未计算真实数据上的分类准确率。没有对话时间锚点，不能可靠解析“去年”等相对时间；不使用电脑当前日期替代。

实体和属性仅作检索线索，不作强制排除条件；属性词表尚未与 B 的真实 Memory 联调。A/C/D 后续须共用相同分析结果、处理 `time_issue`，并在开发集核查分类、时间解释与候选覆盖。索引、历史候选扩展、Vector RAG 尚未在本轮实现。

## 提交前修复与复验（2026-10-26）

- 新增 6 项回归用例，先观察全部失败，再修复：含糊斜杠日期或相对时间混合表达不再误报确定冲突；`by May 1` 截止条件、`not on` 排除条件、无锚点的 `on Friday` 保留不确定性；问题首尾空格不影响分类。
- 规则版本由 `c-query-rules-v1` 更新为 `c-query-rules-v2`，公共 Schema 不变。
- `.venv/Scripts/python.exe -m pytest tests/unit/test_query_analysis.py -q -p no:cacheprovider`：**70 passed**。
- `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`：**335 passed**。Windows / Python 3.12.14；完整回归因系统临时目录权限需要提升权限运行。未声称 Python 3.11 已验证。
- 在内存中载入已获取的 `origin/A`（b17893b）真实 `pipeline/query_conditions.py`，接入当前 C 分析器：7 类合成输入均通过 A 的公共字段/身份/时间校验；同时验证三个方法的时间冲突早停响应为 `time_uncertain`。这不是完整三路业务联调或真实 LoCoMo 效果评测。
- 本轮仍在旧基线的 C 工作区，尚未合并最新 main；上述全量数量仅代表此工作区。没有 commit/push，待人工提交后同步 main 并验证新基线。
