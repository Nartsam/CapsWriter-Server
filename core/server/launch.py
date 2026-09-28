# coding: utf-8
"""
启动参数：命令行 / 环境变量 → 覆盖 config_server.py 的默认值

两个入口最终都执行 CapsWriterServer().start()（Windows：start_server.exe 的内嵌脚本；
Linux：start_server.sh → start_server.py），start() 第一步就调用这里，
因此两边支持完全相同的参数与环境变量：

  命令行参数           环境变量                    作用
  --host ADDR         CAPSWRITER_HOST             监听地址
  --port N            CAPSWRITER_PORT             监听端口
  --gpu N             CAPSWRITER_GPU              只使用第 N 张 GPU（nvidia-smi 编号）
                      CAPSWRITER_ONNX_PROVIDER    覆盖 ONNX 推理后端：CPU / CUDA / DML
  --download-models                               下载/补全模型后退出

优先级：命令行 > 环境变量 > config_server.py。

命令行参数会写回对应的环境变量。识别子进程在 Windows 上以 spawn 方式启动、会重新
import config_server，所以子进程里也要调用一次 apply_overrides()（见 worker/__init__.py），
环境变量随进程继承，两个进程得到的配置因此一致。
"""

import argparse
import os
import sys

ENV_HOST = 'CAPSWRITER_HOST'
ENV_PORT = 'CAPSWRITER_PORT'
ENV_GPU = 'CAPSWRITER_GPU'
ENV_ONNX_PROVIDER = 'CAPSWRITER_ONNX_PROVIDER'

ONNX_PROVIDERS = ('CPU', 'CUDA', 'DML', 'TRT', 'TENSORRT')


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='CapsWriter-Server 语音识别服务端（参数也可用环境变量指定，见 core/server/launch.py）',
    )
    parser.add_argument('--host', help=f'监听地址，默认读 config_server.py（环境变量 {ENV_HOST}）')
    parser.add_argument('--port', help=f'监听端口，默认读 config_server.py（环境变量 {ENV_PORT}）')
    parser.add_argument('--gpu', help=f'只使用第 N 张 GPU（环境变量 {ENV_GPU}）')
    parser.add_argument('--download-models', action='store_true', help='下载/补全模型后退出')
    return parser.parse_args(argv)


def export_args(args: argparse.Namespace) -> None:
    """把命令行参数写回环境变量，保证子进程也能看到"""
    for value, env in ((args.host, ENV_HOST), (args.port, ENV_PORT), (args.gpu, ENV_GPU)):
        if value is not None:
            os.environ[env] = str(value)


def apply_overrides() -> None:
    """
    读取环境变量覆盖 config_server 中的配置（主进程与识别子进程各调用一次）。

    非法值直接报错退出，而不是带着错误配置继续启动。
    """
    from config_server import ServerConfig, Qwen3ASRGGUFArgs, ForceAlignerGGUFArgs

    host = os.environ.get(ENV_HOST, '').strip()
    if host:
        ServerConfig.addr = host

    port = os.environ.get(ENV_PORT, '').strip()
    if port:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            sys.exit(f'无效的端口：{port!r}（{ENV_PORT} / --port）')
        ServerConfig.port = port

    provider = os.environ.get(ENV_ONNX_PROVIDER, '').strip().upper()
    if provider:
        if provider not in ONNX_PROVIDERS:
            sys.exit(f'无效的 ONNX 推理后端：{provider!r}，可选 {", ".join(ONNX_PROVIDERS)}（{ENV_ONNX_PROVIDER}）')
        Qwen3ASRGGUFArgs.onnx_provider = provider
        ForceAlignerGGUFArgs.onnx_provider = provider

    gpu = os.environ.get(ENV_GPU, '').strip()
    if gpu:
        if not gpu.isdigit():
            sys.exit(f'无效的 GPU 编号：{gpu!r}（{ENV_GPU} / --gpu）')
        # 主推理是 llama.cpp 的 Vulkan 后端，认 GGML_VK_VISIBLE_DEVICES；
        # ONNX 编码器走 CUDA 时认 CUDA_VISIBLE_DEVICES。两个都要设。
        # CUDA 默认按算力排序，改成按 PCI 总线排序，编号才和 nvidia-smi 一致。
        os.environ['GGML_VK_VISIBLE_DEVICES'] = gpu
        os.environ['CUDA_VISIBLE_DEVICES'] = gpu
        os.environ.setdefault('CUDA_DEVICE_ORDER', 'PCI_BUS_ID')


def current_command() -> list:
    """
    启动当前进程所用的命令行（用于重启时原样再启动一次）。

    必须在 CapsWriterServer 切换工作目录之前调用：非冻结模式下 argv[0] 可能是相对路径。
    - Windows exe（冻结运行时）：[start_server.exe 绝对路径, 参数...]
    - Python 入口：[解释器, start_server.py 绝对路径, 参数...]
    """
    if getattr(sys, 'frozen', False):
        return [sys.executable] + sys.argv[1:]
    return [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:]


def spawn_new_instance(cmd: list) -> None:
    """
    用给定命令行启动一个新的服务端进程（重启的后半步）。

    调用前必须已经释放端口和识别子进程，否则新进程会因端口冲突直接退出。
    Windows 上放进新的控制台窗口，效果与双击 exe 相同；旧控制台随当前进程退出而关闭。
    环境变量随之继承（含命令行参数写回的 CAPSWRITER_*），新进程得到相同的配置。
    """
    import subprocess
    kwargs = {}
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NEW_CONSOLE
    else:
        kwargs['start_new_session'] = True
    subprocess.Popen(cmd, **kwargs)


def is_interactive() -> bool:
    """是否运行在可交互的终端里（双击 exe / 手动在终端运行）；被服务管理器托管时为 False"""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if sys.platform == 'win32':
            # Windows 上重定向到 NUL 时 isatty() 也返回 True，要确认是真正的控制台
            import ctypes
            import msvcrt
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(ctypes.c_ulong())))
        return True
    except (ValueError, OSError, AttributeError):
        return False


def pause_before_exit(prompt: str = '按回车键退出...') -> None:
    """交互式终端里等待回车，让双击运行的用户看清报错；无终端时直接返回"""
    if is_interactive():
        try:
            input(f'\n{prompt}')
        except EOFError:
            pass
