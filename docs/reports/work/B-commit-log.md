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
