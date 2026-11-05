# SRE-Project：智能体动态记忆管理系统

课程项目依据根目录 [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) v2.2 开发。A/B/C/E 已有明确的离线合成集成入口；D 的模型驱动状态解析与证据约束问答端口也已接入生产组合根。正式模型质量、多样本数据及独立验收仍属后续阶段。

课程最终汇报以 `main-2` 为主线：事实抽取、事实审核和动态状态解析使用
确定性算法，DeepSeek 仅生成最终自然语言答案。历史 DeepSeek 抽取、审核和
状态路线保留为对照。完整架构、实验数据、复现身份与限制见
[main-2 最终报告](docs/MAIN2_FINAL_REPORT.md) 和
[机器可读指标汇总](docs/main2/metrics.json)。

## 当前已实现

- Python 3.11 / Pydantic v2 公共 DTO、B 的来源加载与严格 Memory 产物、C 的索引与检索、A 的统一服务/Full Text/Vector RAG、E 的 Web 消费者。
- `offline_synthetic` 从合成原始对话经 B Builder、严格发布、C 索引到 A/E 的可重复入口；固定向量、Mock QA 和合成证据检查器有明确标记。
- 预测先落盘并校验的非 D 诊断运行；D 的 `StateResolver`、`DynamicAnswerer`、`DynamicServicePorts` 与生产工厂已接通，Dynamic 和 Vector RAG 使用同一有序 CandidateSet。
- 正式 YAML 中 `null` 仍表示尚未冻结，不会被离线 profile 覆盖成虚构的生产值。
- `main-2` 提供确定性事实抽取、自动审核及状态解析，抽取、审核和状态阶段不调用远端模型；最终自然语言答案仍使用 QA 模型。流程、命令和限制见 [自动抽取与状态解析](docs/AUTO_EXTRACTION.md)。DeepSeek 抽取/语义审核仍作为历史正式路径保留，见[自动事实审核](docs/AUTO_REVIEW.md)及[语义审核](docs/SEMANTIC_REVIEW.md)。

历史 `main` 路线曾在四段开发对话上完成真实 DeepSeek 抽取、v3 语义审核、
严格 Memory 发布、BGE 索引、A/B/C/D/E 生产装配和全部 757 题三路问答；
其 Full Text、Vector RAG、Dynamic 综合得分分别为 `0.733758`、`0.601955`、
`0.580745`。这些结果和旧状态标注保留为对照，配置身份见
[main_dev 历史实验报告](docs/STAGE6_MAIN_DEV_FINAL.md)。历史单样本报告见
[conv-26](docs/STAGE6_CONV26.md)、[固定四题诊断](docs/STAGE6_DEV_THREE_METHOD.md)
和 [conv-41/42](docs/STAGE6_CONV41_CONV42.md)。D 的接口与边界见
[D 交付说明](docs/D-delivery.md)。Mock 输出不能作为正式实验结果。
公开 `offline_integration.py prepare` 默认准备冻结 `main_dev` 的四个合成样本，
使随后 `check/evaluate` 对同四题运行；完整命令见
[A/B/C/E 离线集成说明](docs/ABCE_OFFLINE_INTEGRATION.md)。正式
[SRS](docs/references/Milestone3.md) 与 [逐项需求追踪](docs/SRS_ABCE_TRACE.md)
分别记录需求正文和实证边界。

`main-2` 的确定性抽取、审核和状态解析路径已在四个开发样本和两个独立测试
样本上重建全部 Memory 和索引，三个自动阶段的 HTTP 调用均为 0。开发集
757 题的 Evidence Recall@5/@10 为 `0.560606/0.647230`；正式测试 400 题的
Evidence Recall@5/@10 为 `0.602469/0.672944`。动态状态标注已按新候选集
重新映射，报告同时给出候选覆盖率与条件准确率，不把旧候选集上的分数直接
迁移为正式结论。

## 分工

| 成员 | 责任 |
| --- | --- |
| A 常珂舒 | 统一工程、基线、评测、运行环境和接口集成 |
| B 吴宇轩 | LoCoMo 数据规范化、Memory 抽取与来源校验 |
| C 郭柏村 | 查询分析、检索索引、候选及 Vector RAG |
| D 杨林涛 | 动态状态解析、证据约束问答 |
| E 刘星宇 | Web、Memory 浏览与解释性展示 |

## Git 分支与团队协作

`main` 是稳定的集成分支。五位成员各自从最新 `main` 建立个人分支，独立开发负责模块；个人真实提交历史和贡献应可追溯。各成员按照下列分支命名独立开发，具体已创建及已合并的分支状态以 GitHub 远端为准。

| 成员 | 代号 | 个人开发分支（精确名称） | 负责模块 |
| --- | --- | --- | --- |
| 常珂舒 | A | A | 统一运行、LLM Adapter、Full Text 基线、评测及系统集成 |
| 吴宇轩 | B | B | LoCoMo 数据处理、Memory 构建、来源和 JSON 导出 |
| 郭柏村 | C | C | 查询分析、检索与 Vector RAG |
| 杨林涛 | D | D | 动态状态关系、有效记忆选择与 QA |
| 刘星宇 | E | E | Streamlit Web、记忆与证据可视化 |

人工成员可在各自分支按开发节奏 commit、push。完成基本测试后，通过 Pull Request 申请合并到 `main`，由组长检查结果，避免直接向 `main` 推送；开发期间可定期同步最新 `main` 并解决接口兼容问题。通过测试且接口兼容的模块可逐步合并，不必等所有模块完成；未通过合同测试的代码不能直接进入 `main`。

