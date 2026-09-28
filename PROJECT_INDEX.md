# CapsWriter-Server 项目索引

> 面向 AI Agent 的项目入口文档。改代码前先读这里，再按需读相关源文件。
> 行为变更后必须同步更新本文档对应章节；跨会话的决策、待办、已知问题记在 [docs/DEV_NOTES.md](docs/DEV_NOTES.md)。

## 项目概览

CapsWriter 是离线语音输入工具（上游：[HaujetZhao/CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline) v2.5，MIT）。上游把客户端和服务端放在一起，这里拆成了两个仓库，本仓库**只做服务端**，仅供仓库主人自用：

- **CapsWriter-Client**（`../CapsWriter-Client`，另一个仓库，本仓库不改它）：按住快捷键录音 → 通过 WebSocket 把音频发给服务端 → 拿到文字后做热词替换、LLM 润色并模拟键盘上屏。
- **CapsWriter-Server**（本仓库）：WebSocket 服务，收音频、用 **Qwen3-ASR-1.7B** 识别、返回文字；文件转录任务额外用 **Qwen3-ForcedAligner** 生成字级时间戳。

服务端要在两个平台上运行，**两套入口、一份代码、行为一致**：

| 平台 | 入口 | Python 运行时 | 用途 |
|---|---|---|---|
| Windows 10/11 | `start_server.exe`（双击，最小化到托盘） | `internal/`：上游打包好的 Python 3.13 冻结运行时 | 本机日常使用 |
| Linux + NVIDIA | `start_server.sh` → `start_server.py` | `setup_linux.sh` 建的 conda/venv 环境（Python ≥ 3.10） | 服务器，由 asmgt 托管 |

两个入口最终都只执行 `CapsWriterServer().start()`；命令行参数、环境变量、端口与模型检查全部在这段共用代码里，平台差异只在 `config_server.py` 里显式写出（ONNX 推理后端默认值）和托盘（仅 Windows）。

---

## 目录结构

```
├── start_server.exe          # Windows 入口：PyInstaller 启动器（见“两个入口”），不要改、无法重建
├── start_server.py           # Linux/开发入口：内容与 exe 内嵌脚本完全相同
├── start_server.sh           # Linux 启动脚本：找 Python、设 LD_LIBRARY_PATH，exec start_server.py（兼容 asmgt）
├── setup_linux.sh            # Linux 环境安装（conda/venv + requirements-linux.txt + 自检）
├── requirements-linux.txt    # Linux 依赖（Windows 的依赖在 internal/ 里）
├── config_server.py          # 唯一的配置文件（两平台共用）
├── internal/                 # Windows 冻结运行时：Python 3.13 标准库 .pyc + 第三方库（已剔除客户端依赖）
├── core/
│   ├── __init__.py           # colorama 初始化，导出 logger 工具
│   ├── constants.py          # 音频格式常量（16kHz、float32、单声道）、标点集合 —— 与 Client 仓库同名文件保持一致
│   ├── protocol.py           # C/S 通信协议（AudioMessage / RecognitionMessage）—— 与 Client 仓库同名文件保持一致
│   ├── logger.py             # 日志：文件按 log_level 记录（logs/server_latest.log，超 10MB 截断），控制台只打 WARNING+
│   ├── server/               # ======= 服务端 =======
│   │   ├── app.py            # CapsWriterServer 门面类，唯一入口 start()
│   │   ├── launch.py         # 命令行参数 / 环境变量 → 覆盖配置；交互终端判断
│   │   ├── state.py          # ServerState（主进程）/ WorkerState（子进程）、rich console
│   │   ├── schema.py         # Task / Result / RecognitionSession
│   │   ├── runtime_status.py # logs/server_status.json 忙闲状态（跨进程文件锁）
│   │   ├── model_download.py # 模型清单与下载器（标准库实现）
│   │   ├── connection/       # WebSocket：server_manager（起服务、端口检查）、ws_recv（收音频、分段）、ws_send（回结果）
│   │   ├── worker/           # 识别子进程：process_manager、worker、model_loader、task_handler、pipeline、check_model、audio
│   │   ├── engines/          # 引擎：base（抽象接口）、factory、manager（对齐器懒加载代理）、language（语言映射）
│   │   │   ├── qwen_asr_gguf/        # Qwen3-ASR 适配器 asr_engine.py + 推理内核 inference/（源自上游 Qwen3-ASR-GGUF，已裁剪）
│   │   │   ├── force_aligner_gguf/   # ForcedAligner 适配器（推理内核共用 qwen_asr_gguf/inference）
│   │   │   └── llama/                # llama.cpp 的 ctypes 绑定 llama.py + 两平台运行库 bin/（见 bin/README.md）
│   │   ├── formatter/        # TextFormatter：中文数字 ITN、中英文空格
│   │   ├── merger/           # 分段结果拼接（见 docs/text_merge_algorithm.md）
│   │   └── ui/tray_manager.py# 托盘管理
│   ├── tools/                # chinese_itn、format_tools、token_sync、signal_handler、daemon_executor、
│   │                         # asyncio_to_thread、empty_working_set（Windows 内存回收）、my_status（rich 状态动画）
│   └── ui/tray.py            # 系统托盘（仅 Windows；其他平台 enable_min_to_tray 直接跳过）
├── models/                   # 模型（不进 git，见 models/README.md）
├── tools/smoke_test.py       # 冒烟测试客户端（仅标准库）+ assets/demo.wav
├── assets/icon.ico           # 托盘图标
├── docs/                     # LINUX.md、显卡加速.md、text_merge_algorithm.md、DEV_NOTES.md
└── logs/                     # 运行时生成：server_latest.log、server_status.json（不进 git）
```

