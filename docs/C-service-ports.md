# C 服务接入与多样本隔离（2026-10-27）

本批针对 A `f4b82a6b3d150b856c2a32350039ca8ca7c90d7d` 新增的 DynamicMemoryService 实现 C 端口适配。没有合并 A 分支、复制公共 DTO 或修改 A/E 业务实现。B 严格产物加载器仍未就绪，以下真实工厂中的 loader 需要以后由 B 提供。

## 初始化与接线

```python
from dynamic_memory.retrieval import RetrievalServicePorts, VectorAnswerProvenance
# 以下 A 类需先由团队集成对应 A 版本；当前 C 分支不复制这些定义。
from dynamic_memory.services.dynamic_memory_service import IndexReadiness

ports = RetrievalServicePorts(
    encoder=encoder,
    settings=retrieval_settings,
    load_artifact=load_current_verified_b_artifact,
    index_path=configured_index_path_for_sample,
    readiness_factory=IndexReadiness,
    runtime=shared_runtime,
    provenance_factory=verified_provenance_for_artifact,
    on_diagnostic=record_answer_budget,
)
runner = QueryOrchestrator(
    analyze_query=analyze_query,
    load_verified_index=ports.load_verified_index,
    retrieve=ports.retrieve,
    load_candidates=ports.load_candidates,
    answer_vector_rag=ports.answer_vector_rag,
    # D 的 resolve/answer_dynamic、A 的 answer_full_text 由团队另行注入。
)
service = DynamicMemoryService(
    sources=source_repository,
    build_status=b_build_status,
    load_verified_artifact=b_verified_load_for_a,
    query_orchestrator=runner,
    index_readiness=ports.index_readiness,
)
```

这里没有定义新的 IndexReadiness：通过工厂注入 A 的实际类型。C 的 load_artifact 返回公共 MemoryArtifact，A 的 load_verified_artifact 返回 A 的 VerifiedMemoryLoad；两个适配入口应共同调用 B 同一套严格加载逻辑，不能给手工 DTO 加 b_verified_loader 标签冒充真实验证。provenance_factory 返回 VectorAnswerProvenance，须使用实际数据指纹、分析版本和 C 代码修订身份。

索引路径使用显式配置的样本到路径映射；不要把用户输入直接拼入文件路径。本组件读取已有索引，不调用 build_index、save_index、Embedding 文档编码或下载模型。构建和发布仍需离线完成。

## 一次查询的索引绑定

1. A 根据已验证 MemoryArtifact 先查询 index_readiness。C 返回 ready/not_found/stale/corrupt 和索引指纹；来源 manifest 不一致为 stale。未识别的加载错误继续抛出，不能伪装为 ready。
2. load_verified_index 重新调用 B loader 和 C 磁盘索引校验，并核对来源引用存在于本样本。只返回匹配当前配置、模型、完整源产物的索引。
3. retrieve 复用 SharedRetrieval，并把完整请求/分析身份、索引快照和最终候选 ID 顺序绑定到当前执行上下文。
4. load_candidates 和回答前再次核对当前索引。读取后发布版本改变时拒绝旧查询，不能把旧候选交给新索引回答器。
5. 回答时为本次样本/索引创建轻量 VectorAnswerer，复用共享 runtime；不加载新模型。输入必须与本次选定候选 ID 及顺序一致。绑定仅消费一次。

ContextVar 隔离不同线程/任务的执行上下文，不用全局“当前样本”。适配器面向 A 现有同步调用流程，同一上下文里先检索 A、再检索 B、最后回答 A 会被拒绝；未来若 A 跨线程拆分检索和回答，应改为显式传递查询句柄，不能依赖隐式上下文传播。

各次校验保证不会静默切换到新产物，并不持有 B 发布锁。最后校验后如又发布新版本，本次仍基于已固定的旧快照执行；调用方若要求全链路强一致发布事务，需要 B/A 协同提供锁或版本租约。模型文件哈希和重复磁盘加载有开销，真实规模下再测量优化。

## 验证证据

新增 **9 项测试**：两样本切换与并发、ready/缺失/损坏/过期状态、检索后发布新版的拒绝与恢复、错误样本路径、无绑定/交错查询拒绝、manifest 与来源引用不匹配。只读测试检查不编码、不生成、不创建缺失索引。

Python 3.11 全项目 **615 passed**，Mock smoke PASS。

```bash
.venv/validation311/Scripts/python.exe -m pytest -q -p no:cacheprovider
.venv/validation311/Scripts/python.exe scripts/check_c_service_integration.py --a-ref f4b82a6b3d150b856c2a32350039ca8ca7c90d7d
```

联调脚本在独立进程中从本地 Git 执行已审阅 A 版本的实际服务、状态类型、调度器和来源仓库；使用本地现有 WebBindings，不编辑 checkout 或持久安装这些 A 文件。只对可信版本使用该脚本。

实际断言通过：两个样本回答合法且 Memory ID 不交叉；浏览/回查来源和状态时问题编码次数为 0；发布新 Memory manifest 后旧索引报过期，显式重建后恢复；缺失索引报 INDEX_NOT_FOUND、不自动重建，而记忆浏览仍可用。B 产物为明确 synthetic_fixture，QA/Embedding 为合成替身；并非真实 B 加载器、真实模型或浏览器页面问答验收。

全部改动继续暂存，未 commit/push。下一步仍需对齐 A/D 答案格式、补错误码映射，并在 B 真实产物可用后进行真实数据效果验证。
