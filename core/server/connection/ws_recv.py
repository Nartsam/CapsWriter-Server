# coding: utf-8
"""
WebSocket 接收处理模块

处理客户端发送的音频数据，进行分段和缓冲，提交到识别队列。
"""

import json
import time
from base64 import b64decode

import numpy as np
import websockets

from ..state import console
from ..schema import Task
from .pause_splitter import PauseSplitter
from core.protocol import AudioMessage
from core.constants import AudioFormat
from core.tools.my_status import Status
from .. import logger
from .. import runtime_status


# 麦克风接收状态指示器
status_mic = Status('正在接收音频', spinner='point')


class AudioCache:
    """
    音频缓冲区

    用于缓存接收到的音频数据，直到达到分段阈值后提交处理。
    """
    def __init__(self):
        self.chunks: bytes = b''    # 音频数据缓冲
        self.offset: float = 0.0    # 当前偏移时间（秒）
        self.byte_count: int = 0    # 累计接收字节数
        self.splitter = None        # 流式任务的停顿切句状态，任务的第一个数据包到达时创建

    @property
    def duration(self) -> float:
        """缓冲区音频时长（秒）"""
        return AudioFormat.bytes_to_seconds(len(self.chunks))

    @property
    def total_duration(self) -> float:
        """累计接收的音频总时长（秒）"""
        return AudioFormat.bytes_to_seconds(self.byte_count)

    def reset(self) -> None:
        """重置缓冲区"""
        self.chunks = b''
        self.offset = 0.0
        self.byte_count = 0
        self.splitter = None


def _make_task(msg: AudioMessage, socket_id: str, data: bytes, offset: float,
               is_final: bool, streaming: bool = False, silent: bool = False) -> Task:
    """按消息里的任务参数构造一个识别片段"""
    return Task(
        source=msg.source,
        data=data,
        offset=offset,
        task_id=msg.task_id,
        socket_id=socket_id,
        overlap=0.0 if streaming else msg.seg_overlap,
        is_final=is_final,
        time_start=msg.time_start,
        time_submit=time.time(),
        context=msg.context,
        language=msg.language,
        streaming=streaming,
        silent=silent,
    )


def _handle_streaming(msg: AudioMessage, cache: AudioCache, data: bytes, socket_id: str, queue_in) -> None:
    """
    流式任务：在停顿处切句，每句单独提交，识别子进程用前缀续写逐句识别

    不用 seg_duration / seg_overlap。片段之间没有重叠，前文由识别子进程自己保留。
    """
    if cache.splitter is None:
        cache.splitter = PauseSplitter()
    splitter = cache.splitter

    if data:
        for n_samples, has_speech in splitter.feed(np.frombuffer(data, dtype=np.float32)):
            n_bytes = n_samples * AudioFormat.BYTES_PER_SAMPLE
            segment, cache.chunks = cache.chunks[:n_bytes], cache.chunks[n_bytes:]
            runtime_status.task_queued()
            queue_in.put(_make_task(msg, socket_id, segment, cache.offset, False,
                                    streaming=True, silent=not has_speech))
            logger.debug(f"流式切句，任务ID: {msg.task_id}, 偏移: {cache.offset:.2f}s, "
                         f"句长: {n_samples / AudioFormat.SAMPLE_RATE:.2f}s, 有语音: {has_speech}")
            cache.offset += n_samples / AudioFormat.SAMPLE_RATE

    if msg.is_final:
        # 松手前的静音尾巴不识别（会被识别成「嗯。」）。但只在已经切出过句子时才这样判断：
        # 整次录音只有一句时 VAD 的噪声底估计还不可靠，宁可照常识别，也不能把短句误判成静音丢掉
        silent = splitter.cuts > 0 and not splitter.tail_has_speech
        runtime_status.task_queued()
        queue_in.put(_make_task(msg, socket_id, cache.chunks, cache.offset, True,
                                streaming=True, silent=silent))
        logger.debug(f"提交流式最终片段，任务ID: {msg.task_id}, 共 {splitter.cuts + 1} 句, "
                     f"末句 {AudioFormat.bytes_to_seconds(len(cache.chunks)):.2f}s, 静音: {silent}")
        cache.reset()


