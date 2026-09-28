# CLAUDE.md

本仓库是 CapsWriter 离线语音输入的**服务端**（Windows + Linux）；配套客户端在 `../CapsWriter-Client`（另一个仓库，不在修改范围内）。

## 阅读顺序

1. [PROJECT_INDEX.md](PROJECT_INDEX.md) — 项目地图：架构、跨模块约束、平台差异、启动参数、协议、常见修改指南
2. [docs/DEV_NOTES.md](docs/DEV_NOTES.md) — 当前状态：待验证、已知问题、仍然有效的决策
3. 按需：[docs/LINUX.md](docs/LINUX.md)（Linux 部署 / asmgt）、[docs/显卡加速.md](docs/显卡加速.md)、[docs/text_merge_algorithm.md](docs/text_merge_algorithm.md)、[models/README.md](models/README.md)、[llama.cpp 运行库](core/server/engines/llama/bin/README.md)

## 硬性约束

- **两个入口行为一致**：`start_server.exe`（Windows）与 `start_server.sh` → `start_server.py`（Linux）都只执行 `CapsWriterServer().start()`，启动逻辑一律放在 `core/` 的这条调用链里；`start_server.sh` 只做 Linux 环境准备。`start_server.exe` 无法重建，不要改；`start_server.py` 保持与 exe 内嵌脚本一致。平台相关的分支必须显式写，并加进 PROJECT_INDEX.md 的「平台差异」表。
- **兼容 Python 3.10（Linux）与 3.13（Windows 的 `internal/`）**：不要用 3.11+ 才有的语法和标准库（`tomllib`、`except*`、`typing.Self` 等）。尽量只用标准库；确需新依赖时，Linux 加进 `requirements-linux.txt`，Windows 把 cp313-win_amd64 版本放进 `internal/`，两边都要实测。
- **`core/protocol.py`、`core/constants.py` 与 Client 仓库逐字相同**，改协议必须两边同步（Client 仓库需要改时告知用户）。
- **仓库内容**：不要提交 `models/`、`logs/`、`__pycache__`/`.pyc`（`internal/` 除外）。`internal/` 按字节原样跟踪。Linux `.so` 别名是 git 符号链接，在 Windows 上是小文本文件，不要用普通文件覆盖。`.sh` 保持 LF 且带可执行位（`git add --chmod=+x`）；`.bat` 保持纯 ASCII + CRLF。
- **本机上正运行着用户日常使用的服务端**（`start_server.exe`，端口 6000）。测试用其他端口（如 `--port 6100`），只结束自己启动的进程。
- **不要在 WSL 里测试**（用户要求：曾占满 C 盘）。临时文件和测试副本也不要放 C 盘。

## 验证

改完代码至少：

1. `ast.parse` 全部改动的 `.py`（Linux 也会运行的代码加 `feature_version=(3, 10)`），改了 `.sh` 就 `bash -n`。不要用 `py_compile`（会生成 `__pycache__`）；系统 Python 没有项目依赖，不能直接 `import core`。
2. Windows：`start_server.exe --port 6100` 启动，另开终端 `python tools/smoke_test.py --port 6100`；加 `--source file` 测对齐，加 `--seg-duration 6 --seg-overlap 2` 测拼接（预期结果见 PROJECT_INDEX.md「冒烟测试」）。
3. 在 Windows 开发机上改了 Linux 相关代码：做完第 1 步的静态检查，把需要真机确认的行为写进 docs/DEV_NOTES.md「待验证」，由用户在 Linux 服务器上用 `./start_server.sh --port 6100` 加冒烟测试验证。

## 文档维护

**每个事实只写在一个地方**，其他地方需要时写一句话并链接过去：

| 内容 | 归属 |
|---|---|
| 给人看的操作步骤 | [readme.md](readme.md)（Windows 快速开始）、[docs/LINUX.md](docs/LINUX.md)（Linux 部署与运维）、[models/README.md](models/README.md)（模型） |
| 架构、模块位置、跨模块约束、平台差异、启动参数、修改指南 | [PROJECT_INDEX.md](PROJECT_INDEX.md) |
| 只和某段代码有关的坑与原因 | 那段代码的注释 |
| 待验证、已知问题、仍然有效的决策与待办 | [docs/DEV_NOTES.md](docs/DEV_NOTES.md)，完成或失效就删除 |
| 做了什么、怎么验证的 | git 提交说明 |

- 改了行为，就更新这件事的归属位置，再搜一下其他提到它的地方是否仍然成立。
- PROJECT_INDEX.md 不逐步复述代码，也不写日期和测试记录；某一节超过一屏，就拆到 `docs/` 下单独成文。
