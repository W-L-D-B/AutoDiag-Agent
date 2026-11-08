# AutoDiag 诊断协议子集 v1

本协议在既有纯软件 ECU 上增加真实 Linux `CAN_ISOTP` 诊断通路。它是明确约束的教学子集，不声称覆盖完整 ISO 14229、OEM 诊断、安全访问或诊断刷写。

## 传输与时间

| ECU | 请求 CAN ID | 响应 CAN ID |
|---|---:|---:|
| BMS | 0x7E0 | 0x7E8 |
| HVAC | 0x7E1 | 0x7E9 |
| Body | 0x7E2 | 0x7EA |

采用经典 CAN、标准 11 位 ID、物理寻址。每个 ECU 绑定自己的 `CAN_ISOTP` socket；应用读取/写入完整 PDU，内核完成分段、重组和流控。不能把超过单帧的数据截短后当作成功。功能寻址、CAN FD、SecurityAccess、RoutineControl、清 DTC 和刷写不属于 v1。

服务使用单调毫秒时间，并可在纯模型测试中注入 fake clock。客户端单次交换超时 250 ms；扩展会话的 S3 为 5000 ms。每次处理请求前先检查 S3，到期回 default。成功的受支持请求刷新活动时间，格式错误和不支持请求不刷新。未实现 NRC 0x78 response-pending。

每个节点的 tester 会话进入与后续读取组成串行事务。同一时间不能以两个共享请求/响应 ID 的 tester 交换去消费彼此的响应。

## 会话与服务

### 0x10 DiagnosticSessionControl

只接受两字节请求：`10 01` default、`10 03` extended。正响应：`50 SESSION 00 FA 00 C8`，其中 P2 为 250 ms，P2* 为 200×10 ms；v1 实际响应无需 response-pending。其他 subfunction（包括 suppression bit）返回 `7F 10 12`，长度不符返回 `7F 10 13`。

### 0x22 ReadDataByIdentifier

只接受一个 DID：`22 DID_H DID_L`，长度必须为 3。正响应为 `62 DID_H DID_L DATA...`。SID、DID 标识与 DTC code 按诊断 PDU 的常见大端字节顺序编码；**下表自定义信号 DID 数据明确使用小端整数**，与 CAN 信号契约一致。

F180/F187 在 default 和 extended 都可读。信号 DID 仅在 extended 可读；当前会话不可用或未知 DID 返回 NRC 0x31。信号 DID 的内部 clean model 缺失、无效或属于错误节点时返回 NRC 0x22，不编造默认零值。不能返回整个 0x22 服务不支持的错误来掩盖 identity DID 仍然可读的策略。多 DID 请求不属于本子集，返回 NRC 0x13。

### 0x19 ReadDTCInformation

只接受 `19 02 STATUS_MASK`。正响应为 `59 02 09`，其后是零个或多个 `DTC_H DTC_M DTC_L STATUS`。availability mask 为 0x09；只返回 `status & STATUS_MASK != 0` 的项。mask 0 合法，返回空列表。其他 subfunction 返回 0x12，长度错误返回 0x13；内部 clean model 缺失、无效或属于错误节点时返回 0x22，不能把缺数据当作无故障。

### 0x3E TesterPresent

只支持 `3E 00`，正响应 `7E 00`；在 extended 中刷新 S3，在 default 中不改变会话。`3E 80` 不实现 suppress-positive-response，明确返回 NRC 0x12。长度不符返回 NRC 0x13。

## DID 数据字典

| DID | ECU | DATA 字节 | RPC values | 单位 |
|---|---|---|---|---|
| F180 | 全部 | ASCII `AutoDiag/1.0`，12 字节，无 NUL | software_version | 空字符串 |
| F187 | 全部 | ASCII `bms` / `hvac` / `body`，无 NUL | node_name | 空字符串 |
| D100 | BMS | uint16 LE，0.1 V | voltage_v | V |
| D101 | BMS | int16 LE，0.1 A | current_a | A |
| D102 | BMS | int16 LE，0.1 °C | temperature_c | degC |
| D200 | HVAC | int16 LE 0.1 °C + cabin_valid uint8 | cabin_temperature_c、cabin_sensor_valid | degC、空字符串 |
| D201 | HVAC | int16 LE，0.1 °C | target_temperature_c | degC |
| D202 | HVAC | fan uint8 + cabin_valid uint8 | fan_level、cabin_sensor_valid | 空字符串 |
| D300 | Body | uint16 LE，0.01 km/h | speed_kph | km/h |
| D301 | Body | doors uint8 + lights uint8 | door_mask、light_mask | 空字符串 |
| D302 | Body | uint16 LE，0.01 V | supply_voltage_v | V |

