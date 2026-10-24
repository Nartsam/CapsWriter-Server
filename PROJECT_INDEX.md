# CapsWriter-Server 项目索引

> 项目地图：模块在哪、怎么协作、跨模块的约束与平台差异、改什么动哪里。
> 这里不逐步复述代码，也不记历史与测试记录；硬性约束、验证步骤与文档分工见 [CLAUDE.md](CLAUDE.md)。

## 项目概览

CapsWriter 是离线语音输入工具（上游：[HaujetZhao/CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline) v2.5，MIT）。上游把客户端和服务端放在一起，这里拆成了两个仓库，本仓库**只做服务端**，仅供仓库主人自用：

- **CapsWriter-Client**（`../CapsWriter-Client`）：按住快捷键录音 → 通过 WebSocket 把音频发给服务端 → 拿到文字后做热词替换、LLM 润色并模拟键盘上屏。
- **CapsWriter-Server**（本仓库）：WebSocket 服务，收音频、用 **Qwen3-ASR-1.7B** 识别、返回文字；文件转录任务额外用 **Qwen3-ForcedAligner** 生成字级时间戳。

| 平台 | 入口 | Python 运行时 | 用途 |
|---|---|---|---|
| Windows 10/11 | `start_server.exe`（双击，最小化到托盘） | `internal/`：上游打包好的 Python 3.13 冻结运行时 | 本机日常使用 |
| Linux + NVIDIA | `start_server.sh` → `start_server.py` | `setup_linux.sh` 建的 conda/venv 环境（Python ≥ 3.10） | 服务器，由 asmgt 托管（见 [docs/Linux部署.md](docs/Linux部署.md)） |

两个入口都只执行 `CapsWriterServer().start()`，其余代码全部共用；两个平台的不同之处全部列在下文「平台差异」。

---

## 目录结构

```
├── start_server.exe          # Windows 入口：PyInstaller 启动器（见“Windows 运行时”），无法重建
├── start_server.py           # Linux/开发入口：内容与 exe 内嵌脚本完全相同
├── start_server.sh           # Linux 启动脚本：找 Python、设 LD_LIBRARY_PATH，exec start_server.py（兼容 asmgt）
├── setup_linux.sh            # Linux 环境安装（conda/venv + requirements-linux.txt + 自检）
├── download_models.bat       # 下载模型（Windows，双击）= start_server.exe --download-models
├── download_models.sh        # 下载模型（Linux）= ./start_server.sh --download-models
├── requirements-linux.txt    # Linux 依赖（Windows 的依赖在 internal/ 里）
├── config_server.py          # 唯一的配置文件（两平台共用）
├── hot-server.txt            # 服务端热词（提高概率、不替换），保存后下次识别即生效；格式见文件头
├── internal/                 # Windows 冻结运行时：Python 3.13 标准库 .pyc + 第三方库
├── core/
│   ├── __init__.py           # colorama 初始化，导出 logger 工具
│   ├── constants.py          # 音频格式常量（16kHz、float32、单声道）、标点集合 —— 与 Client 仓库逐字相同
│   ├── protocol.py           # C/S 通信协议（AudioMessage / RecognitionMessage）—— 与 Client 仓库逐字相同
│   ├── logger.py             # 日志：文件按 log_level 记录（logs/server_latest.log，超 10MB 截断），控制台只打 WARNING+
│   ├── server/               # ======= 服务端 =======
│   │   ├── app.py            # CapsWriterServer 门面类：start() / stop() / restart()
│   │   ├── launch.py         # 命令行参数 / 环境变量 → 覆盖配置；交互终端判断；重启时拉起新实例
│   │   ├── state.py          # ServerState（主进程）/ WorkerState（子进程）、rich console
│   │   ├── schema.py         # Task / Result / RecognitionSession
│   │   ├── runtime_status.py # logs/server_status.json 忙闲状态（跨进程文件锁）
│   │   ├── model_download.py # 模型清单与下载器（标准库实现）
│   │   ├── connection/       # WebSocket：server_manager（起服务、端口检查）、ws_recv（收音频、分段）、ws_send（回结果）、pause_splitter（流式任务的停顿切句）
│   │   ├── worker/           # 识别子进程：process_manager、worker、model_loader、task_handler、pipeline、streaming（流式任务的前缀续写）、check_model、audio
│   │   ├── engines/          # 引擎：base（抽象接口）、factory、manager（对齐器懒加载代理）、language（语言映射）
│   │   │   ├── qwen_asr_gguf/        # Qwen3-ASR 适配器 asr_engine.py + 推理内核 inference/（源自上游 Qwen3-ASR-GGUF，已裁剪）
│   │   │   ├── force_aligner_gguf/   # ForcedAligner 适配器（推理内核共用 qwen_asr_gguf/inference）
│   │   │   └── llama/                # llama.cpp 的 ctypes 绑定 llama.py + 两平台运行库 bin/（见 bin/README.md）
│   │   ├── formatter/        # TextFormatter：中文数字 ITN、中英文空格
│   │   ├── merger/           # 分段结果拼接（见 docs/分段拼接算法.md）
│   │   └── ui/tray_manager.py# 托盘管理（菜单回调接到 app 的 stop / restart）
│   ├── tools/                # chinese_itn、format_tools、token_sync、signal_handler、daemon_executor、
│   │                         # asyncio_to_thread、empty_working_set（Windows 内存回收）、my_status（rich 状态动画）
│   └── ui/tray.py            # 系统托盘：显示/隐藏、重启、退出
├── models/                   # 模型（不进 git，见 models/README.md）
├── tools/smoke_test.py       # 冒烟测试客户端（仅标准库）+ assets/demo.wav
├── assets/icon.ico           # 托盘图标
├── docs/                     # 专题文档与开发笔记（清单见 CLAUDE.md 的阅读顺序）
└── logs/                     # 运行时生成：server_latest.log、server_status.json（不进 git）
```

