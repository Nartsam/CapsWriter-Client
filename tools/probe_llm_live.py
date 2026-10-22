# coding: utf-8
"""
厂商行为探针（真发请求，会消耗额度）

有几条设计结论是**押在 DeepSeek 当前行为上**的（写在对应代码注释与 DEV_NOTES 里），本地
单测证不了，改厂商模型/接口时也不会有任何报错——只会表现成某个功能悄悄失灵。
这个脚本把它们一次性重测一遍，坏了哪条一眼看得出来。全部走生产路径：DeepSeek 后端的
Anthropic 兼容接口（api_format='anthropic'，见 core/client/llm/llm_anthropic.py）。

  P1 视觉 + 联网搜索共存         带图的请求照常挂搜索工具，不另走分支（llm_processor._stream_request）
  P2 历史里的图仍可见            「历史里只保留最近一张真图」的前提（ContextManager.demote_images）
  P3 情绪标签跨轮保持            历史存原文才有意义（llm_state_tags 模块说明）
  P4 thinking:disabled 是否生效  Anthropic 接口上应生效（Responses 上无效，见 DEV_NOTES）
  P5 联网搜索真的执行            Responses 接口曾静默忽略搜索工具，flash 把工具调用当正文吐出

请求都用仓库里**真实的** RoleConfig / MessageBuilder / ContextManager /
LLMProcessor 组装，只桩掉网络与 app，所以它们同时也是生产链路的回归测试。

用法：python tools/probe_llm_live.py [P1 P3 ...]     # 不给参数则全跑
密钥经 LLM.backends 从 LLM.secrets 读入，**从不打印**，输出里也不含认证头。
"""
from __future__ import annotations

import base64
import dataclasses
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

def post(payload: dict, role, timeout: int = 120, on_text=None) -> dict:
    """按生产路径发一次 Anthropic 流式请求（地址与请求头同 AnthropicClient）

    返回 dict：status（HTTP 状态，建连异常为 -1）、text、counts（事件计数，块与增量带类型，
    如 block_start[server_tool_use]）、usage（终值）、stop_reason、error、tool_errors
    on_text: 每个正文分片到达时回调（probe_role_segments 用它驱动分段发送器）
    """
    req = urllib.request.Request(
        role.api_url.rstrip('/') + '/v1/messages',
        data=json.dumps(dict(payload, stream=True), ensure_ascii=False).encode('utf-8'),
        headers={'content-type': 'application/json', 'x-api-key': role.api_key,
                 'anthropic-version': '2023-06-01', 'accept': 'text/event-stream'},
        method='POST')
    result = {'status': None, 'text': '', 'counts': {}, 'usage': {}, 'stop_reason': '',
              'error': '', 'tool_errors': []}
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        with e:
            result.update(status=e.code, error=e.read().decode('utf-8', 'replace')[:800])
        return result
    except Exception as e:
        result.update(status=-1, error='{}: {}'.format(type(e).__name__, e))
        return result

    result['status'] = resp.status
    parts, counts = [], result['counts']
    # 读取期间断连、超时或回调抛错时也关闭响应；异常由各探针的请求边界记录。
    with resp:
        for raw in resp:
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            try:
                evt = json.loads(line[5:].strip())
            except ValueError:
                continue
            etype = evt.get('type') or '?'
            delta = evt.get('delta') or {}
            block = evt.get('content_block') or {}
            if etype == 'content_block_start':
                key = 'block_start[{}]'.format(block.get('type'))
                if block.get('type', '').endswith('_tool_result'):
                    content = block.get('content')
                    if isinstance(content, dict) and content.get('error_code'):
                        result['tool_errors'].append(content['error_code'])
            elif etype == 'content_block_delta':
                key = 'delta[{}]'.format(delta.get('type'))
            else:
                key = etype
            counts[key] = counts.get(key, 0) + 1
            if etype == 'content_block_delta' and delta.get('type') == 'text_delta':
                parts.append(delta.get('text') or '')
                if on_text:
                    on_text(delta.get('text') or '')
            elif etype == 'message_start':
                result['usage'].update((evt.get('message') or {}).get('usage') or {})
            elif etype == 'message_delta':
                result['usage'].update(evt.get('usage') or {})
                result['stop_reason'] = delta.get('stop_reason') or result['stop_reason']
            elif etype == 'error':
                result['error'] = str((evt.get('error') or {}).get('message') or evt)[:300]
    result['text'] = ''.join(parts)
    return result


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
            'APIResponseErrorWrapper': type('APIResponseErrorWrapper', (Exception,), {}),
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
    load('core.client.llm.llm_memory', 'llm_memory.py')
    ns.context = load('core.client.llm.llm_context', 'llm_context.py')
    ns.builder = load('core.client.llm.llm_message_builder', 'llm_message_builder.py')
    ns.anthropic = load('core.client.llm.llm_anthropic', 'llm_anthropic.py')
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

