# E Web 接入说明

当前接口以文末“2026-11-01 统一服务接入”一节为准；第一期及 10-21 段落为历史增量记录。

本期从独立原型迁入记忆浏览、筛选、分页、详情与原文追溯。实现位于 `src/dynamic_memory/web/`，启动入口为 `streamlit_app.py`。规范依据为 `TECHNICAL_SPEC.md` v2.2；公共业务 Schema 版本仍为 2.1。公共对象直接引用 `dynamic_memory.contracts.models`，不维护第二套 Schema。

## 运行

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev,web]'
.\.venv\Scripts\python.exe scripts/run_web.py
```

默认地址 http://127.0.0.1:8501。现在必须显式设置 `SRE_INTEGRATION_PROFILE=demo` 才显示 Demo；未配置工厂且未指定 Demo 会显示初始化错误，不隐式回退。`offline_synthetic` 的真实 A/B/C/E 工厂命令见 [离线集成说明](ABCE_OFFLINE_INTEGRATION.md)。测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/web -q
.\.venv\Scripts\python.exe -m pytest -q
```

## A 的服务接入

在可导入的集成模块中定义无参数工厂，返回 `WebBindings(service=已初始化服务)`，再设置：

```powershell
$env:DYNAMIC_MEMORY_WEB_FACTORY = 'your_integration_module:create_bindings'
.\.venv\Scripts\python.exe scripts/run_web.py
```

`your_integration_module` 是其他部署方式的占位名称；当前离线工厂为 `dynamic_memory.integration.offline:create_web_bindings`。工厂每个浏览器会话初始化一次；“刷新服务”会重新初始化当前会话。工厂配置只来自部署环境，不接收页面输入；配置错误会显示失败，不切回演示。

| 方法 | 参数 | 返回 |
| --- | --- | --- |
| list_samples | 无 | list[str] |
| list_memories | sample_id | list[MemoryRecord] |
| get_memory | sample_id, memory_id | MemoryRecord |
| get_turn | sample_id, SourceRef | SourceTurn |

这些是规范统一服务的四个既有只读方法，Protocol 仅描述 Web 使用的子集，不替代统一服务。UI 使用真实 SourceRef 参数；返回值通过公共 Pydantic 模型校验，并核对样本及记录身份。原文渲染使用公共 `render_visible_turn`。若 B 后续迁移该函数的公共导出位置，应同步调整导入和合同测试。

## 本期边界与兼容性调整

- 页面搜索只过滤已读取的列表，采用服务提供的顺序；分页为浏览器端分页，不承担大规模服务端检索。
- `event_time`、`recorded_at` 为空时显示未知；不推导时间、有效记忆、替代关系或冲突结论。
- 详情通过 `get_memory` 获取，原文通过 `get_turn` 获取；保留原始 text、caption 和 caption_origin 的区别。
- Demo ID 使用公共 `compute_memory_id`；旧原型自行拼接 JSON 的算法已移除。旧原型可见文本拼接方式也已改为公共函数。
- 切换样本、筛选、页码、记忆或服务清空过期选择；异常不展示原始敏感信息。
- CSS 随 Python 包分发；CI 安装 web extra 后运行全部测试。Web 依赖下限固定为本次验证的 Streamlit 1.65。

本期不提供问答提交、三路结果比较或关系图，也不迁入旧响应校验器。下一期需按实际 QueryResponse 合同迁移，尤其核对 `time_uncertain` 前置退出条件：当前公共模型只对时间冲突分支豁免常规中间对象要求，旧演示的未知时间分支不能直接沿用。

真实 A/B 数据服务尚未接入，synthetic 测试通过不代表端到端验收。接入后需使用真实开发样本核对来源、caption、空数据和失败分支。AI 开发约束见 `ai-skills/dynamic-memory-web/SKILL.md`。

## v2.2 状态展示准备（2026-10-21）

已完成 E 侧稳定异常码的显示映射，代码位于 `web/status.py`。读取异常的 `.code`，只显示白名单中的固定中文说明及错误码；不解析异常文本，不回显服务原始消息或未知错误码。覆盖数据未加载/加载失败、记忆未构建/部分构建/构建失败、产物损坏/过期、索引过期和来源身份异常。没有稳定错误码的异常继续显示通用诊断提示。

样本读取失败时保留“刷新服务”，便于服务恢复后重试；失败清除旧记忆与来源。真实绑定明确提示构建状态接口尚未接入，成功读取列表不被升级为完整 BuildStatus。空样本提示只表示 `list_samples()` 成功返回空列表。

当前仍使用四个既有只读接口，不添加替代 BuildStatus。A 提供公共类型、`get_build_status(sample_id)` 和统一服务后，再接入完整状态面板，核验 dataset_state、memory_state、partial_build、build_id、error_code 的含义及一致性。完整零 Memory、就绪状态必须结合公共状态结果确认；当前页面不自行推导。Memory 不可用时独立来源访问的页面流程也留待该接入批次完成，现有来源入口仍依赖记忆详情。

本批没有实现构建、产物加载、索引、问答或模型调用。状态测试使用 synthetic 服务异常，不代表实际 A/B 服务联调完成。

