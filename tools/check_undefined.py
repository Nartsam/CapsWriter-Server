# coding: utf-8
"""
未定义名字静态检查（迷你 pyflakes）

起因：`ast.parse` 只验语法，抓不到 NameError。2026-10-29 用它查出
`engines/llama/llama.py` 的 `token_bos()` 引用了从未绑定的 `llama_vocab_bos`——
那个方法没人调用，语法检查和单测都覆盖不到。

系统 Python 没有项目依赖、也装不了 pyflakes，所以自己实现一个够用的：
按作用域链（局部 → 外层函数 → 模块 → 内置）解析每个 Load 名字，解析不到就报。
宁可漏报不误报——`import *` 的模块整个跳过，已知的动态绑定写进
tools/undefined_baseline.txt。

本文件与 CapsWriter-Client 仓库的同名工具同源，两边各自维护即可，不要求逐字相同。

用法：python tools/check_undefined.py [路径...]
    不给路径时检查 core 与 tools
"""
from __future__ import annotations

import ast
import builtins
import os
import sys

sys.dont_write_bytecode = True

BUILTINS = set(dir(builtins)) | {
    '__name__', '__file__', '__doc__', '__package__', '__spec__',
    '__loader__', '__builtins__', '__debug__', '__annotations__',
}


class Scope:
    def __init__(self, parent=None, kind='module'):
        self.parent = parent
        self.kind = kind          # module / function / class / comp
        self.names = set()
        self.globals = set()

    def declare(self, name):
        if name:
            self.names.add(name)

    def resolve(self, name):
        if name in BUILTINS:
            return True
        scope = self
        while scope is not None:
            # 类作用域对嵌套函数不可见，跳过（与 Python 语义一致）
            if scope is self or scope.kind != 'class':
                if name in scope.names or name in scope.globals:
                    return True
            scope = scope.parent
        return False


