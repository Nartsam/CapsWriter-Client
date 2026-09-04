# coding: utf-8
"""
StateTagFilter 单测（桩模块加载，不依赖项目运行时）

重点是**分片切割位置**：`<state:微笑>` 被切在任意一处都必须被完整吃掉。
逐位置穷举，比手写几个用例可靠得多。
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import sys
import types

sys.dont_write_bytecode = True

# 仓库根目录按本文件位置推导，脚本从任何工作目录跑都成立
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = REPO

for name in ('core', 'core.client', 'core.client.llm'):
    m = types.ModuleType(name)
    m.__path__ = []
    sys.modules[name] = m

spec = importlib.util.spec_from_file_location(
    'core.client.llm.llm_state_tags',
    os.path.join(ROOT, 'core', 'client', 'llm', 'llm_state_tags.py'))
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

StateTagFilter = mod.StateTagFilter
strip_state_tags = mod.strip_state_tags

PASS = FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print('  [×] %s' % label)


def run(text, cuts):
    """按 cuts 里的下标把 text 切片喂进过滤器，返回放行的全部文本"""
    f = StateTagFilter()
    out = []
    prev = 0
    for c in list(cuts) + [len(text)]:
        out.append(f.feed(text[prev:c]))
        prev = c
    out.append(f.finish())
    return ''.join(out)


def section(title):
    print()
    print('[%s]' % title)


# ---------------------------------------------------------------- 1
section('strip_state_tags 基本行为')
check(strip_state_tags('<state:微笑>你好') == '你好', '开头标签')
check(strip_state_tags('<状态:失落>难过') == '难过', '中文标签名')
check(strip_state_tags('a<state:x>b<state:y>c') == 'abc', '多个标签')
check(strip_state_tags('') == '', '空串')
check(strip_state_tags(None) is None, 'None 原样返回')
check(strip_state_tags('3 < 5 且 5 > 3') == '3 < 5 且 5 > 3', '普通尖括号不动')
check(strip_state_tags('<states:x>') == '<states:x>', '相近但不匹配的名字不动')

# ---------------------------------------------------------------- 2
section('单切点穷举：标签被切在任意位置都要被吃掉')
TEXT = '<state:微笑>今天天气很好。'
EXPECT = '今天天气很好。'
bad = [i for i in range(len(TEXT) + 1) if run(TEXT, [i]) != EXPECT]
check(not bad, '单切点失败于下标 %s' % bad)

TEXT2 = '前言<状态:失落>正文。'
EXPECT2 = '前言正文。'
bad = [i for i in range(len(TEXT2) + 1) if run(TEXT2, [i]) != EXPECT2]
check(not bad, '中文标签单切点失败于下标 %s' % bad)

# ---------------------------------------------------------------- 3
section('双切点穷举')
bad = [(i, j) for i, j in itertools.combinations(range(len(TEXT) + 1), 2)
       if run(TEXT, [i, j]) != EXPECT]
check(not bad, '双切点失败 %d 处，例如 %s' % (len(bad), bad[:3]))

# ---------------------------------------------------------------- 4
section('逐字符喂入（最坏情况）')
for t, e in ((TEXT, EXPECT), (TEXT2, EXPECT2),
             ('a<state:x>b<state:y>c', 'abc'),
             ('无标签的普通回复', '无标签的普通回复')):
    check(run(t, range(len(t))) == e, '逐字符: %r' % t)

# ---------------------------------------------------------------- 5
section('不该被吞掉的文本')
for t in ('3 < 5', 'a<b', '<', '<<<', '<state', '<state:', 'x<y<z',
          '数学：a<b 且 b>c', '<states:x>', '<state:a<b>'):
    got = run(t, range(len(t)))
    check(got == t, '原样通过失败: %r -> %r' % (t, got))

# ---------------------------------------------------------------- 6
section('缓冲上界：孤立的 < 之后不能无限攒着不放行')
f = StateTagFilter()
out = f.feed('<state:' + 'x' * 200)
check(len(out) > 100, '超长伪标签必须放行，实际放行 %d 字' % len(out))
check(len(f._pending) <= mod._MAX_PENDING, '暂存长度 %d 超上界' % len(f._pending))

f = StateTagFilter()
out = ''.join(f.feed(c) for c in '<' + 'a' * 300)
check(len(out) + len(f._pending) == 301, '孤立 < 后的正文既不丢也不无限缓冲')

# ---------------------------------------------------------------- 7
section('finish 不吞字')
f = StateTagFilter()
first = f.feed('结尾有个尖括号<')
check(first + f.finish() == '结尾有个尖括号<', '未闭合尾巴要交出来')

f = StateTagFilter()
first = f.feed('半截标签<state:微')
check(first + f.finish() == '半截标签<state:微', '半截标签在 finish 时原样交出')

f = StateTagFilter()
check(f.finish() == '' and f.finish() == '', 'finish 幂等且不产生残留')

# ---------------------------------------------------------------- 8
section('跨分片与流式一致性：过滤器结果 == 整段 strip')
CASES = [
    '<state:微笑>你好，<state:得意>世界。',
    '普通文本没有任何标签',
    '<状态:惊讶>啊！<state:普通>好的。',
    '公式 a<b 与 c>d 混在一起',
]
for t in CASES:
    check(run(t, range(len(t))) == strip_state_tags(t), '一致性: %r' % t)

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
