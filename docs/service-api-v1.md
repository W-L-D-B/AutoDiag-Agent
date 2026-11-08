# AutoDiag 本地服务 API v1

本接口用于纯软件台架，连接 Android、Agent 与 C++ 诊断/采集服务。它是本机开发接口，不是带认证的公网 API。

## 进程与入口

```text
autodiag-service --interface vcan0 --socket /home/autodiag/.cache/autodiag/runtime/service.sock
autodiag-gateway --socket /home/autodiag/.cache/autodiag/runtime/service.sock --host 127.0.0.1 --port 18770
```

服务只接受真实 `vcan` 虚拟接口。Unix socket 的父目录属于运行用户、权限 0700，socket 权限 0600；程序不得删除非自身拥有的现存路径。网关只绑定 `127.0.0.1`，每个请求转发至 Unix 服务，不能把参数解释为命令。

每个进程同时最多处理 8 个客户端；达到上限拒绝新连接而非无限创建线程。SIGINT/SIGTERM 停止监听、解除等待并回收自己创建的资源。

## 消息分帧

每条消息由 **4 字节无符号大端长度**和紧随其后的 UTF-8 JSON 组成。长度仅计 JSON 字节，允许 1..65536。接收端必须处理头部/正文分多次到达及连续多帧，不能把一次 `recv` 当作一条消息。

长度为 0、超上限、正文不完整或事务超时：关闭该连接。完整正文中的无效 JSON 或请求可返回错误信封；不得执行方法。JSON 必须为对象，不接受重复键、NaN、Infinity 或非法 UTF-8。一个连接支持顺序的请求/响应；v1 不要求同连接多请求并行。

每个事务从开始接收请求起有不超过 2000 ms 的绝对期限，零碎字节到达不延长期限。UDS 单次交换的内部期限为 250 ms。客户端应同样设置读写期限，并把连接级超时与合法错误信封分开处理。

## 信封

请求：

```json
{"version":1,"id":"req-001","method":"snapshot","params":{}}
```

`version` 必须为整数 1；`id` 为 1..64 个 ASCII 字符，允许字母、数字和 `._:-`；`method` 为下表中的方法名；`params` 为对象。方法未定义的参数或整数参数中的 bool/小数均为错误。

成功：

```json
{"version":1,"id":"req-001","ok":true,"result":{"evidence_id":"boot123:snapshot:1","observed_at_ms":1250},"error":null}
```

失败：

```json
{"version":1,"id":"req-001","ok":false,"result":null,"error":{"code":"uds_timeout","message":"ECU did not respond before deadline","retryable":true,"details":{"node":"bms","phase":"request","evidence_id":"boot123:uds:2"}}}
```

响应沿用有效请求 ID；无法提取有效 ID 时使用空字符串。`ok=true` 时只有 `result` 为对象；`ok=false` 时只有 `error` 为对象。所有错误含 `code/message/retryable/details`，普通错误的 `details` 可为空对象。客户端应校验响应 version、id 和成功/失败分支。

所有成功结果都含 `evidence_id:string` 和 `observed_at_ms:uint64`。时间来自服务启动后的单调毫秒数，不是 Unix 时间；证据 ID 以该服务 boot namespace 为前缀，重启后改变。ID 用于关联证据，不代表加密签名。

## 方法与结果

### health

参数 `{}`。结果增加：

```json
{"service":"autodiag-service","boot_id":"boot123","uptime_ms":1250,"collector_running":true,"interface":"vcan0"}
```

这里只报告服务和采集线程健康；不能把 ECU offline 解释为服务崩溃。

### snapshot

参数 `{}`。结果为既有 `MonitorSnapshot`：`elapsed_ms/global_rejections/nodes`，加公共证据字段。节点键固定 `bms/hvac/body`；节点字段和单位沿用 [protocol-v1.md](protocol-v1.md)。无数据返回 null/missing，不填造默认正常值。data fresh 阈值仍为 500 ms，heartbeat online 阈值仍为 1200 ms。

### list_ecus

参数 `{}`。结果增加 `ecus` 数组，每项为：

```json
{"node":"bms","node_id":1,"online":true,"data_status":"fresh","capabilities":["read_did","read_dtcs","signal_window"]}
```

三节点始终列出；能力描述已实现的协议能力，不以 online 与否改变。

### read_did

参数：`{"node":"bms","did":53506}`。`node` 只允许三节点名，`did` 为 uint16；示例 53506 为 `0xD102`。服务通过真实 ISO-TP 执行必要的扩展会话进入和读取，不从 CAN 缓存伪造诊断响应。同一节点的完整 tester 事务串行化，避免并发调用串回复。

