---
marp: true
theme: default
paginate: true
size: 16:9
style: |
  section { font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; font-size: 25px; line-height: 1.20; padding: 52px 72px; }
  section h1 { font-size: 1.62em; margin-bottom: 0.44em; }
  section h2 { font-size: 1.15em; }
  section table { display: table; width: 100%; max-width: 100%; box-sizing: border-box; margin: 0.42em 0 0.45em; table-layout: auto; }
  section table th, section table td { padding: 5px 9px; line-height: 1.18; vertical-align: middle; }
  section li { margin: 0.17em 0; }
  section blockquote { font-size: 1.1em; }
  section.compact-last { font-size: 23px; line-height: 1.14; }
  section.compact-last h1 { font-size: 1.63em; margin-bottom: 0.36em; }
  section.compact-last table { margin-top: 0.3em; margin-bottom: 0.25em; }
  section.compact-last table th, section.compact-last table td { padding-top: 4px; padding-bottom: 4px; }
  section.compact-last li { margin: 0.08em 0; }
  section.compact-last p { margin-top: 0.25em; margin-bottom: 0.25em; }
---

# Milestone 2 — Project Proposal

## 智能体动态记忆管理

**课程：** 软件需求工程  

**团队名称：** 收手吧阿组  

**成员：** 吴宇轩、刘星宇、杨林涛、郭柏村、**常珂舒**

**项目周期：** 2026.10.12 — 2026.11.09

---

# 1. 项目背景——与智能体长期对话中的记忆管理

在与智能体进行时间周期较长的对话时，用户的状态自然会发生变化，例如：

**正在求职 → 已经入职 → 工作变更**

在与大模型的对话中，模型检索或许能够找到相关历史，但不一定知道该使用哪条记录。

- 用旧状态作为参考来回答当前的问题，可能得到过时结果；
- 把之前发生过的事件当作即将发生的事件，会混淆计划和事实；
- 如果在记忆控制中直接删除“过时”的结果，会导致记忆的丢失。

因此本项目的基本目的就是希望在保留历史记忆的同时，判断哪些信息应该被用于本次回答。

---

# 2. Vision——项目愿景

> **让智能体不仅保留记忆，还能根据问题判断哪些记忆适合用于当前回答。**

围绕这一基本目标，可以将项目的需求分为以下三项：

- **记忆可追溯：** 提取事实、事件和状态，保留时间及原文来源；
- **新旧状态可区分：** 根据查询意图和时间条件判断更新、替代或冲突；
- **回答可以核实：** 展示使用的记忆与证据，证据不足时说明原因。

**例如：** 

“现在在哪里工作？”智能体能够判断并找到“最新有效”的记忆，并利用其进行回答；

“过去在哪里工作？”智能体则会查询历史记忆，选取工作相关记忆进行回答。

---

# 3. Scope｜项目范围

| 项目范围 | 主要内容 |
|---|---|
| 数据与 Memory | LoCoMo 导入、结构化记忆、JSON 导出/回读与来源 |
| 检索与状态解析 | 查找相关历史，判断更新、替代、冲突和有效信息 |
| 问答与 Web | 根据证据回答，展示候选、关系和原文 |
| 对照与评测 | Full Text / Vector RAG / Dynamic 三路比较 |
| 工程交付 | 源码、测试、SRS、项目报告及过程记录 |

---

# 4. 数据范围｜LoCoMo

**LoCoMo 官方数据共 10 段长对话，根据课程要求，我们将 10 段对话的用法分为：**

| 数据组 | 对话顺序 | 使用方式 |
|---|---|---|
| **main_dev** | 1–4 | 主要开发与调试 |
| **extra_dev** | 5–8 | 补充开发检查，单独报告 |
| **test** | 9–10 | 独立测试，配置确定后使用 |

- 课程主实验采用 **4 段用于训练 + 2 段独立测试**；
- 样本分组按照技术文档 v2.1 的原始文件顺序；
- 标准答案和证据 Gold 仅供评测，不能进入记忆抽取、检索和问答。

---

