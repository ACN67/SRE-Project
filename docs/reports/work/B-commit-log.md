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

## B-06｜来源复核、规范化、同会话去重和正式 ID｜2026-10-25

- Git Gate：独立 B worktree 的 `B` 分支，HEAD `01852aa` 已包含 B-05，原三份未跟踪审计报告保持原样；本轮未同步、合并或提交。新增 `src/dynamic_memory/memory/normalization.py`、`tests/unit/test_memory_normalization.py`，仅续写本工作记录。
- 入口 `normalize_memories(sample_id, results: Sequence[WindowExtractionResult], repository: SourceRepository, *, extractor_version, evidence_checker=None) -> list[MemoryRecord]` 可接收多个 B-05 窗口结果。先验证样本存在、草稿结构、同会话来源及 `get_turn(sample_id, SourceRef)` 的实际轮次，记录时间须来自被引用轮次；每条引用分别做证据判断。`EvidenceAssessment` 使用 `supported/unsupported/unclear` 和脱敏原因码；无检查器或无法判断时抛 `EVIDENCE_UNCLEAR`，不生成正式记录。少量词面规则只拦截明显不相关的实体/事件、可疑否定或计划、单一明确 ISO 日期矛盾，不声称严格语义蕴含；外部可信检查器仍需依据原文及 caption 元数据逐条复核。失败有稳定 `.code`、窗口 ID 和脱敏 `.reason_code`，不打印对话或 Gold。
- 对通过证据审核的草稿执行 Unicode NFC、首尾空白移除及连续空白压缩；保留 null/unknown、事实类别、断言性质、极性、记录时间和 `TimeSpan`，来源升序。仅在同一 session 且规范化内容、主体、属性、值、断言、极性和时间完全一致时合并重叠窗口事实，汇总真实来源；相近但不同的历史事件或冲突不合并。复用 `contracts.memory_id.compute_memory_id` 与 `validate_memory_id`，对规范化合并后的公共 `MemoryRecord` 生成 ID，检查碰撞；抽取版本按规范不进入 ID。
- TDD：先观察模块缺失，再补来源、证据三态、否定/计划/时间风险、跨样本、跨会话、重复来源、稳定排序、ID、空输入及真实 B-04→B-05 Mock→B-06 重叠窗口链路。`.venv\Scripts\python.exe -m pytest -q tests/unit/test_memory_normalization.py`：35 passed；相关 B/合同组合测试 100 passed；`.venv\Scripts\python.exe -m pytest -q`：487 passed；`.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke`：PASS；`git diff --check`：PASS。全部使用 Python 3.11 离线环境，无真实 API、Gold、公共 DTO 或其他成员业务代码改动。
- B-07 可直接调用上述入口并据 `MemoryNormalizationError` 记录窗口级失败；但完整真实数据构建仍需提供可信的语义证据检查器，并按 v2.2 在开发集抽样人工复核 30–50 条。没有该检查器时非空草稿会保守失败，不能把“引用存在”当成“事实已证实”。B-07 的批次汇总、partial_build 和产物发布均未在本轮实现；本轮未 `git add`、commit、push 或 merge。

## B-07：Memory 构建编排与失败诊断（2026-10-25）

