# E 统一服务接入与合并交接

日期：2026-11-01。工作分支 E。未合并其他成员分支，未 commit/push。

## 远端核对

本次实际执行 git fetch origin，读取以下分支文件，而非根据历史说明猜测接口：

| 分支 HEAD | 已核对能力 | E 的使用方式 |
| --- | --- | --- |
| A `02923f7` | `services.DynamicMemoryService`、`contracts.service_status.BuildStatus`、QueryOrchestrator 与业务服务端口 | 通过 WebBindings 消费六个公共方法 |
| B `001c522` | Memory 窗口/抽取/构建、`MemoryArtifactStore.publish/load/import_memories` | E 不直接读产物；A 注入 B 的已验证 loader |
| C `806d229` | 查询分析、索引、检索、Vector RAG、`RetrievalServicePorts` | E 不直接调用；A 注入 C 的只读索引和问答端口 |

## E 已完成的接口与页面

- 保留四个浏览方法：list_samples、list_memories、get_memory、get_turn。
- 新增 get_build_status 消费：延迟导入 A 的唯一公共 DTO，合并前缺失时明确提示，不复制类型。
- 新增 run_query 消费：原样传公共 QueryRequest，核对 QueryResponse 身份，并使用公共上下文验证器。仅补查响应引用的记录及轮次，不读取原始文件或 Gold。
- 问答表单：方法、问题、可选截至日期；每次显式提交生成新 interactive UUID，调用一次统一服务。未注入 run_query 的服务显示未接入提示，Demo 不伪造自由问答。
- 展示五种回答状态、查询分析、候选、状态解析、实际使用项及来源。原始服务异常消息不回显。
- 构建状态面板：只展示服务实际返回的状态，complete 与成功空列表共同确认“完整零 Memory”。构建身份变化清理旧结果。
- 独立原文回查表单：Memory 不可用时仍可通过当前样本、Session ID、Dia ID 调用 get_turn；Full Text 提问也不受 Memory 列表失败阻塞。
- 响应保存在会话，分页、筛选、来源导航不重复 run_query；样本、方法、绑定或构建身份变化清除旧响应。

生产代码仅在 `src/dynamic_memory/web/`。没有实现 A/B/C/D 算法、复制 BuildStatus 或修改公共业务 Schema。

## 明天合并时 A 需要完成的装配

1. 保留 A 的公共 `BuildStatus` 和 `services` 实现，以及 B 的来源仓库和已验证产物读取实现。E 的延迟导入在类型合入后自动启用。
2. 由 A 创建统一服务，注入 B 来源仓库、build_status、可信 load_verified_artifact、C index_readiness 和 QueryOrchestrator。B 的 artifact 不能直接当作 A 的 VerifiedMemoryLoad，需由可信组合入口携带真实验证身份进行装配。
3. C 的 RetrievalServicePorts 接收 A 的 readiness_factory（IndexReadiness），将相应 callable 注入 A；不用 E 实现索引或自行下载模型。
4. A 的部署工厂返回 `WebBindings(service=service)`，将实际可导入工厂配置为 `DYNAMIC_MEMORY_WEB_FACTORY=实际模块:create_bindings`。本报告没有声称已存在一套默认真实服务工厂。
5. Full Text / Vector RAG / Dynamic 分别核验实际业务能力；D 尚未提供或端口未配置时，应保留合法 error 响应，不将 Mock 结果当作完成的动态问答。
6. 先运行 E Web 测试和公共合同测试，再用已授权开发样本核验构建状态、完整零 Memory、来源/caption、三路查询、过期索引和错误分支。不要自动抽取或在浏览中建索引。

## 验证范围

Web 测试包含合并前可运行的接口消费者测试，以及 `test_service_handoff.py` 的公共状态和 A 服务测试。后者在公共模块未合并时显式 skip，不能算作本地已通过；本次另外在临时目录中使用远端 A 代码叠加 E Web 运行，验证 A/E 接口。未执行真正 A+B+C+D 全业务链路，未使用真实 API 或下载模型。

