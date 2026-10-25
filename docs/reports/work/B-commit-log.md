# B 工作记录

## B-01｜数据快照校验与固定划分｜2026-10-19

- 分支：独立 `SRE-Project-B` worktree 的 `B`，从 `origin/main` `c967293f2e54b4ce34ae3460efc4fc2e0394139d` 建立；原 A 目录未编辑或切换。
- 官方来源：`https://github.com/snap-research/locomo`，`main` 提交 `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`，`data/locomo10.json`。原始文件保存在忽略目录 `data/raw/`，2,805,274 字节，SHA-256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`。同一提交的 `LICENSE.txt` 为 CC BY-NC 4.0；不把原始数据放入 Git。
- 原始顺序 ID：`conv-26, conv-30, conv-41, conv-42, conv-43, conv-44, conv-47, conv-48, conv-49, conv-50`。依 1-based 固定顺序冻结 `configs/data_split.yaml`：`main_dev` 前 4 条、`extra_dev` 中间 4 条、`test` 最后 2 条。
- `data.snapshot` 提供字节 SHA、最小结构检查、只读划分验证、默认预览和显式原子冻结、只读 CLI。错误码包含 `DATA_FILE_NOT_FOUND`、`DATA_FORMAT_INVALID`、`DUPLICATE_SAMPLE_ID`、`SPLIT_UNFROZEN`、`DATA_SNAPSHOT_MISMATCH`、`SPLIT_MISMATCH`。不输出对话或 Gold。
- 专项 synthetic 快照测试：27 通过；修正 `tests/unit/test_settings.py` 中三个过时的 null 断言，改为核验冻结 SHA、真实 sample ID 及三组映射，关联专项测试共 45 通过。完整离线测试：311 通过、0 失败。Mock smoke 通过。真实快照只读 CLI 验证通过。
- 后续：B-02/B-03 待重新授权；第一批合并节点仍为 B-03，B-01 不单独合并 `main`。

## B-02｜会话标准化为公共容器｜2026-10-19

- Gate：独立 B worktree 干净，HEAD 与 `origin/B` 同为 `1a0f49145ad930254b1cf43a5a9ab7c63c098387`；A-10 的 `ConversationSession`、`ConversationSample`、`DatasetBundle` 从 B 虚拟环境的公共 `dynamic_memory.contracts` 成功导入。
- 新增 `data/loader.py` 的 `load_conversations(path, split_path) -> DatasetBundle` 和纯内存 `_parse_sample`。正式入口复用 B-01 `inspect_snapshot` 与 `validate_split`，再次核对原始字节 SHA；读取配置后按固定划分生成实际 ID 映射。原始 session 与 turn 保序，孤立时间键不生成会话，英文及带偏移 ISO 日期严格解析，缺失或 unknown 为 null；只传 SourceTurn 白名单字段，caption 保持来源标记，错误码和消息脱敏。收尾修复补齐 `inspect_snapshot` 与 `_parse_json` 的深层 JSON 递归错误处理，将 `RecursionError` 稳定转换为 `DATA_FORMAT_INVALID`，不回显原始输入。
- 公开入口从 `data/__init__.py` 导出；新增 `tests/unit/test_data_loader.py`，仅使用 synthetic fixture。专项 24 passed（含 2 项深层 JSON 错误码和脱敏回归），完整 pytest 361 passed，Mock smoke PASS；所有命令退出码 0。真实忽略文件只读核验：SHA `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`，10 样本、272 实际 session、5882 turns、1226 caption、5882 个唯一来源三元键、16 个孤立日期键；划分 4/4/2。
- 未修改公共 DTO、冻结配置、A/C/D/E 模块；未读取 Gold 字段值、下载图片、调用模型/API 或将原始文件加入 Git。B-03 来源仓库与回查待重新授权；B-02 经 ChatGPT 独立验收、补齐边界测试后统一提交并推送至 B 分支，第一批 B→main 合并仍在 B-03 完成后单独授权。

## B-03｜SourceRepository 与来源回查｜2026-10-20

- Gate：B worktree 及暂存区初始干净，`B` / `origin/B` HEAD `8c39bc9c9651ea9606087ff5351eeefffb4520c4`；公共 DTO 和 B-02 loader 从 B `.venv` 导入，无现成 Repository。
- 新增 `SourceRepository(bundle: DatasetBundle | None = None)`，提供 `list_samples() -> list[str]`、`get_sample(sample_id: str) -> ConversationSample`、`get_turn(sample_id: str, ref: SourceRef) -> SourceTurn`。构造时独立检查嵌套类型、版本及父子身份、重复 sample/session/turn，并通过普通字段重新验证 `model_copy(update=...)` 绕过的约束；内部持有深拷贝和三元键索引，读取返回防御性副本。收尾修正将 `source_order_1_based` 校验限定为正整数且在当前 Bundle 中不重复，不再要求从 1 连续编号；子集可保留原始序号（例如 9，或 5、6）及样本列表顺序。`None` 为未加载态，合法空 Bundle 返回空列表；构造失败不发布半成品索引。
- `SourceRepositoryError.code` 稳定区分 `DATA_NOT_LOADED`、`DATA_FORMAT_INVALID`、`DUPLICATE_SOURCE_REF`、`SAMPLE_NOT_FOUND`、`SOURCE_NOT_FOUND`、`SAMPLE_MISMATCH`。仅外样本存在的 pair 报 `SAMPLE_MISMATCH`；两个样本均有同名 pair 时以显式 sample_id 限定范围，无法据 SourceRef 推断来源者意图。错误消息不包含原文、Gold 或未审查输入。
- synthetic 合同测试 17 passed（含非连续源序号与非法/重复序号回归），完整 pytest 378 passed，Mock smoke PASS，`git diff --check` 通过，退出码均为 0。真实忽略数据只读 smoke：SHA `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`、10 样本、272 sessions、5882 turns、1226 captions、5882 个唯一三元键、4/4/2 分组；选取首、中、末三条实际 SourceRef 回查均与来源 DTO 相等，仅输出布尔值。
- 未修改公共 DTO、B-01/B-02、冻结配置及其他组模块；无网络、模型/API、Gold 或图片访问。经独立审查修复并统一提交 B-03 到 B 分支，提交信息：`B03：实现来源仓库与轮次回查`；第一批 B→main 合并仍待单独授权，本次不合并 main。

## B-04｜Memory 抽取分窗规划｜2026-10-23

- Git Gate：本 worktree 位于 `B`，同步前 HEAD `863910c97bd3450b53749ad0ed8e2de12cda220a`，仅有三份此前未跟踪的 B 审计报告；`git fetch origin --prune` 后 `origin/B=1b8aa37d7c8820471cb637c74afd0a6082b49335`，确认祖先关系及无路径碰撞，`git pull --ff-only origin B` 成功，现 HEAD 与 `origin/B` 同为 `1b8aa37`。同步后 `origin/main=c62bd03` 仅多过程资料提交；`origin/A=7d240ed` 尚多查询条件入口及合并提交，未发现 B-04 输入/预算依赖变化。本轮未合并 main/A。
- 新增 `src/dynamic_memory/memory/windows.py`：内部 `WindowTurn`、`ExtractionWindow`、`WindowPlanningError` 和 `plan_extraction_windows(sample, settings, ...)`；逐 session 贪心分窗、默认按配置 4096 输入预算和 2 条重叠，统一渲染 caption、保留来源/顺序，按序列化输入与预算/估算配置生成稳定窗口 ID。默认以 UTF-8 字节数作保守输入估算，预留 512 tokens 给尚未最终确定的 Prompt；可注入有版本号的估算器。超长单轮、重叠无法推进、预算/估算异常显式报脱敏错误。没有模型调用、Memory 抽取或产物构建。
- 新增 `tests/unit/test_memory_windows.py`，仅使用 synthetic 公共 DTO 和本地配置。TDD：先观察模块缺失及长 session 分窗测试失败，再逐步实现；最终专项 `13 passed`。完整 `python -B -m pytest -q -p no:cacheprovider` 在系统 Python 3.13 因缺少可选 `streamlit` 于 2 个 Web 文件收集失败；非 Web 测试为 `403 passed, 2 failed`，失败是 B-02 深层 JSON 的 2 项旧测试在此解释器下未触发预期递归错误/先报快照不符，与本次文件无关。排除这两项后非 Web 回归 `404 passed, 2 deselected`；`python -B -m dynamic_memory.pipeline.mock_smoke` 输出 `synthetic mock smoke: PASS`；`.venv` 不存在，未安装依赖。`git diff --check` 及对新增文件的单独空白检查通过。
- 尚未验证真实 API Token 数；B-05 必须对完整 system/user Prompt 和实际请求再次做预算校验。B-04 本轮无 commit、push、PR 或 main/A 合并；等待人工验收及提交授权。

### B-04-R1｜测试环境与最终验收｜2026-10-23

- 工作区仍为独立 `B` worktree，HEAD 与 `origin/B` 均为 `1b8aa37`；B-04 两个新增文件仍未提交，三份既有未跟踪审计报告未改动，无其他成员新增的未提交改动。
- 先前测试误用系统 Python 3.13.7，且当时无 `.venv` 和 `streamlit`，导致 Web 测试收集失败。两项 B-02 深层 JSON 测试在 3.13 下分别未触发 `RecursionError`、提前报 `DATA_SNAPSHOT_MISMATCH`；它们位于既有 `tests/unit/test_data_loader.py`，先于 B-04 存在。`uv` 已有 Python 3.11.15，按 `pyproject.toml` 已声明的 `dev`、`web` extras 在本 worktree 新建忽略的 `.venv` 并安装依赖；未改依赖声明或全局环境。
- `.venv\Scripts\python.exe -m pytest -q tests/unit/test_data_loader.py -k deeply_nested_json`：2 passed、22 deselected（仅用于定位）；`.venv\Scripts\python.exe -m pytest -q tests/unit/test_memory_windows.py`：13 passed；`.venv\Scripts\python.exe -m pytest -q tests/unit/test_data_snapshot.py tests/unit/test_data_loader.py tests/contract/test_source_repository.py tests/contract/test_dataset_containers.py`：94 passed；`.venv\Scripts\python.exe -m pytest -q`：424 passed，包含 Web 测试，无排除或跳过；`.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke`：PASS；`git diff --check`：PASS，命令退出码均为 0。
- 复核 B-04：来源轮次逐 session 保序覆盖，默认重叠 2 条且无法推进时报错；超长单轮、预算或估算异常均显式脱敏报错；窗口 ID 从内容和分窗参数确定；序列化包含统一 caption 可见文本，预留 Prompt 预算。无需修改 B-04 代码。此轮未读取 Gold、调用模型或真实 API，也未修改公共契约或其他成员业务代码。
- 最终验收：在 README 指定的 Python 3.11 环境中 PASS，具备人工提交 B-04 的条件；系统 Python 3.13 下既有深层 JSON 测试的兼容性差异仍存在，未将其算作 3.11 回归失败。B-05 可在另行授权后启动，且须对最终完整 LLM 请求复核实际 Token 预算。本轮未 commit、push、PR 或合并。

## B-05｜原子事实草稿抽取与共享 LLM 接入｜2026-10-23

- Git Gate：独立 `B` worktree，HEAD `93cf9f50404486b434b2642a12016333216c1142`（本地 B-04 提交），暂存区为空；原有三份未跟踪审计报告保留原样。未同步或合并其他分支。
- 新增 `src/dynamic_memory/memory/extraction.py` 和 `tests/unit/test_memory_extraction.py`。入口 `extract_window(window, executor, identity, models_settings, ...) -> WindowExtractionResult` 接受 B-04 窗口、A 的 `RecordedLLMExecutor` 与 `CallIdentity`；B 自有版本化 Prompt 使用 A 的 `PromptTemplate/render_prompt/build_prompt_request` 组装请求，草稿 Schema 为内部 `AtomicFactDraftBatch/AtomicFactDraft`（版本 1，与公共 DTO Schema 2.1 不同）。字段要求 `content/entity/attribute/value/fact_kind/assertion_type/polarity/recorded_at/event_time/source_refs`；允许合法空 `facts` 和规定的 null/unknown，拒绝缺字段、多余字段、非法枚举及窗口外来源。`recorded_at` 若给出，须匹配至少一条被引用轮次的记录时间。这里只返回草稿，不生成 `MemoryRecord` 或最终 ID。
- 使用共享 `validate_structured_output`、Guarded 重试/并发及记录执行器的校验回调；Schema/来源失败发生在成功缓存写入前，不增加 B 层重试。失败附稳定错误码和窗口 ID，异常不含原文。默认完整请求预算按 system/user 消息的 UTF-8 字节加消息开销估算，另留 128 token 安全余量；也可注入估算器，未知或无效估算直接拒绝。此为离线保守估算，未测真实 API tokenizer。正式 Prompt 比 B-04 默认预留的 512 更长，近上限窗口会在请求前报 `INPUT_BUDGET_EXCEEDED`；后续构建时可在 B 内以更大 Prompt 预留重新规划窗口，不截断来源或擅改统一 4096 配置。
- TDD：先观察入口不存在，再逐项增加事实类别、空结果、来源、Schema、完整预算、缓存、重试和多窗口测试；自检时修复注入估算器返回 0 被放行，以及多来源不同记录日期被误拒的边界。`.venv\Scripts\python.exe -m pytest -q tests/unit/test_memory_extraction.py`：28 passed；相关 B-04/LLM/合同组合测试 170 passed；`.venv\Scripts\python.exe -m pytest -q`：452 passed；`.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke`：PASS；`git diff --check`：PASS。无真实网络/API、Gold 读取或公共 DTO 修改。
- B-06 交接：输入为经过结构与窗口来源检查的 `WindowExtractionResult.facts`；仍须逐条核对原文语义支持、规范化、去重及最终 Memory ID。B-07 才汇总失败窗口及 partial_build；B-08 才发布产物。此轮未 commit、push、merge 或创建 PR。
