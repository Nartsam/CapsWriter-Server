# CapsWriter-Server

CapsWriter 离线语音输入的**服务端**：通过 WebSocket 接收 CapsWriter-Client（另一个仓库）发来的音频，用 **Qwen3-ASR-1.7B** 在本地识别后返回文字；客户端的“文件转录”额外用 **Qwen3-ForcedAligner** 生成字级时间戳。

Fork 自 [HaujetZhao/CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline) v2.5，拆出服务端单独维护（客户端代码和其他识别模型已移除），仅供个人使用。

- 完全离线，识别在本机/自己的服务器上完成
- Windows 与 Linux 两套入口、同一份代码、相同的参数与行为
- 显卡加速：LLM 解码走 llama.cpp Vulkan，音频编码器 Windows 用 DirectML、Linux 用 CUDA

## 快速开始

### Windows 10/11

1. 需要 [VC++ 运行库](https://aka.ms/vs/17/release/vc_redist.x64.exe)（缺 DLL 时安装）。
2. 下载模型（约 2.5GB，只需一次）：双击 `download_models.bat`（等同于 `start_server.exe --download-models`）。也可以直接双击 `start_server.exe`，缺模型时会询问是否下载。
3. 双击 `start_server.exe`，窗口会自动最小化到托盘；右键托盘图标可显示/隐藏窗口、重启或退出（重启等同于退出后再手动启动一次，改完配置用它即可生效）。
4. 开机启动：把 `start_server.exe` 的快捷方式放进 `Win+R` → `shell:startup` 打开的文件夹。

`start_server.exe` 只是启动器，运行时在 `internal/`，代码在 `core/`，都随仓库提供，clone 下来即可用（模型除外）。

### Linux（NVIDIA 显卡）

安装环境 → 下载模型 → 启动，见 [docs/LINUX.md](docs/LINUX.md)（含 asmgt 托管与从旧部署迁移）。

## 配置与启动参数

所有设置在 [`config_server.py`](config_server.py)（两个平台共用）。监听地址、端口、GPU 也可以启动时临时指定，两个入口完全相同：

```
start_server.exe / ./start_server.sh  [--host ADDR] [--port N] [--gpu N] [--download-models]
```

也可以用对应的 `CAPSWRITER_*` 环境变量，完整说明见 [PROJECT_INDEX.md](PROJECT_INDEX.md#启动参数与环境变量)。

客户端连接地址：`ws://<服务端IP>:<端口>`（在 Client 仓库的 `config_client.py` 中配置）。

## 验证服务是否正常

```bash
python tools/smoke_test.py --port 6000               # 任意 Python 3.8+，无需安装依赖
python tools/smoke_test.py --host 10.0.0.2 --port 6000 --source file
```

## 文档

| 文档 | 内容 |
|---|---|
| [PROJECT_INDEX.md](PROJECT_INDEX.md) | 项目结构与架构（开发/AI Agent 入口） |
| [docs/LINUX.md](docs/LINUX.md) | Linux 部署、asmgt、故障排查 |
| [docs/显卡加速.md](docs/显卡加速.md) | GPU 加速配置 |
| [models/README.md](models/README.md) | 模型下载与目录结构 |
| [docs/text_merge_algorithm.md](docs/text_merge_algorithm.md) | 长音频分段拼接算法 |
| [docs/DEV_NOTES.md](docs/DEV_NOTES.md) | 开发笔记：决策、已知问题、待办 |

## 常见问题

**Q：启动后立即退出，提示端口冲突？** 已有服务端在运行（托盘里找找），或换个端口 `--port 6001`。

**Q：识别很慢？** 把 `config_server.py` 的 `log_level` 改为 `'INFO'` 后查看 `logs/server_latest.log`，确认 ONNX 后端与 Vulkan 设备是否用上了显卡，见 [docs/显卡加速.md](docs/显卡加速.md)。

**Q：想隐藏/找回控制台窗口？** 双击托盘图标切换显示/隐藏。
