# coding: utf-8
"""
离线验证一把梭：语法 → 未定义名字 → 桩模块单测

本目录是打包后的 Release，系统 Python 没有项目依赖，`import core` 会在包
__init__ 里连锁导入 rich 而失败，所以这里跑的都是「预注入桩模块 + 按路径加载」
的测试，不需要任何第三方库，也不联网、不花额度。厂商行为相关的检查在
probe_llm_live.py，那个要真发请求，单独跑。

用法：python tools/run_tests.py
退出码非 0 表示有失败项。
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))

# core/server 是上游 vendored 的推理引擎，internal 是内嵌运行时，都不是本项目源码
SKIP_DIRS = {'__pycache__', '.git', 'internal', 'logs', 'assets'}

SUITES = ['test_vision.py', 'test_state_tags.py', 'test_history_payload.py']


def syntax_check() -> int:
    """ast.parse 而非 py_compile：后者会在仓库里留下 __pycache__"""
    failed = total = 0
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if not name.endswith('.py'):
                continue
            path = os.path.join(root, name)
            total += 1
            try:
                ast.parse(io.open(path, encoding='utf-8-sig').read())
            except Exception as e:
                failed += 1
                print('  [×] {}: {}'.format(os.path.relpath(path, REPO), e))
    print('  {} 个文件，失败 {}'.format(total, failed))
    return failed


def run(script: str, args=()) -> int:
    """子进程跑，避免各测试脚本预注入的 sys.modules 桩互相污染"""
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    proc = subprocess.run([sys.executable, os.path.join(HERE, script)] + list(args),
                          cwd=REPO, env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace')
    tail = [ln for ln in (proc.stdout or '').splitlines() if ln.strip()]
    if proc.returncode != 0:
        print((proc.stdout or '').rstrip())
        print((proc.stderr or '').rstrip())
    elif tail:
        print('  ' + tail[-1])
    return proc.returncode


def main() -> int:
    steps = []

    print('[1/3] 语法检查')
    steps.append(('语法检查', syntax_check()))

    print('[2/3] 未定义名字检查')
    # 已知命中记在 tools/undefined_baseline.txt（都在自测块里，生产路径跑不到），
    # 这一步看的是**有没有新增**，不是归零。tools 自己也扫，检查器写错了要能自曝
    steps.append(('未定义名字检查',
                  run('check_undefined.py', ['core/client', 'core/ui', 'tools'])))

    print('[3/3] 桩模块单测')
    failed = 0
    for suite in SUITES:
        print('  - ' + suite)
        failed += 1 if run(suite) else 0
    steps.append(('桩模块单测', failed))

    print()
    print('=' * 60)
    for name, rc in steps:
        print('  {:<16} {}'.format(name, '通过' if rc == 0 else '**有失败项**'))
    print()
    print('厂商行为（图片、情绪标签、thinking）另见 tools/probe_llm_live.py，需联网且消耗额度')
    return 1 if any(rc for _, rc in steps) else 0


if __name__ == '__main__':
    sys.exit(main())
