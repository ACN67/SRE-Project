# B-09 离线联调与 B→C 交接（2026-10-25）

## 状态与复现

**离线 synthetic＋Mock 链路已通过。** 2026-10-25 从官方固定提交恢复了 `data/raw/locomo10.json`，2,805,274 字节，SHA-256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`，与 `configs/data_split.yaml` 一致。真实数据加载、来源回查、分窗及一个 main_dev 样本的固定 Mock 构建发布与回读通过；这仍不是生产 LLM/语义审核验收。

运行：`.venv\Scripts\python.exe -m pytest -q tests/integration/test_memory_pipeline_offline.py`，再运行 `.venv\Scripts\python.exe -m pytest -q`、`.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke`、`git diff --check`。集成测试只在 pytest 临时目录创建 LoCoMo 形状的十样本快照并冻结临时划分；Gold 哨兵字段会被 B-02 剥离，既不进入请求也不进入产物。外部模型响应和证据判断由固定 Mock 提供；数据加载、来源仓库、窗口、请求校验、规范化、构建状态、文件发布和加载均调用真实 B 代码。Mock 审核只批准与 synthetic 来源原文及 SourceRef 完全相符的草稿，**不是生产语义审核**。

## 已有接口与产物

| 阶段 | 实际可调用接口 |
|---|---|
| B-01/B-02 | `inspect_snapshot(path)`；`load_conversations(path, split_path) -> DatasetBundle` |
| B-03 | `SourceRepository(bundle)` 的 `get_sample`、`get_turn` |
| B-04/B-05 | `plan_extraction_windows(sample, settings)`；`extract_window(window, executor, identity, models_settings)` |
| B-06/B-07 | `normalize_memories(...)`；`MemoryBuilder(...).build_memories_with_report(sample)` 与严格 `build_memories(sample)` |
| B-08 | `MemoryArtifactStore(root, bundle, extraction_settings, models_settings, allow_mock_publication=True)` 的 `publish(report)`、`load()`、`import_memories()`；默认不允许 Mock 发布 |

B-08 写入按 `memory_id` 排序的 `memories.jsonl`、`memories.json` 与公共 `MemoryManifest` v1。Manifest 绑定 Schema 2.1、快照 SHA、来源/配置/Prompt 指纹、抽取版本、窗口计数、Memory 数量及实际文件 SHA-256；`current.json` 指向唯一 build 目录。文件先写入 staging 并回读校验，再以原子指针切换发布；失败保留旧指针。严格加载复核指针、状态、文件哈希、Memory ID、来源与当前快照/配置。只有 complete 且 `partial_build=false` 可以发布；partial/failed 被 B-08 拒绝。B 侧加载的公共 `MemoryArtifact.require_complete_for_index()` 合同测试通过。

## 尚未完成的正式交接

1. **真实数据：B 负责。** 官方固定文件已恢复到 Git 忽略的 `data/raw/`。B-01～B-04 对四个 main_dev 样本的校验与完整窗口覆盖通过；`conv-26` 的 83 个真实窗口使用固定 Mock 响应和逐条精确来源检查，得到 complete、1 条 Memory，并通过 B-08 临时 Artifact 发布回读。后续仍须用真实生产抽取与可信语义审核检查开发集重要事实覆盖、时间和断言正确性；不能使用 QA Gold 调参。
2. **公共构建结果：A 定义，B 适配。** v2.2 要求公共 `MemoryBuildResult`（`sample_id`、`memories`、`build_status`、`partial_build`、窗口总数/成功数、`window_diagnostics`、`extractor_version`）；`contracts/` 尚无此 DTO。B-07 目前在 `memory/build.py` 使用同字段的内部 dataclass，最小适配点是构建结果返回及 B-08 `publish` 输入。A 发布公共类型后，B 增加合同测试并移除本地替代类型。
3. **生产证据审核：B 负责，必要时与 A 的共享模型执行器协作。** 当前 `EvidenceChecker = Callable[[AtomicFactDraft, tuple[SourceTurn, ...]], EvidenceAssessment]`，审核结果是 `supported/unsupported/unclear` 加脱敏原因码；无检查器、unclear 或异常均不发布非空事实。需有不依赖 Gold 的可信逐条语义审核实现及开发集人工抽查；测试 Mock 不能代替它。
4. **正式 B→C：C 负责索引入口，B 配合。** B 当前分支的 `src/dynamic_memory/retrieval/` 仍是占位模块；远端 `origin/C` 的 `d4bff06` 已提供 `IndexManager.build_index(artifact: MemoryArtifact, settings)`，并声明拒绝 partial/failed。该提交尚未合入 B，本轮只读确认了接口形态，未把 B 严格加载的真实磁盘产物交给 C 执行，因此**正式跨分支索引/读取联调仍未完成**。合并窗口确定后，应使用 B 的 `load()` 结果验证 manifest 身份、索引指纹、版本与 partial 拒绝行为。
5. **B 的公共调用适配：B 负责。** B-08 现以显式依赖注入的 `MemoryArtifactStore` 提供加载接口，尚无 v2.2 示例中的独立 `export_memories`、`import_memories(path)`、`load_memory_artifact(path, *, allow_partial=False)` 函数形式；与 A 的 Runtime 初始化及 C 调用方式确认后提供薄适配，不放松来源/快照校验。

DDL 前最小路径：落地公共构建结果与 B 适配 → 实现并抽查可信证据审核 → 与 C 的正式入口执行 Artifact 合同测试。真实 DeepSeek/API、生产语义审核及正式索引验收均未在本轮执行。
