---
marp: true
theme: default
paginate: true
size: 16:9
style: |
  section {
    font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    font-size: 22px;
    line-height: 1.38;
    letter-spacing: 0.018em;
    padding: 38px 52px;
  }
  section h1 {
    color: #1E3A5F;
    font-size: 1.54em;
    margin-top: 0;
    margin-bottom: 0.40em;
    line-height: 1.24;
  }
  section h2 {
    color: #2A5278;
    font-size: 1.13em;
    margin-top: 0.20em;
    margin-bottom: 0.25em;
  }
  section p {
    margin: 0.27em 0 0.40em;
  }
  section ul, section ol {
    margin-top: 0.26em;
    margin-bottom: 0.28em;
  }
  section li {
    margin: 0.19em 0;
  }
  section table {
    width: 100%;
    max-width: 100%;
    font-size: 0.83em;
    line-height: 1.36;
    border-collapse: collapse;
    margin: 0.40em 0 0.58em;
  }
  section table th {
    background: #1C4C82;
    color: white;
    font-weight: 700;
    padding: 8px 11px;
    border: 1px solid #C9D5E5;
    vertical-align: middle;
  }
  section table td {
    background: #F6F9FC;
    padding: 8px 11px;
    border: 1px solid #CFD9E6;
    vertical-align: middle;
  }
  section table tr:nth-child(even) td {
    background: #EAF2FA;
  }
  section blockquote {
    background: #EEF4F9;
    border-left: 5px solid #1C4C82;
    padding: 0.26em 0.60em;
    font-size: 0.98em;
    margin: 0.55em 0;
  }
  section strong {
    color: #1E426A;
  }
  section.cover {
    display: flex;
    flex-direction: column;
    justify-content: center;
    font-size: 24px;
  }
  section.cover h1 {
    font-size: 1.90em;
    margin-bottom: 0.50em;
  }
  section.dense {
    font-size: 21px;
  }
  section.dense table {
    font-size: 0.84em;
  }
  section.diagram {
    font-size: 21px;
  }
  section.diagram table {
    font-size: 0.81em;
  }
  section.diagram table th, section.diagram table td {
    padding: 10px 9px;
  }
  section.diagram h2 {
    margin-top: 0.38em;
  }
---
<!-- _class: cover -->

# 智能体动态记忆管理系统

## Milestone 4｜中期展示

**整体架构 · 关键设计 · Use Case · 开发与测试**

**课程：** 软件需求工程  
**团队：** 收手吧阿组  
**成员：** 吴宇轩、郭柏村、杨林涛、刘星宇、常珂舒

**项目周期：** 2026.10.12—2026.11.09

---

<!-- _class: dense -->

# 1. 系统整体架构

采用**离线记忆构建 + 在线问题处理**，将长对话预处理与用户每次提问分开。

| 离线构建 | 在线查询 | 统一输出 |
|---|---|---|
| LoCoMo 原始对话 | 用户选择对话、输入问题 | 回答、状态及证据 |
| ↓ 数据导入与规范化 | ↓ 问题意图与时间分析 | ↓ Web 展示 |
| ↓ 结构化 Memory 与来源 | ↓ 相关及历史候选检索 | ↓ 实验评测 |
| ↓ 校验、保存与索引 | ↓ 根据方法生成回答 | 保留可核查记录 |

### 三条回答路径

| Full Text | Vector RAG | Dynamic |
|---|---|---|
| **原始对话 → 回答** | **候选 Memory → 回答** | **相同候选 → 状态解析 → 有效 Memory → 回答** |
| 直接利用原文作为基线 | 检查结构化检索的效果 | 检查状态判断带来的区别 |

**设计原则：** 原始对话始终保留；Web 和评测复用统一结果，不各自实现一套判断。

---

<!-- _class: diagram -->

# 2. 记忆处理原理：Memory Processing

**目标：** 将 LoCoMo 长对话转成可检索、可回读、可追溯的 Memory 集合。

| ① LoCoMo 数据 → | ② Loader → | ③ Window → | ④ LLM 抽取 → | ⑤ 校验与产物 |
|---|---|---|---|---|
| 多会话原始文本 | 规范化 Sample / Session / Turn | 按会话和长度分窗 | 提取人物、事实、状态与时间 | 检查字段和来源，形成 Memory JSON |

