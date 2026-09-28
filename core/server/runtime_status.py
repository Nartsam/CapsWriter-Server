# coding: utf-8
"""
运行状态文件：logs/server_status.json

给外部的服务管理工具（Linux 上的 asmgt 等）判断服务忙闲用：
queued + processing > 0 即为 busy，此时不宜重启。

主进程（ws_recv 收到音频分段时 +queued）与识别子进程（开始/完成/丢弃任务时）
都会改写这个文件，所以读-改-写全程持有跨进程文件锁（Linux: fcntl，Windows: msvcrt）。
写入失败只会让状态不准，不影响识别，因此所有 OSError 都被吞掉。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = BASE_DIR / "logs"
STATUS_FILE = LOG_DIR / "server_status.json"
LOCK_FILE = LOG_DIR / "server_status.lock"


def _empty_status() -> dict:
    now = time.time()
    return {
        "state": "idle",
        "queued": 0,
        "processing": 0,
        "updated_at": now,
        "started_at": now,
        "pid": os.getpid(),
    }


@contextmanager
def _locked_status():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        if os.name == 'nt':
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)   # 阻塞重试，最多约 10 秒
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _read_status() -> dict:
    if not STATUS_FILE.exists():
        return _empty_status()
    try:
        with STATUS_FILE.open("r", encoding="utf-8") as f:
            status = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty_status()

    base = _empty_status()
    base.update(status)
    base["queued"] = max(0, int(base.get("queued", 0)))
    base["processing"] = max(0, int(base.get("processing", 0)))
    return base


def _write_status(status: dict) -> None:
    status["state"] = "busy" if status.get("queued", 0) + status.get("processing", 0) > 0 else "idle"
    status["updated_at"] = time.time()
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    tmp.replace(STATUS_FILE)


def _mutate(mutator: Callable[[dict], None]) -> None:
    try:
        with _locked_status():
            status = _read_status()
            mutator(status)
            _write_status(status)
    except OSError:
        pass


def initialize() -> None:
    """启动时重置状态（主进程调用）"""
    def reset(status: dict) -> None:
        status.clear()
        status.update(_empty_status())

    _mutate(reset)


def task_queued(count: int = 1) -> None:
    def inc(status: dict) -> None:
        status["queued"] = max(0, int(status.get("queued", 0))) + count

    _mutate(inc)


def task_started() -> None:
    def move(status: dict) -> None:
        status["queued"] = max(0, int(status.get("queued", 0)) - 1)
        status["processing"] = max(0, int(status.get("processing", 0))) + 1

    _mutate(move)


def task_finished(count: int = 1) -> None:
    def dec(status: dict) -> None:
        status["processing"] = max(0, int(status.get("processing", 0)) - count)

    _mutate(dec)


def task_abandoned(count: int = 1) -> None:
    def dec(status: dict) -> None:
        status["queued"] = max(0, int(status.get("queued", 0)) - count)

    _mutate(dec)
