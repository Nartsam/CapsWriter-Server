# coding: utf-8
"""
识别子进程工作包 (Worker Package)

包含模型加载、任务处理和 Worker 门面类。
"""

from multiprocessing import Queue
from multiprocessing.managers import ListProxy
from .. import logger
from .worker import RecognizerWorker

def start_worker(queue_in: Queue, queue_out: Queue, sockets_id: ListProxy, stdin_fn: int):
    """识别子进程启动入口"""
    # Windows 下子进程是 spawn 出来的，config_server 被重新 import，
    # 需要按继承来的环境变量再覆盖一次配置（见 core/server/launch.py）
    from ..launch import apply_overrides
    apply_overrides()

    worker = RecognizerWorker(queue_in, queue_out, sockets_id, stdin_fn)
    worker.run()

__all__ = ['RecognizerWorker', 'start_worker']
