# C：Embedding 第一版

Embedding 将文字转换成数字向量。本模块只负责转换；检索排序、索引落盘和 Vector RAG 将在后续实现，不需要先部署向量数据库。

## 安装与运行

在 README 的 Python 3.11 虚拟环境中安装额外依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev,embedding]'
```

下面的 Python 示例可放入本地脚本运行。将模型位置和版本换成实际值：

```python
from dynamic_memory.retrieval import EmbeddingConfig, SentenceTransformerEmbedder

embedder = SentenceTransformerEmbedder(EmbeddingConfig(
    model_name="path/to/local/sentence-transformer-model",
    revision="local-snapshot-v1",
))

vectors = embedder.embed_documents([
    "Sam previously worked in Beijing.",
    "Sam now works in Shanghai.",
])
question_vector = embedder.embed_query("Where does Sam work now?")
print("记忆数量：", len(vectors))
print("向量维数：", len(question_vector))
```

已有 `list[MemoryRecord]` 时调用 `embedder.embed_memories(memories)`。只编码 `content` 原文，返回向量与输入记忆顺序一一对应；调用方须将向量与 `memory_id` 一起保存。

若需要从 Hugging Face 首次下载，显式传 `local_files_only=False`，并将 `model_name` 设为模型仓库 ID、`revision` 设为具体版本。正式实验使用不可变的 commit SHA；本地模型需另外记录权重文件校验值。模型缓存和权重放在仓库外，不提交到 Git。

## 行为与边界

- 默认 CPU、本地缓存加载，不调用收费 API。导入模块和空列表不会加载模型。
- 通过 Sentence Transformers 的 `encode` 批量编码；统一输出单位长度向量，后续可用点积比较相似度。
- 查询与文档可分别配置 `query_prompt` / `document_prompt`，根据所选模型要求设置。默认为空，显式覆盖模型的默认 prompt。
- 单条空文本、无效向量、数量或维度变化均报错，不返回伪造向量。
- 长文本遵循模型自身的 token 截断规则。接入真实 Memory 后须检查文本长度与模型最大输入长度，避免关键内容被截断。
- `configuration_fingerprint` 只标识配置和编码实现；后续索引指纹还需合并数据快照、实际权重版本和依赖版本。
- `configs/retrieval.yaml` 的模型仍待开发集比较后确定，`real_retrieval_implemented` 仍为 false。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_embedding.py -q
```

单元测试用可控编码器验证加载策略、输入顺序、归一化、错误处理和配置身份，不下载模型，不能证明真实语义效果。真实模型的加载、编码和开发集召回需另行验证。

实现依据：[Sentence Transformers 官方接口文档](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html)。

## 真实模型小示例

提供一个使用英文合成句子的入口。它展示“职业问题”与两条记忆的相似度，不读取 LoCoMo，也不代表正式检索评测：

```powershell
$env:HF_HOME = "$PWD/.venv/hf-cache"
.\.venv\Scripts\python.exe -m dynamic_memory.retrieval.embedding_smoke --revision 1110a243fdf4706b3f48f1d95db1a4f5529b4d41 --allow-download
```

这里暂用 `sentence-transformers/all-MiniLM-L6-v2` 做英文运行检查；它输出 384 维向量，默认截断超过 256 word pieces 的输入，见[模型说明](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)。这不是已冻结的正式模型，也不能据此声称支持中文检索。首次下载后可以去掉 `--allow-download` 验证本地缓存运行。

本机已下载同一版本到 `.venv/models/all-MiniLM-L6-v2`，可直接运行（无需下载）：

```powershell
.\.venv\Scripts\python.exe -m dynamic_memory.retrieval.embedding_smoke --model .venv/models/all-MiniLM-L6-v2 --revision 1110a243fdf4706b3f48f1d95db1a4f5529b4d41
```

本次实际输出：384 维；职业记忆相似度约 0.661，足球记忆约 0.552。相似度不是答案正确概率。
