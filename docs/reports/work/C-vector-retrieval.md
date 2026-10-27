# C-04 基础向量检索工作记录

日期：2026-10-26。基线：C 分支 `361b468`，已由用户提交并推送 C-03。本批尚未提交。

## 交付

- VectorRetriever 绑定编码器/配置，提供 retrieve 和 load_candidates 公共入口形状。
- 单位向量余弦排序、默认 Top-8、稳定同分规则、真实 Memory 来源、按 rank 加载、空结果与失败区分。
- 检查请求/分析/样本/模型/配置身份，编码前阻止时间冲突；不以提示硬过滤历史事实。
- LocalIndexEncoder 增加问题编码并在编码前后核验本地模型身份。
- 合成数据真实模型演示、使用文档及专项测试。

## 验证

Python 3.11.15（独立 `.venv/validation311`）：

```bash
.venv/validation311/Scripts/python.exe -m pytest tests/unit/test_vector_retrieval.py -q -p no:cacheprovider
.venv/validation311/Scripts/python.exe -m pytest -q -p no:cacheprovider
.venv/validation311/Scripts/python.exe -m dynamic_memory.pipeline.mock_smoke
```

新增检索专项 **32 passed**；全项目 **568 passed**（含合同与 Web）；Mock smoke PASS。覆盖已知相似度排序、同分 Top-8、参数预算、重复查询、重载一致性、空索引不编码、负分/提示不筛选、错误向量、样本与查询错配、时间冲突、模型中途变化、错误脱敏、候选来源和 ID 校验、返回对象隔离。

Python 3.12.14 原模型环境，HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1：

```bash
.venv/Scripts/python.exe -m dynamic_memory.retrieval.retrieval_smoke
```

真实本地 MiniLM，对两条合成记忆的实际结果：

| 问题 | Top-1 | 两条候选分数（降序） |
| --- | --- | --- |
| What is Sam's job? | Sam works as a teacher in Shanghai. | 0.660922、0.551565 |
| What sport does Sam like? | Sam likes playing football. | 0.745434、0.448854 |

两个问题均通过 Top-1 断言，保存重载结果一致；退出码 0。只有两条候选，因此本演示不证明 Top-8 截断质量；截断和稳定同分用 13 条合成 Memory 的单元测试验证。

## 尚未完成

历史扩展、真实 B 产物检索效果、A 真实运行器及错误码对接、Vector RAG 回答生成。公共 Schema、统一默认配置保持原样；不能把基础 Top-K 当作已完成的主对照最终候选。下一批 C-05 实现历史候选扩展。
