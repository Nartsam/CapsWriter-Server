"""角色请求整段识别的标准库协议探针；先启动独立端口的测试服务。

python tools/smoke_test_revisable.py --port 6100 --action request_full --when early
python tools/smoke_test_revisable.py --port 6100 --action commit_stream --when decision
python tools/smoke_test_revisable.py --port 6100 --action cancel --when decision
"""

import argparse
import base64
import json
import socket
import threading
import time
import uuid

from smoke_test import MiniWebSocket, load_audio_f32, DEFAULT_AUDIO


def main():
    parser = argparse.ArgumentParser(description='流式预览后决策的真实协议测试')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=6100)
    parser.add_argument('--audio', default=DEFAULT_AUDIO)
    parser.add_argument('--action', choices=['request_full', 'commit_stream', 'cancel'], default='request_full')
    parser.add_argument('--when', choices=['early', 'decision'], default='decision')
    parser.add_argument('--seg-duration', type=float, default=60)
    parser.add_argument('--seg-overlap', type=float, default=4)
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    audio = load_audio_f32(args.audio)
    ws = MiniWebSocket(args.host, args.port, args.timeout)
    task_id, started = str(uuid.uuid4()), time.time()
    sent_bytes, sent_end = 0, 0.0
    errors = []

    def control(action):
        ws.send_text(json.dumps(dict(type='recognition_control', task_id=task_id, action=action)))

    def send(data, final):
        ws.send_text(json.dumps(dict(
            task_id=task_id, source='mic', data=base64.b64encode(data).decode(),
            is_final=final, time_start=started, seg_duration=args.seg_duration,
            seg_overlap=args.seg_overlap, streaming=True, revisable=True,
            context='', language='auto')))

    def sender():
        nonlocal sent_bytes, sent_end
        try:
            for index, offset in enumerate(range(0, len(audio), 3200)):
                time.sleep(max(0, started + index * .05 - time.time()))
                piece = audio[offset:offset + 3200]
                send(piece, False)
                sent_bytes += len(piece)
                if index == 0 and args.when == 'early':
                    if args.action == 'cancel':
                        send(b'', True)
                        control('cancel')
                        sent_end = time.time()
                        return
                    control(args.action)
                    control(args.action)  # 重复决定不能启动第二次普通识别。
            send(b'', True)
            sent_end = time.time()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=sender, daemon=True)
    thread.start()
    messages, final, decision = [], None, None
    try:
        while final is None:
            raw = ws.recv_text()
            if raw is None:
                raise RuntimeError('终局前连接断开')
            msg = json.loads(raw)
            if msg.get('task_id') != task_id:
                continue
            messages.append(msg)
            assert msg['recognition_mode'] in ('stream', 'full'), msg
            assert msg['phase'] in ('partial', 'decision', 'final'), msg
            assert msg['is_final'] == (msg['phase'] == 'final'), msg
            print(f"{msg['recognition_mode']}/{msg['phase']}: {msg['text']}")
            if msg['phase'] == 'decision':
                assert args.when == 'decision' and decision is None
                assert msg['recognition_mode'] == 'stream' and msg['status'] == 'ok'
                decision = msg
                control(args.action)
                control(args.action)
            elif msg['phase'] == 'final':
                final = msg
        thread.join(timeout=args.timeout)
        assert not thread.is_alive() and not errors, errors
        assert final['time_start'] == started
        assert abs(final['duration'] - sent_bytes / 64000) < 1e-6
        if args.action == 'cancel':
            assert final['status'] == 'cancelled' and final['text'] == final['delta'] == ''
        else:
            assert final['status'] == 'ok' and final['text'].strip(), final
            if args.action == 'request_full':
                assert final['recognition_mode'] == 'full' and final['delta'] == ''
                assert final['time_submit'] >= sent_end - .1
                assert all(not m['delta'] for m in messages if m['recognition_mode'] == 'full')
                first_full = next(i for i, m in enumerate(messages) if m['recognition_mode'] == 'full')
                assert all(m['recognition_mode'] == 'full' for m in messages[first_full:])
            else:
                assert final['recognition_mode'] == 'stream'
                assert ''.join(m['delta'] for m in messages if m['phase'] != 'decision') == final['text']
                if decision:
                    assert (final['text'], final['delta']) == (decision['text'], decision['delta'])
        # 终局后重发决定和 cancel，连接上不应再出现本任务的第二个终局。
        control(args.action)
        control('cancel')
        ws.sock.settimeout(.5)
        try:
            duplicate = ws.recv_text()
            assert duplicate is None, duplicate
        except socket.timeout:
            pass
        print(f"通过：{args.action}/{args.when}，{final['duration']:.2f}s，唯一终局 {final['status']}")
        return 0
    finally:
        ws.close()


if __name__ == '__main__':
    raise SystemExit(main())
