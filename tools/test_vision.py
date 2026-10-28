# coding: utf-8
"""
视觉输入功能的桩模块单测

本目录是打包后的 Release，系统 Python 没有项目依赖（rich / pynput / PIL / openai），
`import core` 会在包 __init__ 里连锁失败。按 PROJECT_INDEX「开发环境与验证」的做法：
先往 sys.modules 里预注入桩包，再用 importlib 按文件路径加载目标模块。

用法（在仓库根目录执行）：python <本文件>
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import struct
import sys
import types
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True

# 仓库根目录按本文件位置推导，脚本从任何工作目录跑都成立
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.environ.get('CAPSWRITER_ROOT') or REPO
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PASS = []
FAIL = []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    if not cond:
        print('  [×] %s  %s' % (name, detail))


# ====================================================================
# 桩模块
# ====================================================================

class _StubLogger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def install_stubs():
    def pkg(name, **attrs):
        mod = types.ModuleType(name)
        mod.__path__ = []
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    log = _StubLogger()
    pkg('core', logger=log)
    pkg('core.client', logger=log)
    pkg('core.client.llm', logger=log)
    pkg('core.client.vision', logger=log)
    pkg('core.ui', logger=log)

    # rich：state.py 在模块级构造 Console
    rich = pkg('rich')
    console_mod = pkg('rich.console')

    class Console:
        def __init__(self, **kw):
            pass

        def print(self, *a, **k):
            pass

    console_mod.Console = Console
    theme_mod = pkg('rich.theme')
    theme_mod.Theme = lambda *a, **k: None
    rich.console = console_mod
    rich.theme = theme_mod


def load(module_name, rel_path):
    """按文件路径加载模块，并登记进 sys.modules（供相对导入解析）"""
    path = os.path.join(ROOT, rel_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ====================================================================
# 1. estimate_tokens 容忍多模态 content
# ====================================================================

def test_estimate_tokens(consts):
    et = consts.estimate_tokens
    print('\n[1] estimate_tokens 多模态容忍')

    # 不钉估算系数（每字几个 token、一张图算多少），只验性质：图片的估算与 base64 长度无关
    check('纯文本有正数估算', et('你好世界') > 0, et('你好世界'))
    check('空值返回 0', et('') == 0 and et(None) == 0)

    small = 'data:image/png;base64,AAAA'
    huge = 'data:image/png;base64,' + 'A' * 400000

    def chat(url):
        return [{'type': 'text', 'text': '看看这个报错'}, {'type': 'image_url', 'image_url': {'url': url}}]

    def resp(url):
        return [{'type': 'input_text', 'text': 'abc'}, {'type': 'input_image', 'image_url': url}]

    check('Chat 口味：40 万字符的 base64 不撑大估算', et(chat(huge)) == et(chat(small)),
          (et(chat(huge)), et(chat(small))))
    check('Chat 口味：图片本身计入估算', et(chat(small)) > et('看看这个报错'))
    check('Responses 口味同样与 base64 长度无关', et(resp(huge)) == et(resp(small)),
          (et(resp(huge)), et(resp(small))))
    check('非字符串非列表不炸', isinstance(et(12345), int))
    check('列表里混入非法条目不炸', et([None, 'x', 42]) >= 1)


# ====================================================================
# 2. ContextManager.demote_images
# ====================================================================

def test_demote_images(ctx_mod, consts):
    print('\n[2] 历史只保留最近一张图')
    ph = consts.IMAGE_PLACEHOLDER
    cm = ctx_mod.ContextManager(max_length=100000)

    img = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAA'}}
    cm.add_message('user', [{'type': 'text', 'text': '第一张'}, dict(img)])
    cm.add_message('assistant', '好的')
    cm.add_message('user', [{'type': 'text', 'text': '第二张'}, dict(img)])
    cm.add_message('assistant', '收到')

    demoted = cm.demote_images(ph)
    check('两条历史里的图都被降级', demoted == 2, demoted)

    contents = [m['content'] for m in cm.history]
    check('降级后全部退回字符串', all(isinstance(c, str) for c in contents), contents)
    check('占位符已写入', ph in contents[0] and ph in contents[2], contents)
    check('文本部分未丢失', '第一张' in contents[0] and '第二张' in contents[2], contents)
    check('assistant 消息未被动过', contents[1] == '好的')

    check('再次降级为 0（幂等）', cm.demote_images(ph) == 0)

    # 关键性质：降级后紧接着写入的新带图消息应当是历史里唯一的列表
    cm.add_message('user', [{'type': 'text', 'text': '第三张'}, dict(img)])
    lists = [m for m in cm.history if isinstance(m['content'], list)]
    check('历史里最多只有一条列表形态的消息', len(lists) == 1, len(lists))

    # 裁剪逻辑不再因为列表 content 崩溃
    try:
        small = ctx_mod.ContextManager(max_length=10)
        small.add_message('user', [{'type': 'text', 'text': '一' * 50}, dict(img)])
        small.add_message('user', '再来一句')
        check('带图消息进入裁剪逻辑不抛异常', True)
    except Exception as e:
        check('带图消息进入裁剪逻辑不抛异常', False, '%s: %s' % (type(e).__name__, e))


# ====================================================================
# 3. Chat → Responses 口味转换
# ====================================================================

def test_responses_content(proc_mod):
    print('\n[3] Responses API 图片格式转换')
    conv = proc_mod.LLMProcessor._to_responses_content

    plain = '普通字符串'
    check('纯字符串原样返回（同一对象）', conv(plain) is plain)
    check('None 原样返回', conv(None) is None)

    url = 'data:image/png;base64,ABC'
    out = conv([{'type': 'text', 'text': '看图'},
                {'type': 'image_url', 'image_url': {'url': url}}])
    check('文本条目 type 变 input_text', out[0] == {'type': 'input_text', 'text': '看图'}, out[0])
    check('图片条目 type 变 input_image 且 image_url 是裸字符串',
          out[1] == {'type': 'input_image', 'image_url': url}, out[1])

    already = conv([{'type': 'input_text', 'text': 'x'},
                    {'type': 'input_image', 'image_url': url}])
    check('已是 Responses 口味则幂等',
          already == [{'type': 'input_text', 'text': 'x'},
                      {'type': 'input_image', 'image_url': url}], already)

    check('空 url 的图片条目被丢弃',
          conv([{'type': 'image_url', 'image_url': {'url': ''}}]) == [])
    check('非法条目被跳过', conv(['裸串', None, {'type': 'text', 'text': 'ok'}]) ==
          [{'type': 'input_text', 'text': 'ok'}])


def test_responses_params(proc_mod):
    print('\n[4] _build_responses_params 整体行为')

    class Role:
        model = 'deepseek-v4-flash-vision-exp'
        temperature = 0.3
        top_p = 0.9
        max_tokens = 4096
        enable_web_search = True
        thinking_level = 'none'
        stop = ''
        extra_options = None

    proc = proc_mod.LLMProcessor.__new__(proc_mod.LLMProcessor)
    url = 'data:image/png;base64,ZZZ'
    messages = [
        {'role': 'system', 'content': '你是助理'},
        {'role': 'user', 'content': '上一句'},
        {'role': 'assistant', 'content': '上一答'},
        {'role': 'user', 'content': [{'type': 'text', 'text': '看图'},
                                     {'type': 'image_url', 'image_url': {'url': url}}]},
    ]
    params = proc._build_responses_params(Role, messages)

    check('system 走 instructions', params.get('instructions') == '你是助理')
    check('max_tokens 映射为 max_output_tokens', params.get('max_output_tokens') == 4096)
    check('web_search 工具照常挂载', {'type': 'web_search'} in (params.get('tools') or []))
    inp = params['input']
    check('system 不在 input 里', len(inp) == 3, len(inp))
    check('纯文本历史消息内容原样透传',
          inp[0] == messages[1] and inp[1] == messages[2])
    check('带图消息已转成 Responses 口味',
          inp[2]['content'] == [{'type': 'input_text', 'text': '看图'},
                                {'type': 'input_image', 'image_url': url}], inp[2])
    check('原始 messages 未被就地改写',
          messages[3]['content'][1]['type'] == 'image_url')

    plain_msgs = [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'U'}]
    p2 = proc._build_responses_params(Role, plain_msgs)
    check('不带图时 input 条目内容不变', p2['input'] == [plain_msgs[1]], p2['input'])


# ====================================================================
# 5. DIB → BMP 拼头
# ====================================================================

def _make_bmp(width, height, bpp=24, bitfields=False, palette=0):
    """构造一张合法 BMP，返回 (完整 BMP 字节, 去掉文件头的 DIB 字节)"""
    header_size = 40
    row = ((width * bpp + 31) // 32) * 4
    pixels = bytes(row * height)
    compression = 3 if bitfields else 0
    dib = struct.pack('<IiiHHIIiiII', header_size, width, height, 1, bpp,
                      compression, len(pixels), 2835, 2835, palette, 0)
    if bitfields:
        dib += struct.pack('<III', 0x00FF0000, 0x0000FF00, 0x000000FF)
    dib += bytes(palette * 4) + pixels
    offset = 14 + len(dib) - len(pixels)
    bmp = b'BM' + struct.pack('<IHHI', 14 + len(dib), 0, 0, offset) + dib
    return bmp, dib


def test_dib_to_bmp(clip_mod):
    print('\n[5] DIB 拼 BITMAPFILEHEADER')
    f = clip_mod._dib_to_bmp

    for label, kwargs in [
        ('24bpp 无调色板', dict(bpp=24)),
        ('32bpp BI_RGB', dict(bpp=32)),
        ('32bpp BI_BITFIELDS', dict(bpp=32, bitfields=True)),
        ('8bpp 显式调色板', dict(bpp=8, palette=256)),
    ]:
        bmp, dib = _make_bmp(8, 4, **kwargs)
        out = f(dib)
        check('%s：还原出与原始逐字节一致的 BMP' % label, out == bmp,
              '得到 %r' % (out[:20] if out else out))

    # 8bpp 未写 biClrUsed 时按位深推算调色板项数
    row = ((8 * 8 + 31) // 32) * 4
    pixels = bytes(row * 4)
    dib = struct.pack('<IiiHHIIiiII', 40, 8, 4, 1, 8, 0, len(pixels), 0, 0, 0, 0)
    dib += bytes(256 * 4) + pixels
    out = f(dib)
    offset = struct.unpack_from('<I', out, 10)[0]
    check('8bpp 未写 biClrUsed 时按 2^bpp 推算调色板', offset == 14 + 40 + 256 * 4, offset)

    check('太短的输入返回 None', f(b'\x01\x02') is None)
    check('不认识的头大小返回 None', f(struct.pack('<I', 99) + bytes(40)) is None)


# ====================================================================
# 6. 图片状态机（挂载 / 绑定 / 消耗 / 回滚）
# ====================================================================

def test_state(state_mod):
    print('\n[6] 待挂图片状态机')
    S = state_mod.ClientState

    def new_state():
        return S()

    st = new_state()
    check('初始无待挂图片', st.pending_image is None)

    st.set_pending_image('IMG1')
    check('挂载后进入待挂槽', st.pending_image == 'IMG1')

    st.set_pending_image('IMG2')
    check('连按两次后者覆盖前者', st.pending_image == 'IMG2')

    bound = st.begin_image_snapshot('task-a')
    check('launch 时把图移入本次录音', bound == 'IMG2')
    check('移入后待挂槽清空（已绑定与待挂互斥）', st.pending_image is None)
    check('绑定后可按 task_id 查回', st.image_snapshots.get('task-a') == 'IMG2')

    # --- 消耗 ---
    got = st.pop_image_snapshot('task-a')
    check('结果到达时取出图片', got == 'IMG2')
    check('取出后字典里不再有', 'task-a' not in st.image_snapshots)
    check('取出不再产生待挂图（严格「下一条」：用掉即消失）', st.pending_image is None)

    # --- 上一句的结果到达时，下一句已经开始录音 ---
    st = new_state()
    st.set_pending_image('OLD')
    st.begin_image_snapshot('task-old')
    st.set_pending_image('NEW')
    st.begin_image_snapshot('task-new')
    st.pop_image_snapshot('task-old')
    check('消费上一句的图不影响下一句', st.pop_image_snapshot('task-new') == 'NEW')

    # --- 回滚：录音作废，图退回待挂 ---
    st = new_state()
    st.set_pending_image('IMG')
    st.begin_image_snapshot('t1')
    restored = st.rollback_image_snapshot('t1')
    check('作废时图退回待挂', restored == 'IMG' and st.pending_image == 'IMG')
    check('回滚后字典里不再有', 't1' not in st.image_snapshots)

    # --- 回滚不覆盖期间新截的图 ---
    st = new_state()
    st.set_pending_image('OLD')
    st.begin_image_snapshot('t1')
    st.set_pending_image('NEWER')          # 录音期间又按了一次快捷键
    restored = st.rollback_image_snapshot('t1')
    check('回滚不用旧图顶掉更新的待挂图', restored is None and st.pending_image == 'NEWER')

    # --- 没有图时各操作都安全 ---
    st = new_state()
    check('无图时 begin 返回 None', st.begin_image_snapshot('t') is None)
    check('无图时不写入空条目', st.image_snapshots == {})
    check('无图时 rollback 返回 None', st.rollback_image_snapshot('t') is None)
    check('无图时 pop 返回 None', st.pop_image_snapshot('t') is None)

    # --- reset 清理 ---
    st.set_pending_image('X')
    st.begin_image_snapshot('t')
    st.set_pending_image('Y')
    st.reset()
    check('reset 清空待挂图与已绑定的图', st.pending_image is None and st.image_snapshots == {})


# ====================================================================
# 7. 消息构建：带图消息结构
# ====================================================================

def test_message_builder(mb_mod):
    print('\n[7] MessageBuilder 带图消息')

    class Role:
        name = '助理'
        system_prompt = 'SYS'
        enable_datetime = False
        enable_hotwords = False
        prompt_prefix_selection = '选中文字：'
        prompt_prefix_input = '用户输入：'
        prompt_prefix_hotwords = '热词列表：'
        max_context_length = 4096

    builder = mb_mod.MessageBuilder(app=None)
    url = 'data:image/png;base64,QQQ'

    msgs = builder.build_messages(Role, '这是什么报错', None, image_data=url)
    last = msgs[-1]
    check('带图时 content 变列表', isinstance(last['content'], list), type(last['content']))
    check('文本条目在前', last['content'][0]['type'] == 'text')
    check('用户输入前缀保留', '用户输入：这是什么报错' in last['content'][0]['text'])
    check('图片条目是 Chat 口味（转换留给 llm_processor）',
          last['content'][1] == {'type': 'image_url', 'image_url': {'url': url}})

    plain = builder.build_messages(Role, '普通一句', None)
    check('不带图时 content 仍是字符串（旧行为逐字不变）',
          isinstance(plain[-1]['content'], str), type(plain[-1]['content']))


# ====================================================================
# 8. 剪贴板时效观测
# ====================================================================

def test_watcher(watch_mod):
    print('\n[8] 剪贴板变更时刻观测')
    import time as _t

    seq = {'v': 100}
    watch_mod._sequence_number = lambda: seq['v']

    W = watch_mod.ClipboardWatcher

    # 不启线程，只验判定逻辑（_refresh 由 seconds_since_change 同步调用）
    w = W(poll_interval=999)
    w._last_seq = seq['v']
    w._changed_at = None
    check('启动后剪贴板没变过 → 未知（None）', w.seconds_since_change() is None)

    seq['v'] = 101
    age = w.seconds_since_change()
    check('序号变化后立刻可算出年龄', age is not None and age < 0.5, age)

    # 同步补读：轮询没跑也要能捕捉到刚发生的变化（截完图 0.2s 就按快捷键）
    w._changed_at = _t.monotonic() - 300     # 假装上次变更是 5 分钟前
    seq['v'] = 102                            # 此刻刚复制了新内容
    age = w.seconds_since_change()
    check('轮询未及时，同步补读仍判为刚变（不会误杀新图）',
          age is not None and age < 0.5, age)

    # 序号不变时年龄持续增长
    w._changed_at = _t.monotonic() - 120
    age = w.seconds_since_change()
    check('序号不变则年龄持续增长', 119 < age < 121, age)

    # 启动第 1/2/3 次成功：后续尝试停止，首次成功只建立未知时刻的基线。
    for failures in range(watch_mod.START_ATTEMPTS):
        startup = W()
        startup._available = False  # 同一观测器曾失败后重新 start，也要恢复可用状态。
        with patch.object(watch_mod, '_sequence_number', side_effect=[0] * failures + [100]) as read, \
                patch.object(watch_mod.threading, 'Thread') as thread_factory, \
                patch.object(watch_mod.logger, 'warning', Mock()) as warning, \
                patch.object(startup._stop, 'wait', return_value=False) as wait:
            startup.start()
            check('失败 %d 次后成功启用' % failures, startup.available is True)
            check('成功后停止启动重试 %d' % failures, read.call_count == failures + 1)
            check('成功前按间隔等待 %d' % failures,
                  wait.call_count == failures
                  and all(call.args == (watch_mod.START_RETRY_INTERVAL,) for call in wait.call_args_list))
            check('启动成功不告警 %d' % failures, not warning.called)
            check('启动成功仅创建一个轮询线程 %d' % failures,
                  thread_factory.call_count == 1 and thread_factory.return_value.start.call_count == 1)
            startup.start()
            check('重复 start 不重新检查 %d' % failures, read.call_count == failures + 1)
            read.side_effect = None
            read.return_value = 100
            check('重试成功不会将已有图片当新图 %d' % failures, startup.seconds_since_change() is None)
            read.return_value = 101
            age = startup.seconds_since_change()
            check('基线后变更正常记录时效 %d' % failures, age is not None and age < 0.5)

    # 连续失败：只告警一次，保持原来的停用与不再自动重试行为。
    w2 = W()
    with patch.object(watch_mod, '_sequence_number', return_value=0) as read, \
            patch.object(watch_mod.threading, 'Thread') as thread_factory, \
            patch.object(watch_mod.logger, 'warning', Mock()) as warning, \
            patch.object(w2._stop, 'wait', return_value=False) as wait:
        w2.start()
        check('连续失败才告警一次', read.call_count == watch_mod.START_ATTEMPTS and warning.call_count == 1)
        check('最后失败后不再等待', wait.call_count == watch_mod.START_ATTEMPTS - 1)
        check('失败不创建轮询线程', not thread_factory.called)
        read.return_value = 123
        w2.seconds_since_change()
        check('停用后不再自动重试', read.call_count == watch_mod.START_ATTEMPTS)
    check('序号不可用时标记为不可用', w2.available is False)
    check('不可用时年龄返回 None', w2.seconds_since_change() is None)
    check('不可用时不会起线程', w2._thread is None)

    interrupted = W()
    with patch.object(watch_mod, '_sequence_number', return_value=0) as read, \
            patch.object(watch_mod.threading, 'Thread') as thread_factory, \
            patch.object(watch_mod.logger, 'warning', Mock()) as warning, \
            patch.object(interrupted._stop, 'wait', side_effect=lambda _: (interrupted.stop(), True)[1]):
        interrupted.start()
        check('退出可中止重试', read.call_count == 1)
        check('退出期间不启动线程或打印停用警告', not thread_factory.called and not warning.called)

    # 线程生命周期
    watch_mod._sequence_number = lambda: seq['v']
    def wait_until(condition, timeout=2.0):
        # 等到条件成立或超时，不用固定的 sleep：机器一忙固定等待就会偶发失败
        deadline = _t.monotonic() + timeout
        while not condition() and _t.monotonic() < deadline:
            _t.sleep(0.01)
        return condition()

    w3 = W(poll_interval=0.01)
    w3.start()
    check('可用时线程已启动', w3._thread is not None and w3._thread.is_alive())
    seq['v'] = 200
    check('后台轮询能自行发现变化', wait_until(lambda: w3._changed_at is not None))
    thread = w3._thread
    w3.stop()
    check('stop 后线程退出', wait_until(lambda: not thread.is_alive()))


def test_is_fresh(vm_mod, watch_mod):
    print('\n[9] VisionManager 时效判定')

    class FakeWatcher:
        def __init__(self, age, available=True):
            self._age = age
            self.available = available

        def seconds_since_change(self):
            return self._age

    vm = vm_mod.VisionManager.__new__(vm_mod.VisionManager)
    import config_client
    original = getattr(config_client.ClientConfig, 'vision_clipboard_max_age', 60)

    try:
        config_client.ClientConfig.vision_clipboard_max_age = 60
        vm._watcher = FakeWatcher(10)
        check('10s 前的图在 60s 时效内', vm._is_fresh() is True)

        vm._watcher = FakeWatcher(3600)
        check('一小时前的图判为过期', vm._is_fresh() is False)

        vm._watcher = FakeWatcher(None)
        check('时刻未知按过期处理（证不出来就不算）', vm._is_fresh() is False)

        vm._watcher = FakeWatcher(None, available=False)
        check('观测不可用时退回不检查（不连坐禁用功能）', vm._is_fresh() is True)

        config_client.ClientConfig.vision_clipboard_max_age = 0
        vm._watcher = FakeWatcher(99999)
        check('配置为 0 时完全不检查', vm._is_fresh() is True)

        config_client.ClientConfig.vision_clipboard_max_age = 'x'
        check('非法配置值退化为不检查而不是崩溃', vm._is_fresh() is True)
    finally:
        config_client.ClientConfig.vision_clipboard_max_age = original


# ====================================================================
# 11. 编码兜底与体积上限
# ====================================================================

class _FakeImage:
    """只提供 grab_clipboard_image 用得到的那几个属性，避免依赖 PIL"""

    def __init__(self, size, mode='RGB'):
        self.size = size
        self.mode = mode

    @property
    def width(self):
        return self.size[0]

    @property
    def height(self):
        return self.size[1]


def _run_grab(clip_mod, *, png_bytes, jpeg_bytes=None, jpeg_raises=False,
              raw_png=None, size=(800, 600), source='CF_DIB'):
    """在桩掉 PIL 相关helper 的前提下跑一次 grab_clipboard_image

    Returns:
        (PendingImage 或 None, {'png': 次数, 'jpeg': 次数})
    """
    calls = {'png': 0, 'jpeg': 0}
    image = _FakeImage(size)

    class _FakeUser32:
        @staticmethod
        def OpenClipboard(_):
            return 1

        @staticmethod
        def CloseClipboard():
            return 1

    def fake_encode_png(_img):
        calls['png'] += 1
        return b'P' * png_bytes

    def fake_encode_jpeg(_img):
        calls['jpeg'] += 1
        if jpeg_raises:
            raise ValueError('编码失败')
        return b'J' * jpeg_bytes

    saved = {name: getattr(clip_mod, name) for name in
             ('_user32', '_grab_from_clipboard', '_drop_dead_alpha',
              '_shrink_if_needed', '_encode_png', '_encode_jpeg')}
    try:
        clip_mod._user32 = _FakeUser32
        clip_mod._grab_from_clipboard = lambda: (image, source, raw_png)
        clip_mod._drop_dead_alpha = lambda img: img
        clip_mod._shrink_if_needed = lambda img: img
        clip_mod._encode_png = fake_encode_png
        clip_mod._encode_jpeg = fake_encode_jpeg
        return clip_mod.grab_clipboard_image(), calls
    finally:
        for name, value in saved.items():
            setattr(clip_mod, name, value)


def test_encode_fallback(clip_mod):
    print('\n[11] 编码兜底与体积上限')
    limit = clip_mod.MAX_IMAGE_BYTES

    # 上限本身是可调参数，断言一律用相对值构造，不钉死具体数字
    # （同 2026-09-12 关于 DEEPSEEK_MODEL 的决策：钉住会日常变动的值只会制造无效失败）
    under, over = limit - 1024, limit + 1024

    # --- PNG 没超限：原样发，不付 JPEG 重编码的时间 ---
    img, calls = _run_grab(clip_mod, png_bytes=under)
    check('PNG 未超限时挂载成功', img is not None)
    check('PNG 未超限时编码标记为 PNG', img and img.encoding == 'PNG')
    check('PNG 未超限时 data_url 是 png MIME',
          img and img.data_url.startswith('data:image/png;base64,'))
    check('PNG 未超限时完全不调用 JPEG 编码器', calls['jpeg'] == 0, calls)
    check('byte_size 记的是编码后字节数', img and img.byte_size == under)

    # --- PNG 超限、JPEG 压得下：改用 JPEG ---
    img, calls = _run_grab(clip_mod, png_bytes=over, jpeg_bytes=under)
    check('PNG 超限时回落到 JPEG', img is not None and img.encoding == 'JPEG')
    check('JPEG 兜底后 data_url 是 jpeg MIME',
          img and img.data_url.startswith('data:image/jpeg;base64,'))
    check('JPEG 兜底只编码一次', calls['jpeg'] == 1, calls)
    check('JPEG 兜底后 byte_size 跟着换成 JPEG 的', img and img.byte_size == under)

    # --- JPEG 也压不下：丢弃 ---
    img, calls = _run_grab(clip_mod, png_bytes=over, jpeg_bytes=over)
    check('JPEG 仍超限时丢弃', img is None)

    # --- JPEG 编码本身抛异常：按丢弃处理，不得把异常抛给调用方 ---
    img, calls = _run_grab(clip_mod, png_bytes=over, jpeg_bytes=1, jpeg_raises=True)
    check('JPEG 编码异常时返回 None 而不是崩溃', img is None)
    check('JPEG 编码异常时确实尝试过一次', calls['jpeg'] == 1, calls)

    # --- 剪贴板原生 PNG 且尺寸合格：复用原字节，一次编码都不做 ---
    raw = b'R' * under
    img, calls = _run_grab(clip_mod, png_bytes=99, raw_png=raw,
                           size=(clip_mod.MAX_EDGE_PX, 10), source='PNG')
    check('原生 PNG 尺寸合格时不重新编码', calls['png'] == 0 and calls['jpeg'] == 0, calls)
    check('原生 PNG 复用的是原字节', img and img.byte_size == len(raw))

    # --- 原生 PNG 但超限：同样要走兜底，不能因为复用路径就绕过体积检查 ---
    img, calls = _run_grab(clip_mod, png_bytes=99, jpeg_bytes=under,
                           raw_png=b'R' * over,
                           size=(clip_mod.MAX_EDGE_PX, 10), source='PNG')
    check('原生 PNG 超限时同样回落到 JPEG',
          img is not None and img.encoding == 'JPEG', img)

    # --- 尺寸取的是最终图片的尺寸 ---
    img, _ = _run_grab(clip_mod, png_bytes=under, size=(321, 123))
    check('宽高取自最终图片', img and (img.width, img.height) == (321, 123))


# ====================================================================
# 12. 剪贴板被占用时的退避重试
# ====================================================================

class _Win32Busy(Exception):
    """复刻 pywin32 抛出的形态：args = (5, 'OpenClipboard', '拒绝访问。')"""

    def __init__(self):
        super().__init__(5, 'OpenClipboard', '拒绝访问。')
        self.winerror = 5


def test_clipboard_retry(clipboard_mod, sel_mod, pyclip_stub, kb_stub):
    print('\n[12] 剪贴板被占用时的退避重试')
    busy = _Win32Busy()
    no_text = Exception('Clipboard contents have no standard formats available.')

    # --- 只认「被占用」，不认「没有文本格式」---
    check('识别 pywin32 的 ERROR_ACCESS_DENIED', clipboard_mod._is_clipboard_busy(busy))
    check('识别只有 winerror 属性的变体',
          clipboard_mod._is_clipboard_busy(
              type('E', (Exception,), {'winerror': 5})()))
    check('不把「没有文本格式」当成被占用',
          not clipboard_mod._is_clipboard_busy(no_text))
    check('不把普通异常当成被占用',
          not clipboard_mod._is_clipboard_busy(ValueError('别的错')))

    # --- 被占用时重试，最终成功 ---
    calls = {'n': 0}

    def flaky():
        calls['n'] += 1
        if calls['n'] < 3:
            raise busy
        return 'ok'

    check('被占用时重试到成功', clipboard_mod._retry_while_busy(flaky) == 'ok')
    check('重试次数符合预期', calls['n'] == 3, calls)

    # --- 剪贴板里是图片：必须只试一次，不能白等 5 轮退避 ---
    calls = {'n': 0}

    def always_no_text():
        calls['n'] += 1
        raise no_text

    try:
        clipboard_mod._retry_while_busy(always_no_text)
    except Exception:
        pass
    check('非占用类异常立即抛出，不重试（带图对话每句都会走这条路）',
          calls['n'] == 1, calls)

    # --- 一直被占用：耗尽次数后抛出，不静默吞掉 ---
    calls = {'n': 0}

    def always_busy():
        calls['n'] += 1
        raise busy

    raised = False
    try:
        clipboard_mod._retry_while_busy(always_busy)
    except Exception:
        raised = True
    check('一直被占用时耗尽次数后抛出', raised)
    check('重试上限生效', calls['n'] == clipboard_mod.CLIPBOARD_RETRY_ATTEMPTS, calls)

    # --- restore_clipboard 失败只返回 False，不抛 ---
    original_copy = pyclip_stub.copy
    try:
        def copy_busy(_v):
            raise busy
        pyclip_stub.copy = copy_busy
        check('restore_clipboard 失败返回 False 而不是抛异常',
              clipboard_mod.restore_clipboard('原内容') is False)
    finally:
        pyclip_stub.copy = original_copy

    check('restore_clipboard 正常时返回 True',
          clipboard_mod.restore_clipboard('原内容') is True and pyclip_stub.copies[-1] == '原内容')
    clipboard_mod.restore_clipboard('')
    check('空字符串也照样写回（还原的语义是恢复原状）',
          pyclip_stub.copies and pyclip_stub.copies[-1] == '', pyclip_stub.copies[-1:])

    # --- 核心回归：还原失败不得连累已经读到的选中文字 ---
    class Role:
        enable_read_selection = True
        name = '测试角色'
        enable_history = False
        selection_max_length = 1000

    class State:
        last_output_text = ''

    pyclip_stub.copies.clear()
    pyclip_stub.set_sequence(['原内容'.encode('utf-8'),
                              '选中的文字'.encode('utf-8')])
    original_copy = pyclip_stub.copy
    try:
        def copy_busy(_v):
            raise busy
        pyclip_stub.copy = copy_busy
        got = sel_mod.get_selected_text(Role, State)
    finally:
        pyclip_stub.copy = original_copy
    check('还原失败时仍返回已读到的选中文字（此前会被整个丢掉）',
          got == '选中的文字', repr(got))


# ====================================================================
# 13. 图片归档（落盘 / 改名 / 清理）
# ====================================================================

def test_image_archive(archive_mod, clip_mod):
    print('\n[13] 图片归档')
    import base64, shutil, tempfile

    # 临时目录放在仓库的 tools/ 下（同其他测试），不写系统临时目录
    base = tempfile.mkdtemp(prefix="archive-test-", dir=os.path.join(REPO, 'tools'))
    try:
        payload = b"fake-png-bytes"
        url = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
        img = clip_mod.PendingImage(url, 988, 1297, len(payload), "CF_DIBV5")

        # --- 基本落盘 ---
        path = archive_mod.save_mounted(base, img, keep=10)
        check('归档返回了路径', bool(path), path)
        saved = pathlib.Path(path)
        check('文件真的写出来了', saved.exists())
        check('写出的是原始字节', saved.read_bytes() == payload)
        check('落在 logs/vision 下', saved.parent.name == 'vision'
              and saved.parent.parent.name == 'logs', str(saved))
        check('来源写成定长代号', '_DV5_' in saved.name, saved.name)
        check('格式原名不进文件名（长度不一会让后面的列错开）',
              'CF_DIBV5' not in saved.name, saved.name)
        check('尺寸不进文件名', '988x1297' not in saved.name, saved.name)
        check('去向未定时标 pend', saved.name.endswith('_pend.png'), saved.name)
        check('时间戳是固定 12 位且排在最前面（按名排序即时间序）',
              saved.name[:12].isdigit() and saved.name[12] == '_', saved.name)

        # --- 关闭归档 ---
        check('keep<=0 时不归档', archive_mod.save_mounted(base, img, keep=0) == '')

        # --- JPEG 用 .jpg 后缀 ---
        jpg_img = img._replace(encoding='JPEG')
        jpg_path = archive_mod.save_mounted(base, jpg_img, keep=10)
        check('JPEG 存成 .jpg', jpg_path.endswith('.jpg'), jpg_path)

        # --- 同一秒内连按两次不覆盖 ---
        again = archive_mod.save_mounted(base, img, keep=10)
        check('同秒撞名时不覆盖旧文件', again != path and pathlib.Path(again).exists(),
              again)

        # --- 按去向改名 ---
        task = '97def774-ae18-11f1-b3ea-581cf893a9d2'
        used_path = archive_mod.mark_outcome(path, task, used=True)
        used_name = pathlib.Path(used_path).name
        check('改名后带 task_id', task in used_name, used_name)
        check('改名后标 used', '_used_' in used_name, used_name)
        check('pend 标记已去掉', '_pend' not in used_name, used_name)
        check('原文件已不在', not saved.exists())
        check('新文件内容不变', pathlib.Path(used_path).read_bytes() == payload)

        dropped = archive_mod.mark_outcome(again, task, used=False)
        check('未用掉的标 drop', '_drop_' in pathlib.Path(dropped).name, dropped)
        check('同秒序号后缀也能正确剥掉',
              '_pend' not in pathlib.Path(dropped).name, dropped)
        # 被新图顶掉的那张从没绑定过录音，没有 task_id
        orphan = archive_mod.save_mounted(base, img, keep=10)
        orphan_out = pathlib.Path(archive_mod.mark_outcome(orphan, '', used=False)).name
        check('task_id 为空时不留下多余的下划线',
              orphan_out.endswith('_drop.png'), orphan_out)

        # --- 边界：空路径、文件已不存在，都不得抛 ---
        check('空路径原样返回', archive_mod.mark_outcome('', task, True) == '')
        missing = str(pathlib.Path(base) / 'logs' / 'vision' / '不存在_pend.png')
        check('文件不存在时原样返回', archive_mod.mark_outcome(missing, task, True) == missing)

        # --- 来源代号定长（放在清理之前、改名断言之后：这里会新建文件，
        #     提前做会把刚被改名腾出来的文件名又占回去）---
        codes = list(archive_mod._SOURCE_CODES.values()) + [archive_mod.SOURCE_CODE_UNKNOWN]
        check('来源代号全部等长', len(set(len(c) for c in codes)) == 1, codes)
        check('三种剪贴板格式都有代号',
              set(archive_mod._SOURCE_CODES) == {'PNG', 'CF_DIBV5', 'CF_DIB'},
              archive_mod._SOURCE_CODES)
        unknown = archive_mod.save_mounted(
            base, img._replace(source='某种没见过的格式'), keep=10)
        check('未知来源回退成等长的 UNK',
              f'_{archive_mod.SOURCE_CODE_UNKNOWN}_' in pathlib.Path(unknown).name,
              unknown)
        # 去向列的起始位置必须与来源无关
        offsets = set()
        for src in ['PNG', 'CF_DIBV5', 'CF_DIB']:
            name = pathlib.Path(
                archive_mod.save_mounted(base, img._replace(source=src), keep=20)).name
            offsets.add(name.index('_pend'))
        check('三种来源下去向列都落在同一列', len(offsets) == 1, offsets)

        # --- 清理：只留最近 N 张 ---
        directory = pathlib.Path(base) / 'logs' / 'vision'
        for f in directory.iterdir():
            f.unlink()
        made = []
        for i in range(8):
            f = directory / ('26091%d030000_DV5_pend.png' % i)
            f.write_bytes(payload)
            os.utime(f, (1000 + i, 1000 + i))
            made.append(f)
        archive_mod._prune(directory, keep=3)
        left = sorted(p.name for p in directory.iterdir())
        check('清理后只剩 keep 张', len(left) == 3, left)
        check('删的是最旧的，留下最新的',
              left == sorted(p.name for p in made[-3:]), left)

        # --- 清理只动本模块自己的文件 ---
        stranger = directory / '用户自己放的图.png'
        stranger.write_bytes(b'x')
        os.utime(stranger, (1, 1))   # 故意做成最旧的
        archive_mod._prune(directory, keep=1)
        check('不符合命名格式的文件不会被删掉', stranger.exists())
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ====================================================================
# 14. 连按快捷键的合并闸门
# ====================================================================

def test_capture_coalescing(vm_mod):
    print('\n[14] 连按快捷键的合并闸门')
    import threading

    class FakeExecutor:
        def __init__(self):
            self.submitted = []

        def submit(self, fn):
            self.submitted.append(fn)

    vm = vm_mod.VisionManager.__new__(vm_mod.VisionManager)
    vm._executor = FakeExecutor()
    vm._capture_queued = False
    vm._capture_lock = threading.Lock()
    vm._last_mounted_seq = 0
    vm.app = types.SimpleNamespace(state=types.SimpleNamespace(pending_image=None))

    # --- 第一次按键排一个捕获 ---
    vm._on_hotkey()
    check('第一次按键提交一次捕获', len(vm._executor.submitted) == 1,
          len(vm._executor.submitted))

    # --- 排队期间再按多少次都只算一次 ---
    for _ in range(5):
        vm._on_hotkey()
    check('排队未执行时连按不重复提交', len(vm._executor.submitted) == 1,
          len(vm._executor.submitted))

    # --- 捕获一开始执行就放闸：之后的按键必须能排进来 ---
    # 用 _is_fresh 返回 False 让 _capture 在放闸后立刻返回，不去碰真实剪贴板
    vm._is_fresh = lambda: False
    vm._capture()
    check('捕获开始执行后闸门已放开', vm._capture_queued is False)
    vm._on_hotkey()
    check('放闸后的按键会重新排一个捕获', len(vm._executor.submitted) == 2,
          len(vm._executor.submitted))

    # --- executor 已关闭（退出过程中）不得把闸门永久卡住 ---
    class DeadExecutor:
        def submit(self, fn):
            raise RuntimeError('cannot schedule new futures after shutdown')

    vm._executor = DeadExecutor()
    vm._capture_queued = False
    vm._on_hotkey()
    check('提交失败时闸门被复位，不会卡死后续按键', vm._capture_queued is False)

    vm._executor = None
    vm._on_hotkey()   # 不应抛异常
    check('executor 为 None 时直接返回', True)


# ====================================================================
# 15. 重复按键不重复挂载 / 提示去重
# ====================================================================

def test_repeat_press(vm_mod, notice_mod):
    print('\n[15] 重复按键不重复挂载 / 提示去重')
    import threading

    original_seq = vm_mod.current_sequence
    try:
        vm = vm_mod.VisionManager.__new__(vm_mod.VisionManager)
        vm._capture_queued = False
        vm._capture_lock = threading.Lock()
        vm.app = types.SimpleNamespace(state=types.SimpleNamespace(pending_image=None))
        reached = {"n": 0}
        vm._is_fresh = lambda: reached.__setitem__("n", reached["n"] + 1) or False

        vm_mod.current_sequence = lambda: 12345

        # --- 剪贴板没变 + 图还挂着 → 整个跳过，连时效检查都不走 ---
        vm._last_mounted_seq = 12345
        vm.app.state.pending_image = object()
        vm._capture()
        check('剪贴板未变且图仍在待挂时整个跳过', reached['n'] == 0, reached)

        # --- 图已被某次录音消耗掉 → 同一张图要能重新挂上 ---
        vm.app.state.pending_image = None
        vm._capture()
        check('图被消耗后同一张图能重新挂载', reached['n'] == 1, reached)

        # --- 剪贴板变了 → 照常走 ---
        vm.app.state.pending_image = object()
        vm_mod.current_sequence = lambda: 99999
        vm._capture()
        check('剪贴板变了就照常捕获', reached['n'] == 2, reached)

        # --- 序号读不到（无剪贴板访问权限）→ 不能因此永久跳过 ---
        vm_mod.current_sequence = lambda: 0
        vm._last_mounted_seq = 0
        vm._capture()
        check('序号读不到时不跳过（退回旧行为）', reached['n'] == 3, reached)
    finally:
        vm_mod.current_sequence = original_seq

    # --- 提示去重：连续相同只打一次，换一条又能打 ---
    printed = []
    original_console = notice_mod.console
    try:
        notice_mod.console = types.SimpleNamespace(print=lambda m: printed.append(m))
        notice_mod.reset()
        notice_mod.notify('同一句话')
        notice_mod.notify('同一句话')
        notice_mod.notify('同一句话')
        check('连续相同的提示只打一次', printed == ['同一句话'], printed)
        notice_mod.notify('换了一句')
        check('内容变了就照常打', len(printed) == 2, printed)
        notice_mod.notify('同一句话')
        check('不是紧邻重复则不跳过', len(printed) == 3, printed)
        notice_mod.reset()
        notice_mod.notify('同一句话')
        check('reset 后同一句话能重新打', len(printed) == 4, printed)

        # --- dedupe=False：状态真的变了就必须打，哪怕逐字相同 ---
        # 两张不同的图完全可能渲染出一样的一行（同尺寸、同整数 KB、同来源），
        # 吞掉第二条就等于让用户以为新图没挂上
        printed.clear()
        notice_mod.reset()
        notice_mod.notify('已挂载图片 401×169', dedupe=False)
        notice_mod.notify('已挂载图片 401×169', dedupe=False)
        check('挂载成功这类消息逐字相同也照常打两次', len(printed) == 2, printed)

        # --- 不去重的消息也要刷新「上一条」，否则会误吞更早的同款消息 ---
        printed.clear()
        notice_mod.reset()
        notice_mod.notify('失败提示')                      # 打印
        notice_mod.notify('挂载成功', dedupe=False)        # 打印，并把上一条刷成它
        notice_mod.notify('失败提示')                      # 与上一条不同，应当打印
        check('不去重的消息会刷新「上一条」，更早的同款不被误吞',
              len(printed) == 3, printed)
    finally:
        notice_mod.console = original_console
        notice_mod.reset()


# ====================================================================
# 10. 剪贴板还原不得清掉图片
# ====================================================================

def test_clipboard_restore(clipboard_mod, sel_mod, pyclip_stub, kb_stub):
    print('\n[10] 剪贴板还原不得清掉图片')

    # --- try_paste 能区分「读不到」和「本来就是空文本」---
    pyclip_stub.set_raise(Exception(
        'Clipboard contents have no standard formats available.'))
    check('剪贴板是图片时 try_paste 返回 None', clipboard_mod.try_paste() is None)
    check('同一情况 safe_paste 仍返回空串（向后兼容）', clipboard_mod.safe_paste() == '')

    pyclip_stub.set_value(b'')
    check('剪贴板是空文本时 try_paste 返回空串而非 None',
          clipboard_mod.try_paste() == '')

    pyclip_stub.set_value('你好'.encode('utf-8'))
    check('正常文本照常读出', clipboard_mod.try_paste() == '你好')

    # --- 核心回归：剪贴板里是图片、且没选中任何文字 ---
    class Role:
        enable_read_selection = True
        enable_history = True
        name = '助理'
        selection_max_length = 1000

    class State:
        last_output_text = ''

    pyclip_stub.set_raise(Exception('no standard formats'))   # 图片，读不到文本
    kb_stub.presses.clear()
    pyclip_stub.copies.clear()

    result = sel_mod.get_selected_text(Role, State)
    check('没选中文字时返回空串', result == '')
    check('Ctrl+C 确实发出去了', kb_stub.presses == ['ctrl+c'])
    check('**没有把空串写回剪贴板**（图片得以保住）',
          pyclip_stub.copies == [], pyclip_stub.copies)

    # --- 对照：剪贴板本来是文本，还原行为必须与改动前一致 ---
    pyclip_stub.set_value('原有文本'.encode('utf-8'))
    pyclip_stub.copies.clear()
    sel_mod.get_selected_text(Role, State)
    check('原内容是文本时照常写回还原', pyclip_stub.copies == ['原有文本'],
          pyclip_stub.copies)

    # --- 对照：剪贴板本来是空文本，也应还原（保持旧行为）---
    pyclip_stub.set_value(b'')
    pyclip_stub.copies.clear()
    sel_mod.get_selected_text(Role, State)
    check('原内容是空文本时仍写回（与改动前一致）', pyclip_stub.copies == [''],
          pyclip_stub.copies)

    # --- 真的选中了文字：仍要能取到 ---
    pyclip_stub.set_sequence([b'', '选中的内容'.encode('utf-8')])
    pyclip_stub.copies.clear()
    got = sel_mod.get_selected_text(Role, State)
    check('确实选中文字时能正常取回', got == '选中的内容', repr(got))


# ====================================================================

def main():
    install_stubs()

    consts = load('core.client.llm.llm_constants', 'core/client/llm/llm_constants.py')
    sys.modules['core.client.llm'].llm_constants = consts

    interfaces = load('core.client.llm.llm_interfaces', 'core/client/llm/llm_interfaces.py')
    sys.modules['core.client.llm'].llm_interfaces = interfaces

    load('core.client.llm.llm_memory', 'core/client/llm/llm_memory.py')
    ctx_mod = load('core.client.llm.llm_context', 'core/client/llm/llm_context.py')
    role_cfg = load('core.client.llm.llm_role_config', 'core/client/llm/llm_role_config.py')
    sys.modules['core.client.llm'].llm_role_config = role_cfg
    mb_mod = load('core.client.llm.llm_message_builder', 'core/client/llm/llm_message_builder.py')

    # llm_processor 依赖 openai/ollama（client_pool、exceptions），桩掉即可
    openai_stub = types.ModuleType('openai')
    openai_stub.OpenAI = object
    for err in ('AuthenticationError', 'RateLimitError', 'APITimeoutError',
                'APIConnectionError', 'APIError', 'BadRequestError',
                'NotFoundError', 'PermissionDeniedError', 'InternalServerError',
                'APIStatusError', 'APIResponseValidationError', 'ConflictError',
                'UnprocessableEntityError'):
        setattr(openai_stub, err, type(err, (Exception,), {}))
    sys.modules['openai'] = openai_stub
    ollama_stub = types.ModuleType('ollama')
    ollama_stub.Client = object
    ollama_stub.ResponseError = type('ResponseError', (Exception,), {})
    sys.modules['ollama'] = ollama_stub
    load('core.client.llm.llm_anthropic', 'core/client/llm/llm_anthropic.py')
    pool = load('core.client.llm.llm_client_pool', 'core/client/llm/llm_client_pool.py')
    sys.modules['core.client.llm'].llm_client_pool = pool
    exc = load('core.client.llm.llm_exceptions', 'core/client/llm/llm_exceptions.py')
    sys.modules['core.client.llm'].llm_exceptions = exc
    proc_mod = load('core.client.llm.llm_processor', 'core/client/llm/llm_processor.py')

    state_mod = load('core.client.state', 'core/client/state.py')
    notice_mod = load('core.client.vision.notice', 'core/client/vision/notice.py')
    sys.modules['core.client.vision'].notice = notice_mod
    clip_mod = load('core.client.vision.clipboard_image', 'core/client/vision/clipboard_image.py')

    test_estimate_tokens(consts)
    test_demote_images(ctx_mod, consts)
    test_responses_content(proc_mod)
    test_responses_params(proc_mod)
    test_dib_to_bmp(clip_mod)
    test_state(state_mod)
    test_message_builder(mb_mod)

    watch_mod = load('core.client.vision.clipboard_watcher',
                     'core/client/vision/clipboard_watcher.py')
    sys.modules['core.client.vision'].clipboard_watcher = watch_mod
    sys.modules['core.client.vision'].clipboard_image = clip_mod
    archive_mod = load('core.client.vision.image_archive',
                       'core/client/vision/image_archive.py')
    sys.modules['core.client.vision'].image_archive = archive_mod
    vm_mod = load('core.client.vision.vision_manager', 'core/client/vision/vision_manager.py')
    test_watcher(watch_mod)
    test_is_fresh(vm_mod, watch_mod)
    test_encode_fallback(clip_mod)
    test_image_archive(archive_mod, clip_mod)
    test_capture_coalescing(vm_mod)
    test_repeat_press(vm_mod, notice_mod)

    # pyclip / pynput 桩
    class PyclipStub(types.ModuleType):
        def __init__(self):
            super().__init__('pyclip')
            self.copies = []
            self._value = b''
            self._exc = None
            self._seq = None

        def set_value(self, v):
            self._value, self._exc, self._seq = v, None, None

        def set_raise(self, e):
            self._exc, self._seq = e, None

        def set_sequence(self, values):
            self._seq = list(values)
            self._exc = None

        def paste(self):
            if self._seq:
                return self._seq.pop(0)
            if self._exc:
                raise self._exc
            return self._value

        def copy(self, v):
            self.copies.append(v)

    pyclip_stub = PyclipStub()
    sys.modules['pyclip'] = pyclip_stub

    class KbStub(types.ModuleType):
        def __init__(self):
            super().__init__('keyboard')
            self.presses = []

        def press_and_release(self, combo):
            self.presses.append(combo)

    kb_stub = KbStub()
    sys.modules['keyboard'] = kb_stub

    pynput = types.ModuleType('pynput'); pynput.__path__ = []
    pynput_kb = types.ModuleType('pynput.keyboard')
    pynput_kb.Controller = object
    pynput_kb.Key = types.SimpleNamespace(ctrl=None, cmd=None)
    pynput.keyboard = pynput_kb
    sys.modules['pynput'] = pynput
    sys.modules['pynput.keyboard'] = pynput_kb

    pkg_clip = types.ModuleType('core.client.clipboard'); pkg_clip.__path__ = []
    pkg_clip.logger = sys.modules['core.client'].logger
    sys.modules['core.client.clipboard'] = pkg_clip
    clipboard_mod = load('core.client.clipboard.clipboard',
                         'core/client/clipboard/clipboard.py')
    for n in ('safe_paste', 'try_paste', 'safe_copy', 'restore_clipboard', 'paste_text',
              'save_and_restore_clipboard', 'copy_to_clipboard', 'CLIPBOARD_ENCODINGS'):
        setattr(pkg_clip, n, getattr(clipboard_mod, n))
    llm_clip = load('core.client.llm.llm_clipboard', 'core/client/llm/llm_clipboard.py')
    sys.modules['core.client.llm'].llm_clipboard = llm_clip
    # 这两组测试专门验证旧复制方法的剪贴板保护，显式开启兼容回退并隔离 UIA。
    selection_native = types.ModuleType('core.tools.windows_selection')
    selection_native.get_input_target = lambda: (123, 456)
    selection_native.read_selection = lambda limit: types.SimpleNamespace(
        status='unsupported', text='', target=(123, 456), detail='test provider')
    sys.modules[selection_native.__name__] = selection_native
    sys.modules['config_client'].ClientConfig.selection_mode = 'uia_then_ctrl_c'
    sel_mod = load('core.client.llm.llm_get_selection',
                   'core/client/llm/llm_get_selection.py')
    test_clipboard_restore(clipboard_mod, sel_mod, pyclip_stub, kb_stub)
    test_clipboard_retry(clipboard_mod, sel_mod, pyclip_stub, kb_stub)

    print('\n' + '=' * 60)
    print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
    if FAIL:
        print('失败项：')
        for name in FAIL:
            print('  -', name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