- Git Gate：独立 B worktree，HEAD `01852aa`；B-06 本地未提交文件与三份既有未跟踪审计报告保持原样。未执行 add/commit/push/merge。
- 新增 `memory/build.py`：`MemoryBuilder` 注入 SourceRepository、RecordedLLMExecutor、配置、CallIdentity 工厂与可选 EvidenceChecker；两个入口为 `build_memories(sample)` 和 `build_memories_with_report(sample)`。真实 B-04→B-05→B-06 链路逐窗口运行；严格入口只在 complete 返回正式记录，partial/failed 分别报 `MEMORY_BUILD_INCOMPLETE`/`MEMORY_BUILD_FAILED`。
- 构建状态遵循窗口计数：全成功（含零窗口或合法零 facts）为 complete；有成功也有失败为 partial；全失败为 failed。规划或最终合并失败以阶段诊断表示，不将其误作合法零窗口。逐窗口记录来源、输入/接受/丢弃数量、脱敏错误码及原因码。一个窗口审核失败时该窗口所有草稿均不发布，其他成功窗口保留供诊断，不作为正式索引产物。
- B-06 局部兼容扩展 `merge_reviewed_memories`：按既有同会话事实键合并已经独立审核的窗口记录与来源，并复算/验证最终 Memory ID；未改原 `normalize_memories` 入口。默认额外预留 1800 的保守窗口 Prompt 预算，B-05 仍对完整请求再校验；预算超限显式记录失败，不截断。
- 无证据检查器、unclear/unsupported、检查异常均保守失败。仓库尚无可信生产 EvidenceChecker；仅以 synthetic Mock checker 证明链路。`contracts` 目前无公共 `MemoryBuildResult` 源码，B 模块暂用规范字段的内部不可变结果；A 发布公共 DTO 后需适配，不能将此内部类型直接视为跨模块已冻结接口。
- 新增 `tests/unit/test_memory_build.py`；覆盖完整 Mock 链路、partial、失败、零事实、零窗口、规划与请求预算、来源越界、审核拒绝/异常、跨会话隔离、重叠窗口去重、稳定 ID 与严格入口。专项 B-07+B-06：48 passed；完整离线 pytest：500 passed；Mock smoke：PASS；git diff --check：PASS。Python 为现有 `.venv` 的 3.11。无真实 API、Gold 或其他成员业务文件改动。
- B-08 交接：仅 complete 且 partial_build=false 可正式发布；生产审核器和公共构建结果 DTO 尚需就绪。B-08 JSONL/JSON/manifest、原子发布及恢复未在本轮实施。未发生 commit，无 SHA 可记录。

## B-08｜中断恢复、离线 Artifact 与原子发布｜2026-10-25

- 恢复现场：B worktree / B 分支，HEAD `01852aa`；B-06/B-07 未提交代码、工作记录及三份历史未跟踪审计报告均保留。中断前已新增 `memory/artifact_store.py` 与对应测试；恢复时无进行中的 git.exe、合并或 rebase，`.git/FETCH_HEAD` 为上次已完成 fetch 留下的常规文件。未执行 add/commit/push/merge/reset/clean。
- 远程检查：执行 `git ls-remote --heads origin`、`git fetch origin --prune`、`git branch -r -vv`，确认 `origin/A` 从 `7d240ed` 到 `b17893b6d1411982cb5b474a2d38e6b1426cf78f`，主要为 QA 与评测；`origin/main=c62bd03`、`origin/B=93cf9f5`、C/E 未更新。对已获取 A/B/C/E/main 的相关源码与合同检索，未发现公共 `MemoryBuildResult` 或可信生产 Evidence Checker，故继续 B-08 离线开发，未合并远程分支。
- 中断前已实现：B-08 内部 `MemoryArtifactStore` 生成稳定排序的 JSONL、JSON 和正式 `MemoryManifest` v1；绑定真实传入 DatasetBundle 快照、样本来源、提取配置、Prompt 与记录指纹；逐条复核 MemoryRecord Schema、ID、样本及 SourceRef；staging 写入并校验后移入唯一 build 目录，以原子 `current.json` 指针发布；严格加载校验指针、文件字节哈希、版本、状态、来源与 DTO 一致性。仅显式 `allow_mock_publication=True` 可发布离线 Mock complete，默认禁止 Mock 发布；partial/failed 不发布。
- 恢复后补齐：发布前重新按同一配置规划窗口，核对 B-07 报告中的全部窗口 ID、覆盖来源与 complete 计数，拒绝伪造的遗漏窗口；增加缺字段/错版本、重算哈希后的坏 Memory ID、指针损坏、staging 写入失败和指针切换失败的回归测试。写入或切换失败时旧 `current.json` 继续指向原有效产物；新文件只有切换后通过正式入口可见。
- `.venv` Python 3.11.15；B-08 专项 15 passed；B-06/B-07 相关 48 passed；完整离线 pytest 515 passed，无跳过；Mock smoke PASS；`git diff --check` PASS。B-07→B-08 synthetic Mock complete → MemoryArtifact → 严格加载往返通过。未调用真实 API 或读取 Gold。
- 生产交接限制：A 的公共 `MemoryBuildResult` 尚未落地，B-08 暂接 B-07 内部类型；可信生产 Evidence Checker 尚未实现，不能将 Mock 产物宣称为真实 LoCoMo 生产验收。当前 ArtifactStore 只支持显式离线 Mock 发布；B-09 可进行离线联调准备，正式索引发布须等待上述契约与审核能力。无本轮 commit SHA。

