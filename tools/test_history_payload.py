# coding: utf-8
"""
对话历史上行载荷单测（桩模块加载，不联网）

覆盖 2026-09-04 的两处修复：
1. build_messages 走 get_history()，历史条目不再把 timestamp 带上行
2. _stream_request 存 assistant **原文**，状态标签留在历史里

外加 _describe_messages 这行新诊断的口径。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import types

sys.dont_write_bytecode = True

# 仓库根目录按本文件位置推导，脚本从任何工作目录跑都成立
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = REPO


class _L:
    def __getattr__(self, n):
        return lambda *a, **k: None


for name in ('core', 'core.client', 'core.client.llm'):
    m = types.ModuleType(name)
    m.__path__ = []
    sys.modules[name] = m
sys.modules['core.client.llm'].logger = _L()

for mod, attrs in (
    ('core.client.llm.llm_interfaces', {'IContextManager': type('IContextManager', (), {})}),
    ('core.client.llm.llm_client_pool', {'ClientPool': type('ClientPool', (), {})}),
    ('core.client.llm.llm_exceptions', {
        'APIException': type('APIException', (Exception,), {}),
        'wrap_openai_error': lambda *a, **k: None,
        'OpenAIErrorWrapper': type('OpenAIErrorWrapper', (Exception,), {}),
        'TimeoutErrorWrapper': type('TimeoutErrorWrapper', (Exception,), {}),
        'APIResponseErrorWrapper': type('APIResponseErrorWrapper', (Exception,), {}),
    }),
):
    m = types.ModuleType(mod)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[mod] = m


def load(mod, fname):
    p = os.path.join(ROOT, 'core', 'client', 'llm', fname)
    spec = importlib.util.spec_from_file_location(mod, p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod] = m
    spec.loader.exec_module(m)
    return m


load('core.client.llm.llm_constants', 'llm_constants.py')
load('core.client.llm.llm_state_tags', 'llm_state_tags.py')
rc = load('core.client.llm.llm_role_config', 'llm_role_config.py')
load('core.client.llm.llm_memory', 'llm_memory.py')
ctx = load('core.client.llm.llm_context', 'llm_context.py')
mb = load('core.client.llm.llm_message_builder', 'llm_message_builder.py')
load('core.client.llm.llm_anthropic', 'llm_anthropic.py')
proc = load('core.client.llm.llm_processor', 'llm_processor.py')

RoleConfig = rc.RoleConfig
ContextManager = ctx.ContextManager
MessageBuilder = mb.MessageBuilder
LLMProcessor = proc.LLMProcessor
IMAGE_PLACEHOLDER = sys.modules['core.client.llm.llm_constants'].IMAGE_PLACEHOLDER

PASS = FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print('  [×] %s' % label)


def section(t):
    print()
    print('[%s]' % t)


role = RoleConfig(name='助理', enable_history=True, enable_web_search=True,
                  max_context_length=1024 * 1024, model='m', enable_vision=True)
builder = MessageBuilder(app=None)
p = LLMProcessor.__new__(LLMProcessor)
IMG = 'data:image/png;base64,AAAA'


def store(cm, messages, answer):
    """走 _stream_request 里真实的历史更新（_record_turn：assistant 存原文）"""
    p._record_turn(role, cm, messages, answer)


# ---------------------------------------------------------------- 1
section('历史不再把 timestamp 带上行')
cm = ContextManager(max_length=1024 * 1024)
m1 = builder.build_messages(role, '第一句', cm, image_data=IMG)
store(cm, m1, '<state:微笑>第一句的回复')
m2 = builder.build_messages(role, '第二句', cm)

check(all(set(x) <= {'role', 'content'} for x in m2),
      'build_messages 产出仍含额外键: %s' % [sorted(x) for x in m2 if set(x) - {'role', 'content'}])

params = p._build_responses_params(role, m2)
extra = [sorted(set(it) - {'role', 'content'}) for it in params['input'] if set(it) - {'role', 'content'}]
check(not extra, '上行 input 仍含额外键: %s' % extra)

# ---------------------------------------------------------------- 2
section('history 原对象不再被交出去（demote_images 是原地改的）')
cm2 = ContextManager(max_length=1024 * 1024)
cm2.add_message('user', [{'type': 'text', 'text': 'x'},
                         {'type': 'image_url', 'image_url': {'url': IMG}}])
got = builder.build_messages(role, 'q', cm2)
check(all(got[i] is not cm2.history[j] for i in range(len(got)) for j in range(len(cm2.history))),
      'build_messages 交出了 history 的活引用')

# ---------------------------------------------------------------- 3
section('assistant 历史保留状态标签')
check(cm.history[1]['content'] == '<state:微笑>第一句的回复', '历史里的标签被剥掉了')
hist_assistant = [x['content'] for x in m2 if x['role'] == 'assistant']
check(hist_assistant == ['<state:微笑>第一句的回复'], '上行的历史里没有标签: %s' % hist_assistant)

# ---------------------------------------------------------------- 4
section('带图历史仍然完整上行')
n_img = sum(1 for it in params['input'] if isinstance(it.get('content'), list)
            for q in it['content'] if q.get('type') == 'input_image')
check(n_img == 1, '第二轮请求里的历史图片数 = %d，应为 1' % n_img)

# ---------------------------------------------------------------- 5
section('_describe_messages 口径（只比数字：历史条数、图片张数，措辞随便改）')


def counts(messages):
    return [int(n) for n in re.findall(r'\d+', p._describe_messages(messages))]


check(counts(m1) == [0, 1], '首轮摘要: %r' % p._describe_messages(m1))
check(counts(m2) == [2, 1], '次轮摘要: %r' % p._describe_messages(m2))
check(counts([{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'q'}]) == [0, 0],
      'system 不计入历史')

# ---------------------------------------------------------------- 6
section('新一轮带图时旧图仍降级为占位符')
m3 = builder.build_messages(role, '第三句', cm, image_data=IMG)
store(cm, m3, '第三句的回复')
m4 = builder.build_messages(role, '第四句', cm)
imgs = sum(1 for x in m4 if isinstance(x.get('content'), list)
           for q in x['content'] if q.get('type') in ('image_url', 'input_image'))
check(imgs == 1, '历史里应只剩最近一张真图，实为 %d 张' % imgs)
check(any(IMAGE_PLACEHOLDER in x['content'] for x in m4 if isinstance(x.get('content'), str)),
      '更早的图没有降级成占位符')

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