结果增加：

```json
{"node":"bms","did":53506,"values":{"temperature_c":32.0},"units":{"temperature_c":"degC"},"valid":true,"raw_hex":"4001","response_hex":"62D1024001"}
```

`values` 始终为对象；identity DID 使用 `software_version` 或 `node_name` 字符串。`units` 对离散/字符串值使用空字符串。`raw_hex` 仅为 DID 数据，`response_hex` 为完整 UDS positive PDU，两者均为大写十六进制。D200/D202 的 `valid` 表达 cabin input validity，保留 last-good 数值时仍必须报告 false；D201 是独立的设定温度，正响应时 valid=true。

### read_dtcs

参数：`{"node":"bms","status_mask":255}`，mask 可省略，默认 255，范围 0..255。结果增加：

```json
{"node":"bms","status_mask":255,"dtcs":[{"code":"A10001","status":9,"description":"BMS temperature above 90 C"}],"response_hex":"590209A1000109"}
```

无匹配项时 `dtcs=[]`。DTC 由 ECU 实际模型条件产生；返回内容不包含场景名、注入器配置或正确根因标签。

### signal_window

参数：`node`、`signal`、整数 `window_ms`（1..60000），以及可选 `max_samples`（1..512，默认 128）。signal 必须是该节点已定义的单个信号名。

结果增加：

```json
{"node":"bms","signal":"temperature_c","unit":"degC","samples":[{"t_ms":1200,"value":32.0,"evidence_id":"boot123:sample:15"}],"sample_count":1,"returned_count":1,"truncated":false,"oldest_available_ms":1200,"latest_ms":1200}
```

每节点最多保留 512 个已接受的数据样本；拒绝帧和心跳不充当新的信号样本。只返回窗口内最新的至多 N 项，按时间升序排列。`sample_count` 是保留历史中匹配窗口的数量，`returned_count` 等于返回数组长度。因数量上限或环形缓存截断请求历史时，`truncated=true`；不声称覆盖未保留历史。没有匹配数据时数组为空，时间字段为 null，单位仍可来自静态契约。

### probe

参数：`{"node":"bms","kind":"diagnostic"}`；kind 为 `identity/diagnostic/freshness`。

结果增加 `node/kind/passed/checks`。identity 实际读取 F180/F187；diagnostic 实际进入诊断会话并读取代表性信号 DID 与 DTC；freshness 检查 live snapshot 的在线及新鲜状态。`passed` 表示该探针检查通过，不能等同整车无故障：诊断路径正常且返回 DTC 时，diagnostic 探针仍可通过。失败的传输/NRC 使用错误信封。

### contract

参数 `{}`。结果增加 `protocol:"autodiag-service-v1"`、`methods` 方法名数组和 `limits` 对象。limits 至少包含 `max_frame_bytes=65536/max_clients=8/history_per_node=512/max_window_ms=60000/transaction_timeout_ms=2000/uds_timeout_ms=250`。

## 错误码

| code | 场景 | retryable |
|---|---|---|
| invalid_request | JSON/信封/version/id/params 结构不合法 | false |
| method_not_found | 未实现的方法 | false |
| invalid_params | 节点、DID 数字范围、信号、窗口或探针参数错误 | false |
| unavailable | 网关无法连接服务，或服务明确不可用 | true |
| uds_timeout | ECU 未及时响应（发送后的超时会隔离通道）；或尚未发送时等待 tester 锁超时 | 仅尚未发送可重试 |
| uds_quarantined | 此 ECU 诊断通道因先前超时而被隔离 | false |
| uds_negative_response | 收到真实 `7F SID NRC` | false |
| internal_error | 内部失败或非法上游响应 | false |

UDS 错误 details 可包含 `node/sid/nrc/phase/request_hex/response_hex/evidence_id`；sid/nrc 为整数，phase 为 `session/request`。错误 message 不回显私钥、环境变量、内部堆栈或无关文件内容。

UDS 没有应用层请求编号。即使按 ECU 串行请求，也不能区分同 SID/DID 的迟到旧回复。因此，发送后的超时或传输失败会隔离该节点在当前服务进程中的全部诊断请求；心跳恢复不会自动解除隔离，其他节点与周期采集不受影响。恢复需要受控重启 ECU 和服务（推荐重启整个 `scripts.live_stack`），确保旧 ECU 不再发送迟到响应。仅重启服务而保留一个行为异常的旧发送者不能保证隔离。`unavailable` 来自此类传输失败时也为不可重试。
