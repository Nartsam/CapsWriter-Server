# llama.cpp 运行库

`core/server/engines/llama/llama.py` 通过 ctypes 加载这里的库做 LLM 解码。两个平台的库都随仓库跟踪，版本均为 **llama.cpp b7798（Vulkan 版）**：

| 平台 | 文件 | 官方发布包 |
|---|---|---|
| Windows | `*.dll`（`libomp140.x86_64.dll` 是 CPU 后端依赖的 OpenMP 运行库） | `llama-b7798-bin-win-vulkan-x64.zip` |
| Linux | `lib*.so*` | `llama-b7798-bin-ubuntu-vulkan-x64.tar.gz` |

- 加载入口只认 `ggml` / `ggml-base` / `llama` 三个库；`ggml_backend_load_all()` 会在本目录按文件名扫描并加载 `ggml-cpu-*`（按 CPU 指令集自动挑最优）、`ggml-vulkan`、`ggml-rpc` 后端。未用到的 `mtmd`（多模态）与 `llama-quantize` 已删除。
- Linux 的 `libggml.so`、`libggml.so.0` 等别名是 **git 符号链接**，与官方压缩包的布局一致；在 Windows 上检出时会变成只含目标文件名的小文本文件，属正常现象（Windows 不用它们）。
- `libvulkan.so.1` 来自 Ubuntu 22.04 的 `libvulkan1` 包（1.3.204），供系统没装 Vulkan 加载器的服务器使用；这些 `.so` 自带 `RUNPATH=$ORIGIN`，会优先加载同目录的库。显卡的 Vulkan 驱动（ICD）仍由系统的 NVIDIA 驱动提供。

升级 llama.cpp：从 <https://github.com/ggml-org/llama.cpp/releases> 下载同一版本号的 Windows/Linux Vulkan 发行包，替换对应文件（Linux 别名保持符号链接）。`llama.py` 里的 ctypes 结构体（`llama_model_params` 等）必须与新版本头文件一致，升级后务必用 `tools/smoke_test.py` 验证。