---

## 热词同步

`core/hotword_sync.py` 与客户端逐字相同（兼容 Python 3.8），提供格式、三方合并和原子写入。`server/hotword_sync.py` 管理服务端持久状态、独立 WebSocket `/hotword-sync` 和客户端广播；`server/hotword_git.py` 只操作 `logs/hotword_sync/git/` 的配置仓库，在 `app.start()` / `_finish()` 同步。

同步连接不登记到音频会话池。服务端保存后才 ACK，客户端按服务端身份保存共同基线；第一次切换服务器时使用可证明的共同 Git 提交，没有共同基线时合并双方内容。配置和用户操作集中在 [热词同步](docs/热词同步.md)。

## 运行时架构

三个进程：

| 进程 | 内容 |
|---|---|
| 主进程 | asyncio 事件循环 + WebSocket 服务（`connection/`）、托盘、信号处理 |
| Manager 进程 | `multiprocessing` 的 `SyncManager`，只托管一个跨进程列表 `sockets_id`（当前在线连接），子进程靠它判断任务的客户端是否已断开 |
| 识别子进程 | 加载模型并串行处理任务（`worker/`）；只有它 import onnxruntime / llama.cpp |

数据流：

```
Client ──AudioMessage(JSON, base64 float32 16k)──▶ ws_recv ─▶ AudioCache 按 seg_duration/seg_overlap 切片（流式任务按停顿切句）
   ─▶ queue_in ─▶ TaskHandler（按 task_id 缓冲，优先处理最新会话）─▶ TaskPipeline.process
   ─▶ queue_out ─▶ ws_send ──RecognitionMessage(JSON)──▶ Client
```

### 启动顺序的约束（`app.py` 的 `start()`）

具体顺序看代码；调整时要保持这些前后关系：

- 参数与环境变量覆盖（`launch.py`）最先执行，之后的一切都读 `config_server`。
- 端口检查、模型检查在托盘之前：两者都可能需要用户看提示或回答问题，而托盘会隐藏控制台窗口。
- GPU 相关环境变量必须在识别子进程启动前设好。Windows 的子进程会重新 import 配置，所以子进程里要再调用一次 `apply_overrides()`。
- 识别子进程加载完模型后，WebSocket 才开始监听。
- `start()` 只从 `_finish()` 退出：退出码与重启都在那里统一处理。

### 退出与重启

