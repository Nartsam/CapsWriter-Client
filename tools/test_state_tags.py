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
section('切点穷举：标签被切在任意一处或两处都要被吃掉')
# 两处切点的组合里含切点 0，已经覆盖了只切一处的全部情形
for text, expect in (('<state:微笑>今天天气很好。', '今天天气很好。'),
                     ('前言<状态:失落>正文。', '前言正文。')):
    bad = [(i, j) for i, j in itertools.combinations(range(len(text) + 1), 2)
           if run(text, [i, j]) != expect]
    check(not bad, '%r 切点失败 %d 处，例如 %s' % (text, len(bad), bad[:3]))

# ---------------------------------------------------------------- 3
section('逐字符喂入时与整段 strip 结果一致')
for t in ('<state:微笑>你好，<state:得意>世界。', '<状态:惊讶>啊！<state:普通>好的。',
          'a<state:x>b<state:y>c', '普通文本没有任何标签', '公式 a<b 与 c>d 混在一起'):
    check(run(t, range(len(t))) == strip_state_tags(t), '一致性: %r' % t)

# ---------------------------------------------------------------- 4
section('不该被吞掉的文本')
for t in ('3 < 5', 'a<b', '<', '<<<', '<state', '<state:', 'x<y<z',
          '数学：a<b 且 b>c', '<states:x>', '<state:a<b>'):
    got = run(t, range(len(t)))
    check(got == t, '原样通过失败: %r -> %r' % (t, got))

# ---------------------------------------------------------------- 5
section('缓冲上界：孤立的 < 之后不能无限攒着不放行')
for text, label in (('<state:' + 'x' * 200, '超长伪标签'), ('<' + 'a' * 300, '孤立的 <')):
    f = StateTagFilter()
    out = ''.join(f.feed(c) for c in text)
    check(len(text) - len(out) <= mod._MAX_PENDING,
          '%s：扣着没放行的 %d 字超过上界' % (label, len(text) - len(out)))
    check(out + f.finish() == text, '%s：正文不能丢' % label)

# ---------------------------------------------------------------- 6
section('finish 不吞字')
for text in ('结尾有个尖括号<', '半截标签<state:微'):
    f = StateTagFilter()
    first = f.feed(text)
    check(first + f.finish() == text, '未闭合的尾巴要在 finish 时原样交出: %r' % text)

f = StateTagFilter()
check(f.finish() == '' and f.finish() == '', 'finish 幂等且不产生残留')

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
