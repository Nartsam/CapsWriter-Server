# coding: utf-8
"""
CapsWriter Offline 服务端主程序门面类 (Facade)

采用外观模式统一管理进程管理器 (ProcessManager) 和网络管理器 (SocketManager)。
该类是整个服务端应用的中心指挥部，负责初始化生命周期、托盘图标、
并协调子进程与 WebSocket 服务的启动与退出。
"""

import os
import sys
import asyncio
import threading
from pathlib import Path
from config_server import ServerConfig as Config, __version__
from .state import ServerState, console
from core.tools.signal_handler import register_signal
from .worker.process_manager import ProcessManager
from .worker.check_model import check_model
from .connection.server_manager import SocketManager
from .ui.tray_manager import TrayManager
from . import launch
from . import runtime_status
from . import logger

class CapsWriterServer:
    """
    CapsWriter 服务端外观类
    
    管理的外部接口极其简洁：start()；托盘菜单另外会调用 stop() / restart()，
    识别子进程中途退出时 ProcessManager 会调用 abort()。
    """
    def __init__(self):
        # 重启时要用同一条命令再启动一次；非冻结模式下 argv[0] 可能是相对路径，必须在切换目录前记下
        self._launch_cmd = launch.current_command()
        self._restart_requested = False
        self._exit_code = 0                 # abort() 置为 1，由 start() 末尾交给 _finish()
        self._stopped = threading.Event()   # stop() 全部清理完成后置位

        # 确保正确的工作目录
        self.base_dir = Path(__file__).parents[2]
        os.chdir(self.base_dir)

        # 初始化事件循环
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # 初始化状态容器
        self.state = ServerState(app=self)

        # 基本配置与组件实例化
        self.process_manager = ProcessManager(self)
        self.socket_manager = SocketManager(self)
        self.tray_manager = TrayManager(self)

        self.version = __version__
        self.is_alive = False
        self.hotword_sync = None


    def _print_banner(self):
        """打印启动信息"""
        console.line(2)
        console.rule('[bold #d55252]CapsWriter Offline Server[/]'); console.line()
        console.print(f'版本：[bold green]{self.version}[/]', end='\n\n')
        console.print(f'项目地址：[cyan underline]https://github.com/HaujetZhao/CapsWriter-Offline', end='\n\n')
        console.print(f'当前基文件夹：[cyan underline]{self.base_dir}[/]', end='\n\n')
        console.print(f'绑定的服务地址：[cyan underline]{Config.addr}:{Config.port}[/]', end='\n\n')

    def stop(self):
        """
        清理服务端资源
        """
        # 防连续触发
        if not self.is_alive: return
        self.is_alive = False 

        if self.hotword_sync is not None:
            self.hotword_sync.close()

        logger.info("=" * 50)
        logger.info("开始清理服务端资源...")

        self.state.queue_out.put(None)

        # 1. 关闭 WebSocket 服务（立即释放端口）
        self.socket_manager.stop()

        # 2. 终止识别子进程
        self.process_manager.stop()

        # 3. 停止托盘图标
        self.tray_manager.stop()

        # 4. 最后停止协程（需在其他资源释放之后）。
        # stop() 常在托盘线程、子进程监视线程里调用，直接 loop.stop() 不会唤醒阻塞中的事件循环
        self.loop.call_soon_threadsafe(self.loop.stop)

        logger.info("服务端资源清理完成")
        self._stopped.set()
        if not self._restart_requested and not self._exit_code:
            console.print('[green4]再见！')

    def abort(self):
        """
        无法继续服务时（识别子进程中途退出）：做与“退出”相同的清理，然后以退出码 1 结束，
        由托管程序决定是否重启。双击运行时没有托管程序，托盘图标消失即表示服务已停止。
        """
        if not self.is_alive: return
        self._exit_code = 1
        self.stop()

    def restart(self):
        """
        重启服务端（托盘菜单“重启”），效果等同于“退出”后再手动启动一次。

        不能像客户端那样先拉起新进程再退出：新进程启动时会检查端口，而旧进程此时还占着
        端口和显存。所以这里只做与“退出”完全相同的清理，新进程由 start() 在主循环结束、
        端口与识别子进程都已释放之后拉起（见 _finish）。
        """
        if not self.is_alive: return
        logger.info("收到重启请求：先完整停止服务端，再以相同命令行启动新实例")
        self._restart_requested = True
        self.stop()

    def _finish(self, exit_code: int):
        """start() 的唯一出口：如有重启请求，此时资源已全部释放，拉起新实例后退出"""
        if self.hotword_sync is not None:
            self.hotword_sync.close()
            self.hotword_sync.sync_remote()
        if self._restart_requested:
            # stop() 在托盘线程里执行，它的第一步就会让主循环结束；
            # 必须等识别子进程真正退出（显存释放）后再拉起新实例
            if not self._stopped.wait(timeout=30):
                logger.warning("等待资源清理超时，仍继续重启")
            try:
                launch.spawn_new_instance(self._launch_cmd)
                logger.info(f"已启动新的服务端实例：{self._launch_cmd}")
                console.print('[green4]正在重启...')
                exit_code = 0
            except Exception as e:
                logger.error(f"重启失败，无法启动新实例（{self._launch_cmd}）：{e}", exc_info=True)
                console.print(f'[bold red]重启失败：{e}[/bold red]')
                launch.pause_before_exit()
                exit_code = 1
        sys.exit(exit_code)


    def start(self):
        """
        同步启动服务端 (主入口)

        Windows 的 start_server.exe 与 Linux 的 start_server.py 都只调用这一个方法，
        命令行参数与环境变量也在这里处理，保证两个入口行为一致。

        注册信号处理、拉起子进程并进入网络服务监听循环。
        """
        # 防连续触发
        if self.is_alive: return

        # 命令行参数 / 环境变量覆盖配置（见 launch.py）
        args = launch.parse_args()
        launch.export_args(args)
        launch.apply_overrides()

        if args.download_models:
            from .model_download import download_models
            sys.exit(0 if download_models() else 1)

        self.is_alive = True

        # 注册退出信号处理
        register_signal(self.stop)

        self._print_banner()

        # 先确认端口可用、模型齐全，再花时间加载模型。
        # 这两步都可能需要用户看提示/回答问题，而托盘会隐藏控制台窗口，所以必须在托盘之前。
        if not self.socket_manager.check_port_available():
            self.is_alive = False
            launch.pause_before_exit()
            sys.exit(1)
        check_model()

        # 远端失败只降级到本地；先同步服务端热词，再加载识别模型。
        try:
            from config_server import HotwordSync, Qwen3ASRGGUFArgs
            from .hotword_sync import HotwordSyncServer
            self.hotword_sync = HotwordSyncServer(
                self.base_dir, HotwordSync.repository,
                self.base_dir / HotwordSync.token_file, Qwen3ASRGGUFArgs.hotword_bonus)
            self.hotword_sync.sync_remote()
        except Exception as error:
            logger.warning('热词同步初始化失败，继续使用本机词库：%s', error)

        if not self.is_alive:
            self._finish(self._exit_code)

        # 托盘图标
        self.tray_manager.start()

        runtime_status.initialize()

        # 拉起识别子进程（阻塞到模型加载完成）
        self.process_manager.start()
        if not self.is_alive:   # 子进程加载模型失败，或加载期间被要求退出/重启，已触发 stop()
            self._finish(1)

        # 开启网络服务监听 (接管当前线程直至退出)
        try:
            self.loop.run_until_complete(self.socket_manager.start())
        except RuntimeError:
            pass

        self._finish(self._exit_code)
