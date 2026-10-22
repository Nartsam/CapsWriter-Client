# coding: utf-8
"""
Anthropic Messages 路径单测（api_format='anthropic'，桩模块加载，不联网）

1. Chat 口味的内部消息转成 Anthropic 口味：system 提到顶层、data URL 图片转 base64 块
2. 请求参数：max_tokens 必填、stop_sequences、thinking_level 按协议转换、联网时挂搜索工具
3. 流解析：正文分片、server_tool_use 触发状态占位（只一次、早于正文）与 used_tools、
   usage 取终值、中断时断开连接、流内错误不写入记忆且不降级打字
4. 路由：provider 为 ollama 时不看 api_format；anthropic / responses 均不随搜索开关切换
5. 客户端：URL 拼接、认证头、SSE 解析、重试只发生在拿到响应头之前、HTTP 状态到包装
   异常的映射（与 openai SDK 路径同一套，llm_error_handler 靠类型决定弹框还是降级）
6. 厂商探针：P4/P5 的通过条件、读取失败释放响应与清理发送器、失败请求不影响后续实验

系统 Python 没有 httpx / openai，二者以最小替身注入；被测的都是仓库里的真代码。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import types
from contextlib import contextmanager, redirect_stdout
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _L:
    def __getattr__(self, n):
        return lambda *a, **k: None


for name in ('core', 'core.client', 'core.client.llm'):
    m = types.ModuleType(name)
    m.__path__ = []
    sys.modules[name] = m
sys.modules['core.client.llm'].logger = _L()

# ---- 外部库替身：只提供被测代码用到的名字
openai_stub = types.ModuleType('openai')
for err in ('AuthenticationError', 'RateLimitError', 'APITimeoutError', 'APIConnectionError', 'APIError'):
    setattr(openai_stub, err, type(err, (Exception,), {}))
openai_stub.OpenAI = lambda **kw: types.SimpleNamespace(kind='openai', **kw)
sys.modules['openai'] = openai_stub
ollama_stub = types.ModuleType('ollama')
ollama_stub.Client = lambda **kw: types.SimpleNamespace(kind='ollama', **kw)
sys.modules['ollama'] = ollama_stub


class FakeResponse:
    def __init__(self, status_code, payload=None, lines=()):
        self.status_code = status_code
        self.status = status_code  # urllib 的响应状态字段
        self.payload = payload if payload is not None else {}
        self.lines = list(lines)
        self.closed = False

    def read(self):
        return json.dumps(self.payload).encode('utf-8')

    def json(self):
        return self.payload

    def iter_lines(self):
        for line in self.lines:
            if isinstance(line, Exception):
                raise line
            yield line

    def __iter__(self):
        # 生产客户端按文本行读取，urllib 探针按字节行读取，共用同一份响应剧本。
        return (line.encode('utf-8') for line in self.iter_lines())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True


class FakeHttpClient:
    """按预设剧本依次返回响应或抛异常；记录每次发出的请求"""
    script = []

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.sent = []

    def build_request(self, method, url, json=None, headers=None, timeout=None):
        return {'method': method, 'url': url, 'json': json, 'headers': headers, 'timeout': timeout}

    def send(self, request, stream=False):
        self.sent.append(dict(request, stream=stream))
        step = FakeHttpClient.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def close(self):
        pass


httpx_stub = types.ModuleType('httpx')
httpx_stub.TransportError = type('TransportError', (Exception,), {})
httpx_stub.TimeoutException = type('TimeoutException', (httpx_stub.TransportError,), {})
httpx_stub.ConnectError = type('ConnectError', (httpx_stub.TransportError,), {})
httpx_stub.Client = FakeHttpClient
sys.modules['httpx'] = httpx_stub

m = types.ModuleType('core.client.llm.llm_interfaces')
m.IContextManager = type('IContextManager', (), {})
sys.modules[m.__name__] = m


def load(mod, fname):
    p = os.path.join(REPO, 'core', 'client', 'llm', fname)
    spec = importlib.util.spec_from_file_location(mod, p)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod] = module
    spec.loader.exec_module(module)
    return module


consts = load('core.client.llm.llm_constants', 'llm_constants.py')
exc = load('core.client.llm.llm_exceptions', 'llm_exceptions.py')
anth = load('core.client.llm.llm_anthropic', 'llm_anthropic.py')
pool_mod = load('core.client.llm.llm_client_pool', 'llm_client_pool.py')
tags = load('core.client.llm.llm_state_tags', 'llm_state_tags.py')
rc = load('core.client.llm.llm_role_config', 'llm_role_config.py')
load('core.client.llm.llm_memory', 'llm_memory.py')
ctx = load('core.client.llm.llm_context', 'llm_context.py')
mb = load('core.client.llm.llm_message_builder', 'llm_message_builder.py')
proc = load('core.client.llm.llm_processor', 'llm_processor.py')
error_handler = load('core.client.llm.llm_error_handler', 'llm_error_handler.py')
config_stub = types.ModuleType('config_client')
config_stub.ClientConfig = types.SimpleNamespace(llm_stop_key='esc')
with patch.dict(sys.modules, {'config_client': config_stub}):
    stop_monitor = load('core.client.llm.llm_stop_monitor', 'llm_stop_monitor.py')

RoleConfig = rc.RoleConfig
ToolNames = consts.ToolNames
LLMProcessor = proc.LLMProcessor
# 重试的退避不真睡
sleeps = []
anth.time = types.SimpleNamespace(sleep=lambda s: sleeps.append(s))

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


def raises(fn, cls):
    try:
        fn()
    except cls as e:
        return e
    except Exception as e:  # noqa: BLE001 —— 抛错了类型也要报出来
        print('    实际抛出 %s: %s' % (type(e).__name__, e))
        return None
    return None


IMG = 'data:image/jpeg;base64,QUJD'
p = LLMProcessor.__new__(LLMProcessor)
builder = mb.MessageBuilder(app=None)


def role_of(**kw):
    base = dict(name='助理', provider='deepseek', api_format='anthropic', model='m',
                system_prompt='你是助理', enable_web_search=True, enable_vision=True,
                enable_history=True, max_context_length=1024 * 1024, request_timeout=60)
    base.update(kw)
    return RoleConfig(**base)


# ---------------------------------------------------------------- 1
section('消息转换')
role = role_of()
cm = ctx.ContextManager(max_length=1024 * 1024)
cm.add_message('user', '上一句')
cm.add_message('assistant', '<state:微笑>上一句的回复')
messages = builder.build_messages(role, '图里是什么', cm, image_data=IMG)
system, items = anth.to_anthropic_messages(messages)
check(system == role.system_prompt, 'system 没有提到顶层字段：%r' % system)
check([it['role'] for it in items] == ['user', 'assistant', 'user'],
      'messages 里只能有 user / assistant：%r' % [it['role'] for it in items])
check(items[1]['content'] == '<state:微笑>上一句的回复', '历史里的 assistant 原文应原样上行')
last = items[-1]['content']
images = [b for b in last if b.get('type') == 'image']
check(len(images) == 1 and images[0]['source'] == {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'QUJD'},
      'data URL 没转成 base64 image 块：%r' % images)
check(any(b.get('type') == 'text' and '图里是什么' in b.get('text', '') for b in last), '文字块丢了')
_, url_items = anth.to_anthropic_messages([{'role': 'user', 'content': [
    {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/a.png'}}]}])
check(url_items[0]['content'][0]['source'] == {'type': 'url', 'url': 'https://example.invalid/a.png'},
      '非 data URL 的图片应转成 url 来源')
check(messages[-1]['content'][1]['type'] == 'image_url', '转换不能改动传入的消息（历史里存的是 Chat 口味）')

# ---------------------------------------------------------------- 2
section('请求参数')
params = p._build_anthropic_params(role_of(max_tokens=0, stop='甲, 乙'), messages)
check(params['max_tokens'] == anth.DEFAULT_MAX_TOKENS, 'max_tokens 为 0 时应给默认值（Anthropic 必填）')
check(params['stop_sequences'] == ['甲', '乙'] and 'stop' not in params, '停止序列应写成 stop_sequences')
check(params['tools'] == [anth.WEB_SEARCH_TOOL], '开启联网时应挂 web_search_20250305：%r' % params.get('tools'))
check(params['thinking'] == {'type': 'disabled'} and 'output_config' not in params,
      'thinking_level=none 时应关闭思考，不携带 effort')
check('stream' not in params, 'stream 由客户端按调用方式补上，参数里不应预先写死')
check(all(it['role'] != 'system' for it in params['messages']), 'system 不应留在 messages 里')
params = p._build_anthropic_params(role_of(max_tokens=321, enable_web_search=False, thinking_level='high',
                                           extra_options={'metadata': {'user_id': 'u'}}), messages)
check(params['max_tokens'] == 321, 'max_tokens 应取角色配置')
check('tools' not in params, '未开启联网时不应挂工具')
check(params['thinking'] == {'type': 'enabled'} and params['output_config'] == {'effort': 'high'},
      '具体档位应开启思考并使用 Anthropic output_config.effort')
check(params.get('metadata') == {'user_id': 'u'}, 'extra_options 应合并进参数')

section('Responses 请求：搜索开关不切换协议，思考强度使用标准字段')
for level in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
    for web in (False, True):
        custom_role = role_of(provider='custom', api_format='responses', thinking_level=level,
                              enable_web_search=web, enable_history=False)
        sdk = Mock()
        sdk.responses.create.return_value = iter([
            types.SimpleNamespace(type='response.output_text.delta', delta='回答'),
            types.SimpleNamespace(type='response.completed', response=types.SimpleNamespace(
                status='completed', usage=types.SimpleNamespace(output_tokens=2))),
        ])
        shown = []
        answer, tokens, _ = p._stream_request(sdk, shown.append, None, custom_role, None, messages)
        check(sdk.responses.create.call_count == 1 and not sdk.chat.completions.create.called,
              'custom 搜索=%s、档位=%s 时应只调用 Responses' % (web, level))
        sent = sdk.responses.create.call_args.kwargs
        check(sent['reasoning'] == {'effort': level} and 'extra_body' not in sent and 'thinking' not in sent,
              'Responses 应发送 reasoning.effort，不注入厂商 thinking 字段')
        check(sent.get('tools') == ([{'type': 'web_search'}] if web else None),
              '搜索开关只控制 web_search 工具')
        check(sent['stream'] is True and sent['timeout'] == custom_role.request_timeout
              and answer == '回答' and shown == ['回答'] and tokens == 2,
              'Responses 正文回调、终局 usage 和请求超时应保持生效')
    if level != 'none':
        anth_params = p._build_anthropic_params(role_of(thinking_level=level), messages)
        check(anth_params['thinking'] == {'type': 'enabled'}
              and anth_params['output_config'] == {'effort': level},
              'Anthropic 的具体档位交给后端解释：%s' % level)
    chat_params = p._build_request_params(role_of(thinking_level=level), messages)
    check(chat_params['reasoning_effort'] == level, 'Chat Completions 使用 reasoning_effort')

section('Ollama 保留思考开关语义')
for level, expected in [('none', False), ('high', True)]:
    sdk = Mock()
    sdk.chat.return_value = iter([])
    p._process_ollama_stream(sdk, role_of(provider='ollama', thinking_level=level), messages, None, None)
    check(sdk.chat.call_args.kwargs['think'] is expected, 'Ollama 档位=%s 的开关映射' % level)


# ---------------------------------------------------------------- 3
class StreamClient:
    def __init__(self, events, stop_after=None):
        self.events = events
        self.closed = False
        self.params = self.timeout = None

    @contextmanager
    def stream(self, params, timeout=None):
        self.params, self.timeout = params, timeout
        try:
            yield iter(self.events)
        finally:
            self.closed = True


def ev(type_, **kw):
    return dict(type=type_, **kw)


EVENTS = [
    ev('message_start', message={'usage': {'input_tokens': 500, 'output_tokens': 0}}),
    ev('content_block_start', index=0, content_block={'type': 'server_tool_use', 'name': 'web_search', 'input': {}}),
    ev('ping'),
    ev('content_block_delta', index=0, delta={'type': 'input_json_delta', 'partial_json': '{"query":"x"}'}),
    ev('content_block_stop', index=0),
    ev('content_block_start', index=1, content_block={'type': 'web_search_tool_result', 'content': [{}, {}]}),
    ev('content_block_stop', index=1),
    # 同一回合第二次搜索：状态与 used_tools 都不应重复
    ev('content_block_start', index=2, content_block={'type': 'server_tool_use', 'name': 'web_search', 'input': {}}),
    ev('content_block_start', index=3, content_block={'type': 'web_search_tool_result',
                                                      'content': {'type': 'web_search_tool_result_error',
                                                                  'error_code': 'max_uses_exceeded'}}),
    ev('content_block_start', index=4, content_block={'type': 'text', 'text': ''}),
    ev('content_block_delta', index=4, delta={'type': 'text_delta', 'text': '今天'}),
    ev('content_block_delta', index=4, delta={'type': 'text_delta', 'text': '晴。'}),
    ev('content_block_stop', index=4),
    ev('message_delta', delta={'stop_reason': 'end_turn'},
       usage={'input_tokens': 6000, 'output_tokens': 42, 'server_tool_use': {'web_search_requests': 2}}),
    ev('message_stop'),
]

section('流解析：正文、状态占位、used_tools、usage')
order, used = [], []
client = StreamClient(EVENTS)
text, tokens, _ = p._stream_request(
    client, lambda c: order.append(('text', c)), lambda: False, role_of(), None, messages,
    lambda s: order.append(('status', s)), used)
check(text == '今天晴。', '正文拼接错误：%r' % text)
check(tokens == 42, 'token 数应取 message_delta 里的终值，实为 %r' % tokens)
check(used == ['web_search'], 'used_tools 应只记一次 web_search：%r' % used)
statuses = [s for k, s in order if k == 'status']
check(statuses == [ToolNames.status('web_search')], '状态占位应恰好触发一次：%r' % statuses)
check(order and order[0][0] == 'status', '状态占位必须早于第一个正文分片：%r' % order[:2])
check(client.timeout == 60, '应把角色的 request_timeout 传给请求')
check(client.params.get('tools') == [anth.WEB_SEARCH_TOOL], '流式请求没有带上搜索工具')
check(client.closed, '流结束后连接应关闭')

section('流解析：中断后不再读、连接关闭')
seen = []
client = StreamClient(EVENTS)
text, _, _ = p._stream_request(
    client, lambda c: seen.append(c), lambda: bool(seen), role_of(), None, messages, None, [])
check(text == '今天', '中断后不应再拼接后续分片：%r' % text)
check(client.closed, '中断时应断开连接（退出 with 块）')

section('本地 Toast 取消半截回答：不写记忆、不降级历史图片；完成后关闭保留整轮')
store = Mock()
cm = ctx.ContextManager(max_length=10000, store=store, key='test')
cm.add_turn([{'type': 'text', 'text': '之前的问题'}, {'type': 'image_url', 'image_url': {'url': IMG}}],
            '之前的回答')
before = cm.get_history()
store.reset_mock()
request = stop_monitor.ToastRequest(lambda: None)
client = StreamClient(EVENTS)
text, _, _ = p._stream_request(client, lambda chunk: request.cancel(), request.cancelled.is_set,
                               role_of(), cm, messages, toast_request=request)
check(bool(text) and request.cancelled.is_set(), '应在已经生成部分正文后取消')
check(cm.get_history() == before, '取消整轮不得增加消息或降级历史图片')
check(not store.save.called, '取消回合不得写入记忆文件')
check(client.closed, '取消后应关闭正在读取的流')

request = stop_monitor.ToastRequest(lambda: None)
client = StreamClient(EVENTS)
text, _, _ = p._stream_request(client, None, request.cancelled.is_set,
                               role_of(), cm, messages, toast_request=request)
request.cancel()
check(not request.cancelled.is_set() and cm.get_history()[-1]['content'] == text,
      '生成已经完成时关窗应保留完整回答')
check(store.save.call_count == 1, '完整回合应正常落盘一次')

section('流解析：正常空回复不写记忆')
client = StreamClient([ev('message_start', message={'usage': {}}),
                       ev('message_delta', delta={'stop_reason': 'end_turn'}), ev('message_stop')])
cm = ctx.ContextManager(max_length=10000)
text, tokens, _ = p._stream_request(client, None, None, role_of(), cm, messages, None, None)
check((text, tokens) == ('', 0), '空回复应返回空串与 0：%r' % ((text, tokens),))
check(not cm.get_history(), '正常空回复也不应写入记忆')

section('流内错误：首字前或部分输出后都抛响应错误、关闭连接、不写记忆')
for prefix in ('', '尚未完成的回答'):
    events = [ev('message_start', message={'usage': {}})]
    if prefix:
        events.append(ev('content_block_delta', delta={'type': 'text_delta', 'text': prefix}))
    events.extend([ev('error', error={'type': 'overloaded_error', 'message': 'busy'}),
                   ev('content_block_delta', delta={'type': 'text_delta', 'text': '错误后的内容'})])
    client = StreamClient(events)
    processor = LLMProcessor(types.SimpleNamespace(get_client=lambda **kw: client))
    cm = ctx.ContextManager(max_length=10000)
    cm.add_turn('之前的问题', '之前的回答')
    before = cm.get_history()
    shown = []
    e = raises(lambda: processor.process(role_of(), messages, callback=shown.append,
                                          context_manager=cm), exc.APIResponseErrorWrapper)
    check(e is not None, '流内错误必须沿 process 传给展示层')
    check(client.closed, '流内错误后必须断开连接')
    check(cm.get_history() == before, '失败回合不能改动已有记忆')
    check(''.join(shown) == prefix, '错误后的正文不应继续交付')
    check(e is not None and not error_handler.should_fallback_to_original(e),
          '流内错误应提示失败，不应把识别原文打进当前窗口')

# ---------------------------------------------------------------- 4
section('路由')
called = []
router = LLMProcessor.__new__(LLMProcessor)
for name in ('_process_ollama_stream', '_process_anthropic_stream',
             '_process_responses_stream', '_process_openai_stream'):
    setattr(router, name, (lambda n: lambda *a, **k: (called.append(n), ('', 0, 0.0))[1])(name))
cases = [
    (dict(api_format='anthropic', enable_web_search=True), '_process_anthropic_stream'),
    (dict(api_format='anthropic', enable_web_search=False), '_process_anthropic_stream'),
    (dict(api_format='openai', enable_web_search=True), '_process_responses_stream'),
    (dict(api_format='openai', enable_web_search=False), '_process_openai_stream'),
    (dict(provider='custom', api_format='responses', enable_web_search=True), '_process_responses_stream'),
    (dict(provider='custom', api_format='responses', enable_web_search=False), '_process_responses_stream'),
    (dict(provider='ollama', api_format='anthropic'), '_process_ollama_stream'),
    (dict(provider='ollama', api_format='responses'), '_process_ollama_stream'),
]
for kw, want in cases:
    called.clear()
    router._stream_request(None, None, None, role_of(enable_history=False, **kw), None, messages)
    check(called == [want], '%r 应走 %s，实为 %r' % (kw, want, called))

section('客户端池按 api_format 区分')
pool = pool_mod.ClientPool()
a = pool.get_client('deepseek', 'https://x.invalid/anthropic', 'k', api_format='anthropic')
o = pool.get_client('deepseek', 'https://x.invalid/anthropic', 'k')
check(isinstance(a, anth.AnthropicClient), 'api_format=anthropic 应返回 AnthropicClient')
check(getattr(o, 'kind', '') == 'openai', '同一地址的 OpenAI 格式客户端不能被缓存串用')
check(pool.get_client('deepseek', 'https://x.invalid/anthropic', 'k', api_format='anthropic') is a,
      '同一组参数应复用缓存的客户端（保持连接）')
custom_client = pool.get_client('custom', 'https://custom.invalid/api', 'k', api_format='responses')
check(custom_client.kind == 'openai' and custom_client.base_url == 'https://custom.invalid/api',
      'custom 使用通用 OpenAI 客户端，接口根地址不强制添加 /v1')

# ---------------------------------------------------------------- 5
section('客户端：URL、认证头、SSE')
c = anth.AnthropicClient('https://api.example.invalid/anthropic/', 'sk-test', 30, 'deepseek')
check(c.url == 'https://api.example.invalid/anthropic/v1/messages', 'URL 拼接错误：%r' % c.url)
FakeHttpClient.script = [FakeResponse(200, lines=[
    'event: message_start', 'data: {"type": "message_start"}', '',
    'data: {坏的 JSON', 'data: {"type": "ping"}', ': 注释行'])]
with c.stream({'model': 'm'}, timeout=12) as events:
    got = [e['type'] for e in events]
sent = c._http.sent[-1]
check(got == ['message_start', 'ping'], 'SSE 解析应只取 data 行并跳过坏行：%r' % got)
check(sent['json'].get('stream') is True and sent['stream'] is True, 'stream() 应以流式发出且请求体带 stream')
check(sent['headers'].get('x-api-key') == 'sk-test' and 'anthropic-version' in sent['headers'],
      'Anthropic 认证头缺失')
check(sent['timeout'] == 12, '单次请求的超时没有传下去')

section('客户端：重试与异常映射')


def run_create(script, **kw):
    FakeHttpClient.script = list(script)
    client = anth.AnthropicClient('https://x.invalid/anthropic', 'k', 30, 'deepseek')
    result = {}

    def call():
        result['value'] = client.create({'model': 'm'}, **kw)
    return client, result, call


client, result, call = run_create([FakeResponse(503, {'error': {'message': 'busy'}}),
                                   FakeResponse(200, {'content': [{'type': 'text', 'text': 'ok'}]})])
call()
check(len(client._http.sent) == 2 and anth.extract_text(result['value']) == 'ok', '5xx 应重试后成功')
check(client._http.sent[0]['json'].get('stream') is False, 'create() 的请求体应带 stream=False')

client, _, call = run_create([FakeResponse(503, {'error': {'message': 'busy'}})], max_retries=0)
e = raises(call, exc.APIResponseErrorWrapper)
check(e is not None and getattr(e, 'status_code', None) == 503, '5xx 不重试时应映射为 APIResponseErrorWrapper 并带状态码')

client, _, call = run_create([FakeResponse(401, {'error': {'message': 'bad key'}})])
check(raises(call, exc.AuthenticationErrorWrapper) is not None and len(client._http.sent) == 1,
      '401 应映射为认证错误且不重试')

client, _, call = run_create([FakeResponse(429, {})], max_retries=0)
check(raises(call, exc.RateLimitErrorWrapper) is not None, '429 应映射为限流错误')

client, _, call = run_create([httpx_stub.TimeoutException('slow'), FakeResponse(200, {})])
check(raises(call, exc.TimeoutErrorWrapper) is not None and len(client._http.sent) == 1,
      '超时应映射为超时错误且不重试（超时本身就要等很久）')

client, _, call = run_create([httpx_stub.ConnectError('refused')] * 3)
check(raises(call, exc.ConnectionErrorWrapper) is not None and len(client._http.sent) == 3,
      '连接失败应重试 MAX_RETRIES 次后映射为连接错误')

# ---------------------------------------------------------------- 6
# 只导入探针函数，沿用上面已加载的生产模块，不调用读取角色或凭证的入口。
import probe_llm_live as live
import probe_role_segments as segments

prod = types.SimpleNamespace(role_config=rc, builder=mb, processor=proc, tags=tags)
openmate = segments.load_openmate(prod)
probe_role = role_of(api_url='https://example.invalid/anthropic', api_key='test-key',
                     enable_result_splitter=True)
stream_error = ev('error', error={'type': 'overloaded_error', 'message': 'busy'})


def probe_events(thinking=False, search=False):
    events = [ev('message_start', message={'usage': {}})]
    if thinking:
        events.append(ev('content_block_start', content_block={'type': 'thinking'}))
    if search:
        events.extend([
            ev('content_block_start', content_block={'type': 'server_tool_use', 'name': 'web_search'}),
            ev('content_block_start', content_block={'type': 'web_search_tool_result', 'content': []}),
        ])
    return events + [
        ev('content_block_delta', delta={'type': 'text_delta', 'text': '有效回答'}),
        ev('message_delta', delta={'stop_reason': 'end_turn'}), ev('message_stop'),
    ]


def probe_response(events):
    return FakeResponse(200, lines=[item if isinstance(item, Exception)
                                    else 'data: ' + json.dumps(item) for item in events])


def probe_result(fn, *event_lists):
    responses = [probe_response(events) for events in event_lists]
    with patch.object(live.urllib.request, 'urlopen', side_effect=responses), redirect_stdout(io.StringIO()):
        result = fn(None, prod, probe_role)
    check(all(r.closed for r in responses), '探针完成后应关闭全部响应')
    return result


section('探针：思考开关需要有效对照组')
for off, on, expected in (
    (probe_events(), probe_events(thinking=True), True),
    ([stream_error], probe_events(thinking=True), False),
    (probe_events(), [stream_error], False),
    (probe_events(), probe_events(), False),
    (probe_events(thinking=True), probe_events(thinking=True), False),
):
    check(probe_result(live.p4_thinking_disabled, off, on) == expected,
          'P4 应仅在关闭组无思考、有效对照组有思考时通过')

section('探针：错误、空正文和截断不能判为通过')
for invalid in ('empty', 'truncated', 'error'):
    events = probe_events(search=True)
    if invalid == 'empty':
        events[-3]['delta']['text'] = '  '
    elif invalid == 'truncated':
        events[-2]['delta']['stop_reason'] = 'max_tokens'
    else:
        events.append(stream_error)
    check(not probe_result(live.p4_thinking_disabled, events, probe_events(thinking=True)),
          'P4 错把 %s 判为通过' % invalid)
    check(not probe_result(live.p5_web_search_executes, events), 'P5 错把 %s 判为通过' % invalid)

section('探针：搜索必须收到无错误的工具结果')
check(probe_result(live.p5_web_search_executes, probe_events(search=True)), '正常搜索应通过')
check(not probe_result(live.p5_web_search_executes, probe_events()), '没有搜索事件不能通过')
events = probe_events(search=True)
del events[2]
check(not probe_result(live.p5_web_search_executes, events), '只有搜索开始事件不能通过')
check(not probe_result(live.p5_web_search_executes, events[:2] + [stream_error]),
      '搜索开始后流内报错、没有正文不能通过')
events = probe_events(search=True)
events[2]['content_block']['content'] = {'type': 'web_search_tool_result_error', 'error_code': 'unavailable'}
check(not probe_result(live.p5_web_search_executes, events), '工具失败后生成正文也不能通过')
events = probe_events(search=True)
events[-3]['delta']['text'] = '<tool_call>web_search</tool_call>'
check(not probe_result(live.p5_web_search_executes, events), '工具调用泄漏到正文不能通过')

section('探针：读取失败关闭响应并清理发送器，后续请求仍可完成')
response = probe_response([probe_events()[-3], TimeoutError('read timed out')])
sender = Mock()
with patch.object(live.urllib.request, 'urlopen', return_value=response):
    result = segments._run_anthropic(probe_role, {}, sender)
check(response.closed, '读取失败后必须关闭响应')
check(sender.abort.call_count == 1 and not sender.finish.called, '读取失败应中止发送器，不走完成路径')
check(result['errors'] == ['TimeoutError'], '读取失败应留下无效样本的错误类型')

responses = [probe_response([TimeoutError('read timed out')]), probe_response(probe_events())]
with patch.object(live.urllib.request, 'urlopen', side_effect=responses):
    results = [segments.run_request(prod, openmate, probe_role, {}, '测试角色', '问题', [])
               for _ in responses]
check([r['valid'] for r in results] == [False, True], '一个失败请求不能中止后续实验')
check(all(r.closed for r in responses), '分段探针应关闭所有响应')

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
