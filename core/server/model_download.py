# coding: utf-8
"""
模型下载器

模型文件不进 git。上游 CapsWriter-Offline 在 GitHub Release 发布了模型压缩包，
这里登记本项目用到的包、它的 SHA-256，以及解压后应该得到的文件。

只用标准库：Windows 上跑在 start_server.exe 的冻结运行时里，Linux 上跑在项目环境里。

入口：
- `start_server.exe --download-models` / `./start_server.sh --download-models`
- 启动时缺少必需模型、且运行在交互终端里时，check_model 会询问是否立即下载

流程：下载到 models/.downloads/*.part（支持断点续传与重试）→ 校验 SHA-256 →
解压到临时目录（zipfile 会校验每个文件的 CRC）→ 逐个移动到最终位置 → 删除压缩包。
手动下载的压缩包放进 models/.downloads/ 也会被识别（校验通过即跳过下载）。
"""

import hashlib
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from config_server import BASE_DIR, ModelPaths, ModelDownload


@dataclass(frozen=True)
class ModelPackage:
    name: str                   # 显示名
    archive: str                # Release 中的压缩包文件名
    sha256: str                 # 压缩包的 SHA-256（取自 GitHub Release 的 asset digest）
    size: int                   # 压缩包字节数
    extract_to: Path            # 解压目标目录（压缩包内自带一层同名文件夹）
    files: Tuple[Path, ...]     # 解压后必须存在的文件，用于判断是否已安装
    required: bool              # True：缺少时服务无法启动；False：只影响可选功能


PACKAGES: Tuple[ModelPackage, ...] = (
    ModelPackage(
        name='Qwen3-ASR-1.7B (q5_k)',
        archive='Qwen3-ASR-1.7B-q5_k.zip',
        sha256='f40040fe62a5ef0c09f8699fdbcb30f18bb8ae2bcd515ed4954e1f62b8b0e88f',
        size=1951570656,
        extract_to=ModelPaths.model_dir / 'Qwen3-ASR',
        files=(
            ModelPaths.qwen3_asr_gguf_encoder_frontend,
            ModelPaths.qwen3_asr_gguf_encoder_backend,
            ModelPaths.qwen3_asr_gguf_llm_decode,
        ),
        required=True,
    ),
    ModelPackage(
        name='Qwen3-ForcedAligner-0.6B（文件转录的精确时间戳）',
        archive='Qwen3-ForcedAligner-0.6B.zip',
        sha256='8fa035db150e1621a01cbb313bf152b593818b0f15dd94180452f22fc77fbf43',
        size=626205170,
        extract_to=ModelPaths.model_dir / 'Qwen3-ForcedAligner',
        files=(
            ModelPaths.force_aligner_gguf_encoder_frontend,
            ModelPaths.force_aligner_gguf_encoder_backend,
            ModelPaths.force_aligner_gguf_llm_decode,
        ),
        required=False,
    ),
)

DOWNLOAD_DIR = ModelPaths.model_dir / '.downloads'
_RETRIES = 8
_CHUNK = 1 << 20


def _abs(p: Path) -> Path:
    """配置里的模型路径相对于项目根目录；这里统一转绝对路径，不依赖当前工作目录"""
    return p if p.is_absolute() else Path(BASE_DIR) / p


def missing_files(pkg: ModelPackage) -> List[Path]:
    return [f for f in pkg.files if not _abs(f).exists()]


def missing_packages(required_only: bool = False) -> List[ModelPackage]:
    return [p for p in PACKAGES if missing_files(p) and (p.required or not required_only)]


def download_models(packages: Optional[Sequence[ModelPackage]] = None) -> bool:
    """下载并安装缺失的模型包，全部成功返回 True"""
    packages = PACKAGES if packages is None else packages
    ok = True
    for pkg in packages:
        if not missing_files(pkg):
            print(f'[已就绪] {pkg.name}')
            continue
        try:
            _install(pkg)
            print(f'[完成] {pkg.name}')
        except Exception as e:
            ok = False
            print(f'[失败] {pkg.name}：{e}')
    if not ok:
        print(f'\n可以手动下载压缩包放到 {_abs(DOWNLOAD_DIR)} 后重新运行，下载地址：{ModelDownload.base_url}')
    return ok