本次使用项目虚拟环境实际执行：

- 本地 `python -m pytest tests/web -q`：72 passed、8 skipped（A 公共类型/服务尚未合并）。
- 本地 `python -m pytest tests/contract -q`：72 passed。
- 本地 `python -m pytest -q`：450 passed、8 skipped。
- 临时目录：远端 A `02923f7` 的源码叠加最终 E Web 及测试，`python -m pytest tests/web -q`：80 passed、0 skipped；包含真实 A 服务的未构建状态、来源回查、缺组件合法错误响应。
- 启动 Streamlit，健康检查 ok；Headless Edge 在 1440px 与 390px 视口完成 synthetic 提交及来源回查，页面无水平溢出。截图为 `docs/screenshots/service-query-1440.png` 和 `service-query-390.png`，均为标注的合成服务，不是实际模型回答。
- `git diff --check`：通过。

临时测试源码没有复制到生产目录；未切换 E 分支，未合并远端提交。正式统一服务工厂与 B/C/D 装配仍须按上面的清单联调。

## B/C 接入补充验证（2026-11-01）

新增 `tests/web/test_bc_handoff.py` 和可重复运行的检查工具：

```powershell
# 在 E 仓库，使用已获取的 origin/A、origin/B、origin/C，不自动 fetch
.\.venv\Scripts\python.exe scripts/check_web_handoff.py
```

工具在系统临时目录展开 A 源码，只叠加 B 的 memory 目录、C 的 retrieval 目录及当前 E Web/测试；退出时清理临时目录，不切分支、不修改当前源码、不 merge/commit/push。这是选定源码的兼容性演练，不等同 Git 自动合并成功或全组业务验收。

本次使用上述三个固定 HEAD，实际结果 **84 passed，0 skipped**。其中新增四项验证：

1. B 的真实 `MemoryArtifactStore` 在 pytest 临时目录显式发布合成产物，严格 `load()` 回读；通过 A 的真实服务和 E 的 WebBindings 浏览状态、列表、详情与原文。
2. C 的真实 IndexManager、RetrievalServicePorts、检索及 VectorAnswerer，经 A 的 QueryOrchestrator 和统一服务返回合法 Vector RAG 答案，再通过 E 的候选/来源校验。
3. 索引文件缺失时返回 `INDEX_NOT_FOUND`，不调用生成、不重建索引，来源仍可回查。
4. 在 Streamlit AppTest 中接入上述 A/B/C 组合，提交一次问答并点击答案来源，生成只调用一次，浏览不重新编码记忆。

测试明确使用合成原文、固定向量编码器和离线 Fake LLM；只有索引持久化、B 产物回读、检索/回答逻辑、A 服务与 E 消费者使用真实代码。VerifiedMemoryLoad 标记为 `synthetic_fixture`，没有伪造真实数据验证身份。不是 DeepSeek/真实 Embedding 或 D 动态推理验收。

### 精确端口映射

| 提供方 | A 的装配点 | E 的消费点 |
| --- | --- | --- |
| B SourceRepository | `DynamicMemoryService(sources=...)` | list_samples/get_turn |
| B MemoryArtifactStore.load | 可信组合入口封装 VerifiedMemoryLoad，注入 `load_verified_artifact` | list_memories/get_memory/get_build_status |
| B 构建结果/产物诊断 | A 的 `build_status` callback，返回公共 BuildStatus | 构建状态面板；不从空列表猜测状态 |
| C RetrievalServicePorts.index_readiness | A 的 `index_readiness` callback；C readiness_factory 使用 A IndexReadiness | 查询错误/就绪状态由 A 统一报告 |
| C analyze_query/load_verified_index/retrieve/load_candidates/answer_vector_rag | A QueryOrchestrator 对应同名端口 | WebBindings.run_query(request, method="vector_rag") |