- **退出**（托盘菜单、SIGTERM/SIGHUP、1 秒内两次 Ctrl+C）都走 `CapsWriterServer.stop()`：关闭 WebSocket（释放端口）→ 结束识别子进程并等它退出 → 卸载托盘 → 停止事件循环。
- **重启**（托盘菜单，仅 Windows）：执行同一个 `stop()`，完成后由 `_finish()` 用原命令行拉起新实例。必须先退出再启动，原因见 `app.py` 的 `restart()`。Linux 的重启交给 asmgt。
- **识别子进程中途退出**（崩溃、被系统杀掉）：`ProcessManager` 的监视线程发现后调用 `app.abort()`，走同一个 `stop()`，服务端以退出码 1 整体退出，由托管程序（asmgt 等）重启；进程内不自行拉起。双击运行时服务端直接结束，托盘图标消失。
- 识别子进程和 Manager 进程在主进程消失后会自行退出，不残留、不占显存（`core/tools/signal_handler.py`）。

### 识别流水线（`worker/pipeline.py`）

每个片段：float32 字节 → numpy（< 0.1s 的空片段直接跳过）→ `QwenASREngine.decode_stream`（ONNX 编码器 → 拼 prompt embedding → llama.cpp 解码，温度 0.4、异常时升温重试，**同一段音频多次识别结果可能有细微差异**）→ `merge_by_text` 累积 `text`。
解码的每一步先经过 `inference/biasing.py` 的 `DecodeBias`：弱逗号只删不换（少加逗号）、`hot-server.txt` 热词的词级先验（试探写完整个词，比较后采纳或回滚 KV Cache）。原理与实测数据在该文件头。
文件任务（`source == 'file'`）调用 `ManagedAlignerProxy.align` 得到字级时间戳（首次使用时加载，闲置 `aligner_idle_timeout` 秒后卸载；编码器走 DirectML 时常驻不卸载，原因见 `engines/manager.py`）→ `merge_tokens_by_sequence_matcher` 累积 tokens/timestamps。
最后一段：`TextFormatter.format`（ITN、中英空格）→ `sync_tokens_from_text` 把格式化结果同步回 tokens → 若无 tokens（麦克风任务）按字数均分时长生成时间戳。

### 流式任务（`AudioMessage.streaming`，仅麦克风）

客户端按住录音键时逐句上屏用。不按 `seg_duration` 切，`ws_recv` 用 `PauseSplitter`（因果能量 VAD，50ms 帧）在停顿处切句，每句提交一个片段（片段间无重叠）；`TaskPipeline._process_stream` 用 `StreamState` 做前缀续写：把前几句的音频连同本句送进模型、前几句的识别文字作 assistant 前缀，模型只续写本句。每句末尾的标点暂留，由下一句开头的标点决定；每句单独格式化后放进 `Result.delta` 发出，发出的文字不再改，`text` 是各 `delta` 的拼接。松手前的静音尾巴（已切出过句子时）标为 `silent` 不识别。参数、实验数据与取舍写在 `connection/pause_splitter.py`、`worker/streaming.py` 的文件头。

同一任务的片段在 `TaskBuffer` 里先进先出，前缀依赖上一句的结果，所以必须按序处理；改调度时要保持这一点。

---

## 平台差异

这是完整清单：平台相关的分支都要显式写在代码里，并加进这张表。

| 差异 | 位置 | Windows | Linux |
|---|---|---|---|
| ONNX 推理后端默认值 | `config_server.py` 的 `_DEFAULT_ONNX_PROVIDER` | `DML`（onnxruntime-directml，另可选 `CPU`） | `CUDA`（onnxruntime-gpu），原因见 [显卡加速.md](docs/显卡加速.md) |
| 托盘与“重启”菜单 | `core/ui/tray.py` | 有 | 自动跳过 |
| 子进程启动方式 | `multiprocessing` 默认值 | spawn：重新 import 配置，需再覆盖一次 | fork：会继承主进程的信号处理，子进程需恢复默认 |
| 识别子进程内存回收 | `worker/worker.py` → `empty_working_set` | 模型加载后清空工作集 | 无 |
| 端口检查的 `SO_REUSEADDR` | `connection/server_manager.py` | 不设（Windows 上设了会检查不出冲突） | 设 |
| 状态文件锁 | `runtime_status.py` | `msvcrt` | `fcntl` |
| llama.cpp 运行库 | `engines/llama/llama.py` | `.dll` | `.so` |
| 交互终端判断 | `launch.is_interactive` | `isatty` + `GetConsoleMode`（排除重定向到 NUL） | `isatty` |
| 重启时的新进程 | `launch.spawn_new_instance` | 新控制台窗口 | 新会话（Linux 没有托盘，实际不会触发） |
| 缺模型时提示的下载命令 | `worker/check_model.py` | `download_models.bat` | `./download_models.sh` |

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

