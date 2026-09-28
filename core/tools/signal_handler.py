import os
import signal
import sys
import threading
import time
import asyncio


class SignalHandler:
    """Ctrl+C：1 秒内连按两次才退出，防止误触"""
    def __init__(self, callback):
        self.last_time = time.time()
        self.callback = callback

    def __call__(self, signum, frame):
        now = time.time()
        if now - self.last_time > 1.0:
            self.last_time = now
            print(f"\n收到 {signal.Signals(signum).name}，1秒内再次按下将会退出...")
        else:
            print(f"\n收到 {signal.Signals(signum).name}，确认退出...\n")
            self.last_time = 0
            self.callback()


def register_signal(callback):
    """主进程注册退出信号"""
    signal.signal(signal.SIGINT, SignalHandler(callback))

    # 服务管理器（systemd / asmgt 等）用 SIGTERM 停止服务，终端断开时是 SIGHUP：立即退出
    def _exit_immediately(signum, frame):
        callback()

    signal.signal(signal.SIGTERM, _exit_immediately)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, _exit_immediately)


def reset_child_signals():
    """
    子进程调用：把 SIGTERM / SIGHUP 恢复为默认处理（直接终止）。

    Linux 以 fork 创建子进程时会继承主进程用 register_signal 注册的处理函数，
    其中引用的是主进程 app 对象的副本；服务管理器常对整个进程组发 SIGTERM，
    子进程若执行这个不属于它的 stop() 会抛异常而不退出。Windows 的 spawn 子进程不继承，调用也无害。
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal.SIG_DFL)


def exit_when_parent_dies(interval: float = 1.0):
    """启动守护线程：父进程消失（例如被强杀）后立即结束本进程，避免残留"""
    import multiprocessing
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def _watch():
        while parent.is_alive():
            time.sleep(interval)
        os._exit(0)

    threading.Thread(target=_watch, name='parent-watch', daemon=True).start()


def init_manager_process():
    """multiprocessing.Manager 服务进程的初始化函数（需为模块级函数，spawn 时要能被 pickle）"""
    reset_child_signals()
    exit_when_parent_dies()
