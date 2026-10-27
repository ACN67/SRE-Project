# C-06 Vector RAG 回答工作记录

## 10/27 提交前审查修复

修复 P2：预算诊断原先没有查询/样本身份，不同问题使用相同记忆时记录完全相同。现增加 query_id、sample_id、query_fingerprint、retriever_fingerprint；指纹与检索快照一致，从调用局部的已验证请求/分析计算，不写入问题正文，不引入共享可变查询状态。

新增 7 个回归用例，覆盖不同查询 ID、同 ID 改题/改分析及四种早停/预算分支；扩充原多样本并发、索引更新测试，核对记录归属。专项 38 passed，全项目 631 passed，A 新服务合成联调通过。下文保留原阶段验证记录。

日期：2026-10-26。基线 `361b468`；按用户要求与 C-04/C-05 一起保留，尚未提交或推送。

## 交付

- VectorAnswerer 三参数回答端口及 callable，通过 A 共享 runtime 调用 QA_PROMPT，不调用 D。
- 绑定索引核验候选内容/来源，输入完整 Memory 与 QueryAnalysis；按完整消息预算保留 rank 前缀，记录 Token 排除项。
- 校验答案状态、文本 JSON 封装和引用边界；代码生成实际使用 ID/来源；失败不能进入成功缓存。
- 空候选和明确时间冲突无模型早停；默认 Mock 占位答案禁止冒充业务回答；错误脱敏。
- 使用说明记录文本输出封装与 A Full Text 自由文本之间的差异，明确三路实验前需统一。

## 验证

Python 3.11.15 全项目 **598 passed**，Mock smoke PASS；本模块新增 **22 项测试**。全部使用明确合成 Adapter，经实际 LLMRuntime/Guard/Recorder 执行；无真实 API 请求。

覆盖回答公共上下文合同、正常缓存命中与问题变化失效、完整 Memory 预算裁剪、禁止引用被裁掉的来源、估计无效或未知、预算全超、空输入、时间冲突、错样本/查询/篡改 Memory、重复候选、伪造引用、无来源回答、坏格式不缓存、模型不确定、默认 Mock 禁止、Provider 错误脱敏、截断与重复 JSON 键。

```bash
.venv/validation311/Scripts/python.exe -m pytest -q -p no:cacheprovider
.venv/validation311/Scripts/python.exe -m dynamic_memory.pipeline.mock_smoke
```

## 尚待完成

A 真实调度器注入与错误码映射、A/D 回答格式统一、真实数据来源核验和模型回答效果评估。当前证据说明结构和调用链正确，不代表答案语义正确率。未改公共 Schema 或正式配置开关，未发起付费模型请求。下一步先完成 A/B 接口联调准备，再在显式配置下做小规模真实样本验证。
