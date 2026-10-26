# E Web 第一期接入说明

本期从独立原型迁入记忆浏览、筛选、分页、详情与原文追溯。实现位于 `src/dynamic_memory/web/`，启动入口为 `streamlit_app.py`。规范依据为 `TECHNICAL_SPEC.md` v2.2；公共业务 Schema 版本仍为 2.1。公共对象直接引用 `dynamic_memory.contracts.models`，不维护第二套 Schema。

## 运行

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev,web]'
.\.venv\Scripts\python.exe scripts/run_web.py
```

默认地址 http://127.0.0.1:8501。未配置服务时仅显示明确标注的 synthetic 合成数据，不使用真实数据或 API。测试：

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

`your_integration_module` 是接入方实际模块的占位名称，本期没有实现 A 的运行时。工厂每个浏览器会话初始化一次；“刷新服务”会重新初始化当前会话。工厂配置只来自部署环境，不接收页面输入；配置错误会显示失败，不切回演示。

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