## B-09｜离线端到端联调与交付验收｜2026-10-25

- Git Gate：B worktree、B 分支，HEAD `01852aa`；B-06～B-08 本地未提交实现及三份历史未跟踪审计报告原样保留。未执行 add/commit/push/merge/rebase/reset/clean。
- 本轮新增 `tests/integration/test_memory_pipeline_offline.py` 与 `docs/reports/work/B-09-offline-handoff.md`。测试在临时目录生成明确标注的 LoCoMo 形状 synthetic 十样本文件，实际调用 B-01 快照检查与冻结划分、B-02 loader、B-03 来源仓库、B-04 分窗、B-05 共享 Mock 请求与结构验证、B-06 审核/去重/ID、B-07 complete/partial/failed、B-08 JSONL/JSON/Manifest 发布与严格回读。仅模型响应和语义证据判断使用固定 Mock；Mock Checker 只接受与选定 synthetic 来源原文及 SourceRef 完全相符的事实。
- 三条集成路径：完整成功并再次构建验证稳定 ID/Memory 集合指纹，部分窗口失败且不能覆盖旧产物，无证据检查器导致全部失败且不能发布。B 侧 `MemoryArtifact.require_complete_for_index()` 通过；C 当前 retrieval 目录仅占位，无可调用 `build_index`，正式 B→C 联调未完成。Gold 哨兵未进入 Prompt 或 Artifact；未访问真实 DeepSeek/API。
- 当前 B 仓库 `data/raw/` 仅 `.gitkeep`，用户确认本机没有冻结的真实 LoCoMo 原始文件。故真实 LoCoMo 端到端链路未运行，不能称真实数据离线 PASS。远程分支无需重复检查；公共 `MemoryBuildResult` 和可信生产 Evidence Checker 仍未就绪，B-08 只允许显式 Mock 离线发布。交接报告记录字段映射、责任和最小剩余任务。
- Python 3.11 `.venv`；B-09 专项 `3 passed`；完整离线回归 `518 passed`，无跳过；Mock smoke PASS；`git diff --check` PASS。B-09 synthetic＋Mock 路径通过，真实数据与生产交接均为 PARTIAL。B-06～B-09 保留本地，等待统一人工审查/提交；本轮无 commit SHA。

## 冻结 LoCoMo 原始数据恢复与只读验收｜2026-10-25

