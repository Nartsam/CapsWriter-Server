# coding: utf-8
"""
工具模块

提供各种通用工具函数和类。

模块架构：
- chinese_itn: 中文数字转阿拉伯数字
- empty_working_set: Windows 内存管理
- format_tools: 文本格式化（中英文空格调整）
- my_status: Rich Status 扩展
- daemon_executor: 守护线程执行器（asyncio 默认执行器）
- signal_handler: 退出信号处理
- token_sync: 格式化后把标点同步回 token 序列
"""

from core.tools.empty_working_set import empty_working_set, empty_current_working_set
from core.tools.format_tools import adjust_space
from core.tools.my_status import Status

__all__ = [
    'empty_working_set',
    'empty_current_working_set',
    'adjust_space',
    'Status',
]
