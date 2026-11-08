# 固定台架评测

任务集为 12 个场景族，各使用五组实际 seed/counter 参数，共 60 项。前三个参数变体用于 development（36 项），后两个用于 holdout（24 项）。相同场景族使用相同问题，但台架参数会变化；这不是 60 种独立故障，也不是跨车型泛化测试。

| 场景族 | 数量 | 观测与任务 |
|---|---:|---|
| normal | 5 | 三 ECU 在线、数据新鲜 |
| data_loss | 5 | 心跳继续但 BMS 数据过期 |
| crc | 5 | HVAC 应用 CRC 拒绝 |
| frozen_counter | 5 | Body 重复序号拒绝 |
| temperature | 5 | BMS 125°C 与实际 DTC |
| lin | 5 | HVAC 温度输入无效 |
| node_exit | 5 | Body 心跳离线 |
| old_sequence | 5 | BMS 旧序号拒绝 |
| scaling | 5 | BMS 广播与 DID 电压不一致 |
| service | 5 | 停止实际采集服务 |
| history | 5 | 请求台架未保留的历史证据 |
| unsupported_did | 5 | 未知 DID 得到实际否定响应 |

`tasks.json` 在首次真实运行前冻结，LF 文件 SHA256：`7192295cdabf753e134a15f65f5b82007f4574bac3a70d3c4f83d441c4e1e753`。`build_tasks.py` 用于重建相同定义，不在留出评测过程中改动任务或答案。JSON 包含评测器真值，只由编排器读取，工具检索白名单不包含此文件或 manifest。

每题重新创建真实虚拟 CAN 台架，等待故障条件可观测，然后给规则基线和模型相同问题及相同 RPC 能力。规则基线使用显式阈值和问句特征；它是已知场景专用基线，不能视作通用自然语言系统。模型输入只有问题、工具定义和实际工具观测，不接收 scenario、family 或 expected_codes。

```bash
python -m evaluation.run --mode rules --output artifacts/evaluation/rules-new
python -m evaluation.run --mode live --split development --output artifacts/evaluation/dev-new
python -m evaluation.run --mode live --split holdout --output artifacts/evaluation/holdout-new
```

运行必须激活 WSL Python 环境、完成 C++ debug 构建，并保证没有另一个台架占用 vcan0。live 模式需要本地 `.env`。`--ids task-021` 可用于预检；正式成绩须明确是否为全部 60 题，不能把预检覆盖替代全量评测。

完成率按通过报告结构/引用验证的 completed 任务计算。定位准确率按**首个假设**是否在评测器允许答案中计算，失败任务计错。引用校验检查 ID 存在及证据类型，不自动证明每句自然语言结论的全部语义；无依据数值需要另行人工抽查。`valid_citation_reports` 仅用于真实模型结果，不能解读为零幻觉。

结果逐题保存 status、predicted、rules_predicted、error、usage、耗时；汇总提供划分/场景族准确率、调用数及 token。原始工具步骤和 SQLite 检查点仅保留在忽略的本地 artifacts；可发布经过检查的汇总与逐题指标。费用采用账号余额下降观察值，可能受结算精度和账号其他消费影响，不能用 token 数捏造精确扣费。

每次模型请求前都检查同一全局余额下限，保留 0.25 元余量。`--budget-cny` 为已观察支出的停止阈值（默认最多 5 元），不是提供商扣费硬上限。取消或断网时，已到达提供商的请求可能仍计费。
