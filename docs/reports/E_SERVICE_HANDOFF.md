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
