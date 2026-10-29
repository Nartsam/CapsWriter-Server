"""角色请求整段识别的离线时序验证：真实接收/队列/流水线/发布代码，假模型，无网络或模型依赖。"""

import asyncio
import ast
import base64
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 避免包入口初始化模型、用户日志和热词仓库；业务模块仍从源码真实导入。
for name, directory in [('core', 'core'), ('core.server', 'core/server'),
                        ('core.server.worker', 'core/server/worker')]:
    module = types.ModuleType(name)
    module.__path__ = [str(ROOT / directory)]
    module.logger = Mock()
    sys.modules[name] = module
# 这里只需要能 import：测试路径上不碰真实的 WebSocket 和 rich 渲染。
# 装了真包就用真包；internal/ 里的是 3.13 字节码，别的解释器读不了，所以缺包时用桩补上。
class _Console:
    """rich.console.Console 的替身：吞掉输出"""
    quiet = False

    def __init__(self, *args, **kwargs):
        pass

    def print(self, *args, **kwargs):
        pass

    line = rule = print


def _stub(name, **members):
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = types.ModuleType(name)
        sys.modules[name].__dict__.update(members)


_stub('websockets', ConnectionClosed=type('ConnectionClosed', (Exception,), {}),
      InvalidState=type('InvalidState', (Exception,), {}), ServerConnection=object)
_stub('rich')
_stub('rich.console', Console=_Console, RenderableType=object)
_stub('rich.style', StyleType=object)
_stub('rich.status', Status=_Console)
import websockets

import numpy as np
from core.protocol import AudioMessage, RecognitionControlMessage, RecognitionMessage
from core.server import runtime_status
from core.server.schema import Task, Result, DiscardTask
from core.server.state import WorkerState, console
from core.server.worker.task_handler import TaskHandler
from core.server.connection import ws_recv as recv_module
from core.server.connection.recognition import RecognitionConnection
from core.server.connection.ws_send import ws_send
from smoke_test import load_audio_f32, DEFAULT_AUDIO

console.quiet = True
recv_module.status_mic = Mock()
# recognition 导入时已持有该引用。
import core.server.connection.recognition as recognition_module
recognition_module.status_mic = recv_module.status_mic


class FakeSocket:
    def __init__(self, socket_id='socket', incoming=(), path='/'):
        self.id = socket_id
        self.sent = []
        self.incoming = incoming
        self.remote_address = ('127.0.0.1', 12345)
        # 与 websockets.ServerConnection 一致：握手请求挂在 request 上
        self.request = types.SimpleNamespace(path=path)

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def close(self):
        pass

    async def __aiter__(self):
        for message in self.incoming:
            yield message


class FakeEngine:
    capabilities = []

    def __init__(self):
        self.calls = []
        self.fail = False

    def create_stream(self):
        result = types.SimpleNamespace(text='', tokens=[], timestamps=[])
        stream = types.SimpleNamespace(result=result)
        stream.accept_waveform = lambda rate, samples: setattr(stream, 'samples', samples)
        return stream

    def decode_stream(self, stream, **kwargs):
        self.calls.append((stream.samples.tobytes(), dict(kwargs)))
        if self.fail:
            raise RuntimeError('模拟识别失败')
        stream.result.text = '旧预览。' if 'prefix_text' in kwargs else '完整正文。'


class Counts:
    def __init__(self):
        self.queued = self.processing = self.enqueued = self.started = self.finished = self.abandoned = 0

    def task_queued(self, count=1):
        self.queued += count
        self.enqueued += count

    def task_started(self):
        self.queued -= 1
        self.processing += 1
        self.started += 1
        assert self.queued >= 0

    def task_finished(self, count=1):
        self.processing -= count
        self.finished += count
        assert self.processing >= 0

    def task_abandoned(self, count=1):
        self.queued -= count
        self.abandoned += count
        assert self.queued >= 0


def audio(seconds=0.3):
    samples = np.arange(round(seconds * 16000), dtype=np.float32)
    return (np.sin(samples * 0.08) * 0.1).astype(np.float32).tobytes()


class RecognitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / 'logs').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'logs', prefix='test_role_asr_')
        self.app = types.SimpleNamespace(base_dir=Path(self.temp.name), hotword_sync=None)
        self.app.state = types.SimpleNamespace(queue_in=queue.Queue(), queue_out=queue.Queue(),
                                              sockets={}, sockets_id=[], recognition_connections={})
        self.connections = []
        self.connection = self.connect('socket')
        self.counts = Counts()
        self.counter_patch = patch.multiple(runtime_status, **{
            name: getattr(self.counts, name) for name in
            ('task_queued', 'task_started', 'task_finished', 'task_abandoned')})
        self.counter_patch.start()
        self.engine = FakeEngine()
        self.worker = TaskHandler(self.app.state.queue_in, self.app.state.queue_out,
                                  self.app.state.sockets_id, WorkerState())
        self.worker.set_engine(self.engine)
        self.tasks = []

    async def asyncTearDown(self):
        for connection in self.connections:
            connection.close()
        self.app.state.sockets_id.clear()
        self.drain()
        self.worker.cleanup()
        self.assertEqual(self.counts.queued, 0)
        self.assertEqual(self.counts.processing, 0)
        self.assertEqual(self.counts.enqueued, self.counts.finished + self.counts.abandoned)
        self.counter_patch.stop()
        self.temp.cleanup()

    def connect(self, socket_id):
        ws = FakeSocket(socket_id)
        connection = RecognitionConnection(ws, self.app)
        state = self.app.state
        state.sockets[socket_id] = ws
        state.sockets_id.append(socket_id)
        state.recognition_connections[socket_id] = connection
        self.connections.append(connection)
        return connection

    def message(self, data=b'', final=False, task_id='task', **kwargs):
        values = dict(task_id=task_id, source='mic', data=base64.b64encode(data).decode(),
                      is_final=final, time_start=100.0, seg_duration=2.0, seg_overlap=0.5,
                      streaming=True, revisable=True, context='原上下文', language='zh')
        values.update(kwargs)
        return AudioMessage(**values)

    async def send(self, data=b'', final=False, connection=None, **kwargs):
        await (connection or self.connection).audio(self.message(data, final, **kwargs))

    async def control(self, action, task_id='task', connection=None):
        await (connection or self.connection).control(RecognitionControlMessage(task_id, action))

    def drain(self):
        while not self.app.state.queue_in.empty():
            task = self.app.state.queue_in.get_nowait()
            if isinstance(task, Task):
                self.tasks.append(task)
            self.worker.accept(task)

    async def step(self):
        self.drain()
        if self.worker.buffer.is_empty:
            return False
        self.worker.process_next()
        result = self.app.state.queue_out.get_nowait()
        connection = self.app.state.recognition_connections.get(result.socket_id)
        if connection:
            await connection.publish(result)
        return True

    async def finish_work(self):
        for _ in range(100):
            if not await self.step():
                return
        self.fail('工作队列未能收敛')

    def sent(self, connection=None):
        return (connection or self.connection).websocket.sent

    def finals(self, connection=None):
        return [msg for msg in self.sent(connection) if msg['phase'] == 'final']

    async def test_early_full_waits_for_end_and_runs_once(self):
        first, last = audio(1), audio(0.2)
        await self.send(first)
        spool = self.connection.records['task'].spool
        await self.control('request_full')
        await self.control('request_full')
        await self.finish_work()
        self.assertFalse(self.engine.calls)
        await self.send(last, True, context='不采用', language='en', seg_duration=10)
        await self.finish_work()
        self.assertEqual(self.engine.calls[0], (first + last, {'context': '原上下文', 'language': 'zh'}))
        final = self.finals()[0]
        self.assertEqual((final['recognition_mode'], final['text'], final['delta']), ('full', '完整正文。', ''))
        self.assertEqual(final['duration'], 1.2)
        self.assertEqual(final['time_start'], 100)
        self.assertLessEqual(final['time_submit'], final['time_complete'])
        self.assertTrue(spool.closed)
        await self.control('request_full')
        await self.control('cancel')
        self.assertEqual(len(self.finals()), 1)

    async def test_preview_removed_audio_is_present_in_full(self):
        data = load_audio_f32(DEFAULT_AUDIO)
        cursor = 0
        while cursor < len(data):
            await self.send(data[cursor:cursor + 3200])
            cursor += 3200
            self.drain()
            if not self.worker.buffer.is_empty:
                break
        self.assertLess(cursor, len(data))
        await self.step()
        self.assertTrue(self.sent())
        await self.control('request_full')
        for offset in range(cursor, len(data), 3200):
            await self.send(data[offset:offset + 3200])
        await self.send(final=True)
        await self.finish_work()
        full = [task for task in self.tasks if not task.streaming]
        restored = b''.join(task.data[:128000] for task in full[:-1]) + full[-1].data
        self.assertEqual(restored, data)
        self.assertTrue(all('prefix_text' not in options for _, options in self.engine.calls[1:]))
        self.assertEqual(self.finals()[0]['duration'], len(data) / 64000)
        self.assertNotIn('旧预览', self.finals()[0]['text'])

    async def test_late_full_without_partial_and_candidate_retained(self):
        await self.send(audio(), True)
        spool = self.connection.records['task'].spool
        await self.finish_work()
        self.assertEqual([m['phase'] for m in self.sent()], ['decision'])
        self.assertFalse(spool.closed)
        self.assertFalse(self.worker.state.sessions)
        # 决策没有计时器；一段事件循环空闲不会改变候选或缓存。
        await asyncio.sleep(0)
        await self.control('request_full')
        await self.finish_work()
        self.assertTrue(spool.closed)
        self.assertEqual(len(self.finals()), 1)
        self.assertEqual(self.finals()[0]['text'], '完整正文。')

    async def test_commit_early_and_late_tail_delta(self):
        for early in (True, False):
            task_id = str(early)
            await self.send(audio(), task_id=task_id)
            spool = self.connection.records[task_id].spool
            if early:
                await self.control('commit_stream', task_id)
                self.assertTrue(spool.closed)
            await self.send(final=True, task_id=task_id)
            await self.finish_work()
            if not early:
                decision = self.sent()[-1]
                self.assertEqual(decision['phase'], 'decision')
                await self.control('commit_stream', task_id)
                self.assertEqual(self.sent()[-1]['delta'], decision['delta'])
                self.assertEqual(self.sent()[-1]['text'], decision['text'])
            self.assertTrue(spool.closed)
            messages = [m for m in self.sent() if m['task_id'] == task_id]
            self.assertEqual(sum(m['is_final'] for m in messages), 1)
            self.assertEqual(''.join(m['delta'] for m in messages if m['phase'] != 'decision'), messages[-1]['text'])

    async def test_replay_boundaries_match_direct_and_preserve_bytes(self):
        for i, seconds in enumerate((0, 0.05, 2.999, 3, 3.001, 9.2)):
            data = audio(seconds)
            task_id = 'replay' + str(i)
            direct_id = 'direct' + str(i)
            final_bytes = min(12800, len(data))
            head, tail = data[:len(data) - final_bytes], data[len(data) - final_bytes:]
            for offset in range(0, len(head), 8320):
                piece = head[offset:offset + 8320]
                await self.send(piece, task_id=task_id)
                await self.send(piece, task_id=direct_id, streaming=False, revisable=False)
            if not head:
                await self.send(task_id=task_id)
            await self.control('request_full', task_id)
            await self.send(tail, True, task_id=task_id)
            await self.send(tail, True, task_id=direct_id, streaming=False, revisable=False)
            while await self.step():
                pending = [t for t in list(self.app.state.queue_in.queue)
                           if isinstance(t, Task) and t.task_id == task_id and not t.streaming]
                self.assertLessEqual(len(pending), 1)
            actual = [t for t in self.tasks if t.task_id == task_id and not t.streaming]
            expected = [t for t in self.tasks if t.task_id == direct_id]
            signature = lambda tasks: [(t.data, t.offset, t.overlap, t.is_final) for t in tasks]
            self.assertEqual(signature(actual), signature(expected))
            self.assertEqual(b''.join(t.data[:128000] for t in actual[:-1]) + actual[-1].data, data)
            result = next(m for m in self.finals() if m['task_id'] == task_id)
            self.assertAlmostEqual(result['duration'], len(data) / 64000)

    async def test_recordings_and_connections_are_isolated(self):
        other = self.connect('other')
        await self.send(audio(.2), True, task_id='A')
        await self.finish_work()
        await self.send(audio(.4), task_id='B')
        await self.send(audio(.6), True, task_id='A', connection=other)
        await self.finish_work()
        b_spool = self.connection.records['B'].spool
        await self.control('request_full', 'A')
        await self.control('commit_stream', 'A', connection=other)
        await self.finish_work()
        self.assertFalse(b_spool.closed)
        self.assertEqual(self.finals()[0]['duration'], .2)
        self.assertEqual(self.finals(other)[0]['duration'], .6)
        self.assertEqual(self.finals(other)[0]['recognition_mode'], 'stream')
        await self.send(final=True, task_id='B')
        await self.control('request_full', 'B')
        await self.finish_work()
        self.assertEqual(self.finals()[-1]['duration'], .4)

    async def test_conflicting_and_duplicate_controls(self):
        await self.send(audio())
        await self.control('commit_stream')
        await self.control('request_full')
        await self.control('commit_stream')
        await self.send(final=True)
        await self.finish_work()
        for action in ('commit_stream', 'request_full', 'cancel'):
            await self.control(action)
        self.assertEqual(len(self.finals()), 1)
        self.assertEqual(self.finals()[0]['recognition_mode'], 'stream')

    async def test_cancel_during_receive_decision_and_running_full(self):
        for stage in ('receive', 'decision', 'full'):
            task_id = stage
            await self.send(audio(), task_id=task_id)
            spool = self.connection.records[task_id].spool
            late = None
            if stage != 'receive':
                await self.send(final=True, task_id=task_id)
                if stage == 'decision':
                    await self.finish_work()
                else:
                    await self.control('request_full', task_id)
                    self.drain()
                    self.worker.process_next()
                    late = self.app.state.queue_out.get_nowait()
            await self.control('cancel', task_id)
            await self.control('cancel', task_id)
            if late:
                await self.connection.publish(late)
            await self.send(audio(), True, task_id=task_id)
            await self.finish_work()
            final = [m for m in self.finals() if m['task_id'] == task_id]
            self.assertEqual(len(final), 1)
            self.assertEqual((final[0]['status'], final[0]['text'], final[0]['delta']), ('cancelled', '', ''))
            self.assertTrue(spool.closed)

    async def test_queued_preview_discard_and_late_error_filtered(self):
        await self.send(audio())
        # 模拟已运行片段和随后排队片段，使用真实 TaskHandler 与发布入口。
        for offset in (0, .3):
            task = recv_module.make_task(self.message(), 'socket', audio(), offset, False, streaming=True)
            runtime_status.task_queued()
            self.app.state.queue_in.put(task)
        self.drain()
        self.engine.fail = True
        self.worker.process_next()
        late_error = self.app.state.queue_out.get_nowait()
        self.engine.fail = False
        await self.control('request_full')
        await self.connection.publish(late_error)
        await self.send(final=True)
        await self.finish_work()
        self.assertEqual(len(self.finals()), 1)
        self.assertEqual(self.finals()[0]['status'], 'ok')
        self.assertGreater(self.counts.abandoned, 0)

    async def test_cancel_multisegment_replay_before_late_partial(self):
        await self.send(audio(9))
        spool = self.connection.records['task'].spool
        await self.control('request_full')
        await self.send(final=True)
        self.drain()
        self.worker.process_next()
        late = self.app.state.queue_out.get_nowait()
        self.assertFalse(late.is_final)
        self.assertFalse(spool.closed)
        await self.control('cancel')
        await self.connection.publish(late)
        await self.finish_work()
        self.assertTrue(spool.closed)
        self.assertEqual(len([t for t in self.tasks if not t.streaming]), 1)
        self.assertEqual([m['status'] for m in self.finals()], ['cancelled'])

    async def test_read_failure_after_full_partial(self):
        await self.send(audio(9))
        record = self.connection.records['task']
        spool = record.spool
        await self.control('request_full')
        await self.send(final=True)
        proxy = Mock(wraps=spool)
        proxy.read.side_effect = OSError('重放中途读取失败')
        record.spool = proxy
        await self.finish_work()
        self.assertEqual([m['phase'] for m in self.sent()], ['partial', 'final'])
        self.assertEqual(self.finals()[0]['status'], 'error')
        self.assertEqual(self.finals()[0]['text'], '')
        self.assertTrue(spool.closed)

    async def test_empty_final_formats_previous_ordinary_segment(self):
        def decode(stream, **kwargs):
            stream.result.text = '一百二十三。'

        self.engine.decode_stream = decode
        await self.send(audio(.3), streaming=False, revisable=False, seg_duration=.3, seg_overlap=0)
        await self.finish_work()
        self.assertEqual(self.sent()[0]['text'], '一百二十三。')
        await self.send(final=True, streaming=False, revisable=False, seg_duration=.3, seg_overlap=0)
        await self.finish_work()
        final = self.finals()[0]
        self.assertEqual(final['text'], '123。')
        self.assertEqual(final['duration'], .3)
        self.assertEqual(final['tokens'], list('123。'))

    async def test_pipeline_failure_is_one_error_without_fallback(self):
        await self.send(audio(4))
        await self.control('request_full')
        await self.send(final=True)
        self.engine.fail = True
        await self.finish_work()
        self.assertEqual(len(self.finals()), 1)
        self.assertEqual(self.finals()[0]['status'], 'error')
        self.assertEqual(self.finals()[0]['text'], '')
        await self.send(audio(), True)
        await self.control('request_full')
        self.assertEqual(len(self.finals()), 1)
        self.assertFalse(self.worker.state.sessions)

    async def test_disk_create_write_and_read_failures(self):
        for operation in ('create', 'write', 'read', 'short_read'):
            task_id = operation
            spool = io.BytesIO()
            if operation == 'write':
                spool.write = Mock(side_effect=OSError('磁盘已满'))
            factory = Mock(side_effect=OSError('不能创建')) if operation == 'create' else Mock(return_value=spool)
            with patch.object(recognition_module.tempfile, 'TemporaryFile', factory):
                await self.send(audio(), task_id=task_id)
                if operation in ('read', 'short_read'):
                    if operation == 'read':
                        spool.read = Mock(side_effect=OSError('不能读取'))
                    else:
                        spool.read = Mock(return_value=b'')
                    await self.control('request_full', task_id)
                    await self.send(final=True, task_id=task_id)
            self.assertEqual(self.finals()[-1]['status'], 'error')
            if operation != 'create':
                self.assertTrue(spool.closed)
            await self.send(audio(), True, task_id=task_id)
        self.assertEqual(len(self.finals()), 4)

    async def test_disconnect_releases_waiting_and_receiving_cache(self):
        await self.send(audio(), True, task_id='A')
        await self.finish_work()
        await self.send(audio(), task_id='B')
        spools = [r.spool for r in self.connection.records.values()]
        self.connection.close()
        await self.finish_work()
        self.assertTrue(all(f.closed for f in spools))
        self.assertFalse(self.connection.records)
        self.assertFalse(self.finals())

    async def test_app_finish_closes_cache_after_event_loop_stops(self):
        await self.send(audio())
        spool = self.connection.records['task'].spool
        path = ROOT / 'core/server/app.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CapsWriterServer')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_finish')
        namespace = dict(sys=sys)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
        self.app._restart_requested = False
        with self.assertRaises(SystemExit) as raised:
            namespace['_finish'](self.app, 0)
        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(spool.closed)
        self.assertFalse(self.app.state.recognition_connections)
        self.assertTrue(self.app.state.queue_in.empty())

    async def test_final_claim_before_await_blocks_cancel(self):
        await self.send(audio(), True, streaming=False, revisable=False)
        self.drain()
        self.worker.process_next()
        result = self.app.state.queue_out.get_nowait()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.connection.websocket.send

        async def delayed(text):
            entered.set()
            await release.wait()
            await original(text)

        self.connection.websocket.send = delayed
        publisher = asyncio.create_task(self.connection.publish(result))
        await entered.wait()
        self.assertIn('task', self.connection.finished)
        cancellation = asyncio.create_task(self.control('cancel'))
        release.set()
        await asyncio.gather(publisher, cancellation)
        self.assertEqual(len(self.finals()), 1)
        self.assertEqual(self.finals()[0]['status'], 'ok')

    async def test_normal_modes_do_not_allocate_spool(self):
        with patch.object(recognition_module.tempfile, 'TemporaryFile', side_effect=AssertionError('不应创建缓存')):
            for source, streaming in (('mic', False), ('mic', True), ('file', False)):
                task_id = source + str(streaming)
                await self.send(audio(), True, task_id=task_id, source=source, streaming=streaming, revisable=False)
                await self.finish_work()
                final = self.finals()[-1]
                self.assertEqual(final['recognition_mode'], 'stream' if streaming else 'full')
                self.assertEqual(final['status'], 'ok')

    async def test_streaming_ignores_ordinary_segment_parameters(self):
        for revisable in (False, True):
            task_id = str(revisable)
            await self.send(audio(), True, task_id=task_id, revisable=revisable,
                            seg_duration=0, seg_overlap=0)
            if revisable:
                await self.control('commit_stream', task_id)
            await self.finish_work()
            self.assertEqual(self.finals()[-1]['status'], 'ok')

    async def test_cancel_stops_receiving_status_without_stopping_other_recording(self):
        await self.send(audio(), task_id='A')
        await self.send(audio(), task_id='B')
        recv_module.status_mic.stop.reset_mock()
        await self.control('cancel', 'A')
        recv_module.status_mic.stop.assert_not_called()
        await self.control('cancel', 'B')
        recv_module.status_mic.stop.assert_called_once()

    async def test_strategy_change_fails_and_new_recording_survives(self):
        await self.send(audio())
        spool = self.connection.records['task'].spool
        await self.send(audio(), streaming=False)
        await self.send(audio(), True)
        self.assertTrue(spool.closed)
        self.assertEqual(len(self.finals()), 1)
        self.assertEqual(self.finals()[0]['status'], 'error')
        await self.send(audio(), True, task_id='new', streaming=False, revisable=False)
        await self.finish_work()
        self.assertEqual(self.finals()[-1]['status'], 'ok')

    async def test_real_receive_routes_control_and_cleans_disconnect(self):
        ws = FakeSocket('receiver', [self.message(audio()).to_json(),
                                    RecognitionControlMessage('task', 'request_full').to_json(),
                                    self.message(final=True).to_json(),
                                    RecognitionControlMessage('task', 'cancel').to_json()])
        await recv_module.ws_recv(ws, self.app)
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.sent[0]['status'], 'cancelled')
        self.assertNotIn('receiver', self.app.state.recognition_connections)
        self.assertNotIn('receiver', self.app.state.sockets_id)
        self.assertFalse(list((self.app.base_dir / 'logs/role_full_asr').iterdir()))

    async def test_sender_routes_worker_result(self):
        await self.send(audio(), True, streaming=False, revisable=False)
        self.drain()
        self.worker.process_next()
        self.app.state.queue_out.put(None)
        await ws_send(self.app)
        self.assertEqual(len(self.finals()), 1)

    async def test_worker_snapshot_and_stage_isolation(self):
        base = recv_module.make_task(self.message(), 'socket', audio(), 0, False, streaming=True)
        for task in (base, replace(base, offset=.3), replace(base, streaming=False, is_final=True)):
            runtime_status.task_queued()
            self.worker.accept(task)
        # 新 full session 优先，旧 stream 的两片仍保持 FIFO。
        self.worker.process_next()
        full = self.app.state.queue_out.get_nowait()
        self.assertEqual(full.text, '完整正文。')
        self.worker.process_next()
        first = self.app.state.queue_out.get_nowait()
        text, duration = first.text, first.duration
        self.worker.process_next()
        second = self.app.state.queue_out.get_nowait()
        self.assertEqual((first.text, first.duration), (text, duration))
        self.assertEqual(second.duration, .6)
        self.assertEqual(self.engine.calls[0][1], {'context': '原上下文', 'language': 'zh'})
        self.assertTrue(self.engine.calls[2][1]['prefix_text'])

    async def test_silent_tail_preserved_for_full(self):
        data = load_audio_f32(DEFAULT_AUDIO) + bytes(64000)
        for offset in range(0, len(data), 3200):
            await self.send(data[offset:offset + 3200])
        await self.send(final=True)
        self.drain()
        previews = [task for task in self.tasks if task.streaming]
        self.assertTrue(previews[-1].silent)
        await self.control('request_full')
        await self.finish_work()
        full = [task for task in self.tasks if not task.streaming]
        self.assertEqual(b''.join(t.data[:128000] for t in full[:-1]) + full[-1].data, data)

    async def test_model_segment_limit_fails_instead_of_truncating(self):
        await self.send(audio(), seg_duration=100)
        await self.control('request_full')
        with patch('config_server.Qwen3ASRGGUFArgs.chunk_size', .1):
            await self.send(final=True, seg_duration=100)
        self.assertEqual(self.finals()[0]['status'], 'error')
        self.assertFalse(self.engine.calls)