def production_params(prod, role, text, image=None) -> dict:
    """生产链路组装的单轮请求参数（MessageBuilder + _build_anthropic_params）"""
    builder = prod.builder.MessageBuilder(app=None)
    messages = builder.build_messages(role, text, None, image_data=image,
                                      hotwords=None, selection_text='')
    p = prod.processor.LLMProcessor.__new__(prod.processor.LLMProcessor)
    return p._build_anthropic_params(role, messages)


def p1_vision_with_websearch(key, prod, role) -> bool:
    """P1：图片能与联网搜索工具在同一个请求里共存"""
    params = production_params(prod, role, '这张图被平均分成四个方块。只用一行回答，'
                                           '格式严格为：左上=颜色 右上=颜色 左下=颜色 右下=颜色',
                               data_url())
    params['max_tokens'] = 300
    r = post(params, role)
    print('  HTTP {}  挂了搜索工具: {}  回复: {}'.format(
        r['status'], bool(params.get('tools')), r['text'].strip()[:120] or r['error'] or '（空）'))
    return (r['status'] == 200 and bool(params.get('tools'))
            and all(c in r['text'] for c in ('红', '绿', '蓝')))


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
        params = p._build_anthropic_params(role, messages)
        if extra_instructions:
            params['system'] = params.get('system', '') + extra_instructions
        params['max_tokens'] = 600
        summary = p._describe_messages(messages)
        r = post(params, role)
        answer = r['text'].strip()
        user_content = messages[-1]['content']
        if isinstance(user_content, list):
            cm.demote_images(placeholder)
        cm.add_message('user', user_content)
        cm.add_message('assistant', answer)
        out.append((r['status'], answer, summary))
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


def completed_reply(result: dict) -> bool:
    """HTTP 200 仍可能包含流内错误；验证模型能力前先确认拿到了完整正文。"""
    return (result['status'] == 200 and not result['error']
            and result['stop_reason'] == 'end_turn' and bool(result['text'].strip()))


def p4_thinking_disabled(key, prod, role) -> bool:
    """P4：Anthropic 接口上 thinking:disabled 应真的关掉思考

    角色 thinking_level='none' 时生产请求带这个参数；DeepSeek 的 Responses API 上它被接受
    但无效（DEV_NOTES），每次都先思考、首字慢约 0.5s。判据是有没有 thinking 块，
    不比 token 数（天生抖动）。不带参数的那次只作对照，用来确认判据本身看得到思考
    """
    def once(disabled: bool):
        variant = dataclasses.replace(role, thinking_level='none' if disabled else 'high', enable_web_search=False)
        params = production_params(prod, variant, '用一句话说明什么是快速排序。')
        if not disabled:
            # 对照组仍采用后端默认思考设置，不把显式 high 当作默认行为的证据。
            params.pop('thinking', None)
            params.pop('output_config', None)
        params['max_tokens'] = 400
        r = post(params, role)
        return r, r['counts'].get('block_start[thinking]', 0), 'thinking' in params

    off, t_off, sent_off = once(True)
    on, t_on, _ = once(False)
    print('  thinking:disabled（生产请求带: {}）-> HTTP {}  thinking 块 {} 个'.format(sent_off, off['status'], t_off))
    print('  不带该参数（服务端默认）         -> HTTP {}  thinking 块 {} 个'.format(on['status'], t_on))
    for label, result in (('关闭组', off), ('对照组', on)):
        if not completed_reply(result):
            print('  {}未得到完整正文：结束原因 {}，错误 {}'.format(
                label, result['stop_reason'] or '?', result['error'] or '无'))
    if t_on == 0:
        print('  对照组也没有思考块：服务端默认可能改成了不思考，判据需要复查')
    return (completed_reply(off) and completed_reply(on)
            and sent_off and t_off == 0 and t_on > 0)


