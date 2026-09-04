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
import struct
import sys
import types

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

    check('纯文本仍按原口径', et('你好世界') == 2, et('你好世界'))
    check('空值返回 0', et('') == 0 and et(None) == 0)

    huge = 'data:image/png;base64,' + 'A' * 400000
    chat = [{'type': 'text', 'text': '看看这个报错'},
            {'type': 'image_url', 'image_url': {'url': huge}}]
    n = et(chat)
    check('Chat 口味不抛异常且图片按 384 计', n == 4 + 384, n)
    check('40 万字符的 base64 不会撑爆估算', n < 500, n)

    resp = [{'type': 'input_text', 'text': 'abc'},
            {'type': 'input_image', 'image_url': huge}]
    check('Responses 口味同样按 384 计', et(resp) == 384 + 1, et(resp))
    check('非字符串非列表兜底为 1', et(12345) == 1)
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
    check('占位符已写入', contents[0] == '第一张\n' + ph, repr(contents[0]))
    check('文本部分未丢失', contents[2] == '第二张\n' + ph, repr(contents[2]))
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
    check('web_search 工具照常挂载', params.get('tools') == [{'type': 'web_search'}])
    inp = params['input']
    check('system 不在 input 里', len(inp) == 3, len(inp))
    check('纯文本历史消息原样透传（对象未被复制改写）',
          inp[0] is messages[1] and inp[1] is messages[2])
    check('带图消息已转成 Responses 口味',
          inp[2]['content'] == [{'type': 'input_text', 'text': '看图'},
                                {'type': 'input_image', 'image_url': url}], inp[2])
    check('原始 messages 未被就地改写',
          messages[3]['content'][1]['type'] == 'image_url')

    # 不带图时行为必须与改动前逐字一致
    plain_msgs = [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'U'}]
    p2 = proc._build_responses_params(Role, plain_msgs)
    check('不带图时 input 条目就是原对象', p2['input'][0] is plain_msgs[1])


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

    bound = st.begin_image_snapshot()
    check('launch 时把图移入本次录音', bound == 'IMG2')
    check('移入后待挂槽清空（已绑定与待挂互斥）', st.pending_image is None)

    st.bind_image_snapshot('task-a')
    check('绑定后可按 task_id 查回', st.image_snapshots.get('task-a') == 'IMG2')

    # --- 消耗 ---
    got = st.pop_image_snapshot('task-a')
    check('结果到达时取出图片', got == 'IMG2')
    check('取出后字典里不再有', 'task-a' not in st.image_snapshots)
    check('取出不再产生待挂图（严格「下一条」：用掉即消失）', st.pending_image is None)

    # --- 竞态：pop 不得清掉下一次录音的 image_current ---
    st = new_state()
    st.set_pending_image('OLD')
    st.begin_image_snapshot()
    st.bind_image_snapshot('task-old')
    st.set_pending_image('NEW')
    st.begin_image_snapshot()             # 新录音已 launch，尚未 bind
    st.pop_image_snapshot('task-old')     # 上一句的结果此时到达
    check('pop 不会抹掉新录音尚未绑定的图', st.image_current == 'NEW', st.image_current)
    st.bind_image_snapshot('task-new')
    check('新录音仍能正常绑定', st.image_snapshots.get('task-new') == 'NEW')

    # --- 回滚：录音作废，图退回待挂 ---
    st = new_state()
    st.set_pending_image('IMG')
    st.begin_image_snapshot()
    st.bind_image_snapshot('t1')
    restored = st.rollback_image_snapshot('t1')
    check('作废时图退回待挂', restored == 'IMG' and st.pending_image == 'IMG')
    check('回滚后字典里不再有', 't1' not in st.image_snapshots)

    # --- 回滚不覆盖期间新截的图 ---
    st = new_state()
    st.set_pending_image('OLD')
    st.begin_image_snapshot()
    st.bind_image_snapshot('t1')
    st.set_pending_image('NEWER')          # 录音期间又按了一次快捷键
    restored = st.rollback_image_snapshot('t1')
    check('回滚不用旧图顶掉更新的待挂图', restored is None and st.pending_image == 'NEWER')

    # --- 还没绑定 task_id 就取消（短按取消的典型时序）---
    st = new_state()
    st.set_pending_image('IMG')
    st.begin_image_snapshot()
    restored = st.rollback_image_snapshot(None)
    check('未拿到 task_id 也能回滚', restored == 'IMG' and st.pending_image == 'IMG')

    # --- 没有图时各操作都安全 ---
    st = new_state()
    check('无图时 begin 返回 None', st.begin_image_snapshot() is None)
    st.bind_image_snapshot('t')
    check('无图时 bind 不写入空条目', st.image_snapshots == {})
    check('无图时 rollback 返回 None', st.rollback_image_snapshot('t') is None)
    check('无图时 pop 返回 None', st.pop_image_snapshot('t') is None)

    # --- 定长兜底 ---
    st = new_state()
    limit = state_mod.MAX_CONTINUOUS_SNAPSHOTS
    for i in range(limit + 5):
        st.set_pending_image('IMG%d' % i)
        st.begin_image_snapshot()
        st.bind_image_snapshot('t%d' % i)
    check('快照字典有定长上限', len(st.image_snapshots) == limit, len(st.image_snapshots))
    check('超限时丢的是最旧的', 't0' not in st.image_snapshots)
    check('最新的仍在', ('t%d' % (limit + 4)) in st.image_snapshots)

    # --- reset 清理 ---
    st.set_pending_image('X')
    st.reset()
    check('reset 清空待挂图', st.pending_image is None)
    check('reset 清空快照字典', st.image_snapshots == {} and st.image_current is None)


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

    # 读不到序号（无剪贴板访问权限）
    watch_mod._sequence_number = lambda: 0
    w2 = W()
    w2.start()
    check('序号不可用时标记为不可用', w2.available is False)
    check('不可用时年龄返回 None', w2.seconds_since_change() is None)
    check('不可用时不会起线程', w2._thread is None)

    # 线程生命周期
    watch_mod._sequence_number = lambda: seq['v']
    w3 = W(poll_interval=0.01)
    w3.start()
    check('可用时线程已启动', w3._thread is not None and w3._thread.is_alive())
    seq['v'] = 200
    _t.sleep(0.05)
    check('后台轮询能自行发现变化', w3._changed_at is not None)
    w3.stop()
    _t.sleep(0.05)
    check('stop 后线程退出', not any(
        t.name == 'clipboard-watcher' and t.is_alive() for t in __import__('threading').enumerate()))


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
    pool = load('core.client.llm.llm_client_pool', 'core/client/llm/llm_client_pool.py')
    sys.modules['core.client.llm'].llm_client_pool = pool
    exc = load('core.client.llm.llm_exceptions', 'core/client/llm/llm_exceptions.py')
    sys.modules['core.client.llm'].llm_exceptions = exc
    proc_mod = load('core.client.llm.llm_processor', 'core/client/llm/llm_processor.py')

    state_mod = load('core.client.state', 'core/client/state.py')
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
    vm_mod = load('core.client.vision.vision_manager', 'core/client/vision/vision_manager.py')
    test_watcher(watch_mod)
    test_is_fresh(vm_mod, watch_mod)

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
    for n in ('safe_paste', 'try_paste', 'safe_copy', 'paste_text',
              'save_and_restore_clipboard', 'copy_to_clipboard', 'CLIPBOARD_ENCODINGS'):
        setattr(pkg_clip, n, getattr(clipboard_mod, n))
    llm_clip = load('core.client.llm.llm_clipboard', 'core/client/llm/llm_clipboard.py')
    sys.modules['core.client.llm'].llm_clipboard = llm_clip
    sel_mod = load('core.client.llm.llm_get_selection',
                   'core/client/llm/llm_get_selection.py')
    test_clipboard_restore(clipboard_mod, sel_mod, pyclip_stub, kb_stub)

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
