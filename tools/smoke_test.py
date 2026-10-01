# coding: utf-8
"""
服务端冒烟测试：模拟 CapsWriter-Client 把一段音频发给服务端，打印识别结果。

只依赖 Python 标准库（3.8+），所以 Windows 上用任意系统 Python、Linux 上用系统
python3 或项目环境都能跑，不需要装项目依赖。协议与 core/protocol.py 保持一致。

用法（先启动服务端）：
    python tools/smoke_test.py                          # 默认发 tools/assets/demo.wav，mic 模式
    python tools/smoke_test.py --source file            # 文件转录模式（会触发对齐模型）
    python tools/smoke_test.py --seg-duration 6 --seg-overlap 2   # 强制多分段，测试拼接
    python tools/smoke_test.py --streaming              # 流式上屏：按真实时间逐块发送，打印逐句增量
    python tools/smoke_test.py --host 10.0.0.2 --port 6000 --audio x.mp3   # 非 wav 需要 ffmpeg

退出码：0 = 收到非空最终结果（且满足 --expect），1 = 失败。
"""

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
import wave
from array import array

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_AUDIO = os.path.join(BASE_DIR, 'tools', 'assets', 'demo.wav')
SAMPLE_RATE = 16000


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端（RFC 6455，仅实现本测试需要的部分）
# ---------------------------------------------------------------------------

class MiniWebSocket:
    def __init__(self, host, port, timeout):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f'GET / HTTP/1.1\r\n'
            f'Host: {host}:{port}\r\n'
            f'Upgrade: websocket\r\n'
            f'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            f'Sec-WebSocket-Version: 13\r\n'
            f'Sec-WebSocket-Protocol: binary\r\n\r\n'
        )
        self.sock.sendall(request.encode())
        header = b''
        while b'\r\n\r\n' not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError('握手时连接被关闭')
            header += chunk
        status_line = header.split(b'\r\n', 1)[0].decode(errors='replace')
        if ' 101 ' not in status_line:
            raise ConnectionError(f'握手失败: {status_line}')
        self._buffer = header.split(b'\r\n\r\n', 1)[1]
        self._send_lock = threading.Lock()     # 流式测试时发送线程与回应 ping 的接收线程会同时发

    def _recv_exact(self, n):
        while len(self._buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('连接被关闭')
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack('!H', n)
        else:
            header.append(0x80 | 127)
            header += struct.pack('!Q', n)
        mask = os.urandom(4)
        header += mask
        with self._send_lock:
            self.sock.sendall(bytes(header) + _xor_mask(payload, mask))

    def send_text(self, text):
        self._send_frame(0x1, text.encode('utf-8'))

    def recv_text(self):
        """返回一条完整文本消息；自动回应 ping，收到 close 返回 None。"""
        fragments = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack('!H', self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._recv_exact(8))[0]
            if b2 & 0x80:
                mask = self._recv_exact(4)
                payload = _xor_mask(self._recv_exact(n), mask)
            else:
                payload = self._recv_exact(n)
            if opcode == 0x9:                       # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                       # pong
                continue
            if opcode == 0x8:                       # close
                return None
            fragments.append(payload)
            if fin:
                return b''.join(fragments).decode('utf-8')

    def close(self):
        try:
            self._send_frame(0x8, struct.pack('!H', 1000))
        except OSError:
            pass
        self.sock.close()


def _xor_mask(data, mask):
    n = len(data)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, 'little') ^ int.from_bytes(key, 'little')).to_bytes(n, 'little')


# ---------------------------------------------------------------------------
# 音频读取：统一转成 16kHz 单声道 float32 字节流（服务端要求的格式）
# ---------------------------------------------------------------------------

def load_audio_f32(path):
    if path.lower().endswith('.wav'):
        with wave.open(path, 'rb') as w:
            if w.getsampwidth() != 2:
                raise ValueError('仅支持 16-bit PCM wav；其他格式请装 ffmpeg 并直接传原文件')
            channels, rate = w.getnchannels(), w.getframerate()
            pcm = array('h')
            pcm.frombytes(w.readframes(w.getnframes()))
        if sys.byteorder == 'big':
            pcm.byteswap()
        samples = [pcm[i] / 32768.0 for i in range(0, len(pcm), channels)]
        if rate != SAMPLE_RATE:
            samples = _resample(samples, rate, SAMPLE_RATE)
        return array('f', samples).tobytes()

    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('非 wav 音频需要 ffmpeg 在 PATH 中')
    return subprocess.run(
        [ffmpeg, '-v', 'error', '-i', path, '-f', 'f32le', '-ac', '1', '-ar', str(SAMPLE_RATE), '-'],
        check=True, stdout=subprocess.PIPE,
    ).stdout


