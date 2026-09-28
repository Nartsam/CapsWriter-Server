# Linux 部署与运维

Linux 入口是 `start_server.sh`，它只准备运行环境（找 Python、设动态库路径），然后执行 `start_server.py`——内容与 `start_server.exe` 内嵌的脚本相同，都是 `CapsWriterServer().start()`。命令行参数、环境变量、端口与模型检查都在这段共用代码里，所以 **Linux 与 Windows 的参数和行为完全一致**。平台差异只有 `config_server.py` 中 ONNX 推理后端的默认值（Linux 为 CUDA，见 [显卡加速.md](显卡加速.md)）以及托盘图标（Linux 自动跳过）。

## 前置条件

- x86_64 Linux（在 Ubuntu 22.04 上验证），NVIDIA 显卡驱动（需附带 Vulkan ICD：`/usr/share/vulkan/icd.d/nvidia_icd.json`）
- Python ≥ 3.10：有 conda 就用 conda 建环境，否则用系统 `python3` 建 venv（Debian/Ubuntu 需要 `python3-venv`）
- `libgomp1`（llama.cpp CPU 后端依赖，一般系统自带）
- 能访问 PyPI 与 GitHub（或配置 `HTTPS_PROXY` / pip 镜像）
- **不需要** git-lfs、系统 CUDA、系统 Vulkan 加载器：CUDA/cuDNN 由 pip 装进环境，`libvulkan.so.1` 随仓库提供

## 部署

```bash
git clone <仓库地址> capswriter && cd capswriter
./setup_linux.sh                    # 建环境 + 装依赖（约 3GB），最后会做一次自检
./start_server.sh --download-models # 下载模型（约 2.5GB），带 SHA-256 校验与断点续传
./start_server.sh --port 6000 --gpu 0
```

`setup_linux.sh` 选项：`--conda` / `--venv` 指定建环境方式（默认有 conda 用 conda，建在 `.conda-env/`；否则 venv，建在 `.venv/`）；`--system-cuda` 不用 pip 装 CUDA/cuDNN，改用系统已装的 CUDA 12 + cuDNN 9；`--python P` 指定建 venv 用的解释器。脚本可重复运行。

`start_server.sh` 按 `$CAPSWRITER_PYTHON` → `.conda-env/bin/python` → `.venv/bin/python` 的顺序找解释器，并把环境里 pip 装的 `nvidia/*/lib`、以及旧环境的 `lib/cudnn-local` 加进 `LD_LIBRARY_PATH`。

## 启动参数

