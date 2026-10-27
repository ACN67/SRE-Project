# SRE-Project：智能体动态记忆管理系统

课程项目的工程初始化，依据根目录 [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) v2.2。目标是从 LoCoMo 长期对话构建有来源的结构化 Memory，比较 Full Text、Vector RAG、Dynamic 三路问答，并在后续完成 Web 展示和正式评测。

## 当前已实现

- Python 3.11 / Pydantic v2 公共 DTO、部分结构校验、确定性 Memory ID 工具。
- 无网络 synthetic Mock 入口及合同测试；不读取数据、不调用收费 API。
- 已集成 A-05～A-09 的结构化 JSON 校验、Prompt 模板、模型调用控制、审计缓存与共享 LLM Runtime，并通过离线测试；新增 v2.2 业务接口尚待实现。
- 冻结规范副本、配置初值和模块目录。配置中的 `null` 表示尚未冻结。

真实 DeepSeek 抽取、动态状态推理、完整 Streamlit Web、正式 LoCoMo 评分均未实现；C 的本地 Embedding 检索与历史候选扩展已提供独立实现，真实流水线接入仍待联调。Mock 输出不能作为实验结果。

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
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke
```

测试 fixture 全部为 synthetic。Mock 只证明公共对象能传递和校验，不生成正式答案。

## 配置读取

C 模块已提供独立的本地 Embedding 接口（将 Memory 和问题转换成向量），安装、示例和验证步骤见 [Embedding 使用说明](docs/embedding.md)。Vector RAG 回答端口已实现并通过合成测试，正式 Embedding 模型尚未冻结。

C 模块另提供无需模型或 API 的规则版查询分析，识别常见查询意图、实体/属性提示及时间条件；示例与限制见 [查询分析使用说明](docs/query-analysis.md)。

C 的索引构建、保存与加载已实现，正式输入遵守 v2.2 `MemoryArtifact` 契约，详见 [记忆索引使用说明](docs/retrieval-index.md)。基础 Top-8、最多 12 条历史扩展、共享候选快照和按 rank 加载已实现，详见 [向量检索与历史候选扩展](docs/vector-retrieval.md)；Vector RAG 回答已提供共享 Runtime 接口，见 [Vector RAG 回答说明](docs/vector-rag.md)；A 调度器的合成接线验证已通过，见 [C 接线与联调交接](docs/C-integration-handoff.md)；真实 B/D 链路和三路回答格式统一仍待完成。

`dynamic_memory.pipeline.settings.load_settings(config_dir=None)` 会从仓库的 `configs/` 读取并校验六份 YAML，也可传入自定义目录。它返回类型化的 `AppSettings`，不会读取 API Key 或发起模型请求。例如，在已安装项目的环境中运行：

```powershell
.\.venv\Scripts\python.exe -c "from dynamic_memory.pipeline.settings import load_settings; s = load_settings(); print(s.models.default_backend, s.retrieval.base_top_k)"
```

当前输出为 `mock 8`。缺失文件、格式或字段错误会抛出带文件名和字段名的 `SettingsError`。

## 后续工作和安全边界

各成员按技术规范实现各自模块，并在开发集验证后冻结模型、Embedding、数据快照和评分程序。原始数据、Gold、API Key、权重、缓存及结果文件不应提交到公开仓库。`.env.example` 仅列环境变量名；真实 API 必须经过显式配置。本仓库未附许可证，不应由公开可见推断使用授权。
