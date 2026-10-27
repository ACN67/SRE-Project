# C-07 接线验证与联调交接

**10/27 更新：** 已适配 A 新统一服务 `f4b82a6`，完成两样本隔离、索引发布更新及现有 WebBindings 只读接入的合成验证。实现与 615 项回归记录见 [C 服务端口说明](C-service-ports.md)。下文保留 10/26 调度器接线的历史记录。

2026-10-26；本轮仅完成联调准备和合成接线验证，不声称真实 B/C/D 链路已经完成。所有 C 改动仍在用户 C 分支，尚未提交。

## 核对版本

本轮执行 git fetch origin 后：A 为 `b17893b6d1411982cb5b474a2d38e6b1426cf78f`，B 为 `93cf9f5`。A 包含 QueryOrchestrator 与 prepare_shared_query；B 已有窗口规划，但尚未提供正式 Memory 抽取与严格 load_memory_artifact 实现。本轮没有合并 A/B/E 分支，没有修改其他成员代码。

## C 注入方式

```python
shared = SharedRetrieval(VectorRetriever(encoder, retrieval_settings), max_entries=128)
runner = QueryOrchestrator(
    analyze_query=analyze_query,
    get_sample=source_repository.get_sample,
    load_verified_index=load_current_verified_index,
    retrieve=shared.retrieve,
    load_candidates=shared.load_candidates,
    answer_vector_rag=vector_answerer,
    resolve=dynamic_resolver,
    answer_dynamic=dynamic_answerer,
)
prepared = runner.prepare_query(request)
vector_response = runner.run_prepared(prepared, "vector_rag")
dynamic_response = runner.run_prepared(prepared, "dynamic")
```

SharedRetrieval 按完整 request、analysis、分析版本和索引指纹缓存不可变快照，每次返回独立 CandidateSet。缓存命中前仍检查当前编码器/索引身份；不缓存 B 产物核验，A 每次仍须调用 load_current_verified_index。默认上限 128 项，LRU 淘汰，clear() 可清空；实例间隔离，无磁盘输出。锁保证并发同题只构建一次快照，但会串行化该实例的不同问题检索，后续按负载测量再优化。

**一致性范围：** 同一实例、缓存项未被清除/淘汰且数据与配置不变时复用同次快照。正式批量比较若要求跨长时间/进程固定候选，应将 RetrievalSnapshot 作为实验产物显式保存或由 A 持有；不能把有限缓存当永久快照。

VectorAnswerer 当前绑定一个索引。多样本或索引发布更新时，A 应按样本和索引身份创建/选择匹配的回答器，不能将一个实例用于全部样本；C 会拒绝错样本或与绑定索引不一致的 Memory。

## 可复核的合成联调

```bash
.venv/validation311/Scripts/python.exe scripts/check_c_a_integration.py --a-ref b17893b6d1411982cb5b474a2d38e6b1426cf78f
```

脚本从本地 Git 指定版本读取并执行已经审阅的 A query_conditions/query_runner 源码；不下载、不切分支、不把 A 文件复制进生产目录。仅应传可信项目版本。B SourceRepository 使用合成会话；MemoryArtifact 为明确手工合成产物，load_verified_index 是测试替身，不验证 B 磁盘产物；D 为明确接线替身，只检验候选传递，不计算真实状态。

实际输出/断言：

- A 调用 C 的真实检索/候选加载/回答端口，使用合成编码器和 QA Adapter，vector_status=answered。
- 两路 CandidateSet 完整相等，ID/排名/来源一致；问题编码次数为 1。
- 明确时间冲突 early stop，不再编码。
- INDEX_STALE 被 A 原样保留；INDEX_CORRUPT 被当前 A 映射为 COMPONENT_FAILED。
- real_B_artifact_loader=false，real_D_reasoning=false。

新增共享适配器 **8 项测试**：两路快照复用及修改隔离、并发只编码一次、问题/分析/源版本失效、缓存命中仍拒绝过期模型、LRU、clear 和非法容量。Python 3.11 全项目 **606 passed**，Mock smoke PASS，合成联调退出码 0。未调用真实付费模型。

## 下一步按依赖推进

| 对接对象 | 已确认 | 尚需完成 |
| --- | --- | --- |
| A 调度 | 三参数端口可注入，双路共享候选已合成验证 | 纳入真实服务初始化；保留 C 错误分类；多样本回答器选择 |
| A/D QA | C 使用共享 QA 模板与 Runtime | 统一 C 的答案状态/引用文本封装与 A 自由文本输出，记录正式版本后再跑三路比较 |
| B 产物 | 公共 MemoryArtifact 可建索引 | 严格磁盘加载器验证 manifest/文件哈希/来源/发布指针，随后接入 load_verified_index |
| D 推理 | 合成接线确认其接收相同候选 | 接真实 resolve/answer_dynamic，不使用测试替身评价效果 |
| 真实效果 | 本地模型基础检索合成演示通过 | 真实开发样本的召回率、历史覆盖、引用语义支持和资源用量；最后冻结 Embedding |

真实数据联调不能用手工 DTO 替代 B 严格加载器来宣称完成。A/B/D 对齐之后再执行小规模、显式配置的真实模型验证；当前正式配置开关保持原样。