### Memory 数据内容与校验重点

| 关键信息 | 作用 | 校验关注点 |
|---|---|---|
| `memory_id`、`content` | 每条事实的身份与内容 | 身份稳定、事实不为空 |
| `entity`、`attribute`、`value` | 描述谁的什么属性 | 不把没有依据的信息补成事实 |
| `fact_kind`、`assertion_type` | 区分状态、事件、计划与事实 | “计划做”不能直接视为“已经做” |
| `recorded_at`、`event_time` | 区分记录时间与事件时间 | 时间不明时保留未知 |
| `source_refs` | 定位原始 Session / Turn | 来源真实存在，可以逐轮回查 |

**构建结果还需区分完整成功、部分完成与失败：不完整产物不能直接用于正式问答。**

---

<!-- _class: diagram -->

# 3. 查询处理原理：Query Processing

**核心问题：** 找到相关记忆之后，还要判断它们是否适合回答**这一次**提问。

| ① 用户输入 → | ② 查询分析 → | ③ 候选检索 → | ④ 状态判断 → | ⑤ 回答与来源 |
|---|---|---|---|---|
| 指定对话与问题 | 判断当前/历史意图及时间条件 | 找到相关及旧阶段 Memory | Dynamic 分析更新、替代与冲突 | 选择本次适用证据，并解释结果 |

### 动态状态的两层判断

| 层次 | 输出 | 含义 |
|---|---|---|
| **记忆之间的关系** | UPDATE / SUPERSEDE / CONFLICT | 进展、替代、尚无法消解的矛盾 |
| **针对当前问题的决定** | RETAIN / EXCLUDE / UNRESOLVED | 本次保留、排除、暂不能确定 |

### 三路处理的区别

- **Full Text：** 直接使用原始对话，不经过 Memory 检索；
- **Vector RAG：** 检索候选后直接回答；
- **Dynamic：** 与 Vector RAG 使用**相同候选**，先判断状态，再选择证据回答。

**旧记忆本次被排除，不等于从历史中删除。**

---

<!-- _class: dense -->

# 4. 模块划分与协作

| 模块 | 主要设计 | 与其他模块的关系 |
|---|---|---|
| **B｜数据与记忆** | 导入 LoCoMo、规范化、Memory 抽取与来源回查 | 向 C、A/E 提供记忆及原文 |
| **C｜查询与检索** | 判断意图、时间条件，检索相关及历史候选 | 向 D、问答流程提供候选 |
| **D｜动态状态解析** | 识别更新、替代、冲突，选出有效记忆 | 接收 C 的候选，输出状态结果 |
| **A｜共享服务与评测** | 模型调用、问答入口、Full Text、对照实验 | 统一业务入口和结果格式 |
| **E｜Web 展示** | 样本选择、Memory 详情、来源回查及后续问答 | 消费统一服务的只读结果 |

### 模块之间如何保持一致？

- **统一数据格式：** Memory、候选、关系、回答结果有共同约定；
- **职责分离：** 检索负责“找”，动态解析负责“判断”，Web 负责“展示”；
- **可定位问题：** 可以区分是抽取遗漏、候选未召回，还是状态判断出错。

---

<!-- _class: dense -->

# 5. Use Case：从需求到系统行为

Use Case 需要同时交代：**用户希望完成什么、系统应如何响应、怎样检验结果。**

| 使用场景 | 用户操作 | 设计与验收关注点 |
|---|---|---|
| **浏览 Memory** | 选择样本，查看记录、详情及原始发言 | 记忆来源真实且可回查 |
| **查询当前状态** | “现在在哪里工作？” | 选取最新有依据的适用状态 |
| **查询历史状态** | “以前在哪里工作？” | 保留并使用当时适用的旧状态 |
| **处理冲突或证据不足** | 输入时间不明或信息矛盾的问题 | 说明原因，不随意下确定结论 |
| **对比三种方法** | 对同一问题执行 Full Text / Vector RAG / Dynamic | 题目、输入条件、评分可比较 |

**为什么强调 Use Case？** 它连接了 SRS 中的功能需求、各模块的实现方式，以及后续端到端测试。

