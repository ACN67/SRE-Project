# C 模块阶段交付入口

更新：2026-11-01。负责人范围：Embedding、查询分析、记忆索引、候选检索与历史扩展、Vector RAG 回答和 C 侧接入/效果诊断。

已提交基线为 `d4bff06`；本轮补充 A 最新纯向量消融端口和交付检查，待用户提交。本文区分“代码/合成联调可交付”与“真实实验验收”，不将前者当作后者。

## 一条命令核验

仓库根目录 Git Bash，当前机器已有独立验证环境：

```bash
.venv/validation311/Scripts/python.exe scripts/check_c_delivery.py --a-ref 02923f7bb8d5b61ff3c64ae7619c768a01aeaba6 --b-ref 001c5220330f5d713d0dbf9ff3644f217d432ca4
```

其他成员使用 Python 3.11，在自己的虚拟环境中执行 `python -m pip install -e '.[dev,web]'` 后，用对应 `python` 执行脚本即可。不需要 embedding/api extra 或真实 API Key。A 的上述提交须存在于本地 Git；缺少时先 `git fetch origin`。脚本执行指定版本的项目 Python 源码，只用于已审阅的可信版本。

依次执行全项目 pytest、Mock smoke、合成检索效果演示、A/C 调度验证、A 服务/多样本/纯向量消融验证；任一步失败立即退出，不打印总 PASS。后一步在临时目录复制 C 源码与配置，并使用 A 的实际新公共模型，独立进程完成后自动清理，不修改 checkout、不合并分支。

2026-10-31 实测：**633 passed**，Mock PASS，合成检索召回 1/3→3/3，A 最新 `02923f7` 调度/服务验证通过，`a_pure_vector_entry_verified=true`，最终 `c_delivery_checks=PASS`。合成分数和耗时不是正式模型效果或性能。首次检查遇到临时副本配置目录缺失，修正目录布局后整套重跑退出码 0。

## 交付能力与接口

2026-11-01 增量复验：上述包含 `--b-ref` 的完整命令退出码 0；**633 passed**，Mock、A/C 调度、A 服务消融与新增 B→C 磁盘产物联调均 PASS。B 检查单独运行，尚未声称 A+B+C 同进程的完整服务验收。

| 能力 | 入口 | 说明 |
| --- | --- | --- |
| 本地向量编码 | EmbeddingConfig / LocalIndexEncoder | 模型目录必须提前准备，固定版本，不隐式下载 |
| 查询分析 | analyze_query | 无大模型；意图与时间独立，不推断私人事实 |
| 建索引/保存/读取 | IndexManager / save_index | 完整 MemoryArtifact、身份核验、过期拒绝 |
| 最终候选 | VectorRetriever.retrieve | 默认 Top-8，补相关历史至最多 12 条 |
| 共享候选 | SharedRetrieval | 同题两路复用有限内存快照，不替代 B 核验 |
| 服务接入 | RetrievalServicePorts | 多样本、当前索引读取、候选与回答绑定 |
| 纯向量消融 | retrieve_raw_topk | 返回 snapshot.pure_vector，不从扩展结果临时删历史冒充原始排名 |
| 回答 | VectorAnswerer | 共享 Runtime、完整 Memory 预算、引用验证、诊断身份 |
| 条件召回诊断 | evaluate_retrieval | 开发集标注独立使用，不读取官方 Gold，不执行回答 |

A 新调度器配置同时传入：

```python
retrieve=ports.retrieve,
retrieve_raw_topk=ports.retrieve_raw_topk,
load_candidates=ports.load_candidates,
answer_vector_rag=ports.answer_vector_rag,
```

也可直接注入 `shared.retrieve_raw_topk`，但服务组合推荐 ports，以保证纯向量候选也绑定本次回答索引。A 消融调用的 base_top_k 必须等于 C 配置 optional_pure_vector_top_k（默认 8）；不同 K 应修改配置并重建对应索引，不能让 A 单方面截断后继续使用不匹配的 C 预算。A 的新 QueryResponse 增加 pure_vector_topk，正式合并由 A 负责；本轮只在临时集成验证中加载它，不复制到 C 公共契约。

纯向量消融仍复用既有 Vector QA 端口，A 输出的响应方法为 pure_vector_topk，当前底层 Runtime QA 方法身份仍为 vector_rag。比较耗时/调用量时须区分缓存命中；正式实验前由 A 统一方法追踪规则。只验证了 A 的纯向量单题入口，没有宣称完成 A 的正式批量预测/评分结果验收。

## 实际运行与阅读顺序

1. [Embedding](embedding.md)：本地模型准备与编码。
2. [查询分析](query-analysis.md)、[索引](retrieval-index.md)：建立数据边界。
3. [候选检索](vector-retrieval.md)、[回答](vector-rag.md)：候选、预算与引用。
4. [服务接入](C-service-ports.md)：给 A 的工厂接线、样本隔离、索引更新。
5. [检索效果诊断](retrieval-evaluation.md)：标注与指标分母。

真实本地模型的已有合成演示可选执行（需 embedding extra 和本地模型）：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m dynamic_memory.retrieval.retrieval_smoke
```

该命令此前已验证；本轮交付检查不依赖模型文件，也未下载新模型或调用收费 API。结果输出在被 Git 忽略的 results/，不提交数据、模型、缓存或凭据。

## 仍需团队交付的依赖

2026-11-01 拉取远端后，B 已更新到 `001c522`，严格磁盘加载接口已具备。本轮新增 C 侧 `scripts/check_c_b_integration.py --b-ref <可信提交>`：在临时副本加载 B 的真实 memory 模块，复用其明确标注的合成来源及固定 Mock 构建器，再调用 C 的实际索引与检索入口。验证磁盘加载→建索引→保存回读→检索与来源回查、不完整构建不能覆盖旧产物、重新发布使旧索引失效、重建恢复、损坏磁盘文件拒绝。未修改 B 源码、未合并分支；向量为确定性假向量，不能代表语义检索质量。省略 `--b-ref` 时交付脚本仅运行原有检查，输出会明确 B 未检查。

| 对接方 | 需要的输入/决定 | C 接到后做什么 |
| --- | --- | --- |
| B | 严格 loader 已完成合成跨分支联调；仍需生产抽取/可信语义审核后的真实开发 MemoryArtifact | 真实建索引、召回/历史覆盖、版本更新核验 |
| A | 服务/公共合同集成、C 错误码映射、三路统一回答输出和方法追踪 | 同接口复验，提供候选与回答诊断 |
| D | resolve / answer_dynamic 可调用实现 | 验证相同最终候选与状态链路；不代写 D 算法 |
| E | 问答/比较页面的实际服务接入 | 配合接口排错；不代写 UI |

目前正式 Embedding 尚未由真实开发比较冻结，统一配置保留 `embedding_model: null` 和 `real_retrieval_implemented: false`。不为了演示将其强行开启。还没有真实 B/C/D 全流程结果、正式课程分数或最终实验结论。

**阶段验收结论：** C 核心实现、合成验证和团队接入说明可以交付；真实效果、正式模型冻结及整项目验收待上述依赖具备后完成。
