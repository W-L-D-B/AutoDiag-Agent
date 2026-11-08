# Agent 阶段质检报告

2026-11-09 完整本地质量入口 `bash scripts/quality.sh` 退出 0。Debug、ASan/UBSan、Coverage 各 44/44 CTest 入口通过；每轮包含 40 项 C++ 单元、74 项 Python 契约/编排单元、48 项真实进程集成和 41 项 Agent 工程测试，另有环境检查。行覆盖率 84.4%，函数覆盖率 96.1%，分支覆盖率 45.6%。格式、静态分析与 Python 检查通过。

工程测试覆盖严格参数、MCP 子进程握手、HTTP 边界、模型错误与截断响应、取消/恢复竞态、持久化预算、逐次调用金额停止、文档/错误收据不能独自证明车辆健康、秘密脱敏和中断恢复。独立审查已提出的问题均已修复。

实际验收另外执行，未用离线提供商成绩代替真实模型：

- MCP SDK 经 stdio 调用八个注册工具，访问正在运行的 C++ 服务与原生 ISO-TP，非法参数被拒绝。
- 真实 DeepSeek 过温预检完成，2 次模型调用、7453 token，取得温度与诊断证据。
- Agent 在实际模型调用进行中被 SIGKILL；重启显示 interrupted；恢复后累计 3 次模型调用完成，保留预算、重新取证、排除旧引用。
- Android 实际提交模型任务并收到报告，2 次模型调用、4 次工具观测（含预检）、6998 token、模型任务约 4.8 秒。

首次开发预检曾因 1024 token 上限失败。报告长度约束与 2048 token 上限在 development 样本上调整，随后成功，正式留出任务尚未执行。失败响应中可取得的 usage 也计入记录；连接中断后拿不到的提供商 usage 不会捏造。

GitHub M2 原生测试三轮均通过，但其最后 Ruff 检查因 `scripts` 新增包入口后旧脚本文件名 `configure-wsl.py` 不合法而失败；本阶段将其重命名为 `configure_wsl.py`，同步 bootstrap 与文档，并通过本地完整门禁。Android CI 已独立通过。后续云端结果以对应提交的运行记录为准。

原始证据位于本地 `artifacts/qa/full-quality.log`、`mcp-native.json`、`recovery-live/result.json`、`artifacts/android/agent-task.json` 与界面记录。60 项正式模型定位成绩单独发布，不从上述少量预检外推。
