# coding: utf-8
"""按连接管理录音决策、原始 PCM 缓存和唯一终局。"""

import asyncio
import math
import tempfile
import time
from base64 import b64decode
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from config_server import Qwen3ASRGGUFArgs
from core.constants import AudioFormat
from core.protocol import AudioMessage, RecognitionMessage, RecognitionControlMessage
from ..schema import DiscardTask, Result
from ..state import console
from .. import logger, runtime_status
from .ws_recv import AudioCache, make_task, message_handler, ordinary_segment_size, status_mic


@dataclass
class Recording:
    message: AudioMessage
    cache: AudioCache = field(default_factory=AudioCache)
    spool: object = None
    byte_count: int = 0
    before_final: int = 0
    input_ended: bool = False
    decision: str = ''
    candidate: Optional[Result] = None
    full_started: bool = False
    full_submit: float = 0.0
    replay_position: int = 0
    replay_offset: float = 0.0

    @property
    def revisable(self):
        msg = self.message
        return msg.source == 'mic' and msg.streaming and msg.revisable

    @property
    def mode(self):
        streaming = self.message.source == 'mic' and self.message.streaming
        return 'stream' if streaming and self.decision != 'full' else 'full'

    @property
    def duration(self):
        return AudioFormat.bytes_to_seconds(self.byte_count)

    def close_audio(self):
        if self.spool is not None:
            spool, self.spool = self.spool, None
            spool.close()


