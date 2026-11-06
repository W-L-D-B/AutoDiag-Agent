# AutoDiag 虚拟台架协议 v1

该协议仅用于 AutoDiag 纯软件台架。它运行在 Linux SocketCAN `vcan` 上，不替代车辆 OEM 的 DBC、AUTOSAR 通信栈或物理 CAN/LIN 协议合规验证。

## 通用编码

- CAN 2.0 标准帧，11 位 ID，数据长度固定 8 字节。
- 多字节整数均为小端；有符号字段使用二进制补码。
- 物理值换算为 `physical = raw * scale + offset`，offset 均为 0。
- 数据帧和心跳各自维护独立的 8 位计数器。
- 最后一个字节为应用层 CRC8。它不同于 CAN 控制器自动生成的物理层 CRC。

应用层 CRC8 参数：多项式 `0x1D`、初值 `0xFF`、不反射、输出异或 `0xFF`。输入依次为标准 CAN ID 高字节、低字节和 payload 字节 0..6；ID 高字节仅使用低 3 位。

## 数据帧

### BMS 状态：0x180

| Byte | 字段 | 类型 | 缩放/单位 |
|---|---|---|---|
| 0..1 | pack_voltage | uint16 | 0.1 V |
| 2..3 | pack_current | int16 | 0.1 A |
| 4..5 | max_cell_temp | int16 | 0.1 °C |
| 6 | counter | uint8 | 1 |
| 7 | app_crc | uint8 | CRC8 |

编码允许范围：0.0..800.0 V、-200.0..200.0 A、-40.0..150.0 °C。正常仿真范围：300.0..450.0 V、电流保持在编码允许范围内、温度使用约 32.0 °C 的正常值。过温故障固定发布 125.0 °C；它仍是协议有效的数据，业务诊断应与传输错误区分。

### HVAC 状态：0x181

| Byte | 字段 | 类型 | 缩放/单位 |
|---|---|---|---|
| 0..1 | cabin_temperature | int16 | 0.1 °C |
| 2..3 | target_temperature | int16 | 0.1 °C |
| 4 | fan_level | uint8 | 0..7 |
| 5 | status | uint8 | 位定义见下 |
| 6 | counter | uint8 | 1 |
| 7 | app_crc | uint8 | CRC8 |

`status`：bit0 `cabin_valid`；其余位保留并必须为 0。LIN 采样校验失败时，CAN 帧仍可通过 CRC，`cabin_temperature` 保持 last-good，bit0 清零。

### Body 状态：0x182

| Byte | 字段 | 类型 | 缩放/单位 |
|---|---|---|---|
| 0..1 | vehicle_speed | uint16 | 0.01 km/h |
| 2 | doors | uint8 | bit0 FL、bit1 FR、bit2 RL、bit3 RR、bit4 trunk |
| 3 | lights | uint8 | bit0 position、bit1 low beam、bit2 high beam |
| 4..5 | supply_voltage | uint16 | 0.01 V |
| 6 | counter | uint8 | 1 |
| 7 | app_crc | uint8 | CRC8 |

`door_mask` 的编码允许范围为 0..31，包含 trunk 位；门和灯未定义位必须为 0。正常供电设计范围 9.0..16.0 V。

## 心跳帧

| 节点 | node_id | CAN ID |
|---|---:|---:|
| BMS | 1 | 0x700 |
| HVAC | 2 | 0x701 |
| Body | 3 | 0x702 |

全部心跳使用同一布局：

| Byte | 字段 | 类型 | 说明 |
|---|---|---|---|
| 0 | node_id | uint8 | 必须与 CAN ID 匹配 |
| 1 | status | uint8 | bit0 running、bit2 degraded_input |
| 2..5 | uptime_ms | uint32 | 自该 ECU 进程启动后的单调毫秒数 |
| 6 | counter | uint8 | 独立心跳计数器 |
| 7 | app_crc | uint8 | CRC8 |

`running` 必须为 1；bit1 和其他未定义 status 位必须为 0。仅 HVAC 在 LIN 输入无效时置 `degraded_input`。数据层故障（例如 bad CRC、丢数据、冻结序号或过温）不得通过心跳额外泄漏故障真值。

## 接收状态机

对数据和心跳分别保存 `last_counter`、最后接受时间和拒绝计数。

