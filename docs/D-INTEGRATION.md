# D 模块接入 main 记录

日期：2026-11-04

## 来源

- GitHub 远端 D 分支：`498c64625f7c39e79c346f3e677d52678ae00b26`
- D 源分支未创建提交、未推送、未修改。
- 未直接合并整个 D 分支；仅将 `D-submit-files/` 中的功能文件落到当前 `main`
  工作树的实际项目路径。

## 已接入代码

- `src/dynamic_memory/reasoning/common.py`
- `src/dynamic_memory/reasoning/resolver.py`
- `src/dynamic_memory/reasoning/service_ports.py`
- `src/dynamic_memory/reasoning/smoke.py`
- `src/dynamic_memory/reasoning/__init__.py`
- `src/dynamic_memory/qa/dynamic.py`
- `src/dynamic_memory/qa/__init__.py`
- `scripts/run_dynamic.py`
- `examples/dynamic_request.synthetic.json`
- `tests/dynamic/`

## 运行层调整

- `QueryOrchestrator` 现在保留 D 的结构、预算和模型错误码，不会统一退化为
  `COMPONENT_FAILED`。
- 生产组合根只创建一个共享 `LLMRuntime`。
- Dynamic provenance 由已核验的 B Artifact、C 索引及 Git 身份构造。
- conversation cutoff 从完整原始对话的可见时间生成。
- web 的正式工厂现在启用 `dynamic_available=True`。
- High profile 的状态解析与 QA 均省略 API `max_tokens`；内部使用 1M 输入
  上限和 64K 输出安全预算。

## 验证

- `tests/dynamic`：59 passed。
- `tests/dynamic tests/contract`：158 passed。
- D 接入后的合同、调度、生产装配和 Web service binding 聚焦回归：217 passed。
- 完整仓库回归：1329 passed。
- `python -m compileall -q src tests scripts`：通过。
- `python scripts/run_dynamic.py --smoke`：通过。
- 真实 conv-30 Artifact + 索引的无网络 D 装配冒烟：通过。
  - Vector RAG 与 Dynamic 的 8 个有序候选一致。
  - Dynamic 生成 8 个决策和 8 个有效 ID。
  - 真实 HTTP 调用为 0。
- 真实 conv-26 Artifact + 索引的无网络 D 装配冒烟：通过。
  - Vector RAG 与 Dynamic 的 9 个有序候选一致。
  - Dynamic 生成 9 个决策和 9 个有效 ID。
  - 真实 HTTP 调用为 0。
- 生产 Web 可通过 `SRE_PRODUCTION_SAMPLE_ID` 选择 `conv-30` 或 `conv-26`。

## 尚未验证

- 真实 DeepSeek 的 D 状态判断质量。
- 真实样本上的关系、决策、有效集合和时态指标。
- Dynamic 与 Full Text、Vector RAG 的正式三路对照。

因此当前状态是：**D 已接入生产组合与 Web，真实 D 质量验收待后续执行。**
