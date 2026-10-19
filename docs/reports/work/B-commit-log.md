# B 工作记录

## B-01｜数据快照校验与固定划分｜2026-10-19

- 分支：独立 `SRE-Project-B` worktree 的 `B`，从 `origin/main` `c967293f2e54b4ce34ae3460efc4fc2e0394139d` 建立；原 A 目录未编辑或切换。
- 官方来源：`https://github.com/snap-research/locomo`，`main` 提交 `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`，`data/locomo10.json`。原始文件保存在忽略目录 `data/raw/`，2,805,274 字节，SHA-256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`。同一提交的 `LICENSE.txt` 为 CC BY-NC 4.0；不把原始数据放入 Git。
- 原始顺序 ID：`conv-26, conv-30, conv-41, conv-42, conv-43, conv-44, conv-47, conv-48, conv-49, conv-50`。依 1-based 固定顺序冻结 `configs/data_split.yaml`：`main_dev` 前 4 条、`extra_dev` 中间 4 条、`test` 最后 2 条。
- `data.snapshot` 提供字节 SHA、最小结构检查、只读划分验证、默认预览和显式原子冻结、只读 CLI。错误码包含 `DATA_FILE_NOT_FOUND`、`DATA_FORMAT_INVALID`、`DUPLICATE_SAMPLE_ID`、`SPLIT_UNFROZEN`、`DATA_SNAPSHOT_MISMATCH`、`SPLIT_MISMATCH`。不输出对话或 Gold。
- 专项 synthetic 快照测试：27 通过；修正 `tests/unit/test_settings.py` 中三个过时的 null 断言，改为核验冻结 SHA、真实 sample ID 及三组映射，关联专项测试共 45 通过。完整离线测试：311 通过、0 失败。Mock smoke 通过。真实快照只读 CLI 验证通过。
- 后续：B-02/B-03 待重新授权；第一批合并节点仍为 B-03，B-01 不单独合并 `main`。