# 5. Features｜功能规划

| 功能 | 主要内容 | 优先级 |
|---|---|---|
| **F-01** 对话数据处理 | 规范化会话、轮次、时间和来源 | Must |
| **F-02** 结构化 Memory | 事实/状态抽取、JSON 回读 | Must |
| **F-03** 查询与检索 | 识别问题范围，检索相关候选 | Must |
| **F-04** 动态状态判断 | UPDATE / SUPERSEDE / CONFLICT | Must |
| **F-05** 证据约束 QA | 有依据地回答或说明不确定 | Must |
| **F-06** Web 展示 | 记忆、原文、候选、关系和答案 | Must |
| **F-07** 三路对照与评分 | 运行三种方法并保存逐题结果 | Must |
| **F-08** 纯向量检索消融 | 补充比较未扩展的 Top-K | Should（可选） |

---

# 6. Key Feature｜关键功能——动态记忆判断

**Vector RAG** 直接使用检索到的 Memory 回答；**Dynamic** 会进一步检查候选间的状态关系。

不同候选之间的状态关系包括：

| 处理结果 | 作用 |
|---|---|
| **UPDATE** | 同一事项的进展或补充 |
| **SUPERSEDE** | 有证据的新状态替代旧状态的当前适用性 |
| **CONFLICT** | 信息相互矛盾，暂时无法消解 |
| **RETAIN / EXCLUDE / UNRESOLVED** | 本次查询采用、排除或暂不确定某条 Memory |

**这些决定只对回答产生影响，而不是对历史记录的修改。**

还需要区分计划与事实、事件时间与持续状态，以及“当前”和“曾经”两类问题。

---

# 7. Web｜网页端——用户使用

1. **选择对话样本：** 浏览已经构建的 Memory；
2. **查看记忆详情：** 阅读内容和原始来源；
3. **输入问题：** 选择 Full Text、Vector RAG 或 Dynamic；
4. **查看回答：** 同时查看相关候选和使用的证据；
5. **查看动态结果：** Dynamic 显示状态关系及有效记忆；
6. **处理不确定情况：** 证据不足或存在冲突时说明原因。

Web 端实现的功能不仅是给出一段回答，也能让用户查询回答使用到的历史信息。

---

# 8. Evaluation｜对照实验与验收

| 方法 | 回答时使用的信息 | 状态处理 |
|---|---|---|
| **Full Text** | 同一样本的原始对话历史 | 无 |
| **Vector RAG** | 检索出的结构化 Memory | 无 |
| **Dynamic** | 与 Vector RAG 相同的候选，先解析状态 | 有 |

三种方法均采用相同题集和可比较的基础问答模型设置。

**用于评测的指标：**

- **QA：** LoCoMo 官方或课程评分，报告整体及分题型结果；
- **检索：** Evidence Recall@5 / Recall@10；
- **动态状态：** 关系、单条 Memory 决定、有效集合（缺正式 Gold 则标 N/A）。

**验收：** JSON 可回读、原文可定位、Web 可运行、三路结果可核查。

---

<!-- _class: compact-last -->

# 9. Future Roadmap｜未来四周计划与团队分工

| 周次 | 主要工作 | 预期成果 |
|---|---|---|
| **Week1 10.12–10.18** | 方案、接口、仓库和模块启动 | 工程框架、合同测试 |
| **Week2 10.19–10.25** | 核心模块实现、初步联调 | 功能初版、SRS |
| **Week3 10.26–11.01** | 个人模块完成、开发集验证 | 11月1日前模块交付 |
| **Week4 11.02–11.09** | 完整集成、独立测试、报告 | 最终系统与演示 |

**分工：** 

- A 常珂舒（共享LLM、基线、评测、集成）
- B 吴宇轩（Memory）
- C 郭柏村（检索）
- D 杨林涛（动态QA）
- E 刘星宇（Web）

代码仓库位于 https://github.com/ACN67/SRE-Project  

项目最终提交材料包括 Web 端、源代码、Memory JSON、三路实验与分析、SRS、报告及开发/会议记录。
