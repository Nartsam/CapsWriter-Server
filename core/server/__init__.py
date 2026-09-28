# coding: utf-8
"""
服务端模块

提供 CapsWriter 服务端的所有功能模块。

模块架构：
- app: CapsWriterServer 门面类，唯一入口 start()
- launch: 命令行参数 / 环境变量覆盖配置
- state / schema: 运行状态容器与数据类（Task, Result）
- connection/: WebSocket 收发（ws_recv, ws_send）
- worker/: 识别子进程（模型检查与加载、任务调度、识别流水线）
- engines/: Qwen3-ASR 识别引擎、ForcedAligner 对齐引擎、llama.cpp 绑定
- formatter/ / merger/: 结果格式化与分段拼接
- model_download: 模型下载器
- runtime_status: 忙闲状态文件（logs/server_status.json）
"""

from core.server.state import console
from core.logger import get_logger, setup_logger
from config_server import ServerConfig as Config, __version__

setup_logger('server', level=Config.log_level)
logger = get_logger('server')

from core.server.schema import Task, Result

__all__ = [
    'console',
    'logger',
    'Task',
    'Result',
]
