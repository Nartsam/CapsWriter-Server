# 模型目录

模型文件体积大（约 2.8GB），**不进 git**（本目录除这份说明外都被 `.gitignore` 忽略）。新机器 clone 后先运行项目根目录的下载脚本：

```bash
download_models.bat      # Windows：双击即可，等同于 start_server.exe --download-models
./download_models.sh     # Linux：先 ./setup_linux.sh，等同于 ./start_server.sh --download-models
```

直接启动服务端时如果缺少必需模型，在交互终端里会询问是否立即下载；被服务管理器托管（无终端）时会打印上面的命令并退出。

## 下载来源与校验

- 来源：上游 CapsWriter-Offline 的模型发布页 <https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models>
- 下载器：[`core/server/model_download.py`](../core/server/model_download.py)，清单里登记了每个压缩包的 SHA-256，下载后校验，支持断点续传与重试
- 访问 GitHub 困难时：设置 `HTTPS_PROXY` 环境变量走代理，或把 `config_server.py` 中的 `ModelDownload.base_url` 换成镜像地址；也可以手动下载压缩包放进 `models/.downloads/` 再运行下载命令（校验通过即直接解压）

| 压缩包 | 大小 | 用途 | 缺失时 |
|---|---|---|---|
| `Qwen3-ASR-1.7B-q5_k.zip` | 1.9GB | 语音识别（必需） | 服务端无法启动 |
| `Qwen3-ForcedAligner-0.6B.zip` | 0.6GB | 文件转录的字级时间戳（可选） | 照常启动，文件转录的时间戳退化为按字数均分 |

## 目录结构

与上游压缩包解压后的结构一致，路径配置在 `config_server.py` 的 `ModelPaths`：

```
models/
├── README.md
├── Qwen3-ASR/
│   └── Qwen3-ASR-1.7B/
│       ├── qwen3_asr_encoder_frontend.onnx     # 音频编码器前端（ONNX Runtime）
│       ├── qwen3_asr_encoder_backend.onnx      # 音频编码器后端（ONNX Runtime）
│       └── qwen3_asr_llm.gguf                  # LLM 解码器，q5_k 量化（llama.cpp）
└── Qwen3-ForcedAligner/
    └── Qwen3-ForcedAligner-0.6B/
        ├── qwen3_aligner_encoder_frontend.int4.onnx
        ├── qwen3_aligner_encoder_backend.int4.onnx
        └── qwen3_aligner_llm.q5_k.gguf
```

上游另有更小的 `Qwen3-ASR-1.7B-q4_k.zip`（适合性能较弱的机器）。要换用它，需要同时修改 `model_download.py` 清单里的压缩包名/SHA-256/大小，以及 `ModelPaths` 中的文件名（如有不同）。
