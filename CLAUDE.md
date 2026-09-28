# CLAUDE.md

本仓库是 CapsWriter 离线语音输入的**服务端**（Windows + Linux），配套的客户端在另一个仓库 `../CapsWriter-Client`。

## 先读

1. [PROJECT_INDEX.md](PROJECT_INDEX.md) — 架构、目录、启动流程、启动参数、配置、协议、验证方法、常见修改指南
2. [docs/DEV_NOTES.md](docs/DEV_NOTES.md) — 决策记录、已知怪状态、待验证事项
3. 按需：[docs/LINUX.md](docs/LINUX.md)（Linux 部署 / asmgt）、[docs/显卡加速.md](docs/显卡加速.md)、[docs/text_merge_algorithm.md](docs/text_merge_algorithm.md)、[models/README.md](models/README.md)

## 硬性约束

- **两个入口行为必须一致**：`start_server.exe`（Windows）与 `start_server.sh` → `start_server.py`（Linux）都只执行 `CapsWriterServer().start()`。启动逻辑一律放在 `core/` 的这条调用链里；`start_server.sh` 只做 Linux 环境准备；`start_server.exe` 不要改（无法重建），`start_server.py` 保持与 exe 内嵌脚本一致。平台差异必须显式写在代码里并记入 PROJECT_INDEX.md。
- **兼容 Python 3.10（Linux 环境）与 3.13（Windows 的 `internal/` 冻结运行时）**。尽量只用标准库；确需新依赖时，Linux 加进 `requirements-linux.txt`，Windows 要把兼容 cp313-win_amd64 的包放进 `internal/`，两边都要实测。
- **`core/protocol.py`、`core/constants.py` 与 Client 仓库逐字相同**，改协议必须两边同步（Client 仓库不在本仓库的修改范围内，需要时告知用户）。
- **不要提交** `models/`、`logs/`、`__pycache__`/`.pyc`（`internal/` 除外）。语法检查用 `ast.parse`，不要用 `py_compile`。`internal/` 按字节原样跟踪；Linux `.so` 别名是 git 符号链接，在 Windows 上显示为小文本文件，不要用普通文件覆盖；`.sh` 保持 LF 且带可执行位。
- **本机上可能正运行着用户日常使用的服务端**（默认端口 6000，`start_server.exe` 进程）。测试时用其他端口（如 `--port 6100`），只结束自己启动的进程，不要动它。
- **文档与代码同步**：行为变更同步更新 PROJECT_INDEX.md 对应章节；用法变更同步 readme.md / docs/LINUX.md；决策、待办、已知问题记到 docs/DEV_NOTES.md（署日期）。

## 验证

改完代码至少：`ast.parse` 全部改动文件 → 用 `start_server.exe --port 6100`（Windows）或 `./start_server.sh --port 6100`（Linux）启动 → `python tools/smoke_test.py --port 6100`（加 `--source file` 测对齐、`--seg-duration 6 --seg-overlap 2` 测拼接）。详见 PROJECT_INDEX.md 的“开发环境与验证”。

**不要在 WSL 里测试**（用户要求：会占满 C 盘）。在 Windows 上改了 Linux 相关代码时只做静态检查（`ast.parse(..., feature_version=(3, 10))`、`bash -n`），需要真机验证的项写进 docs/DEV_NOTES.md 的“待验证”，由用户在 Linux 服务器上验证。
