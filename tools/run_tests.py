# coding: utf-8
"""
离线验证一把梭：Python 语法 → Shell 语法 → 未定义名字 → 离线单测

系统 Python 没有项目依赖（`import core` 会在包 __init__ 里连锁导入 rich 而失败），
所以这里跑的都是「预注入桩模块 + 按路径加载」的测试，不需要第三方库（numpy 除外，
test_recognition.py 真的要算音频），不联网、不加载模型。

这一步**替代不了**真机冒烟测试：识别质量、显卡后端、对齐模型只能用
`start_server.exe --port 6100`（Linux 为 `./start_server.sh --port 6100`）加
`smoke_test.py` / `smoke_test_revisable.py` 验证，命令见 CLAUDE.md 的「验证」。

用法：python tools/run_tests.py
退出码非 0 表示有失败项。
"""
import ast
import io
import os
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))

# internal 是 exe 的冻结运行时，不是本项目源码；logs、models 是运行时产物
SKIP_DIRS = {'__pycache__', '.git', 'internal', 'logs', 'models', '.conda-env', '.venv'}

# Linux 跑的是 Python 3.10，所以全部源码按 3.10 检查
MIN_PY = (3, 10)

# 与客户端逐字相同或声称 3.8+ 可用的文件，按客户端运行时的 3.8 检查
PY38_FILES = ('core/protocol.py', 'core/constants.py', 'core/hotword_sync.py',
              'tools/smoke_test.py')

# 需要真机或对端源码的脚本不在这里跑：
# smoke_test*.py 要服务端在跑，test_hotword_transport.py 要 --client-root
SUITES = ['test_recognition.py', 'test_hotword_sync.py']


def iter_sources(suffix):
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(names):
            if name.endswith(suffix):
                yield os.path.join(root, name)


def rel(path):
    return os.path.relpath(path, REPO).replace(os.sep, '/')


def python_syntax():
    """ast.parse 而非 py_compile：后者会在仓库里留下 __pycache__"""
    failed = total = 0
    for path in iter_sources('.py'):
        total += 1
        name = rel(path)
        version = (3, 8) if name in PY38_FILES else MIN_PY
        try:
            ast.parse(io.open(path, encoding='utf-8-sig').read(),
                      filename=name, feature_version=version)
        except SyntaxError as error:
            failed += 1
            print('  [x] %s（按 Python %d.%d 检查）: %s' % (name, version[0], version[1], error))
    print('  %d 个文件（其中 %d 个按 3.8 检查），失败 %d' % (total, len(PY38_FILES), failed))
    return failed


def shell_syntax():
    """.sh 要在 Linux 上跑，交给 bash -n；Windows 上没 bash 就跳过"""
    bash = shutil.which('bash')
    if not bash:
        print('  跳过：本机没有 bash')
        return 0
    failed = total = 0
    for path in iter_sources('.sh'):
        total += 1
        proc = subprocess.run([bash, '-n', path], capture_output=True, text=True,
                              encoding='utf-8', errors='replace')
        if proc.returncode:
            failed += 1
            print('  [x] %s: %s' % (rel(path), (proc.stderr or '').strip()))
    print('  %d 个脚本，失败 %d' % (total, failed))
    return failed


def run(script, args=()):
    """子进程跑，避免各测试脚本预注入的 sys.modules 桩互相污染"""
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    proc = subprocess.run([sys.executable, '-B', os.path.join(HERE, script)] + list(args),
                          cwd=REPO, env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace')
    # unittest 写 stderr，摘要在最后几行
    output = ((proc.stdout or '') + (proc.stderr or '')).splitlines()
    tail = [line for line in output if line.strip()]
    if proc.returncode:
        print('\n'.join(tail[-25:]))
    elif tail:
        print('  ' + tail[-1])
    return proc.returncode


def main():
    steps = []

    print('[1/4] Python 语法检查')
    steps.append(('Python 语法', python_syntax()))

    print('[2/4] Shell 语法检查')
    steps.append(('Shell 语法', shell_syntax()))

    print('[3/4] 未定义名字检查')
    # 已知的动态绑定记在 tools/undefined_baseline.txt，这一步看的是有没有新增
    steps.append(('未定义名字', run('check_undefined.py', ['core', 'tools'])))

    print('[4/4] 离线单测')
    failed = 0
    for suite in SUITES:
        print('  - ' + suite)
        failed += 1 if run(suite) else 0
    steps.append(('离线单测', failed))

    print()
    print('=' * 60)
    for name, code in steps:
        print('  %-14s %s' % (name, '通过' if code == 0 else '**有失败项**'))
    print()
    print('识别质量、显卡后端与对齐模型要用真机冒烟测试，见 CLAUDE.md 的「验证」')
    return 1 if any(code for _, code in steps) else 0


if __name__ == '__main__':
    sys.exit(main())
