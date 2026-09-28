# coding: utf-8
"""
服务端 Python 入口（Linux 由 start_server.sh 调用；Windows 开发时也可直接用 Python 运行）

内容与 start_server.exe 内嵌的脚本完全相同：exe 只是一个 PyInstaller 启动器，
用 internal/ 里的冻结运行时执行这几行。所有启动逻辑（命令行参数、环境变量、
端口检查、模型检查……）都在 CapsWriterServer.start() 里，两个入口因此行为一致。
"""
from multiprocessing import freeze_support
from core.server.app import CapsWriterServer

if __name__ == '__main__':
    freeze_support()
    CapsWriterServer().start()
