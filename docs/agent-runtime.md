# 诊断 Agent 运行说明

## 什么是本项目的 Agent

Agent 是一个有边界的“观察—选择工具—获取结果—继续判断”循环。大模型选择下一步与组织解释，C++/Python 工具负责协议解析、数值计算和实际读取。普通聊天只返回文字，本项目要求模型至少选择一次工具，并从本次任务取得的证据生成结构化报告。

工具共八个：`list_ecus`、`get_service_health`、`read_did`、`read_dtcs`、`get_signal_window`、`query_contract`、`run_probe`、`run_regression`。同一个注册表同时提供给模型 API 和 MCP stdio 服务，避免两份工具定义发生漂移。内置 Agent 直接调用注册表；独立 MCP 客户端通过 SDK 调用同一注册表。不能把这两条调用路径混称为模型每次都经过 MCP 协议。

`query_contract` 只检索 protocol-v1、diagnostics-v1、service-api-v1 三份公开文档，按关键词评分返回有限片段。这是受限文档检索，不是向量数据库，也不读取评测答案、manifest、任意文件或密钥。

## 配置与启动

在仓库根目录将 `.env.example` 复制为 `.env`，只在本机填入 `AUTODIAG_MODEL_API_KEY`。默认适配 DeepSeek，模型名和端点均可由本地配置指定。当前选择 `deepseek-flash`，关闭 thinking，使用 JSON 输出与 function tools。密钥只保留在 Python 进程，Android 和 C++ 不接触模型凭据。

激活 WSL Python 环境并构建 C++ 后：

```bash
python -m scripts.live_stack --with-agent --scenario baseline
```

默认端点：C++ TCP `127.0.0.1:18770`，Agent HTTP `127.0.0.1:18771`，Unix Socket `~/.cache/autodiag/runtime/service.sock`。无认证开发接口只能用于本机实验；没有开启公网监听。

```bash
curl --noproxy '*' http://127.0.0.1:18771/health
curl --noproxy '*' -H 'Content-Type: application/json' \
  -d '{"question":"BMS温度是否异常？请读取当前值和DTC后判断。"}' \
  http://127.0.0.1:18771/tasks
```

提交返回 HTTP 202 与 `task_id`，随后 GET `/tasks/{task_id}` 查询。POST `/tasks/{task_id}/cancel` 或 `/resume`，正文均为 `{}`。任务动作不是新的聊天会话。

## 状态、证据与恢复

任务依次进入 queued、running，最终为 completed、failed、cancelled 或 interrupted。SQLite 保存实际步骤、工具数据、报告和累计调用预算；目录 0700、文件 0600。服务重启将未完成任务改为 interrupted，允许显式恢复。

恢复保留任务 ID 与累计预算，重新获取健康快照、重新开始模型上下文。上次尝试的证据不会通过本次报告的引用验证。取消时可能已有一次模型请求发到提供商；本地取消不能撤销提供商已经计算的费用，记录会保留 `incomplete_calls`。

每次工具执行形成 `agent:任务ID:步骤` 收据，真实车端结果还保留 `boot-...` 证据编号。错误收据证明“这次读取失败”，不证明车辆正常。报告先通过严格 JSON/Pydantic 结构检查，再检查证据 ID 存在；车辆状态类假设还必须引用成功的实时工具观测，不能只引用错误或文档。

这些检查不能完全证明自然语言事实与所有数值之间的语义蕴含。最终定位准确率由独立评测器按隐藏真值评分；引用存在率与诊断正确率应分别报告。

## 有界执行与费用

默认最多 2 个执行任务、8 个待执行任务，每任务最多 8 次模型调用、24 次工具调用、120 秒执行时间；上下文、输出 token、参数长度和 HTTP 请求体也有上限。可以通过本地配置降低模型调用次数。

DeepSeek 模式在每次计费调用前查询余额，同一管理器串行执行余额检查与模型调用。累计余额下降保存在检查点；达到配置金额减 0.25 元余量时停止。评测器将同一全局余额下限传给每个任务，在任务内部每次模型调用前继续检查。

金额限制是基于已观察余额的停止机制，不是提供商的硬扣费限额；费用结算延迟、价格、请求中断和账号其他并发消费会影响归因。模型调用次数、工具调用次数是本地硬限制。开发验收使用最多 5 元观察预算，并保留调用余量；真实成本以实际运行报告为准。兼容接口模式没有验证 DeepSeek 余额接口，不声称具有相同的金额控制。

## MCP 验收

已执行真实模型与 HTTP 验收：过温预检在 2 次模型调用内完成；Android 实际提交的任务 `52165b0752f84e95b4ccae0766f337dd` 完成，读取温度 DID、DTC 与窗口，生成引用报告。另一次独立 Agent 进程在模型调用中被 SIGKILL，重启后任务显示 interrupted，恢复后累计 3 次调用完成，重新取证并拒绝沿用旧引用。该进程测试使用实际配置 API，原始结果保存在本地 `artifacts/qa/recovery-live/result.json`。

首次开发预检曾因 1024 token 输出上限失败；在 development 样本上约束简短报告并将默认输出上限调整为 2048 后重测成功。失败响应中的可用 usage 也会计入累计值。这项调整发生在正式留出评测之前。

保持台架运行，在另一 WSL 终端执行：

```bash
python -m scripts.check_live --output artifacts/qa/mcp-native-new.json
```

此命令通过实际 SDK 子进程执行 initialize、list_tools、八个工具调用和非法参数拒绝，后端是正在运行的 C++ 服务，不调用收费模型。原生启动方式为 `python -m autodiag_agent.mcp_server`，stdout 只用于 MCP 协议。

## 评测与工程测试的区别

`tests/agent` 使用可控提供商和假后端，检查状态机、边界、断点、错误与协议；它不证明模型诊断效果。`evaluation.run --mode live` 才调用真实提供商，并为每题重新运行真实虚拟 CAN 台架。固定任务与实际结果见最终质检报告。
