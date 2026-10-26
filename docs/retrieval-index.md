# C：记忆索引与持久化

索引保存“记忆 ID ↔ 向量”的对应关系和版本信息。当前实现建立、保存、读取索引；相似度排名、历史候选补充及 Vector RAG 在后续任务实现。

## 本机演示（Git Bash）

已有模型位于 `.venv/models/all-MiniLM-L6-v2`，可使用原有 Embedding 环境运行：

```bash
.venv/Scripts/python.exe -m dynamic_memory.retrieval.index_smoke
```

它用两条明确标注的合成 Memory，调用真实本地模型建立索引，保存到 `results/index-smoke/synthetic-index.json`，重新读取并核对相等，再检查修改检索参数后旧索引是否被拒绝。重新运行会替换这个演示文件。该文件被 Git 忽略，不应手工加入提交。

这不是 B 严格加载器或真实数据链路的验证。演示产物由代码手工构造，不能作为正式 Memory 数据使用。

## 正式接口

```python
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.retrieval import EmbeddingConfig, LocalIndexEncoder, IndexManager, save_index

model_dir = ".venv/models/all-MiniLM-L6-v2"
encoder = LocalIndexEncoder(EmbeddingConfig(
    model_name=model_dir,
    revision="1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
))
manager = IndexManager(encoder)
settings = load_settings().retrieval.model_copy(update={"embedding_model": model_dir})

# artifact 必须由 B 的严格 load_memory_artifact 获得；此处不伪造该实现。
# index = manager.build_index(artifact, settings)
# save_index(index, "results/indexes/sample-index.json")
# loaded = manager.load_index("results/indexes/sample-index.json", artifact, settings)
```

`manager.build_index(artifact, settings)` 是绑定模型后的正式双参数入口，输入必须为公共 `MemoryArtifact`。裸 `list[MemoryRecord]` 不接受；partial/failed 不进入索引。合法空产物可产生空索引，维数为 0，不加载模型或调用编码。

正式的 artifact 必须是 B 严格 loader 核验过文件哈希、来源、快照与发布指针后提供的对象。C 再检查内存内容及完整性，不代替 B 的磁盘验证。加载索引也要求重新提供当前 artifact、settings 和编码器身份，以检测旧文件。

模型由构造器显式注入，不使用全局默认模型或隐式下载。`settings.embedding_model` 必须与编码器的 `identity.model_name` 一致；仓库 YAML 仍保留 null，待真实开发集比较后冻结。这里的内存配置仅用于本次运行。

## 保存内容和版本判定

- Memory ID 按字典序保存，向量和 ID 一一对应；每个向量有限、同维、长度归一化。加载不重复编码。
- 文件是单个 JSON，包含索引格式版本、样本、源 manifest 指纹、检索配置指纹、模型/编码身份、Memory ID、向量及负载摘要；不重复保存 Memory 原文，原文从传入的已验证 artifact 获得。
- `LocalIndexEncoder` 对本地模型目录所有文件计算哈希，包括权重、配置、tokenizer 和 pooling；编码身份还记录 Prompt、设备、批大小、相关库版本及编码实现。构造后模型目录必须保持不变，变化会拒绝旧实例；需用新实例重新构建。
- `retriever_fingerprint` 包含源清单、配置、模型/编码身份和索引实现版本。Memory 重建、模型或参数变化时，加载旧索引报 `INDEX_STALE`。
- 索引内部保存不可变快照；返回的 Memory 为副本，调用方修改输入或返回对象不会悄悄改变已建立的索引。
- 保存时先写同目录临时文件、flush/fsync，再原子替换目标文件；替换失败时上一份文件仍可使用。摘要和结构检查用于发现文件损坏，不是来源真实性证明或密码学签名。

当前选择 JSON 是为了方便检查和减少依赖；它比二进制矩阵更占空间，属性读取也会解析快照。每次验证本地模型身份会重新读取文件计算哈希，有额外 I/O。先在样本规模上测量，再决定是否优化；不要将缓存或模型目录放进公共 Git 提交。

## 错误与交接

错误统一为 `RetrievalIndexError`，读取 `.code`；不把原始 Memory、路径或底层异常文本写入消息。

| code | 含义 |
| --- | --- |
| ARTIFACT_CORRUPT / ARTIFACT_INCOMPLETE | 源对象无效或构建不完整 |
| INDEX_CONFIGURATION_INVALID | 配置无效、模型未指定或模型身份不匹配 |
| EMBEDDING_MODEL_UNAVAILABLE / EMBEDDING_FAILED / EMBEDDING_OUTPUT_INVALID | 模型目录、编码执行或向量输出异常 |
| INDEX_NOT_FOUND / INDEX_READ_FAILED / INDEX_CORRUPT | 索引缺失、无法读取或格式/摘要/结构错误 |
| SAMPLE_MISMATCH / INDEX_STALE | 样本错误或源产物/模型/配置已变化 |
| INDEX_WRITE_FAILED | 保存失败，未把临时文件作为可用索引 |

A 的 `load_verified_index(sample)` 适配器应先使用 B 严格加载器，再调用 `manager.load_index`，并核对 sample 与 artifact。A 当前调度器只传播白名单错误码；新诊断码需要与 A 协商接入，尚未完成完整调度联调。

验证命令（本机独立 Python 3.11 环境）：

```bash
.venv/validation311/Scripts/python.exe -m pytest tests/unit/test_retrieval_index.py tests/unit/test_index_embedding.py -q -p no:cacheprovider
```
