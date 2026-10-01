import time
from .factory import EngineFactory
from .base import BaseAlignEngine
from . import logger


def _encoder_on_dml(align_engine) -> bool:
    """对齐引擎的音频编码器是否实际跑在 DirectML 上（请求 DML 但回退了 CPU 时为 False）"""
    encoder = getattr(getattr(align_engine, 'engine', None), 'encoder', None)
    return bool(getattr(encoder, 'active_dml', False))


class ManagedAlignerProxy(BaseAlignEngine):
    """
    对齐引擎托管代理
    
    实现“懒加载”与“闲置卸载”逻辑。
    由于 TaskHandler 是单线程同步的，此处无需线程锁。
    """

    def __init__(self, timeout_sec=600):
        self.engine = None
        self.timeout = timeout_sec
        self.last_active = time.time()
        self.is_processing = False

    def align(self, audio, text, **kwargs):
        # 1. 懒加载
        if self.engine is None:
            logger.info("🚩 [AlignerProxy] 检测到文件任务需求，正在即时加载对齐引擎...")
            self.engine = EngineFactory.create_align_engine()
            # 对齐引擎的编码器跑在 DirectML 上时不做闲置卸载：卸载会销毁它的 DML 会话，
            # 此后识别引擎（同样在 DML 上）的下一次编码会在 onnxruntime 里访问冲突，识别子进程崩溃。
            # 2026-10 在 Windows（GTX 1080 Ti，onnxruntime-directml）上稳定复现：文件转录 → 等卸载 → 任意麦克风请求
            if self.timeout > 0 and _encoder_on_dml(self.engine):
                logger.info("🚩 [AlignerProxy] 对齐引擎的编码器在 DirectML 上，常驻不卸载（卸载会让识别子进程崩溃）")
                self.timeout = 0

        # 2. 标记运行并执行
        self.is_processing = True
        try:
            res = self.engine.align(audio, text, **kwargs)
            self.last_active = time.time()
            return res
        finally:
            self.is_processing = False

    def check_idle(self):
        """ 闲置检查：由外部循环在空闲时调用 """
        if self.timeout <= 0 or self.is_processing or self.engine is None:
            return

        idle_time = time.time() - self.last_active
        if idle_time > self.timeout:
            logger.info(f"🚩 [AlignerProxy] 对齐引擎已闲置 {idle_time:.0f}s，正在自动卸载以释放显存...")
            self.engine.cleanup()
            self.engine = None

    def cleanup(self):
        if self.engine:
            self.engine.cleanup()
            self.engine = None