class ProtocolTests(unittest.TestCase):
    def test_adapter_exhausted_retry_raises(self):
        # 只隔离 native 模型加载；执行适配器真实方法，验证内核的失败标志没有被吞掉。
        path = ROOT / 'core/server/engines/qwen_asr_gguf/asr_engine.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'QwenASREngine')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'decode_stream')
        namespace = dict(np=np, QwenASRStream=object, Optional=__import__('typing').Optional,
                         get_language=lambda engine, language: language, ENGINE_QWEN_ASR='qwen')
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
        engine = Mock()
        engine.encoder.encode.return_value = (np.zeros(1), 0)
        engine._safe_decode.return_value = types.SimpleNamespace(is_aborted=True, text='半截文本')
        owner = types.SimpleNamespace(config=types.SimpleNamespace(chunk_size=80), engine=engine)
        stream = types.SimpleNamespace(audio_data=np.zeros(1600), result=types.SimpleNamespace(text=''))
        with self.assertRaisesRegex(RuntimeError, '熔断'):
            namespace['decode_stream'](owner, stream)
        self.assertEqual(stream.result.text, '')

    def test_serialization(self):
        audio_msg = AudioMessage('id', 'mic', '', True, 123, streaming=True, revisable=True)
        self.assertEqual(AudioMessage.from_dict(json.loads(audio_msg.to_json())), audio_msg)
        for action in ('request_full', 'commit_stream', 'cancel'):
            msg = RecognitionControlMessage('id', action)
            self.assertEqual(RecognitionControlMessage.from_dict(json.loads(msg.to_json())), msg)
        with self.assertRaises(ValueError):
            RecognitionControlMessage.from_dict(dict(type='recognition_control', task_id='id', action='other'))
        result = RecognitionMessage('id', True, 1, 2, 3, 4, '', phase='final', recognition_mode='full',
                                    status='error', error='失败')
        self.assertEqual(RecognitionMessage.from_dict(json.loads(result.to_json())), result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