由 Codex 执行的工作沿用独立审查流程：Codex 只修改、测试，可以 `git add`，不得 commit 或 push；ChatGPT 审查后需用户明确确认，再由 ChatGPT 提交推送。这不限制人工成员在个人分支保留正常提交历史。

[TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) v2.2 是当前技术执行依据；公共契约唯一位置为 `src/dynamic_memory/contracts/`。跨模块接口或 Schema 需要调整时，先通知相关成员，并同步更新代码、[契约说明](docs/contracts.md)、fixtures 和测试。

## 参考资料

- [Milestone1.md](docs/references/Milestone1.md)：本组已有的 Team Workflow、成员分工、会议、Git 协作与 Milestone 计划文档。
- [软需工.pdf](docs/references/软需工.pdf)：课程原始项目与交付要求的依据。
- [指导文档.md](docs/references/指导文档.md)：早期 v1.1 指导文档，仅供背景与沿革参考。

正式实施方案以 [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) v2.2 为准，旧指导材料的技术细节不能覆盖冻结规范。课程 PDF 的 Version Control 页建议使用 private GitHub repository，而当前仓库是 Public；组长需确认课程是否允许公开仓库，以及课程资料是否获准公开发布。

## 安装与验证

在仓库根目录、Python 3.11 环境中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev,web,eval]'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke
```

测试默认使用 6 个 xdist 进程；main-2 最终热运行全量回归实测为
`1385 passed / 28.44s`，满足两分钟内完成的目标。各测试文件中仍保留真实验证 Streamlit 状态机、
进程锁和持久化边界的冒烟测试。

离线集成测试均为 synthetic，Mock 答案始终标明合成性质，不代表正式准确率。`tests/integration/test_offline_system.py` 从合成原始对话真实调用 B 的 Builder 和 C 的 IndexManager；`test_abce_long_history.py` 验证 20 条合成 Memory 的 Top-8、历史扩展、12 条预算以及 A/评测/Web 消费。

## 配置读取

C 模块的阶段交付、统一检查命令、接口清单和未完成的真实验收条件见 [C 交付入口](docs/C-delivery.md)。

C 模块已提供独立的本地 Embedding 接口（将 Memory 和问题转换成向量），安装、示例和验证步骤见 [Embedding 使用说明](docs/embedding.md)。Vector RAG 回答端口已实现并通过合成测试，正式 Embedding 模型尚未冻结。

C 模块另提供无需模型或 API 的规则版查询分析，识别常见查询意图、实体/属性提示及时间条件；示例与限制见 [查询分析使用说明](docs/query-analysis.md)。

C 的索引构建、保存与加载遵守 v2.2 `MemoryArtifact` 契约，详见 [记忆索引使用说明](docs/retrieval-index.md)。基础 Top-8、最多 12 条历史扩展、共享候选快照和按 rank 加载已实现，详见 [向量检索与历史候选扩展](docs/vector-retrieval.md)。当前 main 的离线组合根已接 B 严格加载与 A 服务；正式 B 生产构建、真实 Embedding 和 D 推理仍待后续验收。

`dynamic_memory.pipeline.settings.load_settings(config_dir=None)` 会从仓库的 `configs/` 读取并校验六份 YAML，也可传入自定义目录。它返回类型化的 `AppSettings`，不会读取 API Key 或发起模型请求。例如，在已安装项目的环境中运行：

```powershell
.\.venv\Scripts\python.exe -c "from dynamic_memory.pipeline.settings import load_settings; s = load_settings(); print(s.models.default_backend, s.retrieval.base_top_k)"
```

当前输出为 `mock 8`。缺失文件、格式或字段错误会抛出带文件名和字段名的 `SettingsError`。

### 真实多样本 Web

```powershell
$env:DYNAMIC_MEMORY_WEB_FACTORY = 'dynamic_memory.integration.production:create_web_bindings'
$env:SRE_PRODUCTION_ARTIFACT_ROOT = 'C:\path\to\auto_struct_delivery_artifacts'
$env:SRE_PRODUCTION_INDEX_ROOT = 'C:\path\to\auto_struct_delivery_indexes'
$env:SRE_PRODUCTION_MODEL_ROOT = 'C:\path\to\bge-small-en-v1.5'
$env:SRE_PRODUCTION_MODEL_REVISION = '5c38ec7c405ec4b44b94cc5a9bb96e735b38267a'
$env:SRE_PRODUCTION_SAMPLE_IDS = 'conv-26,conv-30,conv-41,conv-42'
$env:SRE_PRODUCTION_ALLOWED_SPLITS = 'main_dev'
python scripts/run_web.py
```

生产工厂启动时按 `SRE_PRODUCTION_SAMPLE_IDS` 的顺序装配每个样本的已审核
Memory 和真实索引，Web 可在同一实例中切换这些样本。启动过程只读，不自动
调用模型；用户提交 Full Text、Vector RAG 或 Dynamic 问题后，才可能产生
模型请求。旧的 `SRE_PRODUCTION_INDEX_PATH` 与 `SRE_PRODUCTION_SAMPLE_ID`
组合仍可用于单样本启动。

## 后续工作和安全边界

各成员按技术规范实现各自模块，并在开发集验证后冻结模型、Embedding、数据快照和评分程序。原始数据、Gold、API Key、权重、缓存及结果文件不应提交到公开仓库。`.env.example` 仅列环境变量名；真实 API 必须经过显式配置。本仓库未附许可证，不应由公开可见推断使用授权。
