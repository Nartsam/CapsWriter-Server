# 开发笔记

> 跨会话的待验证事项、重要决策与已知怪状态，供后续开发（人或 AI Agent）快速接续上下文。
> 每条一两行并署日期；架构与模块说明写在 [PROJECT_INDEX.md](../PROJECT_INDEX.md)，不要写在这里。

## 待验证

- [ ] **真实 Linux 服务器部署**（2026-09-28）：新入口只在 WSL Ubuntu 22.04 上验证过（结果见“已验证”）。WSL 没有 Vulkan 设备，llama.cpp 走的是 CPU，所以 **Vulkan GPU 解码** 与 **在 asmgt 里实际托管**（契约行为已在 WSL 用等价方式测过）还需要在服务器上按 [LINUX.md](LINUX.md) 的“从旧部署迁移”实测一遍。
- [ ] **双击 exe 缺模型时的下载询问**（2026-09-28）：交互询问的代码路径已在 Linux 伪终端里验证（回答 n 正常退出），Windows 控制台里回答 Y 直接下载的完整流程还没实际走过。

## 已验证

- [x] Windows `start_server.exe` 跑新代码（2026-09-28，GTX 1080 Ti）：麦克风任务结果与改造前基线一致；文件任务的对齐模型首次真正加载成功（时间戳 0.32~16.32s，改造前是均分估算的 0~16.19s）；`--help`、非法端口（退出码 1）、未知参数（退出码 2）、端口冲突（0.9s 内退出，不再加载完模型才报错）、缺必需模型（退出码 1 并提示下载命令）、只缺对齐模型（照常启动，文件任务降级）、`--download-models`（已就绪时直接退出）、强杀主进程后识别子进程与 Manager 进程都在 5 秒内自行退出；`--download-models` 真实下载 Qwen3-ASR 包（冻结运行时的 HTTPS、杀进程后断点续传、读超时后自动重试均实际触发过；下载到约 10% 时手动停止，完整下载+校验+解压流程在 Linux 上用对齐模型包走通）。
- [x] 剔除 57MB 客户端依赖后的 `internal/`（2026-09-28）：托盘、麦克风任务、文件任务（含对齐）均正常。
- [x] Linux 入口（2026-09-28，WSL Ubuntu 22.04 + GTX 1080 Ti，llama.cpp 为 CPU）：全新 `git clone`（符号链接、可执行位、LF 均正确）→ `setup_linux.sh`（venv）→ `--download-models` 真实下载对齐模型（途中两次读超时，断点续传成功，SHA-256 通过）→ `start_server.sh --port --gpu` 启动，ONNX 走 CUDA，麦克风/多段拼接/文件对齐三种冒烟测试结果与 Windows 一致；端口冲突 0.5s 内退出码 1；交互终端缺模型时询问下载（回答 n 退出码 1）；`ASMGT_PORT`/`ASMGT_GPU` 生效；对整个进程组发 SIGTERM 干净退出（退出码 0、无残留）；SIGKILL 主进程后子进程与 Manager 5 秒内退出。

## 决策记录