退出码：0 正常；1 端口冲突 / 缺必需模型 / 识别子进程加载失败或中途退出 / 参数值非法；2 未知参数（argparse）。

---

## 配置（`config_server.py`）

| 类 | 内容 |
|---|---|
| `ServerConfig` | `addr`、`port`（字符串）、`format_num`（中文数字转阿拉伯数字）、`format_spell`（中英空格）、`enable_tray`、`log_level`、`aligner_idle_timeout` |
| `ModelPaths` | 模型文件路径（相对项目根目录；`CapsWriterServer.__init__` 会 chdir 到项目根） |
| `ModelDownload` | `base_url`：模型压缩包下载地址前缀（可换镜像） |
| `Qwen3ASRGGUFArgs` | 识别引擎参数（含 `comma_drop_margin` 少加逗号、`hotword_file` / `hotword_bonus` 服务端热词），**所有公开属性会原样作为关键字参数传给 `ASREngineConfig`**，新增属性必须同时是该 dataclass 的字段（辅助变量用下划线开头） |
| `ForceAlignerGGUFArgs` | 对齐引擎参数，同上，对应 `AlignerConfig` |

---

## 通信协议（`core/protocol.py`）

字段定义看 `protocol.py`（与 Client 仓库逐字相同）。不看代码不容易知道的几点：

- 客户端发 `AudioMessage`（JSON 文本帧），`data` 是 base64 编码的 float32 16kHz 单声道 PCM。
- `seg_duration` / `seg_overlap` 的协议默认值是 15 / 2，但客户端实际发 60 / 4；分段在服务端 `ws_recv` 里做（见 [分段拼接算法.md](docs/分段拼接算法.md)）。
- `context` 作为 Qwen3 的 system prompt；`language` 是统一语言代码（见 `engines/language.py`），`auto` 表示不指定：模型先写出语言行再写正文，判断为没有人声时返回空（见 `inference/asr.py` 的 `_build_prompt_embd`）。
- 服务端每完成一个片段就回一条 `RecognitionMessage`（`is_final=False`），最后一条 `is_final=True`；主输出是 `text`，文件转录用 `tokens` / `timestamps`。
- `streaming=True`（客户端流式上屏）：忽略 `seg_*`，每句回一条，新增文字在 `delta` 里（见上文「流式任务」）。旧客户端不发这个字段，行为不变；新客户端连旧服务端时收不到 `delta`，按整段处理。
- WebSocket 子协议 `binary`，`max_size=None`。结果只发回提交任务的那个连接；连接断开后，其未处理的任务会被子进程丢弃。

---

## Windows 运行时（`start_server.exe` 与 `internal/`）

- **`start_server.exe`** 是 PyInstaller 6 的启动器，内嵌脚本等价于 `start_server.py`，外加一个把 exe 所在目录插入 `sys.path` 的钩子。它不包含任何项目代码：`core/`、`config_server.py` 都从源码目录实时加载，**改完代码重启 exe 即生效**。
- **`internal/`** 提供全部第三方库与标准库（Python 3.13，`.pyc` / `.pyd`）。已按真实运行时的导入追踪删掉未用到的客户端依赖（清单见提交 95469b6）。要加包：纯 Python 包可以直接把源码目录拷进来，带 C 扩展的必须是 cp313-win_amd64 版本。`requests`、`tqdm`、`yaml` 是 gguf 声明的依赖，没被导入也要保留；`_tcl_data`、`_tk_data` 会被 PyInstaller 的运行时钩子检查，**不能删**。
- **Linux** 用 `requirements-linux.txt` 的 pip 包，版本要和 `internal/` 里的库在 API 层面兼容（目前 numpy 2.x、websockets 16、onnxruntime 1.23、gguf、rich、colorama）。

---

## 冒烟测试（`tools/smoke_test.py`）

只依赖标准库，模拟客户端发送 `tools/assets/demo.wav`（16.5 秒新闻播报），可以对任意主机测试（`--host`）。

