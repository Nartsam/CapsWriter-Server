# coding: utf-8
"""
模型检查模块

启动前检查模型文件是否齐全：
- 缺少必需模型（Qwen3-ASR）：交互终端里询问是否立即下载；无终端（服务托管）时打印下载命令并退出
- 缺少可选模型（ForcedAligner）：只提示，服务照常启动，文件转录的时间戳退化为按字数估算
"""

import sys

from core.server.state import console
from core.server.launch import is_interactive, pause_before_exit
from core.server.model_download import missing_files, missing_packages, download_models
from . import logger

_DOWNLOAD_CMD = 'download_models.bat' if sys.platform == 'win32' else './download_models.sh'


def check_model() -> None:
    """
    检查模型文件，必需模型缺失且未能补齐时退出程序

    Raises:
        SystemExit: 必需模型缺失
    """
    logger.debug("检查模型文件")

    missing = missing_packages()
    for pkg in missing:
        for f in missing_files(pkg):
            logger.warning(f"模型文件缺失: {f}")

    if any(p.required for p in missing):
        _report(missing)
        if is_interactive() and _confirm('是否现在下载缺失的模型？[Y/n] '):
            download_models(missing)
            missing = missing_packages()
        if any(p.required for p in missing):
            logger.error("必需的模型文件缺失，服务端退出")
            console.print(f'\n    [bold red]缺少必需的模型文件，无法启动。[/bold red]请运行：[cyan]{_DOWNLOAD_CMD}[/cyan]\n')
            pause_before_exit()
            sys.exit(1)

    for pkg in missing:     # 剩下的都是可选模型
        console.print(f'[yellow]可选模型未安装：{pkg.name}，相关功能降级运行。'
                      f'需要时运行 [cyan]{_DOWNLOAD_CMD}[/cyan] 补全[/yellow]')

    logger.info("模型文件检查通过")
    console.print('[green4]模型文件检查通过 (qwen_asr)', end='\n\n')


def _report(missing) -> None:
    msg = '\n    [bold red]未能找到模型文件[/bold red]\n\n'
    for pkg in missing:
        tag = '必需' if pkg.required else '可选'
        msg += f'    [{tag}] {pkg.name}（{pkg.size / 2**20:.0f} MB）\n'
        for f in missing_files(pkg):
            msg += f'        未找到：{f}\n'
    console.print(msg)


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in ('', 'y', 'yes')