E 的生产接口不再扩展为直接调用 B/C 的另一套服务；新增测试中的组合代码仅供 A 核对真实签名，不能当作正式 Runtime 工厂使用。

### 合并时保留的差异与限制

- A 的 SourceRepository 已新增 `snapshot_sha256` 只读属性；B 分支仍是旧实现，不能整文件覆盖 A 的版本，否则统一服务无法核验快照身份。检查工具刻意保留 A 的 data/repository.py。
- A 的公共 QueryResponse 还增加了实验侧 `pure_vector_topk` 方法。合并公共 contracts 时保留 A 的新增能力；E 的交互方法仍只有规范要求的三种。
- B 当前 `MemoryArtifactStore.publish()` 仍要求 `allow_mock_publication=True` 且后端为 mock。上述测试只验证其明确支持的离线产物路径，不能宣称真实模型构建产物的正式发布已完成。
- 实际部署仍由 A 提供统一服务工厂，配置真实 B/C 资源与可信验证身份；E 不接管模型、产物路径、状态推导或索引构建。

## 页面拆分与布局整理（2026-11-01）

新增功能从浏览页移到侧栏导航的独立工作页：记忆浏览只呈现筛选、卡片、详情及所选来源；问答工作台呈现提问与结果；原文回查提供独立轮次表单；服务状态显示公共构建状态和接口能力。统一标题、留白、表单卡片及当前导航项样式。

导航只渲染当前页，不使用会在后台执行所有面板的标签页。服务与响应继续保存在同一会话，切页不重提问；控件值在会话字典中保存并恢复。新增测试验证页面内容隔离、草稿/方法/日期/响应保留及其他页面不读取完整记忆列表。

最终全套测试：453 passed、12 skipped（未合并的上游模块）；A+B+C+E 临时组合 Web 测试：87 passed、0 skipped。Headless Edge 实测四个页面在 1440px/390px 均可展示，无水平溢出；使用合成服务检查提问及原生侧栏导航，截图位于 `docs/screenshots/pages-*.png`。未修改业务服务接口，启动命令不变。

## 首页与卡片入口（2026-11-01，后续布局更新）

默认入口改为首页，服务标签、刷新按钮和样本选择集中在首页服务面板。四张卡片分别进入记忆浏览、问答工作台、原文回查和服务状态，工作页使用“返回首页”，替代上一版侧栏单选导航。桌面两列、手机纵向排列；合成数据提示始终保留。

样本选择控件与实际会话样本分离保存，离开首页后不会因控件清理丢失样本。卡片跳转先保存当前工作页控件值，返回后恢复筛选、方法、问题及日期；提交结果继续复用，不因进入首页重新查询。刷新服务和换样本仍清除旧结果。记忆详情替换列表展示，返回列表显式恢复原筛选及页码。

验证：本地 Web+公共合同测试 149 passed、12 skipped；远端 A/B/C 加当前 E 的临时组合 Web 测试 89 passed、0 skipped。Headless Edge 验证 1440px/390px 首页、卡片进入、返回首页、无水平溢出，截图为 `docs/screenshots/home-1440.png`、`home-390.png`。这些是合成浏览数据验证，不增加或声称真实业务能力；启动命令、服务接口均未改变。

## 常驻功能导航（2026-11-01）

参考用户提供的 Kaggle 导航布局，为首页和所有工作页增加白底左栏：首页、记忆浏览、问答工作台、原文回查、服务状态。图标与文字左对齐，当前页使用浅色圆角背景。使用原生 Streamlit 按钮与原生侧栏折叠控件，保留键盘操作；不新增 Kaggle 的其他业务功能。

首页卡片继续保留，服务设置仍集中在首页。左栏、卡片及返回按钮共用 navigate 回调，保留会话状态、不重复生成答案。新增测试覆盖侧栏入口与首页卡片往返、当前页高亮、已提交结果保留和单次模型调用。