class RecognitionConnection:
    def __init__(self, websocket, app):
        self.websocket = websocket
        self.socket_id = str(websocket.id)
        self.app = app
        self.records = {}
        # 终结后只保留 ID，拦住剩余音频和迟到控制；连接关闭时一并释放。
        self.finished = set()
        self.closed = False
        # 接收决定与结果发送共用锁。已开始写出的旧帧先完成，再接受切换决定。
        self.lock = asyncio.Lock()

    def _discard(self, task_id, mode=''):
        self.app.state.queue_in.put(DiscardTask(self.socket_id, task_id, mode))

    @staticmethod
    def _validate_segments(msg):
        # 流式切句忽略 seg_*；只有普通识别才要求切片步长能够前进。
        if (not math.isfinite(msg.seg_duration) or not math.isfinite(msg.seg_overlap) or
                AudioFormat.seconds_to_bytes(msg.seg_duration) < 4 or msg.seg_overlap < 0):
            raise ValueError('分段时长必须为正，重叠时长必须为非负有限数')

    async def audio(self, msg: AudioMessage):
        async with self.lock:
            if self.closed or msg.task_id in self.finished:
                return
            record = self.records.get(msg.task_id)
            if record is None:
                record = Recording(replace(msg, data='', is_final=False))
                self.records[msg.task_id] = record
            try:
                original = record.message
                if (msg.source, msg.streaming, msg.revisable) != (
                        original.source, original.streaming, original.revisable):
                    raise ValueError('同一录音的 source、streaming、revisable 必须保持一致')
                if record.input_ended:
                    raise ValueError('音频输入已结束')
                if record.mode == 'full':
                    self._validate_segments(original)
                data = b64decode(msg.data, validate=True)
                if len(data) % AudioFormat.BYTES_PER_SAMPLE:
                    raise ValueError('音频数据必须包含完整的 float32 采样点')
                record.before_final = record.byte_count
                record.byte_count += len(data)
                record.input_ended = msg.is_final

                if record.revisable and record.decision != 'stream':
                    if record.spool is None:
                        directory = Path(self.app.base_dir) / 'logs' / 'role_full_asr'
                        directory.mkdir(parents=True, exist_ok=True)
                        record.spool = tempfile.TemporaryFile(dir=directory, mode='w+b')
                    record.spool.write(data)

                if record.decision == 'full':
                    if msg.is_final:
                        status_mic.stop()
                        self._start_full(record)
                    else:
                        status_mic.start()
                else:
                    await message_handler(self.websocket, replace(original, is_final=msg.is_final),
                                          record.cache, self.app, data=data)
            except Exception as error:
                logger.error('接收录音失败，任务ID: %s: %s', msg.task_id, error, exc_info=True)
                await self._error(record, error)

    async def control(self, msg: RecognitionControlMessage):
        async with self.lock:
            if self.closed or msg.task_id in self.finished:
                return
            record = self.records.get(msg.task_id)
            if record is None:
                logger.warning('忽略未知录音的控制，连接: %s，任务ID: %s', self.socket_id, msg.task_id)
                return
            if msg.action == 'cancel':
                await self._emit(record, self._terminal(record, 'cancelled'))
                return
            if not record.revisable:
                logger.warning('识别策略控制仅适用于 revisable 任务: %s', msg.task_id)
                return
            decision = 'full' if msg.action == 'request_full' else 'stream'
            if record.decision:
                if record.decision != decision:
                    logger.warning('识别控制冲突，任务ID: %s，沿用 %s，忽略 %s',
                                   msg.task_id, record.decision, decision)
                return
            record.decision = decision
            try:
                if decision == 'full':
                    # 决定先落地，后续旧 stream 结果在 publish 中全部过滤。
                    self._discard(msg.task_id, 'stream')
                    record.cache.reset()
                    record.candidate = None
                    if record.input_ended:
                        self._start_full(record)
                else:
                    record.close_audio()
                    if record.candidate is not None:
                        await self._emit(record, replace(record.candidate, is_final=True, phase='final'))
            except Exception as error:
                await self._error(record, error)

    def _start_full(self, record):
        if record.full_started:
            return
        record.full_started = True
        record.full_submit = time.time()
        self._validate_segments(record.message)
        self._submit_full_segment(record)

    def _submit_full_segment(self, record):
        msg = record.message
        position = record.replay_position
        # 原路径只在非结束包上切片。记住结束包前的长度，即可重现相同边界，
        # 无需把每个传输包的内容或大小另存一份。
        sizes = ordinary_segment_size(msg, record.before_final - position)
        is_final = sizes is None
        size, stride = sizes if sizes else (record.byte_count - position, 0)
        # 适配器会静默截断超长片段；完整识别必须明确失败，不能返回缺失尾部的全文。
        if AudioFormat.bytes_to_seconds(size) > Qwen3ASRGGUFArgs.chunk_size:
            raise ValueError('普通识别片段超过模型 chunk_size，请缩短 seg_duration / seg_overlap 或音频结束包')
        record.spool.seek(position)
        data = record.spool.read(size)
        if len(data) != size:
            raise OSError('原始音频缓存读取不完整')
        task = make_task(msg, self.socket_id, data, record.replay_offset, is_final)
        task.time_submit = record.full_submit
        record.replay_position += stride
        record.replay_offset += msg.seg_duration
        if is_final:
            record.close_audio()
        runtime_status.task_queued()
        try:
            self.app.state.queue_in.put(task)
        except Exception:
            runtime_status.task_abandoned()
            raise

    async def publish(self, result: Result):
        async with self.lock:
            record = self.records.get(result.task_id)
            if self.closed or record is None:
                return
            if result.recognition_mode != record.mode:
                return
            if result.status != 'ok':
                await self._emit(record, self._terminal(record, result.status, result.error))
                return
            result = replace(result, phase='final' if result.is_final else 'partial')
            if result.is_final:
                result.duration = record.duration
                if record.revisable and not record.decision:
                    if record.candidate is not None:
                        return
                    record.candidate = result
                    await self._emit(record, replace(result, is_final=False, phase='decision'))
                    return
            await self._emit(record, result)
            # 每任务最多一片 full 在途；结果回来才读下一片，避免将整段录音堆进进程队列。
            if record.decision == 'full' and not result.is_final and not self.closed:
                try:
                    self._submit_full_segment(record)
                except Exception as error:
                    await self._error(record, error)

    def _terminal(self, record, status, error=''):
        msg = record.message
        return Result(task_id=msg.task_id, socket_id=self.socket_id, source=msg.source,
                      duration=record.duration, time_start=msg.time_start,
                      time_submit=record.full_submit or time.time(), time_complete=time.time(),
                      is_final=True, phase='final', recognition_mode=record.mode,
                      status=status, error=str(error))

    async def _error(self, record, error):
        await self._emit(record, self._terminal(record, 'error', error))

    async def _emit(self, record, result):
        task_id = record.message.task_id
        if self.closed or task_id in self.finished:
            return
        if result.is_final:
            # 认领与标记之间不得 await；取消、错误和模型完成只有一个获胜者。
            self.finished.add(task_id)
            self.records.pop(task_id, None)
            if result.status != 'ok':
                self._discard(task_id)
            record.cache.reset()
            if not any(r.message.source == 'mic' and not r.input_ended
                       for connection in self.app.state.recognition_connections.values()
                       for r in connection.records.values()):
                status_mic.stop()
            try:
                record.close_audio()
            except OSError as error:
                logger.error('关闭原始音频缓存失败: %s', error)
        msg = RecognitionMessage(
            task_id=task_id, is_final=result.is_final, duration=result.duration,
            time_start=record.message.time_start, time_submit=result.time_submit,
            time_complete=result.time_complete, text=result.text, text_accu=result.text_accu,
            tokens=result.tokens, timestamps=result.timestamps, delta=result.delta,
            phase=result.phase, recognition_mode=result.recognition_mode,
            status=result.status, error=result.error)
        try:
            await self.websocket.send(msg.to_json())
        except Exception:
            self.close()
            await self.websocket.close()
            raise
        logger.debug('发送识别结果，任务ID: %s，阶段: %s/%s，状态: %s',
                     task_id, msg.recognition_mode, msg.phase, msg.status)
        if record.revisable and result.is_final:
            logger.info('录音终结，任务ID: %s，模式: %s，状态: %s，时长: %.2fs，文本: %s',
                        task_id, msg.recognition_mode, msg.status, msg.duration, msg.text)
        if result.source == 'file':
            console.print(f'    转录进度：{result.duration:.2f}s', end='\r')
            if result.is_final:
                console.print('\n    [green]转录完成' if result.status == 'ok' else '\n    [red]转录失败')

    def close(self, notify_worker=True):
        self.closed = True
        for task_id, record in list(self.records.items()):
            if notify_worker:
                self._discard(task_id)
            try:
                record.close_audio()
            except OSError as error:
                logger.error('关闭原始音频缓存失败: %s', error)
            record.cache.reset()
        self.records.clear()
        self.finished.clear()
