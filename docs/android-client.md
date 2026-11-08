# Android 客户端：AIDL、网络与构建

本模块是原生 Java Android 应用，包名 `io.autodiag.agent`，最低 API 26，编译和目标 API 35。它通过应用级 AIDL/Binder 访问独立 `:gateway` 进程，再由该进程连接 Linux 服务；不实现系统 VHAL。

## 已实现的结构

- `MainActivity`：信号、DID/DTC、Agent 任务入口、步骤与报告；所有网络相关 Binder 调用均在有界后台执行器中发起。
- `GatewayService`：`android:process=":gateway"`、`exported=false`，独占客户端 TCP/HTTP 请求。AIDL 提供 `rpc`、`agent`、进程 ID 与连接状态回调。
- `FramedTransport`：4 字节大端长度、1..65536 字节正文；非阻塞 socket 使用一次绝对截止时间，处理分片、错误长度和中途断连。
- 请求 ID 由客户端产生，RPC 响应校验 version/id/ok/result/error。Agent 任务采用提交代次，避免旧任务的迟到响应覆盖新任务；Binder 重新绑定后不把旧连接响应显示为当前信号。
- 队列拒绝会释放 in-flight 标志；UI 停止时注销回调/解绑，重启后可读取已保存的任务 ID。Binder 死亡或绑定失效会触发有界重绑。
- 页面明确呈现 missing、stale、offline、传感器输入无效、诊断错误和网络失败；无数值时显示 `—`，不会补造正常的零值。

后端地址固定为模拟器访问宿主机的 `10.0.2.2`：原生 RPC 端口 18770，Agent HTTP 端口 18771。API key 不进入 APK，也不由 Android 直接访问模型提供商。

## 可复现构建

仓库根目录运行：

```powershell
python android/build.py
```

脚本依次运行 JVM 网络行为检查、`aapt2` 资源编译/链接、`aidl` 生成、`javac`、`d8`、`zipalign` 和 `apksigner`。使用已安装工具，不需要 Maven、Gradle 或 AndroidX 下载。

本机自动定位 `D:\Android\Sdk` 与 `D:\soft\AndroidStudio\jbr`；其他环境可使用 `ANDROID_HOME`、`JAVA_HOME`，或显式传入：

```powershell
python android/build.py --sdk-root D:\Android\Sdk --java-home D:\soft\AndroidStudio\jbr
python android/build.py --unit-tests-only
```

Python 构建脚本支持 Windows/Linux 工具后缀。构建工作目录每次唯一，不递归删除已有目录。调试签名密钥位于用户缓存 `.cache/autodiag/android/debug.keystore`，不纳入版本控制；这是开发 APK 的调试签名。

输出均位于未跟踪的 `artifacts/android/`：

| 文件 | 含义 |
|---|---|
| `AutoDiag-Agent-debug.apk` | 可安装调试 APK |
| `build-report.json` | 构建路径、SHA256、签名方案与检查状态 |
| `transport-tests.log` | JVM 网络检查结果 |
| `apk-verification.txt` | `apksigner verify --verbose --print-certs` 原始结果 |
| `apk-badging.txt` | 已编译 APK 的包名、版本、SDK 等信息 |
| `manifest-tree.txt` | 已编译 Manifest 树，包括服务进程与导出设置 |

## 构建阶段验证

已执行完整构建并通过 APK 签名验证。网络行为检查共 9 项：UTF-8 请求和分片响应、三种非法响应长度、截断头部、截断正文、绝对超时和两种非法出站长度。测试通过临时本机 socket 完成，不需要 ECU、模型或模拟器。

APK 构建与 Manifest 声明不能替代实际 Android 联调。模拟器安装、真实 Binder 跨进程、后端断连、服务进程恢复和 Agent 任务流程由项目整体验收执行，并应保存实际日志/截图后再宣称通过。

2026-11-08 已完成原生后端联调：Android 15 模拟器安装成功；UI PID 4530 与网关 PID 4563 不同，实际 snapshot 返回三个节点。通过应用同 UID 杀掉网关后，UI 保持 PID 4530，自动重绑新网关 PID 4665 并再次读取三个节点。停止后端后，snapshot 明确返回 unavailable，界面显示断连；未将失败伪装为零值。上述 PID 仅是本次运行证据，不是固定配置。日志与正常/断连 UI 层次记录保存在本地 `artifacts/android/`；真实模型任务另按 M4 验收。

## 理解 AIDL 与 Binder

AIDL 是 Android 接口定义语言，用来生成跨进程调用的代理与 Stub；Binder 是 Android 内核提供的进程间通信机制。Activity 调用 `IVehicleGateway.rpc` 时，调用先经过 Binder 到 `:gateway` 进程，再由该进程访问 TCP 服务。Java 方法看起来像本地调用，但对方可能死亡或超时，所以 UI 在后台线程调用，并在 Binder 死亡后重新绑定。

Linux 的 Unix Socket、Android 的 Binder 和跨系统 TCP 分别解决各自边界的通信。这个项目实现的是应用层 AIDL 服务，没有改造系统 VHAL，也没有实现完整 Android Automotive 平台。

## 模拟器验收入口

在专用模拟器和后端就绪后，由验收负责人执行：

```powershell
$adb = 'D:\Android\Sdk\platform-tools\adb.exe'
& $adb install -r artifacts/android/AutoDiag-Agent-debug.apk
& $adb shell am start -n io.autodiag.agent/.MainActivity --ez smoke true
& $adb logcat -d -s AutoDiagSmoke:I
```

只读 `smoke` 流程会实际调用 AIDL 与 TCP snapshot，日志给出 `ui_pid`、`service_pid`、`binder_cross_process`、`rpc_ok`、`nodes_count`。成功验收要求两个 PID 不同、RPC 成功且三个节点存在，不能只根据 Manifest 推断。

界面控件使用稳定的 `R.id`，例如 `refresh_button`、`signals_text`、`diagnostic_button`、`dtc_button`、`question_edit`、`submit_agent_button`、`agent_state` 和 `agent_report`，方便通过 UI 自动化校验真实显示内容。

## 可选的模型任务自动验收

正常用户通过“提交诊断问题”按钮创建任务。导出的启动 Activity 不会仅凭 `agent_question` extra 自动创建可能计费的任务；自动验收必须提供存于应用私有目录的一次性 QA nonce。

在调试模拟器中，验收负责人可先生成能力文件，再触发一次：

```powershell
$nonce = [Guid]::NewGuid().ToString('N')
& $adb shell "run-as io.autodiag.agent sh -c 'mkdir -p files; printf $nonce > files/qa-token'"
& $adb shell am start -n io.autodiag.agent/.MainActivity --es qa_token $nonce --es agent_question 'BMS温度为什么异常？请取证后解释。'
```

nonce 匹配后先删除能力文件再提交任务，不能重复使用；它不是模型 API key。此入口只有在真实模型验收得到授权、预算允许时才使用。手动 UI、只读 snapshot smoke 和诊断读取不依赖该 nonce。