---

## 运行时架构

三个进程：

| 进程 | 内容 |
|---|---|
| 主进程 | asyncio 事件循环 + WebSocket 服务（`connection/`）、托盘、信号处理 |
| Manager 进程 | `multiprocessing` 的 `SyncManager`，只托管一个跨进程列表 `sockets_id`（当前在线连接），子进程靠它判断任务的客户端是否已断开 |
| 识别子进程 | 加载模型并串行处理任务（`worker/`）。只有它 import onnxruntime / llama.cpp，所以 GPU 相关环境变量必须在它启动前设好 |

数据流：

```
Client ──AudioMessage(JSON, base64 float32 16k)──▶ ws_recv ─▶ AudioCache 按 seg_duration/seg_overlap 切片
   ─▶ queue_in ─▶ TaskHandler（按 task_id 缓冲，优先处理最新会话）─▶ TaskPipeline.process
   ─▶ queue_out ─▶ ws_send ──RecognitionMessage(JSON)──▶ Client
```

### 启动流程（`CapsWriterServer.start()`）

1. `launch.parse_args()` → `export_args()`（参数写回环境变量）→ `apply_overrides()`（环境变量覆盖 `config_server` 的类属性，`--gpu` 设置 GPU 可见性变量）
2. `--download-models`：下载/补全模型后退出
3. 注册信号：SIGINT（1 秒内按两次退出）、SIGTERM / SIGHUP（立即退出）。子进程（识别子进程、Manager）会把 SIGTERM/SIGHUP 恢复为默认处理，并在主进程消失后自行退出——Linux 的 fork 会继承这些处理函数，不恢复会出错
4. 打印横幅；**检查端口**，被占用则退出码 1（交互终端里等回车再退）
5. `check_model()`：缺必需模型 → 交互终端询问是否立即下载，否则退出码 1；只缺对齐模型 → 提示后继续。第 4、5 步可能需要用户看提示，所以必须在托盘隐藏控制台之前
6. 启动托盘（Windows 且有控制台窗口时才生效，会隐藏控制台）
7. `runtime_status.initialize()` 重置忙闲状态文件
8. `ProcessManager.start()`：建 Manager 列表 → 拉起识别子进程 → 阻塞等待子进程发来“模型已加载”（子进程先调用 `apply_overrides()`，因为 Windows 的 spawn 会重新 import 配置）；子进程加载失败则退出码 1
9. `SocketManager.start()`：`websockets.serve`，主线程进入 `ws_send` 发送循环直到退出

### 识别流水线（`worker/pipeline.py`）

每个片段：float32 字节 → numpy（< 0.1s 的空片段直接跳过）→ `QwenASREngine.decode_stream`（ONNX 编码器 → 拼 prompt embedding → llama.cpp 解码，温度 0.4、异常时升温重试，**同一段音频多次识别结果可能有细微差异**）→ `merge_by_text` 累积 `text`。
文件任务（`source == 'file'`）且模型不带时间戳时，调用 `ManagedAlignerProxy.align` 得到字级时间戳（首次使用时加载，闲置 `aligner_idle_timeout` 秒后卸载）→ `merge_tokens_by_sequence_matcher` 累积 tokens/timestamps。
最后一段：`TextFormatter.format`（ITN、中英空格）→ `sync_tokens_from_text` 把格式化结果同步回 tokens → 若无 tokens（麦克风任务）按字数均分时长生成时间戳。

---

## 启动参数与环境变量

两个入口完全相同（实现：`core/server/launch.py`）。优先级：命令行 > 环境变量 > `config_server.py`。