布尔字节仅为 0 或 1。D202 的 validity 与 D200 一样指 cabin 输入质量，不能解释成新增的风机执行器健康检测。F180 的完整 positive PDU 为 15 字节，必须经 ISO-TP 多帧传输。

典型向量：

```text
10 03                -> 50 03 00 FA 00 C8
22 D1 02             -> 62 D1 02 40 01       # 0x0140 = 320 -> 32.0 °C
22 F1 80             -> 62 F1 80 41 75 74 6F 44 69 61 67 2F 31 2E 30
3E 00                -> 7E 00
19 02 FF             -> 59 02 09 A1 00 01 09 # 当前 BMS 过温时
19 02 00             -> 59 02 09
```

## DTC 条件

| 代码 | 条件 | 活跃 status |
|---|---|---:|
| A10001 | BMS 当前内部模型温度严格大于 90.0 °C | 0x09 |
| A20001 | HVAC 当前 cabin_valid=false | 0x09 |

v1 报告当前活跃条件；条件恢复后不保留持久化锁存记录。未实现掉电 DTC 存储、老化、清码或 freeze frame。Body 当前没有定义 DTC。

DTC 只能由实际模型值和质量位产生，不能读取注入器的场景名称、配置文件或正确根因标签。应用 CRC 故障或周期数据丢弃之前，诊断模型仍应更新内部当前信号，因此“周期信号 stale、ECU 心跳 online、DID 可读且正常”可以作为真实跨通路证据。

## 否定响应与校验顺序

负响应统一为 `7F REQUEST_SID NRC`。v1 使用：

| NRC | 意义 |
|---:|---|
| 0x11 | serviceNotSupported |
| 0x12 | subFunctionNotSupported |
| 0x13 | incorrectMessageLengthOrInvalidFormat |
| 0x22 | conditionsNotCorrect，信号读取或 DTC 查询没有有效的本节点内部模型 |
| 0x31 | requestOutOfRange，包括当前会话不可用的信号 DID |

先根据 SID 判定是否支持服务；对已支持服务先检查长度，再检查 subfunction/DID、会话和内部模型条件。identity/session/tester-present 不依赖有效模型。空 PDU 的纯模型测试可返回 `7F 00 13`；实际传输不能把零长度写入当作一次成功请求。有效否定响应与传输超时是不同结果，RPC 分别报告 `uds_negative_response` 和 `uds_timeout`。

## 验收证据

- Fake-clock：default/extended 切换、4999/5000 ms S3 边界、TesterPresent 刷新、错误请求不刷新。
- 字节级单元测试：所有 DID 编码、未知 SID/subfunction/DID、错误长度、status mask 0/FF、DTC 产生与恢复。
- 原生 ISO-TP：实际读取 F180 完整多帧 PDU；节点离线超时；多个并发 RPC 的 per-node 事务不串回复。
- 既有周期 CAN、LIN、原场景与回放测试仍须通过；新通路不能破坏 ECU 的周期发送和退出行为。

接口错误语义见 [service-api-v1.md](service-api-v1.md)。本文件的通过状态以实际测试报告为准。

服务端按 ECU 串行执行诊断事务，但 UDS 无请求编号，不能仅靠 SID/DID 识别超时后的同类迟到响应。发送后超时或传输失败会隔离该 ECU 当前服务生命周期的诊断通道；受控重启整套 ECU 与服务后恢复。超出本项目 4095 字节上限的完整接收 PDU 被丢弃，不能终止 ECU 的周期发送。
