# D 验证记录

日期：2026-11-04（Asia/Shanghai）。源目录为用户提供的 `SRE-Project-main` 下载副本；本交付是可修改的独立副本。

## 测试结果

| 检查 | 结果 |
| --- | --- |
| D 新增测试 `tests/dynamic` | 59 passed |
| D 新增测试 + 全部公共合同测试 `tests/contract` | 158 passed |
| 合成 CLI smoke | PASS；使用固定预设模型完成文本 |
| CLI 已存在输出保护 | PASS；原文件内容保持原样 |
| CLI 实际模型显式启用门槛 | PASS；缺少 `--api-enabled`/`--model-id` 时退出，未调用模型 |
| 完整回归（交付副本） | 1153 passed，19 failed |
| 同环境原始代码对照 | 1094 passed，19 failed |
| 回归失败集合比较 | 完全相同，无新增失败 |

D 测试包括实际 DeepSeekLLMAdapter 的内存 SDK 客户端路径（JSON 状态输出、共享 QA 参数），以及 B Builder → C Index → A Orchestrator → D → E WebBindings 的两个样本、12 次并发查询。接口和真实组件被调用，模型完成文本仍是明确合成预设。历史选择等预设场景验证的是结果处理和上下文约束，不是模型语义准确率。

## 既有失败原因

17 项评测批量运行测试要求 Git 提交身份；用户提供的是无 `.git` 的下载副本，运行时报 `GIT_ID_UNAVAILABLE`。

2 项 `test_data_loader.py` 的深层 JSON 测试在本机 Python 3.12.7 中未按其预期触发递归解析错误。原始副本与交付副本的这两项失败一致。

原始代码对照日志：`validation/baseline.log`。交付完整回归日志：`validation/regression.log`。专项与合同日志：`validation/focused.log`。

## 本机环境与复现

Python 3.12.7，Pydantic 2.8.2，pytest 9.2.0，Streamlit 1.66.1。测试依赖安装在本任务 `work/venv`，系统环境未修改。

本机 Anaconda 的 readline 动态库导入会崩溃，而 pytest 9 在启动时探测该交互模块。验证时仅在工作区测试启动器中将这个可选终端编辑模块标记为不可用，并关闭无关的自动插件加载；没有改动产品代码、测试用例或断言。此启动器附在 `validation/run_tests.py`。

一般 Python 环境按 D-delivery.md 的安装命令执行后，项目根目录运行：

```sh
PYTHONPATH=src:. python -m pytest tests/dynamic tests/contract -q
PYTHONPATH=src:. python -m pytest -q
python scripts/run_dynamic.py --smoke
```

需要本机 readline 绕行时：

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src:. python docs/validation/run_tests.py -q
```

真实 LoCoMo、生产 Memory、真实 Embedding、真实模型语义质量和正式三路指标未在本次运行中验证；没有读取真实 Gold 或发起付费 API 调用。正式使用前应按技术规范冻结版本并进行开发集审核。
