# A 分支第 3 阶段：离线合成闭环验收

基线：A，`02923f7bb8d5b61ff3c64ae7619c768a01aeaba6`。本页只记录 `synthetic_fixture`；所有分数均为 `not_a_formal_score`。实现入口是 `tests/integration/test_a_standalone_flow.py`，产物仅写入 pytest `tmp_path`，没有使用正式 `data/` 或 `results/`。

## 验收证据

| 条目 | 本轮实测证据 |
| --- | --- |
| 合成输入 | 8 个稳定样本 ID、9 道题、5 种题型；answered、no_evidence、error；空候选合法路径、分析失败、回答端口异常、1 道缺失 Gold。数据快照和 split 在临时目录冻结。 |
| 三路保存与验收 | A-17 保存 27 条预测及 3 份 receipt；`verify_run` 独立从磁盘重读，逐文件 SHA-256 与 receipt 相符。主 run 候选比较为 identical 7、mismatch 1（最终候选确实不同）、not_comparable 1；另一个身份未知的 run 经磁盘核验触发 unverified。 |
| 消融隔离 | `pure_vector_topk` 独立保存、回读 9 条预测；raw Top-K 审计通过，history_expansion=false、state_resolution=false；主 run 全部文件字节不变，普通检索和状态解析调用次数也不变。 |
| Gold 后置评分 | 三路、身份未知 run 与消融的保存/验收全部完成后，才调用 `evaluate_saved_qa` 的 Gold gate。9 条预测中 Gold 8、缺失 1、执行失败 2；官方合成分总和 3，主均值 3/8=0.375，条件均值 3/6=0.5，文本 token F1 主均值 1/8=0.125。原因分布：执行失败 2、缺 Gold 1、无证据 5、其他 1、时间不确定 0。二维分组 9 格，包含缺 Gold 格的零分母 `None`。 |
| 人工算例 | `qa_synthetic-0_0` 为题型 1、答案完全相同，官方分与 token F1 均为 1，分母各 1。`qa_synthetic-0_1` 为题型 5、无证据适配文本 “No information available”，官方题型 5 分为 1、token F1 为 0，分母各 1；程序逐项断言一致。 |
| 结果导出与只读消费 | 评分清单 `save_qa_scoring_manifest` 后经 `verify_qa_scoring_manifest` 验证；摘要 JSON 明确写入 `synthetic_fixture` 和 `not_a_formal_score`。`DynamicMemoryService` 经 `WebBindings` 回查合成原文，拒绝跨样本 SourceRef，未建 Memory 返回 `BUILD_STATUS_UNAVAILABLE`。 |
| 敏感字符串 | 合成 Gold 和异常注入 `SECRET_GOLD`、`SECRET_KEY`；断言预测、清单、摘要和捕获日志均不含这些字节。源 Gold 文件自身故意含有哨兵，未作为业务导出。 |

## 本次产物指纹

以下 SHA-256 来自本轮全量测试的临时目录 `C:\Users\Keshu\AppData\Local\Temp\pytest-of-Keshu\pytest-276\test_a_standalone_synthetic_fl0`。运行清单含执行时间，因此再次运行时其哈希可以变化；预测字节与评分结论可重复。

| 文件 | SHA-256 |
| --- | --- |
| `synthetic-main/full_text.predictions.json` | `5db60874d0643b7864ad675b82830c3d7d9a92dc009620330d7a8c2408232f75` |
| `synthetic-main/vector_rag.predictions.json` | `25d080f1b4297d20def8f18bcbbf238d7f100e11414275896eaad497b5cf16e7` |
| `synthetic-main/dynamic.predictions.json` | `afb39b32dd5b3fb2e0b34d102561f3760107f7f64777b557422f61ff04959305` |
| `synthetic-main/run_manifest.json` | `0be1b69760e1d2e979634d009a2666514ba0fca92063681a24536dab31bc3d76` |
| `synthetic-ablation/pure_vector_topk.predictions.json` | `d90673e8b2d794444f9583eb736042ba884a4532fac1bb03630bc22cc43b9b1a` |
| `synthetic-ablation/run_manifest.json` | `a419ea05c79aa3f3cf11ef59e063f1dafcc964111467c259bb2074e57e653791` |
| `qa_scoring_manifest.json` | `b430a9af2f299dcc207318b904af3e2c62c27c72a5142063f8736845fd2b495c` |
| `synthetic_flow_summary.json` | `0293dcef53cc70da244082b637e71c9733872a68479300a362e8c048072fa6f3` |

