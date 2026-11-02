# A/B/C/E 离线合成集成

此运行模式为 `offline_synthetic`，只使用合成 LoCoMo 形状的对话、严格预设的 Mock 抽取、仅接受与原文完全相等的合成 EvidenceChecker、固定二维向量和标为 Synthetic 的 QA 替身。B 的 Loader/MemoryBuilder/ArtifactStore、C 的 IndexManager/检索、A 的 QueryOrchestrator/DynamicMemoryService、E 的 WebBindings/页面是实际项目代码。它不提供真实语义准确率、生产 EvidenceChecker、真实 Embedding、付费 API、Gold 成绩或 D 推理。

## Windows PowerShell 可重复步骤

在仓库根目录使用 Python 3.11，安装声明的普通依赖，不安装 embedding/API extras，也不下载模型：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev,web,eval]'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:SRE_INTEGRATION_PROFILE = 'offline_synthetic'
$env:SRE_OFFLINE_STATE = Join-Path $env:TEMP 'sre-abce-state-new-run'
$env:DYNAMIC_MEMORY_WEB_FACTORY = 'dynamic_memory.integration.offline:create_web_bindings'
.\.venv\Scripts\python.exe scripts/offline_integration.py prepare --state $env:SRE_OFFLINE_STATE
.\.venv\Scripts\python.exe scripts/offline_integration.py check --state $env:SRE_OFFLINE_STATE
$evalResults = Join-Path $env:TEMP 'sre-abce-eval-new-run'
.\.venv\Scripts\python.exe scripts/offline_integration.py evaluate --state $env:SRE_OFFLINE_STATE --results $evalResults
.\.venv\Scripts\python.exe scripts/run_web.py
```

`prepare` 只接受新的状态目录：如果源文件、发布指针或索引已经存在，它会拒绝覆盖；重复演练请选新的目录，不删除旧结果。状态目录须是仓库外的绝对路径。`check` 应对 `synthetic-0`、`synthetic-1` 各打印 `complete; full_text=answered; vector_rag=answered; synthetic`。Web 默认监听 `127.0.0.1:8501`；首页显示 Synthetic 警告，问答工作台只提供 Full Text / Vector RAG。要看独立 Demo，显式设 `SRE_INTEGRATION_PROFILE=demo` 并移除 `DYNAMIC_MEMORY_WEB_FACTORY`；没有配置不能自动进入 Demo。

`evaluate` 在 OS 临时目录内的新结果目录保存 `full_text.predictions.json`、`vector_rag.predictions.json`、`pure_vector_topk.predictions.json` 及 `non_d_summary.json`。摘要还包含 `question_count`、每个方法的 `answer_status_counts`（answered/no_evidence/time_uncertain/unresolved_conflict/error，包含零值）及 `error_code_counts`；统计从落盘后经过 SHA 校验的预测重新读取，不能把保存成功误认为问答成功。纯向量方法调用 C 的原始 Top-K，不把历史扩展候选当消融；没有 D，不生成三路正式比较。由于默认只为两个样本构建 Memory 和索引，main_dev 中其余两个样本的 Vector RAG 预测为明确错误，Full Text 仍可读其原始来源。预测必须先保存并用 SHA 回读校验，才允许独立评分路径读取**显式授权的** Gold；上述命令不读 Gold。

## 版本、资源与故障边界

- `prepare_source` 写合成原始 JSON 与冻结 split；B Loader 剥离 QA 字段，Memory Builder 的请求不接触 Gold。B 每样本 `current.json` 指向唯一完整发布；集成层每次通过 B strict `load()` 核查指针、manifest、字节哈希、快照、来源和配置，然后才产生 `VerifiedMemoryLoad(provenance='b_verified_loader')`。这个 provenance 表示加载途径可信，不表示内容是生产事实；manifest 的 backend 仍为 `mock`。
- `integration_receipt.json` 仅为只读状态诊断记录，不能证明盘面可信。成功发布后才写其 build ID；无指针为 `BUILD_NOT_FOUND`，已有完整发布后的失败尝试不覆盖旧指针。损坏/过期时 B strict loader 仍拒绝读取，状态诊断不会放行 Memory。
- C 离线从 B 当前完整产物建索引，索引绑定 source manifest、检索设置和固定 Encoder 身份。在线工厂只读索引；缺失、损坏或 B 重新发布后旧索引分别返回 `INDEX_NOT_FOUND`、`INDEX_CORRUPT`、`INDEX_STALE`。不会在 Web 请求中自动重建、编码文档或下载模型。若需重建，请使用一个新的安全状态目录完整准备；生产增量维护是后续阶段。
- 一次请求内 A 先检查 B 状态与 C readiness，C 在候选加载和回答前再次从 B strict load 与索引文件核验。**A 在生成答案后、返回前的最后检查点再次验证 B 发布身份和 C 索引状态及完整内容指纹**；索引丢失/损坏分别返回 INDEX_NOT_FOUND/INDEX_CORRUPT，合法但内容已替换的索引返回 INDEX_STALE，而不是返回成功答案。此处完整索引指纹与候选中用于检索设置的 `retriever_fingerprint` 不同，索引向量改变也会使前者变化。该检查点不等于跨进程原子事务或长期版本租约：检查完成后的物理更新不能凭现有接口锁定；C 的 ContextVar 只绑定同步请求，B/C 不提供跨进程发布事务。Web 的 `get_query_state` 包含当前索引状态/内容指纹，变化时清除会话旧答案。
- 无模型配置时正式 profile 不可用；本模块仅允许显式 `offline_synthetic`，不会回退到真实 Provider 或自动下载。`configs/models.yaml` 的 model_id 和 `configs/retrieval.yaml` 的 embedding_model 保持 null。真实生产接入需另行设计配置、证据审核与资源授权。

## 验证与 D 接入准备

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/integration/test_offline_system.py tests/web tests/contract
```

`tests/integration/test_offline_system.py` 从原始合成对话经过 B Builder 与发布、C 索引、A 服务到 E AppTest，另外验证缺/坏/旧索引、B 发布切换、两样本并发、旧答案清理、预测回执及 Fake D 的正反公共合同。Fake D 仅注入 `QueryOrchestrator(resolve(request,analysis,memories), answer_dynamic(request,analysis,resolution,memories))`；候选、Memory 和来源仍由 B/C/A 提供。默认工厂不注入 D，Dynamic API 返回 `COMPONENT_UNAVAILABLE`，页面不提供该方法。未来 D 交付时须核对真实签名、RETAIN/EXCLUDE/UNRESOLVED 与关系方向/来源语义、同次候选公平性、并发及错误路径；当前只表示端口准备，不表示 D 已兼容或正式三路完成。

GitHub Actions workflow 配置了同等 direct-main 合成链路。工作区未提交/推送之前，新增 workflow **尚未在远端执行**。