- 数据责任依据：`TECHNICAL_SPEC.md` B 模块规定 LoCoMo 规范化、来源回查及真实开发样本验收；`README.md` 禁止原始数据进公开仓库。B-01 工作记录锁定官方 `snap-research/locomo` 提交 `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376` 的 `data/locomo10.json`，原始长度 2,805,274 字节、SHA-256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`；同提交 `LICENSE.txt` 为 CC BY-NC 4.0。无需其他成员提供文件。
- B 分支 HEAD `01852aa`，B-06～B-09 未提交代码及三份审计报告保持原样。最初 `data/raw/` 仅 `.gitkeep`，`git check-ignore` 确认目标受 `.gitignore` 保护。从固定提交的官方 raw URL 下载到 `data/raw/` 下隔离临时目录；先核字节数、SHA、十个原始 sample_id、B-01 `inspect_snapshot` 与 B-02 `load_conversations` 对冻结 split 的验证，全部一致后无覆盖地移入 `data/raw/locomo10.json`。实际大小、SHA 与冻结值完全一致；未修改 split、源码或测试期望，未将原始数据加入 Git。
- 真实 B-01～B-04 只读验收：10 样本，4/4/2 划分，272 sessions、5882 turns、1226 captions，业务 Bundle 无 QA/Gold/摘要字段；四个 main_dev 样本 `conv-26/30/41/42` 来源回查与分窗全覆盖均 PASS，窗口数分别为 83/63/123/108，无跨会话窗口。
- 真实数据＋Mock 小规模链路：选 main_dev `conv-26`，对真实 83 窗口构造固定 Mock 响应；仅一条草稿引用预先核对的可见原始轮次，Mock Checker 仅在原文与 SourceRef 精确一致时支持，其余窗口合法零事实。B-05～B-07 得到 complete、83/83 成功、1 条正式 Memory；B-08 在临时目录发布 JSONL/JSON/Manifest 并严格回读，文件摘要一致、来源引用存在。没有用 QA Gold、真实 API 或生产审核器。
- 数据相关 B-01～B-04 与 B-09 测试 84 passed；完整离线回归 518 passed；`git diff --check` 通过。生产公共 `MemoryBuildResult`、可信 Evidence Checker 及 C 正式索引入口仍待完成。修改了现有 `B-09-offline-handoff.md` 的过时“数据缺失”状态；本轮没有业务代码、commit、push 或 merge。

## B-06～B-09 集中提交前验收｜2026-10-31

- Git Gate：B 分支 HEAD `01852aa`，保留 B-06～B-09、本工作记录、三份历史审计报告及 Git 忽略的 `data/raw/locomo10.json`；仅执行 `git fetch origin --prune`，未 pull/merge/rebase/reset/clean/add/commit/push。远端 A/C/E 有更新；公共 `MemoryBuildResult` 仍未在 A/contracts 落地，`origin/C=d4bff06` 已实现接收公共 `MemoryArtifact` 的索引入口，但尚未与 B 未提交产物跨分支联调。
- 审查修复两个 MAJOR。其一，B-08 原先固定使用 1800 重算窗口和生成配置指纹，可能与 B-07 可注入的 `reserved_prompt_tokens` 不一致；现由 `MemoryArtifactStore` 显式接收、校验并统一用于窗口覆盖复核和 manifest 配置身份，不同预算报告被拒绝。其二，跨窗口最终合并/Memory ID 校验失败原先追加伪 finalization 窗口，令窗口总数多 1 且可能误标 partial；现保持真实窗口数，将原成功窗口改记为 finalization failed，构建结果为 failed 且不保留 Memory。两项均先补失败回归再最小修复。
- Evidence Checker 缺失/unclear/unsupported/异常继续 fail-closed；严格 `build_memories` 拒绝 partial/failed；重叠窗口仅同会话、同规范事实合并来源并复算 ID；B-08 complete 门槛、哈希/来源/ID 回读、staging 与原子指针旧版本保护保持通过。B-05 的 128 token 安全余量仍是 MINOR：完整请求会估算并预留，但尚未用真实 provider tokenizer 校准，也未配置化；本轮不扩大范围。
- 专项 B-06～B-09 68 passed；完整离线回归 520 passed；Mock smoke PASS；真实冻结 SHA 校验及 `conv-26` 的 83 窗口固定 Mock complete→Artifact 发布回读 PASS；`git diff --check` PASS。没有发现 Gold、API Key 或原始对话进入待提交代码/日志；原始数据和 `.venv` 均被 Git 忽略。本轮无 commit SHA。