## 自检

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| `.\.venv\Scripts\python.exe --version` | 0 | Python 3.11.9 |
| `.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_a_standalone_flow.py` | 0 | 1 passed；同一命令在现有环境重复通过。 |
| `C:\Users\Keshu\OneDrive\Desktop\历史文件\大三上\软件需求工程\SRE-Project\.venv\Scripts\python.exe -m pytest -q` | 0 | 685 passed in 18.86s；此前一次也是 685 passed。 |
| `.\.venv\Scripts\python.exe -m pytest tests/web -q` | 0 | 33 passed |
| `.\.venv\Scripts\python.exe -m pytest tests/contract -q` | 0 | 97 passed |

全新 Python 3.11 虚拟环境的安装复跑未执行：本轮禁止联网、下载和新增依赖，只能在仓库现有 `.venv` 重复验证。合成路径不代表 B/C/D/E 正式接线、课程 Gold 或正式实验成绩。

## 第 3 阶段第 2 轮复核（2026-10-31）

本轮只增强 `tests/integration/test_a_standalone_flow.py` 的证据断言，保留前两阶段的未提交改动。主三路 run 经 `verify_run` 从磁盘回读后，候选比较精确计数为 `identical=7`、`mismatch=1`、`not_comparable=1`、`unverified=0`；另一个独立落盘并回读的未知索引身份 run 命中 `unverified`。纯向量消融独立回读 9 条，主 run 文件字节以及普通检索、状态解析调用次数未变。全部保存与核验完成后才调用 synthetic Gold 评分。只读服务从重新打开的合成 source bundle 文件经 `DynamicMemoryService`/`WebBindings` 读取原文，并验证跨样本和未建 Memory 的拒绝路径。

人工逐格账本与 `QAReport`、保存后的评分清单均做完全相等断言：

| 题型 × 样本 | 预测 | Gold 计分 | Gold 缺失 | 执行失败 | 主均值 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 × synthetic-0 | 1 | 1 | 0 | 0 | 1 |
| 1 × synthetic-5 | 1 | 1 | 0 | 0 | 0 |
| 2 × synthetic-1 | 1 | 1 | 0 | 1 | 0 |
| 2 × synthetic-6 | 1 | 1 | 0 | 0 | 0 |
| 3 × synthetic-7 | 1 | 1 | 0 | 0 | 0 |
| 4 × synthetic-3 | 1 | 1 | 0 | 1 | 0 |
| 5 × synthetic-0 | 1 | 1 | 0 | 0 | 1 |
| 5 × synthetic-4 | 1 | 1 | 0 | 0 | 1 |
| unknown × synthetic-2 | 1 | 0 | 1 | 0 | null |

总计 9 题、8 个 Gold 分母、2 个执行失败、3 个合成得分；原因分布为 `execution_failure=2, missing_gold=1, no_evidence=5, time_uncertain=0, other=1`。预测、运行与评分清单、导出摘要以及捕获日志中均无注入的 `SECRET_GOLD`/`SECRET_KEY`；导出摘要标记 `synthetic_fixture`、`not_a_formal_score`。

| 本轮实际命令 | 退出码 | 关键输出 |
| --- | ---: | --- |
| `& 'C:\Users\Keshu\OneDrive\Desktop\历史文件\大三上\软件需求工程\SRE-Project\.venv\Scripts\python.exe' -m pytest -q`（增强断言前） | 0 | `685 passed in 42.98s` |
| `& 'C:\Users\Keshu\OneDrive\Desktop\历史文件\大三上\软件需求工程\SRE-Project\.venv\Scripts\python.exe' -m pytest -q`（增强断言后，最终自检） | 0 | `685 passed in 57.41s`，0 failed |
| `& 'C:\Users\Keshu\AppData\Local\Programs\Python\Python311\python.exe' -m pytest -q` | 1 | `No module named pytest` |

系统 Python `C:\Users\Keshu\AppData\Local\Programs\Python\Python311\python.exe` 为独立于项目 `.venv` 的 3.11.9 解释器；检查显示 `pytest`、`pydantic`、`yaml`、`streamlit`、`nltk`、`regex` 均不可导入。本轮禁止联网及擅自安装依赖，因此“干净 Python 3.11 环境同命令重复成功”**未通过**，不得将 `.venv` 的成功复跑充当该证据。第 3 阶段仍有此环境验收阻断，不宣称阶段完成。

