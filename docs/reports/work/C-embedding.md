# C：Embedding 第一版工作记录

日期：2026-10-19。工作分支：`C`。本轮未 commit、push，无新增提交 SHA。

## 交付范围

- `retrieval/embedding.py`：本地 Sentence Transformers 编码，支持 Memory 内容批处理、单个问题、独立查询/文档 prompt、单位向量和输出校验。
- 配置显式记录模型版本；提供配置指纹，供后续索引指纹组合使用。它不代表权重文件校验值。
- `retrieval/embedding_smoke.py`：英文合成输入的真实模型运行入口。
- 可选安装依赖 `.[embedding]`、单元测试和 `docs/embedding.md`。
- 不改变公共 DTO 和检索预算；正式 Embedding 模型尚未冻结，检索索引与 Vector RAG 尚未实现。

## 已执行验证

环境：Windows、CPython 3.12.14（当前可用运行时）；项目推荐的 Python 3.11 尚未验证。

普通沙箱无法联网安装；提升权限后安装测试依赖到 `.venv/check-deps`。使用以下命令运行：

```powershell
$env:PYTHONPATH = "$PWD/.venv/check-deps;$PWD/src"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke
git diff --check
```

结果：全套 **265 passed**；synthetic Mock smoke PASS；diff 格式检查通过。新增单元测试覆盖延迟加载、默认本地加载、实例复用、输入顺序、独立 prompt、归一化、无效输入/输出、维度一致性、依赖缺失提示与配置身份变化。

真实运行依赖安装成功：sentence-transformers 5.7.0、torch 2.14.1+cpu、transformers 5.20.1。固定模型 `sentence-transformers/all-MiniLM-L6-v2` 的版本为 `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`，文件下载到被 Git 忽略的 `.venv/models/all-MiniLM-L6-v2`。

`model.safetensors` 的 SHA-256：`53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db`。

在 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1` 下执行：

```powershell
.\.venv\Scripts\python.exe -m dynamic_memory.retrieval.embedding_smoke --model .venv/models/all-MiniLM-L6-v2 --revision 1110a243fdf4706b3f48f1d95db1a4f5529b4d41
```

结果：退出码 0，生成 384 维向量。问题 `What is Sam's job?` 与职业句子的余弦相似度为 0.6609220809849664，与足球句子为 0.5515651928760503。这只是英文合成输入的真实编码检查，不是正式评测，也未接入 B 的真实数据或 A/D 业务链路。

## 后续工作

接入 B 的实际 Memory、检查真实文本长度及截断、在开发集比较模型后冻结版本；之后完成索引持久化、候选召回和与 A/D 联调。合成编码测试不代表 LoCoMo 检索质量。