```text
首次 CRC/结构有效帧 -> 接受，建立基线
delta = uint8(new_counter - last_counter)
delta == 0            -> 拒绝 duplicate
1 <= delta <= 127     -> 接受 forward
128 <= delta <= 255   -> 拒绝 old_sequence
```

CRC、长度、ID/node 映射或保留位错误优先于序号检查，分别记录 `crc`、`length`、`identity` 或 `reserved_bits`。被拒绝帧不刷新时间和信号。

数据在接受后小于 500 ms 为 `fresh`，此前为 `missing`，达到 500 ms 后为 `stale`。节点在有效心跳接受后小于 1200 ms 为 online；数据新鲜度不影响 online。

节点 epoch 变化规则：若有效心跳的 uptime 小于上次已接受 uptime，或者上一心跳已因 1200 ms 超时而 offline 后重新出现，则接受该心跳作为 resynchronization，清除数据序号基线、旧信号和数据接受时间，使数据状态回到 missing，并增加 `resynchronizations`。数据 stale 不触发重同步，重复数据永远不能建立新基线。

已知限制：v1 的 8 位半窗口比较不能区分在线节点长时间数据缺口后的大幅前进与旧帧。此时接收端拒绝帧，直到节点 epoch 变化。真实产品应使用更宽序号、显式会话/epoch 或满足系统安全分析的机制。

## LIN 温度模型

- 6 位 frame ID：`0x12`。
- 保护 ID：`0x92`。P0/P1 按 LIN parity 规则生成。
- 数据：2 字节小端 int16，缩放 0.1 °C。
- 使用增强校验和，求和包含 PID 和两个数据字节，使用 end-around carry 后按位取反。
- 已知向量：PID `0x92`，数据 `D2 00`（21.0 °C），校验和必须为 `0x9A`。

该 LIN 帧只在 HVAC 进程内生成并验证。`lin_bad_checksum` 故障只破坏 LIN checksum；对外 CAN 帧使用合法应用层 CRC，并通过 `cabin_valid=0` 和心跳 degraded bit 表达输入降级。

## 可执行程序与 CLI

### ECU

```text
autodiag-ecu
  --node bms|hvac|body
  [--interface vcan0]
  [--duration-ms 3000]
  [--seed 1]
  [--counter-start 0]
  [--fault none|drop_data|bad_crc|freeze_counter|overtemperature|lin_bad_checksum|exit]
  [--fault-after-ms 1000]
  [--fault-duration-ms 1000]
```

数据周期固定 100 ms，心跳周期固定 250 ms。`fault-duration-ms=0` 表示故障持续至进程结束。只允许 `overtemperature` 用于 BMS、`lin_bad_checksum` 用于 HVAC；编排器的固定场景决定其他故障目标。无效参数/组合退出 2；`exit` 故障触发时退出 42；正常结束退出 0。

给定相同 node、seed、counter-start 和逻辑 tick，纯信号模型及数据 payload 必须完全一致。实时进程中的 heartbeat uptime 来自单调时钟，调度抖动也可能改变结束边界附近的帧总数，因此两次独立实时运行不要求 heartbeat payload、总帧数或全部拒绝计数逐项相同；必须保持故障的定性结果一致，并可按匹配的数据 counter 比较正常模型字段。同一份已捕获日志的离线回放必须完全复现信号、counter、拒绝计数，并以 `capture_end.elapsed_ms` 精确重算 age/online。

### 监视器

```text
autodiag-monitor
  [--interface vcan0]
  [--duration-ms 3000]
  --snapshot PATH
  --log PATH
```

监视器必须拒绝非 `vcan*` 接口；`snapshot` 使用同目录临时文件写完后 rename，避免读到半个 JSON。

## 场景编排

```text
python scripts/run_bench.py
  --scenario baseline|drop_bms_data|corrupt_hvac_crc|body_sequence_freeze|bms_overtemperature|lin_bad_checksum|node_exit
  [--interface vcan0]
  [--duration-ms 3000]
  [--seed 1]
  --output DIR
```

输出 `manifest.json`、`snapshot.json` 和 `frames.jsonl`。manifest 至少记录 schema_version、场景、seed、接口、持续时间、每个子进程参数/退出码和输出文件，不保存私密环境变量。

离线回放：

```text
python scripts/replay_log.py --dbc config/autodiag.dbc --log frames.jsonl --output replay.json
```

回放只读取逐帧日志和公开协议，不读取 scenario 真值。

## JSON 证据格式