---

<!-- _class: dense -->

# 6. 重点 Use Case：当前状态与历史状态

**说明性示例：** 先记录“在 A 公司工作”，后来明确记录“已离职并加入 B 公司”。

| 输入问题 | 系统应选用的证据 | 预期行为 |
|---|---|---|
| **现在在哪里工作？** | 最新有证据支持的 B 公司记录 | A 的旧状态不用于当前回答 |
| **以前在哪里工作？** | 过去在 A 公司的记录 | 旧事实仍保留，来源可追溯 |
| **何时从 A 转到 B？** | 两次状态及其时间证据 | 时间不足或有冲突时说明不确定 |

### 系统处理流程

**输入问题 → 分析时间和意图 → 检索新旧 Memory → 判断状态关系 → 选择适用证据 → 回答并显示来源**

### 对应测试场景

- 新状态确实替代了旧状态时，当前问题应回答正确；
- 查询过去时点时，系统仍能够找回旧记忆；
- 只有计划、时间不明或证据冲突时，不能编造确定结论。

*以上为设计与验收示例，不代表当前已经完成真实端到端演示。*

---

<!-- _class: dense -->

# 7. 当前实际开发进度

| 功能方向 | 已实现或已形成的阶段性成果 |
|---|---|
| **需求与系统设计** | 完成总体架构、SRS、模块职责以及典型 Use Case |
| **数据导入与原文回查** | 已能规范化读取对话、校验数据身份和划分，并按轮次定位原始内容 |
| **共享模型基础** | 已具备统一模型调用、模拟运行、结构化输出检查和基础失败处理 |
| **Web 一期 UI** | 已形成可操作界面，可选择样本、筛选和分页浏览 Memory、查看详情及来源 |
| **检索准备** | 本地文本向量表示已有独立初步实现与测试，尚待接入完整检索业务 |

### 中期阶段的成果与边界

- 已有**可运行的基础功能和可见的 Web 界面**，不只是纸面设计；
- Web 一期主要采用明确标注的**合成示例**验证交互；
- 真实 Memory 抽取、动态问答和完整三路评测仍需后续集成。

---

<!-- _class: dense -->

# 8. 当前测试情况

| 测试层次 | 已进行的测试 | 能证明什么 |
|---|---|---|
| **数据与来源** | 数据格式、划分约束、样本和轮次定位、无效引用检查 | 基础数据能按规则组织和回查 |
| **公共业务能力** | 共享对象校验、模型调用、模拟输出及异常处理 | 模块间的基础数据和调用规则可检查 |
| **Web 交互** | 页面显示、记忆筛选、分页、详情、来源查看及空/错误状态 | 界面逻辑与合成示例下的操作可用 |
| **检索准备** | 本地文本向量表示的独立基础检查 | 为后续真实索引和召回做准备 |

### 与最终验收的区别

**目前已完成的是基础功能与接口测试，不是完整的真实业务链路验收。**

下一步需要以 LoCoMo 开发样本验证：
**数据导入 → Memory 抽取 → 检索 → 动态状态判断 → 问答 → Web 展示**。

测试还要覆盖历史状态、计划与事实、冲突、无证据以及系统执行失败。

---

<!-- _class: dense -->

# 9. 三路对照与下一阶段计划

### 计划采用的实验对照

| 方法 | 主要输入 | 对比目的 |
|---|---|---|
| **Full Text** | 原始对话 | 原始文本基线 |
| **Vector RAG** | 结构化记忆候选 | 普通检索的结果 |
| **Dynamic** | 与 Vector RAG 相同候选，经状态解析 | 单独分析状态判断的作用 |

三路使用同一题集与可比较的模型条件；计划报告 **QA 表现、Evidence Recall@K 和动态状态判断指标**，真实结果以正式运行和可用 Gold 为准。

### 后续工作

1. **完成真实 Memory 构建与检索索引集成；**
2. **接通动态状态判断、问答及 Web 展示；**
3. **围绕 Use Case 进行端到端联调与异常测试；**
4. **冻结配置，运行独立测试及三路实验。**

> **中期结论：基础架构与可操作功能已经形成，下一阶段以完整集成和效果验证为重点。**
