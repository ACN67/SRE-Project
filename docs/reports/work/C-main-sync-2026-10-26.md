# C 同步 main 验证记录

日期：2026-10-26。

- 合并前 C：`e187634`（实现规则版查询分析与时间条件校验）。
- 本轮重新 fetch 后，合并目标 origin/main：`c62bd03`。
- 执行 `git merge --no-commit --no-ff origin/main`，自动合并成功，无未解决冲突；尚未创建合并提交或推送。
- README 保留最新 v2.2 说明及 C 的两份使用文档入口；pyproject 同时保留 C 的 Embedding extra 和 main 的 Web 依赖/静态资源配置。B 数据及公共产物契约、E Web 一期均按 main 引入。

## 验证环境和结果

为避免覆盖已有 Embedding 环境，在 Git 忽略目录中创建独立环境 `.venv/validation311`，使用 Python 3.11.15，并安装项目声明的 `.[dev,web]`。主要版本：pytest 9.1.1、Pydantic 2.14.1、Streamlit 1.66.1。

```bash
.venv/validation311/Scripts/python.exe -m pytest -q -p no:cacheprovider
.venv/validation311/Scripts/python.exe -m dynamic_memory.pipeline.mock_smoke
```

结果：**502 passed**，包含 Web、合同、C Embedding 和查询分析测试，无排除或跳过；Mock smoke PASS；两条命令退出码均为 0。全套测试使用合成输入，未读取真实 Gold、调用真实 API 或形成正式实验成绩。该独立环境不安装 Embedding 模型依赖；Embedding 单测使用测试编码器，先前的真实模型仍在原 `.venv` 环境。

`git diff --name-only --diff-filter=U` 为空。相对 origin/main 的功能差异仅为 C 已有功能及文档。`git diff --cached --check` 对 main 带入的部分 `process/` 会议文档报告尾部空白（含 Markdown 换行空格）和末尾空行；这些文件保持上游原文，没有为合并修改。新增本记录与更新计划文件的空白检查通过。

下一步：人工完成中文合并提交并推送 C，随后按 v2.2 的 `MemoryArtifact` 契约开发索引与持久化。