- **2026-09-28 拆分为纯服务端**：删除客户端代码（`core/client/`、`config_client.py`、`LLM/`、`hot*.txt`、Toast 等 UI）。刻意保留的共享部分：`core/protocol.py`、`core/constants.py`（与 Client 仓库逐字相同）、`core/ui/tray.py`（服务端托盘在用）。
- **2026-09-28 只保留 Qwen3-ASR**：删除 Fun-ASR-Nano / SenseVoice / Paraformer / CT-Transformer 标点引擎及其模型与导出脚本。服务端热词（`hot-server.txt`）只对被删的引擎生效，一并删除；将来若想给 Qwen 做热词，思路是把热词拼进 `context`（Qwen3 的 system prompt）。
- **2026-09-28 保留 ForcedAligner 并修好路径**：此前本地文件少解压了一层 `Qwen3-ForcedAligner-0.6B/` 目录，对齐模型从未加载成功（静默降级为均分时间戳）。它和 Qwen3-ASR 各带一份几乎相同的推理内核，已合并为 `qwen_asr_gguf/inference` 一份，唯一差异（对齐器 `n_batch = n_ctx`，否则 3072 token 的对齐输入超出 2048 的 batch）已保留。
- **2026-09-28 模型不进 git**：单个 gguf 1.47GB，只能走 Git LFS，GitHub 免费 LFS 额度有限，因此改为从上游 Release 下载（`--download-models`，SHA-256 校验）。本地文件与上游 `Qwen3-ASR-1.7B-q5_k.zip`、`Qwen3-ForcedAligner-0.6B.zip` 逐文件 CRC32 一致。风险：上游若删除/重传这些压缩包，需要换 `ModelDownload.base_url`（例如把压缩包传到自己仓库的 Release）并更新清单里的 SHA-256。
- **2026-09-28 两个入口一份代码**：exe 无法重建，它只会执行 `CapsWriterServer().start()`，所以参数解析（`launch.py`）、端口/模型检查、状态文件等都放在这条调用链里；`start_server.sh` 只做 Linux 专属的环境准备（解释器、`LD_LIBRARY_PATH`、asmgt 变量映射）。旧的 `run_server_linux.py` 在主进程里改写配置类属性，只在 Linux fork 模式下对子进程生效，改为环境变量 + 子进程再覆盖一次，两种进程启动方式都成立。
- **2026-09-28 internal/ 精简**：用真实运行（主进程+托盘 / Manager / 识别子进程+对齐）的 `sys.modules` 追踪确定用到的包，删除 57MB 从未导入的客户端依赖（清单见提交 95469b6）。`requests`、`tqdm`、`yaml` 是 gguf 声明的依赖，虽未导入仍保留；`_tcl_data` / `_tk_data` 被 PyInstaller 运行时钩子检查，不能删。
- **2026-09-28 Linux 运行库**：llama.cpp 用与 Windows 相同的 b7798 Vulkan 版；`.so` 别名按官方包存为 git 符号链接；`libvulkan.so.1` 随仓库提供。ONNX 的 CUDA 12 / cuDNN 9 默认以固定版本的 `nvidia-*` pip 包装进环境，不再依赖服务器上手动放的 `cudnn-local`（仍兼容）。
- **2026-09-28 asmgt**：只移植了符合 LAUNCHER-CONTRACT v1 的启动脚本；旧的 tmux 服务脚本、定时重启 cron 按其注释所说已被 asmgt 取代，未移植。
- **2026-09-28 子进程信号**：Linux fork 出的识别子进程和 Manager 进程会继承主进程的 SIGTERM/SIGHUP 处理函数（调用主进程 app 副本的 `stop()`，报 `can only join a child process` 且不退出），现在子进程一律恢复默认处理；Manager 进程另起守护线程，主进程消失即退出（见 `core/tools/signal_handler.py`）。
- **2026-09-28 cuDNN 固定 9.1.0**：pip 默认装最新 9.26，实测在 Pascal（GTX 1080 Ti）上让 ORT CUDA 后端创建会话时直接段错误；换回旧服务器环境里的 9.1.0 即正常。Blackwell 显卡需要 cuDNN ≥ 9.7，换卡时要重新评估这个固定版本。

## 已知状态 / 怪现象

- **识别结果有随机性**：Qwen3 解码温度 0.4（采样），同一段音频多次识别可能在标点或个别字上不同，测试请用 `--expect` 检查稳定的子串，不要比对全文。
- **对齐模型走 CPU**：`ForceAlignerGGUFArgs.llm_use_gpu = False`（上游默认），16 秒音频的文件任务在 Windows 上约 7~9s（其中首次加载对齐模型占大头），麦克风任务不受影响。需要更快可以试 `True`。
- **默认日志级别 WARNING**：llama.cpp 的 Vulkan 设备、ONNX 实际后端等信息是 INFO，排查性能问题时先把 `log_level` 改为 `'INFO'`。
- **同目录多实例共享日志与状态文件**：在另一个端口起测试实例时，`logs/server_latest.log` 与 `logs/server_status.json` 会与正在使用的实例混写。
- **推理内核里未用到的入口**：`inference/asr.py` 的 `transcribe()` / `asr()` 是上游的整文件转录接口，服务端不走这条路，其依赖 `soundfile` 已不在运行时中。
- Windows 下把 exe 输出重定向到管道时是 GBK 编码（控制台代码页），日志文件是 UTF-8。

## 待办 / 可选改进

- 给 Qwen3-ASR 做服务端热词（拼进 context），取代已删除的 `hot-server.txt`。
- 视需要把对齐模型的 `llm_use_gpu` 改为 `True` 并验证精度。
