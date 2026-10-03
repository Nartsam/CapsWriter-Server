"""
CapsWriter-Server 配置文件（Windows 与 Linux 共用这一份）

平台差异只有一处：ONNX 推理后端的默认值（见 _DEFAULT_ONNX_PROVIDER）。
监听地址/端口/GPU 等还可以在启动时用命令行参数或环境变量临时覆盖，
不必改这个文件，见 core/server/launch.py。
"""
import os
import sys
from pathlib import Path

# 版本信息（基于上游 CapsWriter-Offline v2.5）
__version__ = '2.5'

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ONNX 推理后端（音频编码器部分）：
# - Windows：internal/ 里打包的是 onnxruntime-directml，只有 DML / CPU 可选。
#   上游默认 CPU（部分 AMD 显卡开 DML 反而更慢）；本仓库默认 DML，在 GTX 1080 Ti 上实测快 1.5~3 倍，
#   换到其他显卡上若变慢，改回 'CPU'（数据见 docs/显卡加速.md）
# - Linux：用 onnxruntime-gpu，走 CUDA
# 请求的后端不可用时自动回退 CPU，并在日志里给出警告
_DEFAULT_ONNX_PROVIDER = 'DML' if sys.platform == 'win32' else 'CUDA'


# 服务端配置
class ServerConfig:
    addr = '0.0.0.0'
    port = '6000'

    format_num = True       # 输出时是否将中文数字转为阿拉伯数字
    format_spell = True     # 输出时是否调整中英之间的空格

    enable_tray = True      # 是否启用托盘图标（仅 Windows 有控制台窗口时生效，Linux 自动跳过）

    # 日志配置
    log_level = 'WARNING'        # 日志级别：'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
    aligner_idle_timeout = 10  # 对齐引擎空闲多少秒后自动释放显存 (0 表示不释放；编码器走 DirectML 时一律常驻，见 core/server/engines/manager.py)


class ModelPaths:
    """模型文件路径配置（目录结构与上游模型压缩包解压后一致）"""

    # 基础目录
    model_dir = Path() / 'models'

    # Qwen3-ASR 模型路径，自带标点
    qwen3_asr_gguf_dir = model_dir / 'Qwen3-ASR' / 'Qwen3-ASR-1.7B'
    qwen3_asr_gguf_encoder_frontend = qwen3_asr_gguf_dir / 'qwen3_asr_encoder_frontend.onnx'
    qwen3_asr_gguf_encoder_backend = qwen3_asr_gguf_dir / 'qwen3_asr_encoder_backend.onnx'
    qwen3_asr_gguf_llm_decode = qwen3_asr_gguf_dir / 'qwen3_asr_llm.gguf'

    # Force-Aligner 模型路径（仅文件转录时用于生成精确的字级时间戳，可缺省）
    force_aligner_gguf_dir = model_dir / 'Qwen3-ForcedAligner' / 'Qwen3-ForcedAligner-0.6B'
    force_aligner_gguf_encoder_frontend = force_aligner_gguf_dir / 'qwen3_aligner_encoder_frontend.int4.onnx'
    force_aligner_gguf_encoder_backend = force_aligner_gguf_dir / 'qwen3_aligner_encoder_backend.int4.onnx'
    force_aligner_gguf_llm_decode = force_aligner_gguf_dir / 'qwen3_aligner_llm.q5_k.gguf'


class ModelDownload:
    """模型下载源（`--download-models` 与启动时缺模型的提示使用）"""

    # 上游 CapsWriter-Offline 的模型发布页，下载器会在此地址后拼接压缩包文件名。
    # 访问 GitHub 困难时可换成镜像/自建地址（需保持相同的文件名），
    # 也可以设置 HTTPS_PROXY 环境变量走代理。压缩包清单与校验值见 core/server/model_download.py
    base_url = 'https://github.com/HaujetZhao/CapsWriter-Offline/releases/download/models/'


class Qwen3ASRGGUFArgs:
    """Qwen3-ASR-GGUF 模型参数配置"""

    # 模型路径
    model_dir = ModelPaths.qwen3_asr_gguf_dir.as_posix()
    encoder_frontend_fn = ModelPaths.qwen3_asr_gguf_encoder_frontend.name
    encoder_backend_fn = ModelPaths.qwen3_asr_gguf_encoder_backend.name
    llm_fn = ModelPaths.qwen3_asr_gguf_llm_decode.name

    # 显卡加速
    onnx_provider = _DEFAULT_ONNX_PROVIDER  # ONNX 推理后端 (CPU, DML, CUDA)
    llm_use_gpu = True          # 是否启用 GPU 加速 GGUF 模型（llama.cpp Vulkan 后端）

    # 模型细节
    n_ctx = 2048                # 上下文窗口大小
    chunk_size = 80.0           # 分段长度（秒）
    memory_num = 1              # 记忆段数
    dml_pad_to = 30             # 开启 DirectML 加速时，短音频统一填充到指定长度，有加速效果
    verbose = False

    # 解码干预（原理与实测数据见 core/server/engines/qwen_asr_gguf/inference/biasing.py）
    comma_drop_margin = 4.0     # 少加逗号：逗号不比最可能的字明显强时（差距小于此值）不加标点，只删不换；越大逗号越少，建议 0~6，0 关闭
    hotword_file = 'hot-server.txt'  # 服务端热词：提高这些词被识别出来的概率（不是替换），保存后下次识别即生效；留空关闭
    hotword_bonus = 4.0         # 热词默认加分（文件里可逐词指定），超过 8 容易把相近的词也改成热词


class ForceAlignerGGUFArgs:
    """Force-Aligner-GGUF 模型参数配置"""

    # 模型路径
    model_dir = ModelPaths.force_aligner_gguf_dir.as_posix()
    encoder_frontend_fn = ModelPaths.force_aligner_gguf_encoder_frontend.name
    encoder_backend_fn = ModelPaths.force_aligner_gguf_encoder_backend.name
    llm_fn = ModelPaths.force_aligner_gguf_llm_decode.name

    # 显卡加速
    onnx_provider = _DEFAULT_ONNX_PROVIDER  # ONNX 推理后端 (CPU, DML, CUDA)
    llm_use_gpu = False         # 是否启用 GPU 加速 GGUF 模型

    # 对齐细节
    n_ctx = 3072                # 上下文窗口大小
    dml_pad_to = 30             # 开启 DirectML 加速时，短音频统一填充到指定长度，有加速效果
