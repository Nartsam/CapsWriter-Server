import signal
import sys
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
    signal.signal(signal.SIGINT, SignalHandler(callback))

    # 服务管理器（systemd / asmgt 等）用 SIGTERM 停止服务，终端断开时是 SIGHUP：立即退出
    def _exit_immediately(signum, frame):
        callback()

    signal.signal(signal.SIGTERM, _exit_immediately)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, _exit_immediately)
