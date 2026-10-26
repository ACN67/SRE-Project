# C-03：记忆索引与持久化

日期：2026-10-26。基线：C 分支 `d4cc640`（已同步 main）。本批尚未 commit/push。

## 交付

- `retrieval/index.py`：绑定编码器的 `IndexManager.build_index(artifact, settings)`、`load_index`，不可变快照 `RetrievalIndex`，单文件 JSON 原子保存 `save_index`，稳定错误码。
- 正式输入只能为完整的公共 `MemoryArtifact`，重新核验内容、样本、ID、源清单身份，拒绝部分/失败产物；合法空产物支持空索引且不编码。
- Memory 按 ID 规范化顺序，向量有限、同维且单位化；文件包含版本/配置/模型/源身份和校验摘要，加载必须提供当前 artifact 及编码器身份，不重复编码。
- `retrieval/index_embedding.py`：本地模型目录全部文件哈希、编码配置和依赖版本身份；目录变化拒绝旧模型实例。复用已有 Embedding，不修改其接口，不自动下载模型。
- `retrieval/index_smoke.py`：明确标记的合成 Memory + 真实本地模型演示；文档 `docs/retrieval-index.md` 和 README 入口。

## 验证证据

Python 3.11.15（`.venv/validation311`）：

```bash
.venv/validation311/Scripts/python.exe -m pytest tests/unit/test_retrieval_index.py tests/unit/test_index_embedding.py -q -p no:cacheprovider
.venv/validation311/Scripts/python.exe -m pytest -q -p no:cacheprovider
.venv/validation311/Scripts/python.exe -m dynamic_memory.pipeline.mock_smoke
```

专项 **34 passed**；全量 **536 passed**（含 Web，无排除/跳过）；Mock smoke PASS，退出码均为 0。覆盖 ID/向量对齐、输入重排、保存重载不编码、空索引、partial/failed 拒绝、输入及返回对象修改隔离、无效配置/向量、模型/Prompt/参数/源版本变化、跨样本拒绝、文件损坏、替换失败保留旧文件和清理本次临时文件。初次专项发现内存源快照未按 ID 规范化，修复后全部通过。

Python 3.12.14（原 `.venv`，已有 sentence-transformers 5.7.0、torch 2.14.1+cpu）：在 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1` 下执行：

```bash
.venv/Scripts/python.exe -m dynamic_memory.retrieval.index_smoke
```

实际输出：2 条合成记忆，384 维，`reload_equal=true`、`changed_settings_rejected=true`；退出码 0。模型为此前下载的固定版本 MiniLM，输出位于 Git 忽略的 `results/index-smoke/synthetic-index.json`。本次索引指纹：`sha256:b7ba7be22fc27af53be81b0dc8e420e1ce3e79600bb45f07416e1b67cb825d93`。这不是 LoCoMo 或 B 严格产物加载的真实链路验证。

## 边界与后续

- B 的磁盘哈希、来源、快照和发布指针验证仍由 B 严格 loader 负责；构造内存 DTO 不构成来源证明。
- 尚未实现 retrieve/load_candidates、历史扩展或 Vector RAG；正式模型仍未冻结，未改公共 Schema 和 retrieval.yaml 初始状态。
- A 的索引加载端口适配、错误码白名单接入及 B/C/D 真实联调尚未完成。
- JSON 和属性快照解析适用于当前轻量样本；本地模型文件哈希重复读取有 I/O 开销。后续用实际规模测量再优化。
- 文件摘要检查完整性，不提供恶意篡改的签名验证。索引可能承载私人信息，仍须存入被忽略的缓存/结果目录。
- 合并前未改变其他成员的业务实现；本轮仅 C 模块、专项测试及文档。下一步 C-04：问题向量→稳定 Top-8→CandidateSet 和按 rank 加载 Memory。