def _resample(samples, src, dst):
    n_out = int(len(samples) * dst / src)
    ratio = src / dst
    out = []
    for i in range(n_out):
        pos = i * ratio
        j = int(pos)
        frac = pos - j
        nxt = samples[j + 1] if j + 1 < len(samples) else samples[j]
        out.append(samples[j] * (1 - frac) + nxt * frac)
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def default_port():
    try:
        sys.path.insert(0, BASE_DIR)
        from config_server import ServerConfig
        return int(ServerConfig.port)
    except Exception:
        return 6000


def main():
    parser = argparse.ArgumentParser(description='CapsWriter-Server 冒烟测试')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=None, help='默认读取 config_server.py')
    parser.add_argument('--audio', default=DEFAULT_AUDIO)
    parser.add_argument('--source', choices=['mic', 'file'], default='mic')
    parser.add_argument('--seg-duration', type=float, default=60.0, help='与客户端默认一致：60s')
    parser.add_argument('--seg-overlap', type=float, default=4.0, help='与客户端默认一致：4s')
    parser.add_argument('--chunk', type=float, default=None,
                        help='每条消息携带的音频秒数（默认 1s；--streaming 时 0.05s，与客户端一致）')
    parser.add_argument('--streaming', action='store_true',
                        help='流式上屏任务：按真实时间发送，检查各条 delta 拼起来等于最终文本')
    parser.add_argument('--context', default='')
    parser.add_argument('--language', default='auto')
    parser.add_argument('--timeout', type=float, default=120.0, help='等待最终结果的秒数')
    parser.add_argument('--expect', default='', help='最终文本必须包含的子串')
    parser.add_argument('--json', action='store_true', help='打印完整的最终结果 JSON')
    args = parser.parse_args()
    port = args.port or default_port()

    audio = load_audio_f32(args.audio)
    duration = len(audio) / 4 / SAMPLE_RATE
    print(f'音频: {args.audio} ({duration:.2f}s)  ->  ws://{args.host}:{port}  source={args.source}')

    ws = MiniWebSocket(args.host, port, timeout=args.timeout)
    task_id = str(uuid.uuid4())
    t0 = time.time()
    chunk_sec = args.chunk or (0.05 if args.streaming else 1.0)
    chunk_bytes = int(chunk_sec * SAMPLE_RATE) * 4
    t_sent = None

    def send(data, is_final):
        ws.send_text(json.dumps({
            'task_id': task_id, 'source': args.source,
            'data': base64.b64encode(data).decode(), 'is_final': is_final,
            'time_start': t0, 'seg_duration': args.seg_duration, 'seg_overlap': args.seg_overlap,
            'context': args.context, 'language': args.language, 'streaming': args.streaming,
        }, ensure_ascii=False))

    def send_all():
        nonlocal t_sent
        for n, i in enumerate(range(0, len(audio), chunk_bytes)):
            if args.streaming:
                # 按真实时间发送：模拟边录边发，切句与逐句回传才有意义
                time.sleep(max(0.0, t0 + n * chunk_sec - time.time()))
            send(audio[i:i + chunk_bytes], False)
        send(b'', True)
        t_sent = time.time()

    sender = threading.Thread(target=send_all, daemon=True)
    sender.start()

    final = None
    deltas = []
    while final is None:
        raw = ws.recv_text()
        if raw is None:
            break
        msg = json.loads(raw)
        if msg.get('task_id') != task_id:
            continue
        deltas.append(msg.get('delta', ''))
        if msg.get('is_final'):
            final = msg
        elif args.streaming:
            print(f'  +{time.time() - t0:5.2f}s 新增: {msg.get("delta", "")!r}')
        else:
            print(f'  中间结果 ({msg.get("duration", 0):.2f}s): {msg.get("text", "")}')
    sender.join()
    ws.close()

    if final is None:
        print('失败：连接关闭前未收到最终结果')
        return 1

    text = final.get('text', '')
    stamps = final.get('timestamps') or []
    if args.streaming:
        print(f'  +{time.time() - t0:5.2f}s 新增（最终）: {final.get("delta", "")!r}')
    print(f'最终结果: {text}')
    print(f'时长 {final.get("duration", 0):.2f}s，发送完成后 {time.time() - t_sent:.2f}s 收到；'
          f'tokens={len(final.get("tokens") or [])}，'
          f'时间戳范围={(f"{stamps[0]:.2f}~{stamps[-1]:.2f}s" if stamps else "无")}')
    if args.json:
        print(json.dumps(final, ensure_ascii=False, indent=2))

    if not text.strip():
        print('失败：最终文本为空')
        return 1
    if args.streaming:
        if len(deltas) < 2:
            print('失败：没有收到逐句增量（服务端可能不支持流式，或音频里没有足够长的停顿）')
            return 1
        if ''.join(deltas) != text:
            print(f'失败：各条 delta 拼起来与最终文本不一致：{"".join(deltas)!r}')
            return 1
    if args.expect and args.expect not in text:
        print(f'失败：最终文本不包含 {args.expect!r}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
