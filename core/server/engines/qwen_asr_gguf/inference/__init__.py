# coding=utf-8
"""
Qwen3-ASR / Qwen3-ForcedAligner 推理内核

源自上游 Qwen3-ASR-GGUF 项目（已裁剪为服务端用到的部分）：
ONNX Runtime 跑音频编码器，llama.cpp（core/server/engines/llama）跑 LLM 解码。
识别（asr.py）与强制对齐（aligner.py）共用这一份内核。
"""
from .. import logger
from ...llama import llama

from .asr import QwenASREngine
from .aligner import QwenForcedAligner
from .schema import ForcedAlignItem, ForcedAlignResult, DecodeResult, AlignerConfig, ASREngineConfig, TranscribeResult