## 2026-11-01 统一服务接入

E 已支持 A 的六个公开业务方法：四个既有浏览接口，以及 `get_build_status(sample_id)`、`run_query(request, method="dynamic")`。只调用统一服务，不直接 import B 的产物仓库或 C 的检索器；B/C 端口、模型及索引路径由 A 的组合入口注入。工厂仍返回 `WebBindings(service=已初始化的统一服务)`，每个会话一份绑定。

`get_build_status` 返回 A 的 `dynamic_memory.contracts.service_status.BuildStatus`，E 通过该公共类型重新校验，不定义替代 DTO。当前 E 基线尚未合并 A 的该文件时，页面明确提示缺少公共接口；没有此方法的旧浏览服务仍能使用四个浏览接口。

`run_query` 使用现有公共 `QueryRequest` / `QueryResponse`，方法值为 `full_text/vector_rag/dynamic`。提交表单时生成 `interactive_<UUID>`，传入原问题和可空 `as_of`，只调用一次服务。结果保存在 `st.session_state`；来源选择、搜索及分页不重复查询。切换样本、方法、服务或构建身份会清除旧结果。编辑未提交的问题时，旧结果仍显示其原始提交问题。

E 核对响应的 query/sample/question/method 身份，用公共验证器核验候选、有效集合和使用项关系；候选详情及实际上下文轮次经统一 `get_memory/get_turn` 读取并校验身份。服务侧仍须负责完整来源、产物和索引的可信验证。Full Text 不依赖 Memory 读取成功；读取 Memory 失败后问答及独立 Session ID / Dia ID 回查入口仍可使用。

页面分别展示 answered、no_evidence、time_uncertain、unresolved_conflict、error，展示服务返回的查询分析、候选顺序、状态解析和实际使用 ID。null 中间对象与已完成但为空的候选集合分开处理。仅实际上下文来源生成问答来源按钮，答案证据单独标记；点击后通过 get_turn 回查，原文与 caption 分开显示。服务错误的原始消息不展示，未知错误码使用固定通用文案。

构建状态面板展示数据与 Memory 状态、构建 ID；只有公共状态确认 ready/complete 且列表读取成功，才显示“已就绪”或“完整构建，零条 Memory”。不触发离线构建。完整交接清单与分支核对结果见 [E_SERVICE_HANDOFF.md](reports/E_SERVICE_HANDOFF.md)。

B/C 的实际端口已通过临时 A+B+C+E 合成链路检查。可执行 `python scripts/check_web_handoff.py` 重复验证，使用已获取的远端分支，不合并当前工作区。精确映射、测试限制和合并时必须保留的 A/B 差异见交接说明的“B/C 接入补充验证”。

## 页面导航

默认进入首页，首页统一展示服务标签、刷新服务、样本选择，并以四张卡片进入记忆浏览、问答工作台、原文回查和服务状态。左侧常驻图标功能列表提供首页与四个工作区入口，当前页圆角高亮；使用原生按钮，支持键盘与原生侧栏折叠。首页卡片、左栏和“返回首页”共用 navigate 回调，不重复查询。工作页显示当前样本。每次只渲染所选页。首页、问答、原文和状态页不读取整份 Memory 列表；服务自身的状态核验仍按 A 的合同执行。

切页保留当前筛选、提问草稿、方法、日期和已提交响应，不重复调用 run_query。控件值在切页回调中存入会话的独立字典，回到页面时恢复，以避免 Streamlit 清理未显示控件时丢失输入。样本选择使用独立控件键，实际样本单独保存到会话，离开首页仍保持选中样本。样本/服务切换清理旧上下文，构建身份变化仍清除旧结果。首页入口卡片在手机宽度下自动纵向排列。

记忆列表点击“查看详情”后进入详情视图，替换列表与筛选区；通过“返回记忆列表”恢复原筛选和页码。详情及来源通过 get_memory/get_turn 读取，详情视图不额外加载完整记忆列表，服务内部的验证行为仍由 A 负责。

详情的“原始对话”区域点击 session/dia 来源按钮后，直接在按钮下方加载并展示原文。已移除只负责锚点定位、不会加载内容的“前往原文查看器”链接；未选择来源时只显示操作说明，不显示空查看器。

记忆列表为两列卡片网格，手机端自动单列。卡片展示类型、时间、实体、正文预览及来源数量，整张卡片通过原生按钮点击进入详情，支持键盘 Enter/Space 和可见焦点边框。正文预览最多三行，完整文本在详情中保留。动态 HTML 继续转义，不注入点击脚本，不新增数据或模型调用。

记忆浏览页提供“下载当前样本全部 Memory JSON”，由已经过统一服务校验的
`MemoryRecord` 列表直接序列化为 JSON 数组；文件名包含样本 ID，内容保持
Schema 2.1、稳定字段排序和 UTF-8，不触发检索、模型或外部 API。

卡片列表底部提供“上一页 / 当前页与总页数 / 下一页”，与顶部页码下拉框共用 page 状态。首页禁用上一页、末页禁用下一页；筛选后回到第一页，返回详情前的列表仍保留当前页。按钮只调整本地展示页码，不调用问答或构建。