A/B/C/E 临时组合 Web 测试：90 passed。Headless Edge 在 1440px/390px 验证左栏跳转、高亮、手机展开/收起与卡片可用性，无水平溢出；截图为 `docs/screenshots/sidebar-navigation-1440.png`、`sidebar-navigation-390.png`。服务接口与启动方式不变。

本地 `python -m pytest tests/web tests/contract -q`：150 passed、12 skipped（待合并的公共类型及服务）；`git diff --check` 通过。

## 字体与排版优化（2026-11-01）

参照用户提供的 Kaggle 视觉方向，改用优先中文无衬线的本地字体栈（Noto Sans SC、微软雅黑等），不加载外部字体。取消中文标题负字距；桌面主标题最高 48px、正文 18px、表格 17px、侧栏导航 18px；手机主标题 32px、正文 17px。增加表格单元格留白与记忆卡片内边距，辅助信息使用较小字号保持层次，Material 图标和代码等宽字体不受覆盖。

本次只修改 CSS。`python -m pytest tests/web/test_app.py tests/contract -q`：86 passed。Headless Edge 实测 1440px/390px 首页和详情页，无水平溢出；读取 computed style 核对字号，并确认浏览器实际使用 Noto Sans SC 字形。截图为 `docs/screenshots/typography-home-*.png`、`typography-detail-*.png`。没有重复执行全业务测试或实际模型联调。

## 原文来源交互简化（2026-11-01）

原来的“前往原文查看器”只是页面锚点，不会加载原文，容易让用户误以为无响应。现移除该链接和空查看器，详情页“原始对话”区域提示点击 session/dia 来源；点击后通过 get_turn 校验并在按钮下方卡片显示说话人、时间、原文及单独标注的 caption。

强化既有来源测试，确认查看详情不预先调用 get_turn、点击来源才加载；既有身份校验和异常清理仍保留。公共合同测试 72 passed，Headless Edge 在 1440px/390px 验证一键显示来源、没有多余锚点链接和水平溢出。截图为 `docs/screenshots/source-inline-*.png`。

## 可点击记忆卡片（2026-11-01）

将记忆列表改为两列卡片网格，窄屏自动单列。每张卡片包含事实类型、原始时间、实体、三行正文预览、来源数量和详情入口；移除卡片外的独立按钮，整卡可点击。悬停显示边框与阴影，键盘聚焦显示轮廓线。

使用原生 Streamlit 按钮覆盖卡片区域接收点击，保留键盘与既有 open_memory 回调；视觉 HTML 的服务字段仍经过转义，不使用 JS、不更改查询或详情服务。进入详情后仍可返回原筛选与页码，详情保留完整正文。

Headless Edge 实测 1440px/390px 卡片边缘点击、Enter 激活均进入详情，无水平溢出；截图为 `docs/screenshots/memory-grid-*.png`。页面/详情/公共合同专项测试 92 passed；A+B+C+E 临时组合 Web 测试 90 passed，服务端能力未改变。

完整本地 Web 回归：78 passed、12 skipped（上游公共模块未合并）。

## 列表底部分页（2026-11-01）

卡片下方新增上一页、当前页/总页数、下一页，和顶部页码选择共用会话状态。首页/末页禁用对应按钮，筛选后重置第一页，进入详情再返回保留页码。通过控件回调在页面重运行前修改 page，同时清除旧记忆/来源选择；不触发 run_query 或构建。

新增交互测试覆盖上下翻页、首末页、筛选保留与重置、详情返回、无重复问答。页面/详情/公共合同专项测试 93 passed；A+B+C+E 组合 Web 测试 91 passed。Headless Edge 在 1440px/390px 验证真实按钮跳转、禁用状态与底部横向排列，无水平溢出，截图为 `docs/screenshots/pagination-*.png`。
