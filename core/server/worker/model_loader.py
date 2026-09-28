# coding: utf-8
"""
模型加载模块

负责 ASR 引擎的实例化，并按引擎能力挂载辅助插件（时间戳对齐器）。
"""

import time
from config_server import ServerConfig as Config
from ..engines.factory import EngineFactory
from ..engines.base import EngineCapabilities
from . import logger


class ModelLoader:
    """
    模型加载器

    负责 ASR 引擎和辅助模型（对齐器）的生命周期管理。
    自动根据引擎能力挂载补丁插件。
    """
    def __init__(self):
        self.recognizer = None
        self.aligner = None

    def load(self):
        """
        加载模型资源

        逻辑流程：
        1. 加载 ASR 核心引擎（Qwen3-ASR，自带标点）
        2. 如果引擎不自带时间戳能力，挂载对齐器代理（按需加载，闲置卸载）
        """
        t1 = time.time()
        logger.info("Loader 开始初始化语音系统 (引擎: qwen_asr)")

        try:
            self.recognizer = EngineFactory.create_asr_engine()
            caps = self.recognizer.capabilities
            logger.info(f"引擎加载成功，能力清单: {[c.name for c in caps]}")

            if EngineCapabilities.TIMESTAMPS not in caps:
                self._load_align_model()

            logger.info(f"全系统初始化完成，耗时: {time.time() - t1:.2f}s")

        except Exception as e:
            logger.error(f"Loader 加载失败: {str(e)}", exc_info=True)
            raise e

    def _load_align_model(self):
        """加载时间戳对齐补丁代理 (ManagedAlignerProxy)"""
        from ..engines.manager import ManagedAlignerProxy
        logger.info(f"引擎不具备时间戳能力，已挂载 Aligner 托管代理 (闲置卸载时间: {Config.aligner_idle_timeout}s)")
        # 挂载代理而非实体模型，实现按需加载与自动释放
        self.aligner = ManagedAlignerProxy(timeout_sec=Config.aligner_idle_timeout)

    def cleanup(self):
        """释放模型资源"""
        if self.recognizer and hasattr(self.recognizer, 'cleanup'):
            self.recognizer.cleanup()
        if self.aligner and hasattr(self.aligner, 'cleanup'):
            self.aligner.cleanup()
        self.recognizer = None
        self.aligner = None