class Collector(ast.NodeVisitor):
    """把一个作用域内**所有**绑定名字先收齐（函数体内可后定义先使用）"""

    def __init__(self, scope):
        self.scope = scope

    def _bind_target(self, node):
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                self.scope.declare(n.id)

    def visit_Assign(self, node):
        for t in node.targets:
            self._bind_target(t)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_NamedExpr(self, node):
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_For(self, node):
        self._bind_target(node.target)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_With(self, node):
        for item in node.items:
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node):
        self.scope.declare(node.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            self.scope.declare(a.asname or a.name.split('.')[0])

    def visit_ImportFrom(self, node):
        for a in node.names:
            self.scope.declare(a.asname or a.name)

    def visit_Global(self, node):
        self.scope.globals.update(node.names)

    def visit_Nonlocal(self, node):
        self.scope.globals.update(node.names)

    def visit_FunctionDef(self, node):
        self.scope.declare(node.name)          # 只登记函数名，不进入函数体

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.scope.declare(node.name)

    def visit_Lambda(self, node):
        pass                                    # lambda 体另开作用域


def collect(scope, body):
    c = Collector(scope)
    for stmt in body:
        c.visit(stmt)


class Checker(ast.NodeVisitor):
    def __init__(self, path, ignore):
        self.path = path
        self.ignore = ignore
        self.problems = []
        self.scope = None

    # ---- 作用域进出 ----

    def _enter(self, kind):
        self.scope = Scope(self.scope, kind)
        return self.scope

    def _leave(self):
        self.scope = self.scope.parent

    def run(self, tree):
        self._enter('module')
        collect(self.scope, tree.body)
        for stmt in tree.body:
            self.visit(stmt)
        self._leave()
        return self.problems

    def _visit_function(self, node):
        for d in node.decorator_list:
            self.visit(d)
        args = node.args
        for default in list(args.defaults) + [d for d in args.kw_defaults if d]:
            self.visit(default)

        self._enter('function')
        all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        if args.vararg:
            all_args.append(args.vararg)
        if args.kwarg:
            all_args.append(args.kwarg)
        for a in all_args:
            self.scope.declare(a.arg)
        collect(self.scope, node.body)
        for stmt in node.body:
            self.visit(stmt)
        self._leave()

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._enter('function')
        a = node.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            self.scope.declare(arg.arg)
        if a.vararg:
            self.scope.declare(a.vararg.arg)
        if a.kwarg:
            self.scope.declare(a.kwarg.arg)
        self.visit(node.body)
        self._leave()

    def visit_ClassDef(self, node):
        for d in node.decorator_list:
            self.visit(d)
        for b in node.bases:
            self.visit(b)
        self._enter('class')
        collect(self.scope, node.body)
        for stmt in node.body:
            self.visit(stmt)
        self._leave()

    def _visit_comp(self, node):
        self._enter('comp')
        for gen in node.generators:
            for n in ast.walk(gen.target):
                if isinstance(n, ast.Name):
                    self.scope.declare(n.id)
        for gen in node.generators:
            self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        for field in ('elt', 'key', 'value'):
            sub = getattr(node, field, None)
            if sub is not None:
                self.visit(sub)
        self._leave()

    visit_ListComp = _visit_comp
    visit_SetComp = _visit_comp
    visit_GeneratorExp = _visit_comp
    visit_DictComp = _visit_comp

    # ---- 实际检查 ----

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            if node.id in self.ignore:
                return
            if not self.scope.resolve(node.id):
                self.problems.append((node.lineno, node.id))
        self.generic_visit(node)


def has_star_import(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if any(a.name == '*' for a in n.names):
                return True
    return False


def check_file(path, ignore):
    try:
        src = open(path, encoding='utf-8-sig').read()
        tree = ast.parse(src)
    except Exception as e:
        return [('语法/读取失败', str(e))]
    if has_star_import(tree):
        return []
    return Checker(path, ignore).run(tree)


def load_baseline(path):
    """读取基线：每行 `相对路径:名字`，# 开头为注释

    刻意不记行号——行号一改动就失效，那样的基线只会被人无脑重新生成，等于没有。
    按「哪个文件里的哪个名字」记，既能压住已知项，又拦得住同一文件里新出现的名字。
    """
    known = set()
    if not path or not os.path.isfile(path):
        return known
    for line in open(path, encoding='utf-8'):
        line = line.split('#')[0].strip()
        if line:
            known.add(line)
    return known


def _rel(path, repo):
    return os.path.relpath(path, repo).replace(os.sep, '/')


def main(argv):
    baseline_path = ''
    args = []
    it = iter(argv)
    for a in it:
        if a == '--baseline':
            baseline_path = next(it, '')
        else:
            args.append(a)

    # 不给路径时扫 core 与 tools
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    targets = args or [os.path.join(repo, 'core'), os.path.join(repo, 'tools')]
    known = load_baseline(baseline_path
                          or os.path.join(repo, 'tools', 'undefined_baseline.txt'))

    ignore = set()
    files = []
    for t in targets:
        if os.path.isfile(t):
            files.append(t)
            continue
        for root, dirs, names in os.walk(t):
            dirs[:] = [d for d in dirs if d not in ('__pycache__', 'internal', 'logs')]
            files += [os.path.join(root, n) for n in names if n.endswith('.py')]

    new_hits = baselined = 0
    for path in sorted(files):
        rel = _rel(path, repo)
        shown = []
        for line, name in check_file(path, ignore):
            if '%s:%s' % (rel, name) in known:
                baselined += 1
            else:
                shown.append((line, name))
        if shown:
            print(rel)
            for line, name in shown:
                print('    %s:%s  未定义的名字: %s' % (rel, line, name))
                new_hits += 1
    print()
    print('检查 %d 个文件，新增可疑未定义名字 %d 处（基线内已知 %d 处）'
          % (len(files), new_hits, baselined))
    return 1 if new_hits else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
