# coding: utf-8
from .base import BaseASREngine, BaseAlignEngine
from config_server import Qwen3ASRGGUFArgs, ForceAlignerGGUFArgs


def _public_attrs(args_cls) -> dict:
    """把 config_server 里的 *Args 类转成引擎配置 dataclass 的关键字参数"""
    return {k: v for k, v in args_cls.__dict__.items() if not k.startswith('_')}


class EngineFactory:
    """
    引擎工厂类

    统一管理识别引擎与对齐引擎的实例化逻辑。
    使用延迟导入，避免未用到的推理库拖慢启动。
    """

    @staticmethod
    def create_asr_engine() -> BaseASREngine:
        """创建 ASR 核心引擎（Qwen3-ASR）"""
        from .qwen_asr_gguf.asr_engine import QwenASREngine, ASREngineConfig
        return QwenASREngine(ASREngineConfig(**_public_attrs(Qwen3ASRGGUFArgs)))

    @staticmethod
    def create_align_engine() -> BaseAlignEngine:
        """创建对齐引擎 (Qwen3-ForcedAligner)，失败时降级为无对齐"""
        try:
            from .force_aligner_gguf.align_engine import QwenForceAligner, AlignerConfig
            return QwenForceAligner(AlignerConfig(**_public_attrs(ForceAlignerGGUFArgs)))
        except Exception as e:
            from . import logger
            logger.warning(f"⚠️ [警告] 对齐模型加载失败 (原因: {e})，系统将以【无精确时间戳模式】继续运行...")
            return BaseAlignEngine(None)
