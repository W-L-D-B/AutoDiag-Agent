# AutoDiag-Agent

车载故障诊断与联调 Agent 项目。当前仓库包含开发环境、通信环境验证程序和项目调研，业务 Agent 尚未实现。

远程仓库：<https://github.com/W-L-D-B/AutoDiag-Agent>

## 开发环境

- Windows 11 + WSL2，独立发行版 `AutoDiagUbuntu`（Ubuntu 24.04）。
- C++17、GCC、CMake、Ninja、GDB；SocketCAN/vcan 与 Linux ISO-TP。
- Python 3.12 虚拟环境，MCP、HTTP 客户端、CAN/DBC/UDS 工具与开发检查依赖。
- Android 工具运行在 Windows，通过 TCP 与 Linux 服务通信。

[本机安装路径、验证结果和重启要求](docs/环境配置记录.md)

本机源码位于 `D:\Code\mi\AutoDiag_Agent`，WSL 内为 `/mnt/d/Code/mi/AutoDiag_Agent`，便捷入口为 `/home/autodiag/projects/AutoDiag_Agent`。它们是同一份源码。C++ 构建产物和 Python 环境位于 Linux 文件系统，避免重复 checkout 和混用 Windows/Linux 二进制。

## 日常启动

在仓库目录的 PowerShell 中运行：

```powershell
cd D:\Code\mi\AutoDiag_Agent
.\scripts\dev.ps1
```

进入 Linux 后：

```bash
source scripts/activate.sh
sudo /usr/local/sbin/autodiag-vcan up
cmake --preset linux-debug
cmake --build --preset linux-debug
ctest --preset linux-debug
python tools/check_environment.py
```

Windows 中可运行 `.\scripts\android.ps1 Doctor` 检查模拟器加速，使用 `.\scripts\android.ps1 Start` 启动 `AutoDiag_API35`。启动 IDE 使用 `.\scripts\android.ps1 Studio`。WHPX 功能已启用，且已通过实际 Android 15 冷启动验证。

`vcan0` 由 `autodiag-vcan.service` 随发行版启动自动创建；上面的手动启用命令也可用于恢复。环境检查会实际验证 TCP、Unix Domain Socket、虚拟 CAN 收发、48 字节 ISO-TP 传输和 GitHub 读取，报告写入被忽略的 `artifacts/environment-check.json`。它只发送虚拟总线流量。

## 环境复现

Windows 中以 root 调用系统依赖初始化（不会修改 `CodexUbuntu`）：

```powershell
wsl -d AutoDiagUbuntu -u root -- bash /mnt/d/Code/mi/AutoDiag_Agent/scripts/bootstrap-ubuntu.sh
wsl --manage AutoDiagUbuntu --set-default-user autodiag
```

普通用户安装 Python 环境：

```bash
bash scripts/setup-python.sh
```

常规 Linux 管理操作可以通过 `wsl -d AutoDiagUbuntu -u root` 执行。新建用户不设置公共默认密码；若需要在 Linux 内使用常规 sudo，可在 root 会话运行 `passwd autodiag` 设置自己的密码。无密码 sudo 仅开放项目的固定 vcan 启停命令。

## 仓库与凭据

`origin` 使用 SSH。专用 Linux 用户通过 Windows OpenSSH 访问现有密钥，不向仓库或 WSL 复制私钥。`.env`、`.codex`、临时 PDF 预览、构建目录和运行日志均已忽略。

本地依赖初始化不等于业务功能完成；模型 API 信息需要自行填写到未跟踪的 `.env`。不要把密钥提交到 Git。

## 设计资料

[AutoDiag 项目设计](docs/项目设计.md)

个人简历分析调研稿仅保留在本地，不同步至公开仓库。
