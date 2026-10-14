# A 分支开发提交记录

本文件持续记录 A 分支实际开发的功能提交。每条记录随对应代码和测试一起提交；日期与计划提交信息可从 Git 历史反查最终 SHA，未提交前不预填 SHA。后续 A-02 至 A-26 依序追加，不覆盖既有记录。

## A-01（2026-10-14）

- Commit Message：`实现统一配置加载与校验`。
- 功能及文件：`src/dynamic_memory/pipeline/settings.py` 新增六份 YAML 的类型化离线加载、错误定位、安全 Mock 默认与交叉预算校验；`tests/unit/test_settings.py` 新增正常、异常和注入目录测试；`README.md` 增加已实现的加载示例。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q`：25 passed（退出码 0）；`.\.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke`：synthetic smoke PASS（退出码 0）；README 配置加载示例输出 `mock 8`（退出码 0）。
- 过程记录：本条随 A-01 源码及测试一起提交；对应 SHA 以 Git 历史为准。