`--host` / `--port` / `--gpu` / `--download-models`，以及对应的 `CAPSWRITER_*` 环境变量，完整说明见 [PROJECT_INDEX.md](../PROJECT_INDEX.md#启动参数与环境变量)。优先级：命令行 > 环境变量 > `config_server.py`。

## 接入 asmgt（服务管理）

`start_server.sh` 兼容 asmgt LAUNCHER-CONTRACT v1：

| 契约 | 实现 |
|---|---|
| 参数注入 | `ASMGT_PORT` / `ASMGT_HOST` / `ASMGT_GPU` 映射为 `CAPSWRITER_PORT` / `CAPSWRITER_HOST` / `CAPSWRITER_GPU` |
| 前台运行 | 最后一步 `exec` 成 Python 进程，不 fork 到后台；`PYTHONUNBUFFERED=1` |
| 停止 | SIGTERM / SIGHUP 立即优雅退出（关闭 WebSocket、终止识别子进程）；Ctrl+C 需 1 秒内按两次 |
| 失败即退出 | 端口被占用、模型缺失、参数非法都会在加载模型前以非 0 退出码退出（参数非法为 2） |
| 忙闲探针 | `logs/server_status.json` 中 `queued + processing > 0` 即 busy（WebSocket 服务，没有 HTTP 探针） |

`server_status.json` 示例：`{"pid": 1234, "processing": 0, "queued": 0, "started_at": …, "state": "idle", "updated_at": …}`。对整个进程组发 SIGTERM 也能干净退出；主进程被 SIGKILL 时，识别子进程与 Manager 进程会在几秒内自行退出、释放显存。

## 从旧部署迁移

旧部署（服务器上的 `CapsWriter-Server-Linux-Backup` 那一版）与现在的对应关系：

| 旧 | 新 |
|---|---|
| `run_server_linux.py`（启动时强行改写配置） | `start_server.py`（与 Windows 同一段代码，配置只来自 `config_server.py` + 参数/环境变量） |
| `start_server_linux.sh` | `start_server.sh`（参数与 asmgt 契约不变，另支持 `--download-models`） |
| `CAPSWRITER_VK_VISIBLE_DEVICES` / `CAPSWRITER_CUDA_VISIBLE_DEVICES` | `CAPSWRITER_GPU` / `--gpu`（两个后端用同一个编号，并设 `CUDA_DEVICE_ORDER=PCI_BUS_ID` 使编号与 nvidia-smi 一致） |
| `CAPSWRITER_MODEL` | 已删除（只剩 Qwen3-ASR） |
| `capswriter_service.sh`、`capswriter_maintenance.sh`、cron 脚本 | 已删除（旧脚本注释里就写明已被 asmgt 取代） |
| `.conda-env/lib/cudnn-local` 手动放的 cuDNN | pip 安装的 `nvidia-cudnn-cu12`（旧目录仍被 `start_server.sh` 兼容） |
| `models/Qwen3-ForcedAligner/*.onnx`（少一层目录，从未加载成功） | `models/Qwen3-ForcedAligner/Qwen3-ForcedAligner-0.6B/*` |

迁移步骤：

1. 在 asmgt 中停止旧实例。
2. clone 新仓库到新目录，运行 `./setup_linux.sh`。也可以先沿用旧环境：`export CAPSWRITER_PYTHON=<旧目录>/.conda-env/bin/python`（旧环境的依赖足够，`start_server.sh` 会自动带上它的 `lib/cudnn-local`）。
3. 模型：运行 `./start_server.sh --download-models`；或从旧目录拷贝 `models/Qwen3-ASR/`，对齐模型拷到 `models/Qwen3-ForcedAligner/Qwen3-ForcedAligner-0.6B/` 下。
4. 前台试跑 `./start_server.sh --port <端口> --gpu <N>`，用 `python3 tools/smoke_test.py --port <端口>` 验证（只需标准库，也可在其他机器上加 `--host` 远程测试）。
5. 把 asmgt 的启动命令改为新目录的 `start_server.sh`。

注意：旧版 `config_server.py` 的端口是 6016、日志级别 DEBUG；现在默认 6000、WARNING。asmgt 注入端口时不受影响；需要详细日志就改 `log_level = 'INFO'`。

## 排查

| 现象 | 查看/处理 |
|---|---|
| 识别慢，日志有 `ONNX 推理后端 CUDA 不可用，已回退到 CPUExecutionProvider` | CUDA/cuDNN 库没加载上：确认用 `start_server.sh` 启动（它负责 `LD_LIBRARY_PATH`）；重新运行 `./setup_linux.sh`；`--system-cuda` 模式下检查系统 CUDA 12 / cuDNN 9 |
| 识别慢，`nvidia-smi` 里看不到显存占用 | llama.cpp 没用上 Vulkan：确认 NVIDIA Vulkan ICD 存在；`log_level = 'INFO'` 后看 `ggml_vulkan: Found N Vulkan devices` |
| 启动即退出，提示端口冲突 | 换端口或停掉占用者：`ss -tlnp \| grep <端口>` |
| 启动即退出，提示缺少模型 | `./start_server.sh --download-models` |
| 其他 | 日志在 `logs/server_latest.log`（按 `log_level` 记录，超过 10MB 自动截断）；控制台只输出 WARNING 以上 |
