"""Windows 开发机的真实 WebSocket/文件事件测试，临时数据均在服务端 logs 内。"""

import asyncio
import ast
import importlib.util
import logging
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT.parent / 'CapsWriter-Client'


def package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    module.logger = logging.getLogger('transport-test')
    sys.modules[name] = module


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


try:
    import websockets
except ImportError:
    # 系统 Python 3.13 可直接加载服务端随附的 3.13 字节码，无需安装依赖。
    websockets = load('websockets', ROOT / 'internal/websockets/__init__.pyc')
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

package('core', ROOT / 'core')
package('core.server', ROOT / 'core/server')
package('core.client', CLIENT / 'core/client')
package('core.client.hotword', CLIENT / 'core/client/hotword')
common = load('core.hotword_sync', ROOT / 'core/hotword_sync.py')
server_module = load('core.server.hotword_sync', ROOT / 'core/server/hotword_sync.py')
client_module = load('core.client.hotword.sync', CLIENT / 'core/client/hotword/sync.py')
tree = ast.parse((CLIENT / 'core/client/hotword/manager.py').read_text(encoding='utf-8'))
node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == '_HotwordFileHandler')
namespace = dict(FileSystemEventHandler=FileSystemEventHandler, Path=Path, threading=threading,
                 CLIENT_FILES=common.CLIENT_FILES, logger=logging.getLogger('transport-test'))
exec(compile(ast.Module(body=[node], type_ignores=[]), '<watcher>', 'exec'), namespace)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'logs')
        self.root = Path(self.temp.name)
        self.servers, self.clients, self.watchers = [], [], []

    async def asyncTearDown(self):
        for observer, handler in self.watchers:
            handler.stop()
            observer.stop()
            observer.join()
        for client in self.clients:
            await client.shutdown()
        for listener in self.servers:
            listener.close()
            await listener.wait_closed()
        self.temp.cleanup()

    async def server(self, name):
        service = server_module.HotwordSyncServer(self.root / name, '', self.root / 'missing')
        listener = await websockets.serve(service.handle, '127.0.0.1', 0, subprotocols=['binary'])
        self.servers.append(listener)
        return service, 'ws://127.0.0.1:' + str(listener.sockets[0].getsockname()[1])

    async def client(self, name, url):
        app = types.SimpleNamespace(base_dir=self.root / name, loop=asyncio.get_running_loop())
        service = client_module.HotwordSyncClient(app)
        service.notices = []
        service._notify = service.notices.append
        manager = types.SimpleNamespace(
            files={key: app.base_dir / filename for key, filename in zip(('hot', 'rule'), common.CLIENT_FILES)},
            _load_hot=lambda: None, _load_rule=lambda: None, synchronizer=service)
        handler = namespace['_HotwordFileHandler'](manager)
        observer = Observer()
        for directory in (app.base_dir, service.directory):
            observer.schedule(handler, str(directory), recursive=False)
        observer.start()
        self.watchers.append((observer, handler))
        self.clients.append(service)
        service.connect(url)
        await self.until(lambda: len(service.state['base']) == 2)
        return service

    async def until(self, predicate):
        for _ in range(160):
            if predicate():
                return
            await asyncio.sleep(0.05)
        self.fail('同步条件等待超时')

    async def test_event_broadcast_conflict_resolution_and_restart(self):
        server, url = await self.server('server')
        first = await self.client('first', url)
        second = await self.client('second', url)
        common.atomic_write(first.root / 'hot.txt', 'Claude | 克劳德\n')
        common.atomic_write(first.root / 'hot-rule.txt', 'foo = base\n')
        await self.until(lambda: second._formal('hot-rule.txt') == 'foo = base\n' and
                         second._formal('hot.txt') == 'Claude | 克劳德\n')
        await second.shutdown()
        common.atomic_write(first.root / 'hot-rule.txt', 'foo = remote\n')
        common.atomic_write(second.root / 'hot-rule.txt', 'foo = local\n')
        await self.until(lambda: server.state['files']['hot-rule.txt'] == 'foo = remote\n')
        second.connect(url)
        path = second.conflict_path('hot-rule.txt')
        await self.until(path.exists)
        self.assertTrue(second.notices)
        common.atomic_write(second.root / 'hot-rule.txt', 'foo = local\nbar = added\n')
        common.atomic_write(path, common.CONFLICT_HEADER + 'foo = resolved\n')
        await self.until(lambda: not path.exists() and first._formal('hot-rule.txt') == 'foo = resolved\nbar = added\n')
        self.assertEqual(second._formal('hot-rule.txt'), 'foo = resolved\nbar = added\n')

    async def test_switch_a_b_a_preserves_deletion_without_git(self):
        first, url_a = await self.server('a')
        second, url_b = await self.server('b')
        client = await self.client('client', url_a)
        common.atomic_write(client.root / 'hot.txt', 'X\n')
        await self.until(lambda: first.state['files']['hot.txt'] == 'X\n' and client.state['base']['hot.txt'] == 'X\n')
        client.connect(url_b)
        await self.until(lambda: second.state['files']['hot.txt'] == 'X\n' and
                         client.state['anchors']['hot.txt']['server'] == second.state['server_id'])
        common.atomic_write(client.root / 'hot.txt', '')
        await self.until(lambda: second.state['files']['hot.txt'] == '' and client.state['base']['hot.txt'] == '')
        client.connect(url_a)
        await self.until(lambda: first.state['files']['hot.txt'] == '' and
                         client.state['anchors']['hot.txt']['server'] == first.state['server_id'])
        self.assertEqual(client._formal('hot.txt'), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