async def message_handler(websocket, msg: AudioMessage, cache: AudioCache, app) -> None:
    """
    处理客户端发送的音频消息

    根据消息中的分段参数，将音频数据分段后提交到识别队列；流式任务改为在停顿处切句。
    """
    queue_in = app.state.queue_in

    global status_mic
    is_start = not bool(cache.chunks)
    socket_id = str(websocket.id)

    # 从消息中获取分段参数
    seg_threshold = msg.seg_duration + msg.seg_overlap * 2

    try:
        # base64 解码音频数据（float32, 16kHz, mono）
        data = b64decode(msg.data)
        cache.chunks += data
        cache.byte_count += len(data)

        if msg.streaming and msg.source == 'mic':
            if msg.is_final:
                status_mic.stop()
            else:
                status_mic.start()
            _handle_streaming(msg, cache, data, socket_id, queue_in)
            return

        if not msg.is_final:
            # 打印状态消息
            if msg.source == 'mic':
                status_mic.start()
            if msg.source == 'file' and is_start:
                console.print('正在接收音频文件...')
                logger.info(f"开始接收音频文件，任务ID: {msg.task_id}")

            # 若缓冲已达到分段阈值，将片段作为任务提交
            segment_bytes = AudioFormat.seconds_to_bytes(msg.seg_duration + msg.seg_overlap)
            stride_bytes = AudioFormat.seconds_to_bytes(msg.seg_duration)

            while cache.duration >= seg_threshold:
                segment_data = cache.chunks[:segment_bytes]
                cache.chunks = cache.chunks[stride_bytes:]

                task = _make_task(msg, socket_id, segment_data, cache.offset, False)
                cache.offset += msg.seg_duration
                runtime_status.task_queued()
                queue_in.put(task)
                logger.debug(
                    f"提交音频片段，任务ID: {msg.task_id}, "
                    f"偏移: {cache.offset}s, 缓冲区: {len(cache.chunks)} bytes"
                )

        else:  # is_final
            # 打印状态消息
            if msg.source == 'mic':
                status_mic.stop()
            elif msg.source == 'file':
                print(f'音频文件接收完毕，时长 {cache.total_duration:.2f}s')
                logger.info(f"音频文件接收完毕，任务ID: {msg.task_id}, 时长: {cache.total_duration:.2f}s")

            # 提交最终片段
            task = _make_task(msg, socket_id, cache.chunks, cache.offset, True)
            runtime_status.task_queued()
            queue_in.put(task)
            logger.debug(f"提交最终片段，任务ID: {msg.task_id}, 数据大小: {len(cache.chunks)} bytes")

            # 重置缓冲区
            cache.reset()

    except Exception as e:
        logger.error(f"音频数据处理错误，任务ID: {msg.task_id}: {e}", exc_info=True)
        raise


async def ws_recv(websocket, app) -> None:
    """
    WebSocket 接收主函数

    处理单个客户端连接，接收音频数据并分发处理。
    """
    global status_mic

    # 登记 socket 到连接池
    state = app.state
    sockets = state.sockets
    sockets_id = state.sockets_id
    socket_id = str(websocket.id)
    sockets[socket_id] = websocket
    sockets_id.append(socket_id)
    remote = websocket.remote_address
    console.print(f'[bold green]客户端已连接: {remote[0]}:{remote[1]}[/bold green]\n')
    logger.info(f"新客户端连接: {websocket}, ID: {socket_id}")

    # 创建音频缓冲区
    cache = AudioCache()

    # 接收并处理消息
    try:
        async for raw_message in websocket:
            # 使用协议类解析消息
            try:
                data = json.loads(raw_message)
                msg = AudioMessage.from_dict(data)
                # 处理音频数据
                await message_handler(websocket, msg, cache, app)
            except Exception as e:
                logger.error(f"消息解析失败: {str(e)}")
                continue

        logger.info(f"客户端正常关闭连接: {socket_id}")

    except websockets.ConnectionClosed:
        console.print("ConnectionClosed...")
        logger.warning(f"客户端连接已关闭: {socket_id}")
    except websockets.InvalidState:
        console.print("InvalidState...")
        logger.error(f"WebSocket 状态异常: {socket_id}")
    except Exception as e:
        console.print("Exception:", e)
        logger.error(f"WebSocket 接收异常，客户端ID {socket_id}: {e}", exc_info=True)
    finally:
        # 清理资源
        status_mic.stop()
        status_mic.on = False
        sockets.pop(socket_id, None)
        if socket_id in sockets_id:
            sockets_id.remove(socket_id)

        console.print(f'[bold red]客户端已断开: {remote[0]}:{remote[1]}[/bold red]\n')

        # 注意：session 清理由 TaskHandler 在子进程中定期执行
        # （通过检查 sockets_id 判断客户端是否已断开）
        logger.debug(f"客户端资源已清理: {socket_id}")
