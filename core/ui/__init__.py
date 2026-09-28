"""UI 工具模块

只剩系统托盘（tray.py，仅 Windows 生效，其他平台自动跳过）。
日志记录器通过注入方式加载。
"""
import logging

# ============================================================
# Logger 代理机制
# ============================================================

class _LoggerProxy:
    """
    日志代理类（利用 __getattr__ 动态转发）
    允许先导入 logger 对象，稍后再注入真正的实现。
    """
    def __init__(self):
        self._target = logging.getLogger('core.ui')  # 默认 logger

    def set_target(self, logger):
        """注入真正的 logger 实现"""
        self._target = logger

    def __getattr__(self, name):
        """将所有属性访问转发给真正的 logger"""
        return getattr(self._target, name)

# 1. 创建代理实例
logger = _LoggerProxy()

def set_ui_logger(real_logger):
    """设置 UI 模块使用的日志记录器"""
    logger.set_target(real_logger)

# ============================================================
# 导出组件
# ============================================================

from .tray import enable_min_to_tray, stop_tray

__all__ = [
    'logger',
    'set_ui_logger',
    'enable_min_to_tray',
    'stop_tray',
]
