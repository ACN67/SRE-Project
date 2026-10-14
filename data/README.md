# 数据边界

LoCoMo 原始数据由团队后续从[公开项目](https://github.com/snap-research/locomo)获取，并核对其发布条款和使用许可；本次不下载。`raw/` 与 `processed/` 不追踪真实数据，`annotations/` 不追踪 Gold。

业务输入只取 conversation 及可选问题文本；`qa.answer`、`qa.evidence`、状态 Gold 和辅助 summary 不得进入抽取、检索、解析或生成。测试 Gold 仅由独立评测路径在预测保存后读取。实际数据 SHA 和 sample_id 必须在获取并核验后写入 `configs/data_split.yaml`。