def _install(pkg: ModelPackage) -> None:
    download_dir = _abs(DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)
    archive = download_dir / pkg.archive

    if archive.exists() and archive.stat().st_size == pkg.size and _sha256(archive) == pkg.sha256:
        print(f'使用已下载的压缩包：{archive}')
    else:
        url = ModelDownload.base_url.rstrip('/') + '/' + pkg.archive
        print(f'下载 {pkg.name}：{url}')
        part = archive.with_name(archive.name + '.part')
        _download(url, part, pkg.size)
        print('校验 SHA-256 ...')
        digest = _sha256(part)
        if digest != pkg.sha256:
            part.unlink()
            raise RuntimeError(f'SHA-256 不符（期望 {pkg.sha256}，实际 {digest}），已删除，请重试；'
                               f'若上游重新发布了压缩包，需要更新 core/server/model_download.py 中的清单')
        os.replace(part, archive)

    print(f'解压到 {_abs(pkg.extract_to)} ...')
    _extract(archive, _abs(pkg.extract_to))
    still_missing = missing_files(pkg)
    if still_missing:
        raise RuntimeError(f'解压后仍缺少文件：{", ".join(str(f) for f in still_missing)}')
    archive.unlink()


def _download(url: str, dest: Path, total: int) -> None:
    """带断点续传与重试的下载"""
    for attempt in range(1, _RETRIES + 1):
        done = dest.stat().st_size if dest.exists() else 0
        if done == total:
            return
        if done > total:
            dest.unlink()
            done = 0
        request = urllib.request.Request(url, headers={'User-Agent': 'CapsWriter-Server'})
        if done:
            request.add_header('Range', f'bytes={done}-')
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                if done and resp.status != 206:     # 服务器不支持续传，从头开始
                    done = 0
                with open(dest, 'ab' if done else 'wb') as f:
                    progress = _Progress(total, done)
                    while True:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        progress.update(len(chunk))
                    progress.finish()
            if dest.stat().st_size == total:
                return
            raise ConnectionError(f'下载不完整（{dest.stat().st_size}/{total} 字节）')
        except urllib.error.HTTPError as e:
            if e.code == 416:                       # 续传起点越界：本地 .part 有问题，删掉重来
                dest.unlink()
                continue
            if 400 <= e.code < 500:                 # 地址错误/无权限，重试无意义
                raise RuntimeError(f'下载失败：HTTP {e.code} {e.reason}（{url}）') from e
            if attempt == _RETRIES:
                raise RuntimeError(f'下载失败：{e}') from e
            print(f'\n服务器错误：{e}，{attempt * 3} 秒后第 {attempt + 1} 次尝试...')
            time.sleep(attempt * 3)
        except OSError as e:                        # 含 URLError / 超时 / 连接断开
            if attempt == _RETRIES:
                raise RuntimeError(f'下载失败：{e}') from e
            print(f'\n网络错误：{e}，{attempt * 3} 秒后第 {attempt + 1} 次尝试（断点续传）...')
            time.sleep(attempt * 3)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(_CHUNK * 8), b''):
            h.update(chunk)
    return h.hexdigest()


def _extract(archive: Path, target: Path) -> None:
    """先解压到临时目录再逐个移动到位，避免中断时留下能“骗过”存在性检查的半截文件"""
    staging = archive.with_name(archive.name + '.extracting')
    if staging.exists():
        shutil.rmtree(staging)
    with zipfile.ZipFile(archive) as z:
        base = staging.resolve()
        for member in z.namelist():
            resolved = (staging / member).resolve()
            if resolved != base and base not in resolved.parents:
                raise RuntimeError(f'压缩包内含非法路径：{member}')
        z.extractall(staging)
    for src in staging.rglob('*'):
        if src.is_file():
            dst = target / src.relative_to(staging)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
    shutil.rmtree(staging)


class _Progress:
    """终端里原地刷新进度；输出被重定向（服务日志）时每 10% 打一行"""

    def __init__(self, total: int, done: int):
        self.total, self.done = total, done
        self.start_done, self.t0 = done, time.time()
        self.last_print, self.last_decile = 0.0, done * 10 // total
        self.tty = sys.stdout is not None and sys.stdout.isatty()

    def update(self, n: int) -> None:
        self.done += n
        now = time.time()
        decile = self.done * 10 // self.total
        if (self.tty and now - self.last_print >= 0.5) or (not self.tty and decile > self.last_decile):
            self.last_print, self.last_decile = now, decile
            self._print()

    def finish(self) -> None:
        if self.tty:
            self._print()
            print()
        elif self.last_decile < 10:     # 非终端模式下 100% 那一行已由 update 打印过
            self._print()

    def _print(self) -> None:
        speed = (self.done - self.start_done) / max(time.time() - self.t0, 1e-6) / 2**20
        line = (f'  {self.done / 2**20:8.1f} / {self.total / 2**20:.1f} MB '
                f'({self.done / self.total:6.1%})  {speed:5.1f} MB/s')
        print('\r' + line if self.tty else line, end='' if self.tty else '\n', flush=True)
