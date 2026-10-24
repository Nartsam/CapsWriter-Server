"""热词同步服务。客户端只连接本服务，远端 Git 仅在启动、退出时访问。"""

import asyncio
import json
import threading
import uuid
from pathlib import Path

from core.hotword_sync import (CLIENT_FILES, FILES, VERSION, atomic_write,
                              merge, read_text, revision, save_json, validate)
from . import logger


class HotwordSyncServer:
    def __init__(self, root, repository, token_file, default_bonus=4.0):
        self.root = Path(root)
        self.directory = self.root / 'logs' / 'hotword_sync'
        self.path = self.directory / 'server.json'
        self.lock = threading.RLock()
        self.peers = set()
        self.accepting = True
        self.default_bonus = default_bonus
        self.repository = repository
        self.token_file = token_file
        self.git = None
        if self.path.exists():
            self.state = json.loads(read_text(self.path))
        else:
            self.state = {'files': {}, 'remote_base': {}, 'pending': {}, 'repository': repository}
        if self.state.get('repository') != repository:
            # 更换仓库保留本机词库，新的远端没有共同祖先，按首次同步合并。
            self.state.update(repository=repository, remote_base={}, pending={}, remote_anchor={})
        for name in FILES:
            self.state['files'].setdefault(name, '')
        self.state.setdefault('server_id', str(uuid.uuid4()))
        self.state.setdefault('remote_anchor', {})
        self._read_server_words()
        self._save()

    def _read_server_words(self):
        path = self.root / 'hot-server.txt'
        if path.exists():
            text = read_text(path)
            validate('hot-server.txt', text)
            self.state['files']['hot-server.txt'] = text
        else:
            atomic_write(path, self.state['files']['hot-server.txt'])

    def _save(self):
        save_json(self.path, self.state)

    def close(self):
        with self.lock:
            self.accepting = False

    def sync_remote(self):
        if not self.repository:
            return
        from .hotword_git import HotwordGit
        try:
            with self.lock:
                self._read_server_words()
                self._save()
                remote = HotwordGit(self.directory, self.repository, self.token_file)
                self.git = remote
                # 非快进表示另一服务端刚刚推送，重新获取并语义合并，绝不强推。
                for attempt in range(3):
                    latest = remote.fetch()
                    # Git 网络请求期间用户可能编辑根目录热词，合并必须使用保存后的内容。
                    self._read_server_words()
                    previous = json.loads(json.dumps(self.state))
                    self.state['remote_anchor'] = {'commit': remote.parent, 'files': latest}
                    for name in FILES:
                        current = self.state['files'][name]
                        incoming = latest.get(name, '')
                        base = self.state['remote_base'].get(name)
                        text, conflict = merge(name, base, current, incoming, self.default_bonus)
                        if conflict:
                            self.state['pending'][name] = {
                                'base': base, 'remote': incoming,
                                'token': revision(incoming),
                            }
                        else:
                            self.state['files'][name] = text
                            self.state['remote_base'][name] = incoming
                            self.state['pending'].pop(name, None)
                    try:
                        atomic_write(self.root / 'hot-server.txt', self.state['files']['hot-server.txt'])
                    except Exception:
                        # 正式文件未应用成功时不能前移基线；随后客户端 ACK 也不能保存这份中间状态。
                        self.state = previous
                        raise
                    self._save()
                    if self.state['pending']:
                        logger.warning('远端热词存在规则冲突，将交给连接的客户端处理；本机词库继续使用')
                        return
                    if remote.push(self.state['files']):
                        self.state['remote_base'] = dict(self.state['files'])
                        self.state['remote_anchor'] = {'commit': remote.parent, 'files': dict(self.state['files'])}
                        self._save()
                        logger.info('热词远端同步完成')
                        return
                logger.warning('热词远端连续发生并发提交，已保留本地内容，下次启动或退出重试')
        except Exception as error:
            # 凭证、网络、Git 缺失都不影响识别，也不丢弃尚未上传的词库。
            logger.warning('热词远端同步未完成（%s），本地内容已保留', error)

    def exchange(self, request):
        """一次单文件三方合并；持久化完成才返回 ack，调用时没有异步让出点。"""
        name = request['name']
        if name not in CLIENT_FILES:
            raise ValueError('客户端只能同步 hot.txt 和 hot-rule.txt')
        local, base = request['text'], request.get('base')
        validate(name, local)
        if base is not None:
            validate(name, base)
        with self.lock:
            if not self.accepting:
                raise ValueError('服务端正在退出，请重连后同步')
            current = self.state['files'][name]
            anchor = self._anchor(name)
            previous_anchor = request.get('anchor')
            if previous_anchor and (previous_anchor.get('server') != self.state['server_id'] or
                                    previous_anchor.get('repository') != self.repository):
                # 切换到尚未收到新词库的服务端时，其旧版本不能被误判为主动删除。
                # 两端远端提交在同一条 Git 历史上，取较旧提交的文件作为共同基线。
                base = None
                if previous_anchor.get('repository') == self.repository:
                    old, new = previous_anchor.get('commit'), anchor['commit']
                    if old and old == new:
                        base = anchor['text']
                    elif old and new and self.git:
                        base = self.git.common_text(old, new, name)
            text, conflict = merge(name, base, local, current)
            pending = self.state['pending'].get(name)
            token = ''
            if pending and request.get('resolve') != pending['token'] and not conflict:
                text, conflict = merge(name, pending['base'], text, pending['remote'])
                token = pending['token']
            if conflict:
                return {'type': 'conflict', 'name': name, 'text': text,
                        'base': current, 'resolve': token, 'anchor': anchor}, False
            previous = json.loads(json.dumps(self.state))
            self.state['files'][name] = text
            if pending:
                self.state['remote_base'][name] = pending['remote']
                self.state['pending'].pop(name, None)
            try:
                self._save()
            except Exception:
                self.state = previous
                raise
            return {'type': 'ack', 'name': name, 'text': text, 'revision': revision(text),
                    'anchor': anchor}, text != current

    def _anchor(self, name):
        remote = self.state['remote_anchor']
        return {'server': self.state['server_id'], 'repository': self.repository,
                'commit': remote.get('commit', ''), 'text': remote.get('files', {}).get(name)}

    async def handle(self, websocket):
        self.peers.add(websocket)
        try:
            await websocket.send(json.dumps({'type': 'hello', 'version': VERSION,
                                             'server': self.state['server_id'], 'repository': self.repository}))
            async for raw in websocket:
                try:
                    request = json.loads(raw)
                    if request.get('version') != VERSION or request.get('type') != 'sync':
                        raise ValueError('热词同步协议版本不兼容')
                    response, changed = self.exchange(request)
                except (ValueError, KeyError, TypeError, OSError) as error:
                    response, changed = {'type': 'error', 'message': str(error)}, False
                await websocket.send(json.dumps(response, ensure_ascii=False))
                if changed:
                    # 广播只要求重新取本地快照，避免覆盖其他客户端正在编辑的内容。
                    messages = [peer.send(json.dumps({'type': 'changed', 'name': request['name']}))
                                for peer in self.peers if peer is not websocket]
                    if messages:
                        await asyncio.gather(*messages, return_exceptions=True)
        except Exception as error:
            logger.debug('热词同步连接结束：%s', error)
        finally:
            self.peers.discard(websocket)