### 逐帧 JSONL

首尾各写一条边界记录，使离线回放可以按同一结束时刻计算 age/online：

```json
{"schema_version":1,"type":"capture_start","elapsed_ms":0}
{"schema_version":1,"type":"capture_end","elapsed_ms":3000}
```

两者之间每个 CAN 帧写一个对象：

```json
{"schema_version":1,"type":"frame","elapsed_ms":125,"raw_id":384,"id":384,"dlc":8,"data":"A00F7D004001009A","accepted":true,"reason":"accepted","node":"bms","message":"bms_status","counter":0}
```

`raw_id` 是 Linux `can_frame.can_id` 的无符号整数，保留 `CAN_EFF_FLAG`、`CAN_RTR_FLAG`、`CAN_ERR_FLAG` 等标志；`id` 是移除标志后的 arbitration ID；`dlc` 是实际数据长度。`data` 为长度 `2 * dlc` 的大写十六进制字符，合法协议帧为 16 个字符。这样离线回放仍能独立识别扩展帧、RTR/错误帧和坏长度。未知 ID 时 node/message/counter 为 null。`message` 只允许 `bms_status`、`hvac_status`、`body_status`、`heartbeat` 或 null。`reason` 固定为 `accepted`、`resynchronized`、`unknown_id`、`bad_length`、`bad_crc`、`invalid_flags`、`invalid_payload`、`duplicate` 或 `old`。

### 快照 JSON

```json
{
  "schema_version": 1,
  "capture_state": "completed",
  "ready": false,
  "elapsed_ms": 3000,
  "global_rejections": {
    "bad_crc": 0,
    "bad_length": 0,
    "invalid_flags": 0,
    "unknown_id": 0,
    "invalid_payload": 0,
    "duplicate": 0,
    "old": 0
  },
  "nodes": {
    "bms": {
      "online": true,
      "data_status": "fresh",
      "data_age_ms": 95,
      "heartbeat_age_ms": 240,
      "data_counter": 29,
      "heartbeat_counter": 11,
      "signals": {"voltage_v":400.0,"current_a":12.5,"temperature_c":32.0},
      "rejections": {"bad_crc":0,"bad_length":0,"invalid_flags":0,"unknown_id":0,"invalid_payload":0,"duplicate":0,"old":0},
      "resynchronizations": 0
    },
    "hvac": {},
    "body": {}
  }
}
```

`data_age_ms`、`heartbeat_age_ms`、`data_counter`、`heartbeat_counter` 在从未接受相应帧时为 null。`signals` 在从未接受数据或 epoch 刚改变时为 null；否则键严格固定如下：

- BMS：`voltage_v`、`current_a`、`temperature_c`。
- HVAC：`cabin_temperature_c`、`target_temperature_c`、`fan_level`、`cabin_sensor_valid`。
- Body：`speed_kph`、`door_mask`、`light_mask`、`supply_voltage_v`。

每个节点的 `rejections` 必须包含示例中的七个固定键；`unknown_id` 因无法归属节点而始终只在 `global_rejections` 中增加，节点内保持 0。其他拒绝同时增加对应节点和全局计数。空节点仍必须存在完整字段，不能以 `{}` 代替；上例省略仅为展示紧凑。

## 故障语义

| 固定场景 | ECU fault | 观察结果 |
|---|---|---|
| baseline | 全部 none | 三节点 online/fresh，无协议拒绝 |
| drop_bms_data | BMS drop_data | BMS 数据停止、心跳继续、data stale、online |
| corrupt_hvac_crc | HVAC bad_crc | 只破坏 CAN 数据帧 byte7，CRC 拒绝增加，心跳内容与正常场景一致 |
| body_sequence_freeze | Body freeze_counter | 数据 payload 继续发送但 counter 固定，duplicate 增加且最终 stale |
| bms_overtemperature | BMS overtemperature | 温度 125.0°C，帧协议有效 |
| lin_bad_checksum | HVAC lin_bad_checksum | 保持 last-good cabin temp，cabin_valid 清零，心跳 degraded |
| node_exit | Body exit | 故障开始退出 42，最终 heartbeat 超时并 offline |

所有非 `exit` 故障只在 `[fault_after_ms, fault_after_ms + fault_duration_ms)` 生效；duration 为 0 时持续到结束。`bad_crc` 只翻转应用层 CRC 字节；不得同时改变信号、计数器或心跳。
