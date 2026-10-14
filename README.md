# SRE-Project：智能体动态记忆管理系统

课程项目的工程初始化，依据根目录 [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) v2.1。目标是从 LoCoMo 长期对话构建有来源的结构化 Memory，比较 Full Text、Vector RAG、Dynamic 三路问答，并在后续完成 Web 展示和正式评测。

## 当前已实现

- Python 3.11 / Pydantic v2 公共 DTO、部分结构校验、确定性 Memory ID 工具。
- 无网络 synthetic Mock 入口及合同测试；不读取数据、不调用收费 API。
- 冻结规范副本、配置初值和模块目录。配置中的 `null` 表示尚未冻结。

真实 DeepSeek 抽取、Embedding 检索、动态状态推理、完整 Streamlit Web、正式 LoCoMo 评分均未实现；Mock 输出不能作为实验结果。

## 分工

| 成员 | 责任 |
| --- | --- |
| A 常珂舒 | 统一工程、基线、评测、运行环境和接口集成 |
| B 吴宇轩 | LoCoMo 数据规范化、Memory 抽取与来源校验 |
| C 郭柏村 | 查询分析、检索索引、候选及 Vector RAG |
| D 杨林涛 | 动态状态解析、证据约束问答 |
| E 刘星宇 | Web、Memory 浏览与解释性展示 |

## 安装与验证

在仓库根目录、Python 3.11 环境中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m dynamic_memory.pipeline.mock_smoke
```

测试 fixture 全部为 synthetic。Mock 只证明公共对象能传递和校验，不生成正式答案。

## 后续工作和安全边界

各成员按技术规范实现各自模块，并在开发集验证后冻结模型、Embedding、数据快照和评分程序。原始数据、Gold、API Key、权重、缓存及结果文件不应提交到公开仓库。`.env.example` 仅列环境变量名；真实 API 必须经过显式配置。本仓库未附许可证，不应由公开可见推断使用授权。
