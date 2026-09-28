# coding: utf-8
"""
厂商行为探针（真发请求，会消耗额度）

有几条设计结论是**押在 DeepSeek 当前行为上**的（写在对应代码注释与 DEV_NOTES 里），本地
单测证不了，改厂商模型/接口时也不会有任何报错——只会表现成某个功能悄悄失灵。
这个脚本把它们一次性重测一遍，坏了哪条一眼看得出来。

  P1 视觉 + web_search 共存      带图请求不降级到 Chat（llm_processor._stream_request）
  P2 历史里的图仍可见            「历史里只保留最近一张真图」的前提（ContextManager.demote_images）
  P3 情绪标签跨轮保持            历史存原文才有意义（llm_state_tags 模块说明）
  P4 thinking:disabled 是否生效  DEV_NOTES「已知问题」记的是「接受但完全无效」

P2/P3 用仓库里**真实的** RoleConfig / MessageBuilder / ContextManager /
LLMProcessor 组装请求，只桩掉网络与 app，所以它们同时也是生产链路的回归测试。

用法：python tools/probe_llm_live.py [P1 P3 ...]     # 不给参数则全跑
密钥从 LLM.secrets 读入，**从不打印**，输出里也不含 Authorization 头。
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import re
import struct
import sys
import types
import urllib.error
import urllib.request
import zlib

sys.dont_write_bytecode = True

# 仓库根目录按本文件位置推导，脚本从任何工作目录跑都成立
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

BASE = 'https://api.deepseek.com'
TAG = re.compile(r'<state:([^>]*)>')


# ================================================================ 测试图片

def quadrant_png(size: int = 64) -> bytes:
    """纯标准库合成 PNG：四象限纯色（左上红/右上绿/左下蓝/右下白）

    用颜色而不是文字当探针内容：模型答不答得出四个方位的颜色，是「图片真的进了
    模型」的硬证据，而请求没报错什么也证明不了。
    """
    half = size // 2
    rows = []
    for y in range(size):
        row = bytearray([0])                      # 每行的 filter 字节
        for x in range(size):
            top, left = y < half, x < half
            row += (b'\xff\x00\x00' if (top and left) else
                    b'\x00\xff\x00' if top else
                    b'\x00\x00\xff' if left else
                    b'\xff\xff\xff')
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack('>I', len(data)) + body
                + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows), 9))
            + chunk(b'IEND', b''))


def data_url() -> str:
    return 'data:image/png;base64,' + base64.b64encode(quadrant_png()).decode('ascii')


# ================================================================ HTTP

def post(payload: dict, api_key: str, path: str = '/responses', timeout: int = 120):
    """发一次流式请求，返回 (http_status, 正文, 事件类型计数, usage)"""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + api_key,
                 'Accept': 'text/event-stream'},
        method='POST')
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')[:800], {}, {}
    except Exception as e:
        return -1, '{}: {}'.format(type(e).__name__, e), {}, {}

    parts, counts, usage = [], {}, {}
    for raw in resp:
        line = raw.decode('utf-8', 'replace').strip()
        if not line.startswith('data:'):
            continue
        body = line[5:].strip()
        if body == '[DONE]':
            continue
        try:
            evt = json.loads(body)
        except Exception:
            continue
        etype = evt.get('type') or 'chat.chunk'
        counts[etype] = counts.get(etype, 0) + 1
        if etype == 'response.output_text.delta':
            parts.append(evt.get('delta') or '')
        elif etype == 'response.completed':
            usage = ((evt.get('response') or {}).get('usage')) or {}
    return resp.status, ''.join(parts), counts, usage


def reasoning_tokens(usage: dict) -> int:
    return int(((usage.get('output_tokens_details') or {}).get('reasoning_tokens')) or 0)


# ================================================================ 生产模块加载

def load_production():
    """按桩模块方式加载仓库里真实的 LLM 组件（见 PROJECT_INDEX「开发环境与验证」）

    系统 Python 没有本项目的依赖，`import core` 会在包 __init__ 里连锁导入 rich
    而失败，所以预注入桩包再按路径加载目标模块。桩掉的都是与请求组装无关的东西
    （logger、客户端池、异常包装），组装逻辑本身是真代码。
    """
    class _Silent:
        def __getattr__(self, _):
            return lambda *a, **k: None

    for name in ('core', 'core.client', 'core.client.llm'):
        m = types.ModuleType(name)
        m.__path__ = []
        sys.modules[name] = m
    sys.modules['core.client.llm'].logger = _Silent()

    for mod, attrs in (
        ('core.client.llm.llm_interfaces', {'IContextManager': type('IContextManager', (), {})}),
        ('core.client.llm.llm_client_pool', {'ClientPool': type('ClientPool', (), {})}),
        ('core.client.llm.llm_exceptions', {
            'APIException': type('APIException', (Exception,), {}),
            'wrap_openai_error': lambda *a, **k: None,
            'OpenAIErrorWrapper': type('OpenAIErrorWrapper', (Exception,), {}),
            'TimeoutErrorWrapper': type('TimeoutErrorWrapper', (Exception,), {}),
        }),
    ):
        m = types.ModuleType(mod)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[mod] = m

    def load(mod, fname):
        spec = importlib.util.spec_from_file_location(
            mod, os.path.join(REPO, 'core', 'client', 'llm', fname))
        m = importlib.util.module_from_spec(spec)
        sys.modules[mod] = m
        spec.loader.exec_module(m)
        return m

    ns = types.SimpleNamespace()
    ns.constants = load('core.client.llm.llm_constants', 'llm_constants.py')
    ns.tags = load('core.client.llm.llm_state_tags', 'llm_state_tags.py')
    ns.role_config = load('core.client.llm.llm_role_config', 'llm_role_config.py')
    ns.context = load('core.client.llm.llm_context', 'llm_context.py')
    ns.builder = load('core.client.llm.llm_message_builder', 'llm_message_builder.py')
    ns.processor = load('core.client.llm.llm_processor', 'llm_processor.py')
    return ns


def load_role(prod, role_file: str = '助理.py'):
    """按 role_loader 的口径把角色文件里的模块级变量灌进 RoleConfig"""
    src = io.open(os.path.join(REPO, 'LLM', role_file), encoding='utf-8').read()
    ns = {}
    exec(compile(src, role_file, 'exec'), ns)
    cfg = prod.role_config.RoleConfig()
    for field in cfg.__dataclass_fields__:
        if field in ns:
            setattr(cfg, field, ns[field])
    return cfg


def role_system_prompt() -> str:
    src = io.open(os.path.join(REPO, 'LLM', '助理.py'), encoding='utf-8').read()
    m = re.search(r"system_prompt\s*=\s*'''(.*?)'''", src, re.S)
    return m.group(1) if m else ''


def emotion_prompt() -> str:
    """复刻 llm_output_openmate.build_emotion_prompt 的产物（雾切响子的 18 个状态）"""
    states = ['普通', '审视', '困惑', '害羞', '沉思', '得意', '严肃', '愤怒', '惊讶',
              '惊慌', '推理', '担忧', '笃定', '陈述', '微笑', '指证', '失落', '背影']
    return ('\n\n# 情绪表达\n'
            '你的回复会显示在一个支持情绪立绘的桌面看板上。'
            '请在回复的最开头输出一个情绪标签，格式为 <state:情绪ID>；'
            '若回复中情绪发生明显变化，可在变化处再插入新标签。\n'
            '可用的情绪ID如下：\n' + '\n'.join('- ' + s for s in states) + '\n'
            '标签中只能使用上面列出的情绪ID，从中挑选最贴切的一个，不要自创或使用含义文字。\n'
            '平静陈述等没有明显情绪起伏时，使用默认情绪 普通。\n'
            '不要在代码块内输出情绪标签，除标签本身外不要输出任何关于情绪的说明。')


# ================================================================ 探针

def p1_vision_with_websearch(key, prod, role) -> bool:
    """P1：图片能与服务端内置 web_search 工具在同一个 Responses 请求里共存"""
    payload = {
        'model': role.model,
        'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': '这张图被平均分成四个方块。只用一行回答，'
                                           '格式严格为：左上=颜色 右上=颜色 左下=颜色 右下=颜色'},
            {'type': 'input_image', 'image_url': data_url()},
        ]}],
        'max_output_tokens': 300,
        'tools': [{'type': 'web_search'}],
        'stream': True,
    }
    status, text, _, _ = post(payload, key)
    ok = status == 200 and all(c in text for c in ('红', '绿', '蓝'))
    print('  HTTP {}  回复: {}'.format(status, text.strip()[:120] or '（空）'))
    return ok


def _run_turns(key, prod, role, turns, extra_instructions=''):
    """用生产链路连着跑几轮，复刻 _stream_request 的历史更新（assistant 存原文）"""
    builder = prod.builder.MessageBuilder(app=None)
    p = prod.processor.LLMProcessor.__new__(prod.processor.LLMProcessor)
    cm = prod.context.ContextManager(max_length=role.max_context_length)
    placeholder = prod.constants.IMAGE_PLACEHOLDER
    out = []
    for text, image in turns:
        messages = builder.build_messages(role, text, cm, image_data=image,
                                          hotwords=None, selection_text='')
        params = p._build_responses_params(role, messages)
        if extra_instructions:
            params['instructions'] = params.get('instructions', '') + extra_instructions
        params['stream'] = True
        params['max_output_tokens'] = 600
        summary = p._describe_messages(messages)
        status, answer, _, _ = post(params, key)
        answer = answer.strip()
        user_content = messages[-1]['content']
        if isinstance(user_content, list):
            cm.demote_images(placeholder)
        cm.add_message('user', user_content)
        cm.add_message('assistant', answer)
        out.append((status, answer, summary))
    return out


def p2_image_in_history(key, prod, role) -> bool:
    """第 1 轮带图问无关问题，第 2 轮追问图里的内容——历史里的图必须还看得见"""
    turns = [('请简单介绍下你自己。', data_url()),
             ('上一次给你发的图片被平均分成四个方块，四个方块的颜色分别是什么？'
              '只用一行回答，格式为：左上=颜色 右上=颜色 左下=颜色 右下=颜色。', None)]
    results = _run_turns(key, prod, role, turns)
    for i, (status, answer, summary) in enumerate(results, 1):
        print('  第{}轮 HTTP {}  [{}]  {}'.format(i, status, summary, answer[:100]))
    return all(c in results[-1][1] for c in ('红', '绿', '蓝'))


def p3_state_tags_across_turns(key, prod, role) -> bool:
    """P3：历史存原文之后，情绪标签必须每一轮都还在（见 llm_state_tags 模块说明）"""
    turns = [('请讲一个开心的小故事。', None),
             ('请讲一个悲伤的小故事。', None),
             ('讲一个高兴的小故事。', None)]
    results = _run_turns(key, prod, role, turns, extra_instructions=emotion_prompt())
    ok = True
    for i, (status, answer, summary) in enumerate(results, 1):
        found = TAG.findall(answer)
        print('  第{}轮 HTTP {}  [{}]  标签: {}'.format(
            i, status, summary, '、'.join(found) if found else '**无**'))
        ok = ok and bool(found)
    return ok


def p4_thinking_disabled(key, prod, role) -> bool:
    """DEV_NOTES「已知问题」记的是「接受但完全无效」。此处只报数，由人判断结论是否还成立

    reasoning token 数天生抖动很大，单次对比说明不了问题，所以判定条件放得很松：
    只要两边都产生了 reasoning，就说明「关不掉」这条结论没变。
    """
    def once(disabled: bool):
        payload = {
            'model': role.model,
            'input': [{'role': 'user', 'content': '用一句话说明什么是快速排序。'}],
            'max_output_tokens': 400,
            'stream': True,
        }
        if disabled:
            payload['thinking'] = {'type': 'disabled'}
        status, text, _, usage = post(payload, key)
        return status, reasoning_tokens(usage), text.strip()

    st_off, r_off, _ = once(True)
    st_on, r_on, _ = once(False)
    print('  thinking:disabled -> HTTP {}  reasoning tokens = {}'.format(st_off, r_off))
    print('  不带该参数        -> HTTP {}  reasoning tokens = {}'.format(st_on, r_on))
    still_ineffective = st_off == 200 and r_off > 0
    print('  {}'.format('结论未变：参数被接受但关不掉思考' if still_ineffective
                        else '**结论可能变了**：disabled 时 reasoning 归零，值得复查 DEV_NOTES'))
    return st_off == 200 and st_on == 200


PROBES = [
    ('P1', '视觉 + web_search 共存（约定 21⑧）', p1_vision_with_websearch),
    ('P2', '历史里的图仍可见（生产链路两轮）', p2_image_in_history),
    ('P3', '情绪标签跨轮保持（约定 22，生产链路三轮）', p3_state_tags_across_turns),
    ('P4', 'thinking:disabled 是否生效（DEV_NOTES 已知状态）', p4_thinking_disabled),
]


def main(argv) -> int:
    wanted = {a.upper() for a in argv} or {name for name, _, _ in PROBES}
    try:
        from LLM.secrets import DEEPSEEK_API_KEY as key
    except Exception as e:
        print('[×] 无法读取密钥：{}'.format(e))
        return 2
    if not key:
        print('[×] DEEPSEEK_API_KEY 为空')
        return 2

    prod = load_production()
    role = load_role(prod)
    print('角色 {}  model={}  history={}  vision={}  web_search={}'.format(
        role.display_name, role.model, role.enable_history,
        role.enable_vision, role.enable_web_search))

    verdicts = []
    for name, title, fn in PROBES:
        if name not in wanted:
            continue
        print()
        print('=' * 72)
        print('{}  {}'.format(name, title))
        print('-' * 72)
        try:
            ok = fn(key, prod, role)
        except Exception as e:
            print('  [×] 探针自身出错: {}: {}'.format(type(e).__name__, e))
            ok = False
        print('  判定: {}'.format('通过' if ok else '**失败**'))
        verdicts.append((name, ok))

    print()
    print('=' * 72)
    for name, ok in verdicts:
        print('  {}  {}'.format(name, '通过' if ok else '**失败**'))
    return 0 if all(ok for _, ok in verdicts) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