| 命令行 | 环境变量 | 作用 |
|---|---|---|
| `--host ADDR` | `CAPSWRITER_HOST` | 监听地址（默认 `0.0.0.0`） |
| `--port N` | `CAPSWRITER_PORT` | 监听端口（默认 `6000`） |
| `--gpu N` | `CAPSWRITER_GPU` | 只用第 N 张 GPU：设 `GGML_VK_VISIBLE_DEVICES`、`CUDA_VISIBLE_DEVICES`，并默认 `CUDA_DEVICE_ORDER=PCI_BUS_ID`（编号与 nvidia-smi 一致） |
| — | `CAPSWRITER_ONNX_PROVIDER` | 覆盖 ONNX 推理后端：`CPU` / `CUDA` / `DML` / `TRT` |
| `--download-models` | — | 下载/补全模型后退出 |
| `-h` | — | 帮助 |

仅 `start_server.sh`：`ASMGT_PORT` / `ASMGT_HOST` / `ASMGT_GPU`（asmgt 注入）映射为上面的 `CAPSWRITER_*`；`CAPSWRITER_PYTHON` 指定解释器。

退出码：0 正常；1 端口冲突 / 缺必需模型 / 子进程加载失败 / 参数值非法；2 未知参数（argparse）。

---

## 配置（`config_server.py`）

| 类 | 内容 |
|---|---|
| `ServerConfig` | `addr`、`port`（字符串）、`format_num`（中文数字转阿拉伯数字）、`format_spell`（中英空格）、`enable_tray`、`log_level`、`aligner_idle_timeout` |
| `ModelPaths` | 模型文件路径（相对项目根目录；启动时 `CapsWriterServer.__init__` 会 chdir 到项目根） |
| `ModelDownload` | `base_url`：模型压缩包下载地址前缀（可换镜像） |
| `Qwen3ASRGGUFArgs` | 识别引擎参数，**所有公开属性会原样作为关键字参数传给 `ASREngineConfig`**，新增属性必须同时是该 dataclass 的字段（辅助变量用下划线开头） |
| `ForceAlignerGGUFArgs` | 对齐引擎参数，同上，对应 `AlignerConfig` |

唯一的平台差异：`_DEFAULT_ONNX_PROVIDER = 'CPU' if sys.platform == 'win32' else 'CUDA'`（原因见 [docs/显卡加速.md](docs/显卡加速.md)）。

---

## 通信协议（`core/protocol.py`）

**与 Client 仓库的 `core/protocol.py`、`core/constants.py` 逐字相同，修改协议必须两边同步。**

- 客户端 → 服务端 `AudioMessage`（JSON 文本帧）：`task_id`、`source`（`mic` / `file`）、`data`（base64 的 float32 16kHz 单声道 PCM）、`is_final`、`time_start`、`seg_duration`（默认 15，客户端实际发 60）、`seg_overlap`（默认 2，客户端实际发 4）、`context`（作为 Qwen3 的 system prompt）、`language`（统一语言代码，见 `engines/language.py`，`auto` 表示不指定）
- 服务端 → 客户端 `RecognitionMessage`：`task_id`、`is_final`、`duration`、`time_start/submit/complete`、`text`（主输出）、`text_accu`、`tokens`、`timestamps`
- WebSocket 子协议 `binary`，`max_size=None`；每个完成的片段都会回一条（`is_final=False`），最后一条 `is_final=True`
- 结果只发回提交任务的那个连接；连接断开后，其未处理任务会被子进程丢弃

---

## 两个入口的实现细节

- **`start_server.exe`** 是 PyInstaller 6 的启动器，内嵌脚本等价于 `start_server.py`，再加一个把 exe 所在目录插入 `sys.path` 的钩子。它不包含任何项目代码：`core/`、`config_server.py` 都从源码目录实时加载，**改完代码重启 exe 即生效**。第三方库与标准库全部来自 `internal/`（Python 3.13，`.pyc`/`.pyd`）。exe 本身无法在本仓库重建，也不需要重建。
- `internal/` 只能“减”不能随意“加”：纯 Python 包可以直接把源码目录拷进 `internal/`；带 C 扩展的包必须是 cp313-win_amd64 版本。能用标准库就不要引入新依赖。PyInstaller 的运行时钩子会检查 `internal/_tcl_data`、`_tk_data` 是否存在，**这两个目录不能删**。
- **Linux** 用 `requirements-linux.txt` 的 pip 包，需要和 `internal/` 里的库在 API 层面兼容（目前 numpy 2.x、websockets 16、onnxruntime 1.23、gguf、rich、colorama）。
- 代码必须同时兼容 **Python 3.10（Linux）与 3.13（Windows）**：不要用 3.11+ 才有的语法/标准库（如 `tomllib`、`except*`、`typing.Self`）。
- 平台相关的分支必须显式写（`sys.platform == 'win32'` / `os.name == 'posix'`），并在本文档记录。目前有：ONNX 后端默认值、托盘（仅 Windows）、`empty_working_set`（仅 Windows）、端口检查的 `SO_REUSEADDR`（仅 POSIX）、状态文件锁（fcntl / msvcrt）、`llama.py` 加载 `.dll` / `.so`。

