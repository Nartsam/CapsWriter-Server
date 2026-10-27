# coding: utf-8
"""
识别任务处理器

负责监听任务队列、执行识别流水线并将结果返回主进程。

最新会话优先，同一连接、录音和识别阶段内保持 FIFO 顺序。
"""

from collections import OrderedDict, deque
from dataclasses import replace
import multiprocessing
from multiprocessing import Queue
from multiprocessing.managers import ListProxy
import queue
import time
from .pipeline import TaskPipeline
from ..state import WorkerState
from ..schema import DiscardTask, Result
from .. import runtime_status
from . import logger


def _parent_alive() -> bool:
    """主进程是否还活着（主进程被强杀时，子进程要自行退出，不能继续占着显存）"""
    parent = multiprocessing.parent_process()
    return parent is None or parent.is_alive()


class TaskBuffer:
    """按连接、录音、阶段分组缓冲，最新缓冲优先。"""
    def __init__(self, state: WorkerState):
        self.state = state
        self._buffers: OrderedDict[tuple, deque] = OrderedDict()

    def enqueue(self, task):
        """将片段放入对应阶段的缓冲尾部（同 session 内 FIFO）。"""
        tid = task.key
        if tid not in self._buffers:
            self._buffers[tid] = deque()
        self._buffers[tid].append(task)

    def pop(self):
        """取出最新 session 的下一个任务。没有待处理任务时返回 None。"""
        if not self._buffers:
            return None

        tid, buf = next(reversed(self._buffers.items()))
        task = buf.popleft()

        if not buf:
            del self._buffers[tid]

        return task

    def discard(self, key) -> int:
        return len(self._buffers.pop(key, ()))

    def cleanup_tasks(self, sockets_id) -> int:
        """清理已断开连接的 session 的缓冲任务，返回丢弃的任务数。"""
        removed = 0
        for tid in list(self._buffers):
            if tid[0] not in sockets_id:
                removed += len(self._buffers[tid])
                logger.debug(f"清理断开连接的 session: {tid}")
                del self._buffers[tid]
        return removed

    @property
    def is_empty(self) -> bool:
        return len(self._buffers) == 0


class TaskHandler:
    """
    任务处理器

    协调输入输出队列与识别引擎之间的任务流。
    保持最新会话优先和同阶段 FIFO。
    """
    def __init__(self, queue_in: Queue, queue_out: Queue, sockets_id: ListProxy, state: WorkerState):
        self.queue_in = queue_in
        self.queue_out = queue_out
        self.sockets_id = sockets_id
        self.state = state

        self.recognizer = None
        self.aligner = None
        self.pipeline = None

        self.buffer = TaskBuffer(state)
        self.discarded = set()

    def set_engine(self, recognizer, aligner=None):
        """注入识别引擎实例并初始化管线"""
        self.recognizer = recognizer
        self.aligner = aligner
        self.pipeline = TaskPipeline(recognizer, aligner, self.state)

    def drain_queue(self) -> bool:
        """Drain 队列中所有任务到缓冲区。Returns: False = 退出信号。"""
        while True:
            # 获取任务
            try:
                if self.buffer.is_empty:
                    task = self.queue_in.get(timeout=1)
                else:
                    task = self.queue_in.get(timeout=0.02)
            except queue.Empty:
                if self.buffer.is_empty:
                    if not _parent_alive():
                        logger.warning("主进程已退出，识别子进程随之退出")
                        return False
                    self.cleanup_engines()
                    self.cleanup()
                    continue
                else:
                    return True
            except InterruptedError:
                continue
            
            # 判断退出信号
            if task is None:
                return False

            self.accept(task)

    def discard(self, socket_id, task_id, mode=''):
        for stage in (mode,) if mode else ('stream', 'full'):
            key = socket_id, task_id, stage
            self.discarded.add(key)
            self.state.sessions.pop(key, None)
            removed = self.buffer.discard(key)
            if removed:
                runtime_status.task_abandoned(removed)

    def accept(self, task):
        """按输入队列顺序应用控制，控制自身不参与片段计数。"""
        if isinstance(task, DiscardTask):
            self.discard(task.socket_id, task.task_id, task.recognition_mode)
        elif task.socket_id not in self.sockets_id or task.key in self.discarded:
            runtime_status.task_abandoned()
        else:
            self.buffer.enqueue(task)

    def cleanup(self):
        """清理断连 socket 的缓冲任务和 session。"""
        self.state.cleanup_sessions(self.sockets_id)
        removed = self.buffer.cleanup_tasks(self.sockets_id)
        if removed:
            runtime_status.task_abandoned(removed)
        self.discarded = {key for key in self.discarded if key[0] in self.sockets_id}

    def cleanup_engines(self):
        """时间戳引擎空闲时自动卸载。"""
        if self.pipeline and self.pipeline.aligner:
            self.pipeline.aligner.check_idle()

    def process_next(self):
        self.cleanup()
        task = self.buffer.pop()
        if task is None:
            return
        runtime_status.task_started()
        try:
            try:
                result = self.pipeline.process(task)
            except Exception as error:
                logger.error('任务执行出错: %s', error, exc_info=True)
                self.discard(task.socket_id, task.task_id, task.recognition_mode)
                result = Result(
                    task_id=task.task_id, socket_id=task.socket_id, source=task.source,
                    duration=task.offset + len(task.data) / (task.samplerate * 4),
                    time_start=task.time_start, time_submit=task.time_submit,
                    time_complete=time.time(), is_final=True, phase='final',
                    recognition_mode=task.recognition_mode, status='error', error=str(error))
            # Queue 的后台线程稍后才序列化；普通和流式的所有结果都交不可再变的快照。
            self.queue_out.put(replace(result, tokens=list(result.tokens),
                                       timestamps=list(result.timestamps)))
            if result.is_final:
                self.state.sessions.pop(task.key, None)
        finally:
            runtime_status.task_finished()

    def loop(self):
        """核心任务循环：drain 队列 → 清理断连 → 执行最新会话的一片。"""
        logger.info('TaskHandler 开始工作循环')

        while True:
            try:
                if not self.drain_queue():
                    break

                self.process_next()
            except InterruptedError:
                continue
            except Exception as e:
                logger.error(f"任务执行出错: {str(e)}", exc_info=True)

        logger.info("TaskHandler 工作循环结束")
