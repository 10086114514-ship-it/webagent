# 贡献指南

感谢你对 webagent 感兴趣！本文档说明如何搭建开发环境、运行测试和提交改动。

## 开发环境

```bash
git clone https://github.com/10086114514-ship-it/webagent.git
cd webagent
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pip install -e .
```

要求 Windows 10/11 + Python 3.9+。开发时可直接 `python -m webagent ...`，无需安装。

## 目录导览

| 路径 | 作用 |
| --- | --- |
| `webagent/agent.py` | 核心循环：上下文组装、模型调用、计划解析、执行、复检 |
| `webagent/capture.py` | GDI 截图、窗口查找、DPI 感知 |
| `webagent/ocr.py` + `ps_ocr.ps1` | Windows OCR 封装（PowerShell WinRT 投影） |
| `webagent/models.py` | OpenAI 兼容客户端（`urllib`，支持 vision） |
| `webagent/actions.py` | 鼠标 / 键盘 / 滚轮 / 剪贴板 |
| `webagent/__main__.py` | CLI 命令 |
| `webagent/config.py` | JSON 配置加载与合并 |

修改前建议先跑一次 `python -m webagent doctor` 和 `python -m webagent snapshot --lines`，
确认环境与识别结果正常。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

测试不依赖浏览器、不联网、不消耗 API。提交前请确保全部通过。

## 离线调试模型链路

仓库自带一个假模型服务，可以在不花钱、不联网的情况下验证请求与解析：

```bash
# 终端 1
python tools/mock_server.py --port 8799 --plan-file tools/plan.json --log tools/requests.jsonl

# 终端 2（先打开 demo/quiz.html）
python -m webagent run --config tools/test_config.json --dry-run
```

`tools/requests.jsonl` 会记录每次请求的 path / model / auth / messages，便于对照。

## 代码风格

- 遵循 PEP 8，行宽 100。
- 公共函数写类型注解；模块顶部用中文 docstring 说明用途。
- 与硬件/系统交互的 `ctypes` 调用**必须声明 `argtypes` / `restype`**（64 位句柄截断是踩过的坑）。
- 新增配置项时，同时更新 `config.example.json`、`webagent/config.py` 的 `DEFAULTS` 和 README 的配置表。
- 不要提交 `config.json`、`debug/`、API key 或任何个人数据。

## 提交信息

采用 Conventional Commits 前缀：

```
feat: 新增 scroll 动作的惯性滚动
fix: 修正 150% 缩放下的点击偏移
docs: 补充 Ollama 本地模型配置示例
refactor: 拆分 agent.py 的上下文组装
test: 补充 OCR 分行用例
```

## 提 PR 前

1. `python -m unittest discover -s tests -v` 全部通过。
2. 手动跑一次 `python -m webagent run --dry-run`，确认没有回归。
3. 若改动涉及坐标/识别，附一张 `debug/` 截图说明前后差异。
4. 描述里写清：改了什么、为什么、怎么验证的。

## 报告问题

请附上以下信息，便于快速定位：

- `python -m webagent doctor` 的完整输出
- 使用的模型服务商与模型名（**不要贴 API key**）
- 复现步骤与预期/实际结果
- 相关日志（`run` 的步骤 JSON）和 `debug/` 截图

如果涉及识别错误，`python -m webagent snapshot --lines` 的输出非常有帮助。

## 适用范围

本项目仅支持 Windows，依赖 GDI、`SendInput` 与 Windows OCR。跨平台适配需要替换这三个部分，
欢迎在 Issue 中讨论方案。
