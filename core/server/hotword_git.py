"""独立配置仓库的 Git 适配：不触碰程序仓库，不把凭证写入 URL 或提交。"""

import base64
import os
import re
import signal
import subprocess
from pathlib import Path

from core.hotword_sync import FILES, atomic_write, read_text


class HotwordGit:
    def __init__(self, directory, repository, token_file):
        self.directory = Path(directory) / 'git'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.url = repository.strip()
        if re.fullmatch(r'[\w.-]+/[\w.-]+', self.url):
            self.url = 'https://github.com/' + self.url + '.git'
        self.env = os.environ.copy()
        self.env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
        self.env.setdefault('GIT_SSH_COMMAND', 'ssh -oBatchMode=yes -oConnectTimeout=8')
        self.token = ''
        try:
            candidate = read_text(token_file).strip()
            if re.fullmatch(r'(github_pat_|ghp_)[A-Za-z0-9_]+', candidate):
                self.token = candidate
        except (OSError, UnicodeError):
            pass
        self._git('init', '--quiet')
        self.parent = ''
        self.branch = ''

    def _git(self, *args, network=False, allowed=False):
        env = self.env.copy()
        if network and self.token and self.url.startswith('https://github.com/'):
            # 只给本次 Git 进程注入 HTTP 头；argv、.git/config、日志里均没有 token。
            value = base64.b64encode(('x-access-token:' + self.token).encode()).decode()
            count = int(env.get('GIT_CONFIG_COUNT', '0'))
            env.update({
                'GIT_CONFIG_COUNT': str(count + 2),
                'GIT_CONFIG_KEY_' + str(count): 'http.https://github.com/.extraheader',
                'GIT_CONFIG_VALUE_' + str(count): 'Authorization: Basic ' + value,
                'GIT_CONFIG_KEY_' + str(count + 1): 'credential.helper',
                'GIT_CONFIG_VALUE_' + str(count + 1): '',
            })
        kwargs = dict(cwd=str(self.directory), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs['start_new_session'] = True
        command = ['git'] + list(args)
        process = subprocess.Popen(command, **kwargs)
        try:
            stdout, stderr = process.communicate(timeout=30 if network else 10)
        except subprocess.TimeoutExpired:
            # Git 的 HTTPS/SSH 帮助进程也持有管道和仓库锁，超时须收回本次进程树。
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            raise RuntimeError('Git {} 超时'.format(args[0])) from None
        result = subprocess.CompletedProcess(command, process.returncode,
                                             stdout.decode('utf-8-sig'), stderr.decode('utf-8', errors='replace'))
        if result.returncode and network and self.token:
            # 写错或失效的密钥不能屏蔽机器已有的正常登录状态。
            self.token = ''
            return self._git(*args, network=network, allowed=allowed)
        if result.returncode and not allowed:
            # Git/credential helper 的 stderr 可能带凭证，不直接拼到应用日志。
            raise RuntimeError('Git {} 失败，退出码 {}'.format(args[0], result.returncode))
        return result

    def fetch(self):
        head = self._git('ls-remote', '--symref', self.url, 'HEAD', network=True).stdout
        match = re.search(r'ref: refs/heads/([^\s]+)\s+HEAD', head)
        if not head.strip():
            branches = self._git('ls-remote', '--heads', self.url, network=True).stdout
            if branches.strip():
                raise RuntimeError('配置仓库默认分支未设置')
            self.parent, self.branch = '', 'main'
            return {}
        if not match:
            raise RuntimeError('无法确定配置仓库默认分支')
        self.branch = match.group(1)
        self._git('fetch', '--quiet', self.url, 'refs/heads/' + self.branch, network=True)
        self.parent = self._git('rev-parse', 'FETCH_HEAD').stdout.strip()
        result = {}
        for name in FILES:
            item = self._git('show', self.parent + ':' + name, allowed=True)
            if item.returncode == 0:
                result[name] = item.stdout
        return result

    def push(self, files):
        # index 以刚拉取的远端为准，保留仓库中其他文件；仅覆盖约定的三个热词文件。
        if self.parent:
            self._git('read-tree', self.parent)
        else:
            self._git('read-tree', '--empty')
        for name in FILES:
            atomic_write(self.directory / name, files[name])
        self._git('-c', 'core.autocrlf=false', 'add', '--', *FILES)
        tree = self._git('write-tree').stdout.strip()
        if self.parent and tree == self._git('rev-parse', self.parent + '^{tree}').stdout.strip():
            return True
        args = ['-c', 'user.name=CapsWriter', '-c', 'user.email=capswriter@localhost',
                'commit-tree', tree, '-m', '同步热词']
        if self.parent:
            args.extend(['-p', self.parent])
        commit = self._git(*args).stdout.strip()
        result = self._git('push', '--quiet', self.url, commit + ':refs/heads/' + self.branch,
                           network=True, allowed=True)
        if result.returncode:
            # 非快进交给调用者重新获取；其他错误不能在每轮重复认证。
            if 'non-fast-forward' not in result.stderr and 'fetch first' not in result.stderr:
                raise RuntimeError('Git push 失败，退出码 {}'.format(result.returncode))
            return False
        self.parent = commit
        return True

    def common_text(self, old, new, name):
        if not re.fullmatch(r'[0-9a-f]{40,64}', old) or not re.fullmatch(r'[0-9a-f]{40,64}', new):
            return None
        result = self._git('merge-base', old, new, allowed=True)
        if result.returncode:
            return None
        text = self._git('show', result.stdout.strip() + ':' + name, allowed=True)
        return text.stdout if text.returncode == 0 else None