- 预期结果大致是“财联社1月7日电，从国家邮政局了解到，十四五以来，我国建成世界上规模最大、受益人数最多的寄递网络，邮政快递业业务年均增长10%。”。解码是采样的，标点和个别字可能不同，所以用 `--expect 子串` 检查稳定的部分。
- `--source file`：触发对齐模型。时间戳范围应接近 0.3~16.3s；如果是 0~16.2s 的均分值，说明对齐模型没有生效。
- `--seg-duration 6 --seg-overlap 2`：强制切成多段，用来测试拼接。
- `--streaming`：流式任务，按真实时间每 50ms 发一块（约 17 秒）。应在发送过程中陆续收到 2～3 条「新增」，各条 `delta` 拼起来等于最终结果（脚本会检查），最终文本与整段识别基本相同。
- `--json`：输出原始结果。

---

## 常见修改指南

| 修改目标 | 涉及文件 |
|---|---|
| 默认端口、日志级别、数字/空格格式化开关 | `config_server.py` 的 `ServerConfig` |
| 新增启动参数 / 环境变量 | `core/server/launch.py`（`parse_args` / `export_args` / `apply_overrides`），并更新本文档“启动参数与环境变量”；影响 Linux 部署的同步 `docs/Linux部署.md` |
| ONNX 后端、GPU 加速开关 | `config_server.py` 的 `*Args.onnx_provider` / `llm_use_gpu`；Provider 选择逻辑在 `engines/qwen_asr_gguf/inference/encoder.py` |
| Qwen3 解码参数（温度、prompt 结构、上下文长度） | `engines/qwen_asr_gguf/asr_engine.py`（`decode_stream`）、`inference/asr.py`（`_build_prompt_embd` / `_decode`）、`config_server.py` 的 `n_ctx` / `chunk_size` |
| 标点多少、服务端热词 | `inference/biasing.py`（规则与实测数据在文件头）、`config_server.py` 的 `comma_drop_margin` / `hotword_*`、`hot-server.txt`；KV Cache 回滚用 `engines/llama/llama.py` 的 `LlamaContext.truncate` |
| 语言代码映射 | `engines/language.py` |
| 识别流水线（文件任务对齐、最终格式化） | `worker/pipeline.py` |
| 分段拼接算法 | `merger/text_merger.py`、`merger/token_merger.py`，同步更新 `docs/分段拼接算法.md` |
| ITN / 中英空格 | `core/tools/chinese_itn.py`、`core/tools/format_tools.py`、`formatter/text_formatter.py` |
| 任务调度（多连接公平性、断连清理） | `worker/task_handler.py` |
| 音频分段参数的服务端处理 | `connection/ws_recv.py` |
| 流式任务的切句（停顿阈值、句长限制） | `connection/pause_splitter.py`；接入点在 `ws_recv._handle_streaming` |
| 流式任务的前缀续写（窗口、前缀、暂留标点、逐段格式化） | `worker/streaming.py`、`worker/pipeline.py` 的 `_process_stream`；改完用 `smoke_test.py --streaming` 验证 |
| 通信协议 | `core/protocol.py` + 两端收发代码（**Client 仓库同步修改**） |
| 忙闲状态文件 | `core/server/runtime_status.py` 及调用点（`ws_recv.py`、`task_handler.py`） |
| 换模型 / 换量化版本 | `config_server.py` 的 `ModelPaths` + `core/server/model_download.py` 的 `PACKAGES`（压缩包名、SHA-256、大小），同步更新 `models/README.md` |
| 升级 llama.cpp | `engines/llama/bin/`（两平台同版本）+ 核对 `engines/llama/llama.py` 的 ctypes 结构体，见 `bin/README.md` |
| Linux 启动环境（解释器查找、库路径、asmgt 契约） | `start_server.sh`、`setup_linux.sh`、`requirements-linux.txt`、`docs/Linux部署.md` |
| 托盘菜单项 | `core/ui/tray.py`（`_TraySystem.__init__` 菜单构建、`on_restart` / `on_exit`）、`core/server/ui/tray_manager.py`（回调接线） |
| 退出 / 重启流程 | `core/server/app.py`（`stop` / `restart` / `_finish`）、`core/server/launch.py`（`current_command` / `spawn_new_instance`）、`worker/process_manager.py`（`stop`） |