# 工具调用被当成正文吐出时的痕迹（2026-10-07 在 Responses 接口上实测到的几种）
TOOL_LEAK = re.compile(r'DSML|<tool_|invoke name=|function_calls|<ipython>|tool_call')


def p5_web_search_executes(key, prod, role) -> bool:
    """P5：问时效性问题，服务端必须真的执行搜索

    2026-09 起 DeepSeek 的 Responses API 忽略 web_search，flash 把工具调用当正文吐出
    （DSML 标记、<tool_result>、整段 Python 代码），客户端不会报任何错，Toast 也不再
    显示「正在联网搜索」。判据：收到工具调用和无错误的搜索结果，正常生成完整正文，
    且正文里没有工具调用痕迹
    """
    params = production_params(prod, role, '今天美元兑人民币汇率是多少？')
    r = post(params, role)
    searches = r['counts'].get('block_start[server_tool_use]', 0)
    results = r['counts'].get('block_start[web_search_tool_result]', 0)
    leaked = bool(TOOL_LEAK.search(r['text']))
    print('  HTTP {}  server_tool_use {} 个  结束原因 {}  回复: {}'.format(
        r['status'], searches, r['stop_reason'] or '?',
        r['text'].strip().replace('\n', ' ')[:100] or r['error'] or '（空）'))
    if leaked:
        print('  **正文里出现了工具调用痕迹**：搜索没有被服务端执行')
    if r['tool_errors']:
        print('  搜索工具失败: {}'.format('、'.join(r['tool_errors'])))
    return (completed_reply(r) and searches > 0 and results > 0
            and not r['tool_errors'] and not leaked)


PROBES = [
    ('P1', '视觉 + 联网搜索共存（约定 21⑧）', p1_vision_with_websearch),
    ('P2', '历史里的图仍可见（生产链路两轮）', p2_image_in_history),
    ('P3', '情绪标签跨轮保持（约定 22，生产链路三轮）', p3_state_tags_across_turns),
    ('P4', 'thinking:disabled 在 Anthropic 接口上生效', p4_thinking_disabled),
    ('P5', '联网搜索真的执行（工具调用没有被当成正文）', p5_web_search_executes),
]


def main(argv) -> int:
    wanted = {a.upper() for a in argv} or {name for name, _, _ in PROBES}
    prod = load_production()
    try:
        from LLM.backends import resolve
        # 角色文件可能改到别的后端，这里固定用 DeepSeek 那一套：探针测的就是它的行为
        role = dataclasses.replace(load_role(prod), **resolve('deepseek'))
    except Exception as e:
        print('[×] 无法读取后端配置或密钥：{}'.format(e))
        return 2
    if not role.api_key:
        print('[×] DEEPSEEK_API_KEY 为空')
        return 2
    if role.api_format != 'anthropic':
        print('[×] DeepSeek 后端不是 Anthropic 格式，本探针的请求与生产路径不符')
        return 2
    key = role.api_key
    print('角色 {}  model={}  format={}  history={}  vision={}  web_search={}'.format(
        role.display_name, role.model, role.api_format, role.enable_history,
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
