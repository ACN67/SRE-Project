# D 验证记录

日期：2026-11-04

## 已执行

- D 单元测试覆盖候选与样本校验、逐条决策、有效集合、关系端点与来源、
  冲突、时间条件、预算、缓存、诊断和非法模型输出。
- 合成服务集成调用真实 B Builder、C Index、A QueryOrchestrator、D 端口
  和 E WebBindings。
- 测试使用显式 synthetic adapter，不发起付费 API 调用，不读取 Gold。
- `tests/dynamic`：59 passed。
- `tests/dynamic tests/contract`：158 passed。
- 完整仓库回归：1332 passed（包含 D、合同、集成和 Web）。
- `python -m compileall -q src tests scripts`：通过。
- 真实 conv-30 的已发布 Memory 与 BGE 索引通过无网络 D 装配冒烟：
  Vector RAG 与 Dynamic 使用相同 8 个有序候选，8 个决策，0 次真实 HTTP。
- Production WebBindings 的问答方法包含 `full_text`、`vector_rag`、`dynamic`。

## 未验证

- 真实 DeepSeek 对当前 LoCoMo 样例的状态判断质量。
- 真实样本上的关系分类、决策、有效集合和时态指标。
- 3 路正式对照和独立 test 集。

这些项目在本轮完成后仍需通过统一真实运行验证，不能由合成预设结果替代。