最终全量测试的合成闭环临时目录为 `C:\Users\Keshu\AppData\Local\Temp\pytest-of-Keshu\pytest-283\test_a_standalone_synthetic_fl0`。本轮预测文件 SHA-256 与上表相同；评分清单为 `0b460d31177fc015ed82301fbb30e533f1e3fc85b271e3e30e7a39a71b5bb802`，摘要为 `36d779b91a25a00fce39b1edc13d9586c47ed0b2289815a354f72f31878ca2d3`。评分清单含本轮运行清单哈希，跨次运行的清单哈希不要求相同；相同的是题目、预测字节和评分结论。

## 第 3 阶段第 3 轮复核（2026-10-31）

在已有合成集成测试中增加两次独立三路运行的逐文件预测字节相等断言；第二次运行故意将索引身份设为 `unknown/unverified`，所以候选可比性结论为 `unverified`，但预测字节仍与第一次完全一致。全部 9 道题和五种题型、四类候选比较状态、纯向量消融隔离、Gold 后置评分、二维账本、只读服务与秘密哨兵断言保持通过。该测试仅使用 `tmp_path`，分数标记 `synthetic_fixture` 和 `not_a_formal_score`。

| 本轮实际命令 | 退出码 | 关键输出 |
| --- | ---: | --- |
| `& 'C:\Users\Keshu\OneDrive\Desktop\历史文件\大三上\软件需求工程\SRE-Project\.venv\Scripts\python.exe' -m pytest -q` | 0 | `685 passed in 51.58s`，0 failed |
| `& '.\.venv\Scripts\python.exe' -m pytest tests/web tests/contract -q` | 0 | `130 passed in 21.57s`，0 failed |
| `& '.\.venv\Scripts\python.exe' -m pip cache list` | 0 | `No locally built wheels cached.` |

现有 `.venv` 和独立系统解释器均为 Python 3.11.9。独立系统解释器缺少 pytest 等项目依赖，本机 pip 缓存也没有可用 wheel；按本轮不联网、不新增依赖的限制，无法在全新 Python 3.11 虚拟环境执行同一测试命令。因此验收条目 4 中的“干净环境复跑”仍未实证满足，阶段保持未完成；本轮通过数不替代该门槛，也不代表正式 B/C/D/E 接线或业务成绩。

## 第 3 阶段第 4 轮复核（2026-10-31）

本轮保留原有未提交改动，复核合成闭环实现及全部五项验收证据。`tests/integration/test_a_standalone_flow.py` 在 `tmp_path` 构造 8 个稳定样本 ID、9 道题、5 类题型、合法零候选、两处失败和一处缺失 Gold；三路运行保存后由 `verify_run` 从磁盘重读。主运行比较状态逐项为 `identical=7`、`mismatch=1`、`not_comparable=1`，独立未知身份运行验证 `unverified`；`pure_vector_topk` 独立回读且主运行文件字节不变。Gold 评分在所有预测保存与验收之后执行，二维人工账本、原因分布、评分清单与只读服务消费均通过断言。输出包含 `synthetic_fixture` 与 `not_a_formal_score`；敏感哨兵未进入预测、清单、摘要和捕获日志。这些是合成证据，不是正式业务成绩。

| 本轮实际命令 | 退出码 | 关键输出 |
| --- | ---: | --- |
| `& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_a_standalone_flow.py -q` | 0 | `1 passed in 4.82s` |
| `& 'C:\Users\Keshu\OneDrive\Desktop\历史文件\大三上\软件需求工程\SRE-Project\.venv\Scripts\python.exe' -m pytest -q` | 0 | `685 passed in 48.30s`，0 failed |
| `& '.\.venv\Scripts\python.exe' -m pytest tests/web -q` | 0 | `33 passed in 21.04s`，0 failed |
| `& '.\.venv\Scripts\python.exe' -m pytest tests/contract -q` | 0 | `97 passed in 0.60s`，0 failed |
| `& '.\.venv\Scripts\python.exe' -m pytest -q tests/integration/test_a_standalone_flow.py` | 0 | 重复执行：`1 passed in 1.69s` |
| `& 'C:\Users\Keshu\AppData\Local\Programs\Python\Python311\python.exe' -m pytest -q tests/integration/test_a_standalone_flow.py` | 1 | `No module named pytest` |

独立系统 Python 为 3.11.9；本轮再次确认 `pytest`、`pydantic`、`yaml`、`nltk`、`regex` 均不可导入，现有 `.venv` 的 pip 缓存报告 `No locally built wheels cached.`。在禁止联网、安装依赖的边界内，无法实证“干净 Python 3.11 环境用同一命令重复成功”，故验收条目 4 仅部分通过，第 3 阶段仍未完成。未触碰 `configs/`、模板、`data/`、原有 `results/`，未运行真实模型、外部 API 或任何 Git 写操作。