---

## 开发环境与验证

- **语法检查**：`python -c "import ast; ..."` 用 `ast.parse`，不要用 `py_compile`（会生成 `__pycache__`）。系统 Python 通常没有项目依赖，不能直接 `import core`。
- **Windows 实机验证**：改完代码 → `start_server.exe --port 6100`（换个端口，避免和正在使用的实例冲突）→ 另开终端 `python tools/smoke_test.py --port 6100`。
- **Linux 验证**：`./start_server.sh --port 6100` + `python3 tools/smoke_test.py --port 6100`；本机可用 WSL 模拟（2026-09-28 在 WSL Ubuntu 22.04 上验证过 clone → setup → 下载模型 → 启动 → 冒烟测试 → 信号处理，详见 docs/DEV_NOTES.md；WSL 没有 Vulkan，llama.cpp 只能走 CPU）。
- **`tools/smoke_test.py`**：只依赖标准库，模拟客户端发送 `tools/assets/demo.wav`（16.5 秒新闻播报）。常用参数：`--source file`（触发对齐模型，输出的时间戳范围应接近 0.3~16.3s 而非 0~16.2s 的均分值）、`--seg-duration 6 --seg-overlap 2`（强制多段，测试拼接）、`--expect 子串`、`--json`、`--host`（测远程服务器）。期望结果约为“财联社1月7日电，从国家邮政局了解到，十四五以来，我国建成世界上规模最大、受益人数最多的寄递网络，邮政快递业业务年均增长10%。”（采样有随机性，标点可能略有差异）。
- **提交规范**：`__pycache__`、`models/`、`logs/` 不进仓库；`internal/` 按字节原样跟踪（`.gitattributes` 已设 `-text`）；Linux `.so` 的别名是 git 符号链接，在 Windows 上表现为小文本文件，**不要用普通文件覆盖它们**；`.sh` 必须是 LF 且带可执行位（Windows 上用 `git update-index --chmod=+x`）。

---

## 常见修改指南

| 修改目标 | 涉及文件 |
|---|---|
| 默认端口、日志级别、数字/空格格式化开关 | `config_server.py` 的 `ServerConfig` |
| 新增启动参数 / 环境变量 | `core/server/launch.py`（`parse_args` / `export_args` / `apply_overrides`），并更新本文档“启动参数与环境变量”和 `docs/LINUX.md` |
| ONNX 后端、GPU 加速开关 | `config_server.py` 的 `*Args.onnx_provider` / `llm_use_gpu`；Provider 选择逻辑在 `engines/qwen_asr_gguf/inference/encoder.py` |
| Qwen3 解码参数（温度、prompt 结构、上下文长度） | `engines/qwen_asr_gguf/asr_engine.py`（`decode_stream`）、`inference/asr.py`（`_build_prompt_embd` / `_decode`）、`config_server.py` 的 `n_ctx` / `chunk_size` |
| 语言代码映射 | `engines/language.py` |
| 识别流水线（文件任务对齐、最终格式化） | `worker/pipeline.py` |
| 分段拼接算法 | `merger/text_merger.py`、`merger/token_merger.py`，同步更新 `docs/text_merge_algorithm.md` |
| ITN / 中英空格 | `core/tools/chinese_itn.py`、`core/tools/format_tools.py`、`formatter/text_formatter.py` |
| 任务调度（多连接公平性、断连清理） | `worker/task_handler.py` |
| 音频分段参数的服务端处理 | `connection/ws_recv.py` |
| 通信协议 | `core/protocol.py` + 两端收发代码（**Client 仓库同步修改**） |
| 忙闲状态文件 | `core/server/runtime_status.py` 及调用点（`ws_recv.py`、`task_handler.py`） |
| 换模型 / 换量化版本 | `config_server.py` 的 `ModelPaths` + `core/server/model_download.py` 的 `PACKAGES`（压缩包名、SHA-256、大小），同步更新 `models/README.md` |
| 升级 llama.cpp | `engines/llama/bin/`（两平台同版本）+ 核对 `engines/llama/llama.py` 的 ctypes 结构体，见 `bin/README.md` |
| Linux 启动环境（解释器查找、库路径、asmgt 契约） | `start_server.sh`、`setup_linux.sh`、`requirements-linux.txt`、`docs/LINUX.md` |
| 托盘 | `core/ui/tray.py`、`core/server/ui/tray_manager.py` |
