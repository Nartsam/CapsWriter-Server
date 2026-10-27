# coding=utf-8
import os
import numpy as np
from typing import Optional, List
from .inference.asr import QwenASREngine as QwenInternalEngine
from .inference.schema import ASREngineConfig, MsgType, StreamingMessage
from ..base import BaseASREngine, RecognitionStream, EngineCapabilities, RecognitionResult
from ..language import get_language, ENGINE_QWEN_ASR


class QwenASRStream(RecognitionStream):
    """Qwen-ASR 识别流结构"""
    def __init__(self, sample_rate=16000):
        super().__init__(sample_rate)
        self.audio_data = None

    def accept_waveform(self, sample_rate, audio):
        self.sample_rate = sample_rate
        self.audio_data = audio.astype(np.float32)


class QwenASREngine(BaseASREngine):
    """Qwen-ASR 推理引擎适配器，实现 BaseASREngine 接口"""

    def __init__(self, config: ASREngineConfig):
        super().__init__(config)
        self.engine = QwenInternalEngine(config)

    @property
    def capabilities(self) -> List[EngineCapabilities]:
        """声明具备的能力"""
        return [
            EngineCapabilities.ASR, 
            EngineCapabilities.PUNC
        ]

    def create_stream(self, hotwords: Optional[str] = None) -> QwenASRStream:
        """创建识别流"""
        return QwenASRStream()

    def decode_stream(
        self, 
        stream: QwenASRStream, 
        context: Optional[str] = None,
        language: Optional[str] = None,
        temperature: float = 0.4,
        prefix_text: str = '',
        **kwargs
    ):
        """
        解码识别流

        prefix_text: 作为 assistant 已输出内容的前缀，模型从它之后接着写，结果只含续写部分。
            流式任务用它带上前几句的识别文字，此时音频也要包含这几句（见 worker/streaming.py）
        """
        if stream.audio_data is None:
            return

        sr = 16000
        audio_data = stream.audio_data
        
        # 如果长度超过了最大限制，则截断
        max_samples = int(self.config.chunk_size * sr)
        if len(audio_data) > max_samples:
            audio_data = audio_data[:max_samples]

        # 1. 提交编码任务（同步调用）
        audio_embd, enc_time = self.engine.encoder.encode(audio_data)
        
        # 3. 构造 Prompt 并解码（语言映射：统一代码 → Qwen3 英文明称）
        mapped_lang = get_language(ENGINE_QWEN_ASR, language) if language else None
        full_embd = self.engine._build_prompt_embd(
            audio_embd=audio_embd,
            prefix_text=prefix_text,
            context=context,
            language=mapped_lang
        )

        # 4. 执行解码
        res = self.engine._safe_decode(
            full_embd, 
            prefix_text=prefix_text, 
            rollback_num=5, 
            is_last_chunk=True, 
            temperature=temperature,
            streaming=False,
            detect_language=not mapped_lang,
        )
        # 重试已耗尽时不能把熔断的半截文本当作成功终局交给客户端。
        if res.is_aborted:
            raise RuntimeError('语音解码重试后仍触发熔断')

        # 5. 更新结果
        stream.result.text = res.text
        # Qwen 纯 ASR 模式下暂不支持 token 级时间戳，由 server_recognize 自动补齐

    def update_hotwords(self, hotwords: List[str]):
        """Qwen 暂不支持热词动态更新"""
        pass

    def cleanup(self):
        """释放资源"""
        self.engine.shutdown()

