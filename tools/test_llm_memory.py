# coding: utf-8
"""
LLM 角色记忆落盘单测（桩模块加载，不联网）

覆盖 2026-10-03 的记忆持久化：
1. MemoryStore：保存与读取、不留临时文件、损坏判定、clear 删除全部文件
2. ContextManager：一轮只落盘一次、图片落盘为占位符而内存仍是真图、读回后按新容量裁剪、
   损坏文件改名保留、改名失败时不再保存、保存失败只告警
3. LLMProcessor._record_turn：空回复整轮不写、打断前已输出的部分照常写入
4. LLMHandler：按 .py 文件名区分（改显示名不丢记忆）、热重载沿用同一对象、
   重新开启历史时从磁盘读回、角色表的恢复提示、清除记忆删除全部文件
5. 角色表一行的格式：没有恢复提示时与以前逐字相同
6. 角色显示名重复时告警一行
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.dont_write_bytecode = True

# 仓库根目录按本文件位置推导，脚本从任何工作目录跑都成立
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLM_DIR = os.path.join(REPO, 'core', 'client', 'llm')


class _Log:
    """收集日志，按级别查询"""

    def __init__(self):
        self.records = []

    def _add(self, level):
        return lambda msg, *a, **k: self.records.append((level, str(msg)))

    def __getattr__(self, name):
        if name in ('debug', 'info', 'warning', 'error', 'exception'):
            return self._add(name)
        raise AttributeError(name)

    def messages(self, level):
        return [m for lv, m in self.records if lv == level]


LOG = _Log()


# ====================================================================
# 桩模块：系统 Python 没有 rich / watchdog，core 包 __init__ 会连锁导入失败
# ====================================================================

def package(name):
    m = types.ModuleType(name)
    m.__path__ = []
    sys.modules[name] = m
    return m


def stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Text:
    """rich.text.Text 的最小替身：拼纯文本，另记每段的样式"""

    def __init__(self, text=''):
        self.plain = text
        self.spans = []

    def append(self, text, style=None):
        if isinstance(text, _Text):
            self.plain += text.plain
            self.spans.extend(text.spans)
        else:
            self.plain += text
            self.spans.append((text, style))
        return self


for name in ('core', 'core.client', 'core.client.llm', 'core.client.udp', 'core.tools',
             'watchdog', 'rich'):
    package(name)
sys.modules['core.client.llm'].logger = LOG

SELECTION_CLEARED = []
stub('rich.text', Text=_Text)
stub('rich.console', Console=lambda *a, **k: types.SimpleNamespace(print=lambda *a, **k: None))
stub('watchdog.observers', Observer=lambda *a, **k: None)
stub('watchdog.events', FileSystemEventHandler=object)
stub('config_client', BASE_DIR=REPO)
stub('core.client.llm.llm_interfaces', IContextManager=object, IRoleLoader=object)
stub('core.client.llm.llm_client_pool', ClientPool=object)
stub('core.client.llm.llm_exceptions',
     APIException=type('APIException', (Exception,), {}),
     wrap_openai_error=lambda *a, **k: None,
     OpenAIErrorWrapper=type('OpenAIErrorWrapper', (Exception,), {}),
     TimeoutErrorWrapper=type('TimeoutErrorWrapper', (Exception,), {}))
stub('core.client.llm.llm_role_detector', RoleDetector=object)
stub('core.client.llm.llm_get_selection',
     capture_selection=lambda *a, **k: types.SimpleNamespace(text=''),
     record_selection_usage=lambda *a, **k: None,
     clear_selection_usage=lambda: SELECTION_CLEARED.append(1))
stub('core.client.llm.llm_stop_monitor', StopMonitor=object)
stub('core.tools.asyncio_to_thread', to_thread=None)
stub('core.client.udp.udp_broadcaster', broadcast_output_udp=lambda *a, **k: None)


def load(name):
    spec = importlib.util.spec_from_file_location(
        'core.client.llm.' + name, os.path.join(LLM_DIR, name + '.py'))
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


constants = load('llm_constants')
load('llm_state_tags')
rc = load('llm_role_config')
memory = load('llm_memory')
ctx = load('llm_context')
load('llm_message_builder')
proc = load('llm_processor')
formatter = load('llm_role_formatter')
watcher = load('llm_watcher')
loader = load('llm_role_loader')
handler_mod = load('llm_handler')

RoleConfig = rc.RoleConfig
MemoryStore = memory.MemoryStore
ContextManager = ctx.ContextManager
PH = constants.IMAGE_PLACEHOLDER
IMG = 'data:image/png;base64,AAAA'

PASS = FAIL = 0


def check(cond, label, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print('  [×] %s  %s' % (label, detail))


def section(t):
    print()
    print('[%s]' % t)


def image_msg(text):
    return [{'type': 'text', 'text': text}, {'type': 'image_url', 'image_url': {'url': IMG}}]


def role(module, name, history=True, cap=100000, **kw):
    return RoleConfig(name=name, module_name='LLM.' + module, enable_history=history,
                      max_context_length=cap, model='m', **kw)


class FakeLoader:
    def __init__(self, roles):
        self.roles = roles

    def load_all_roles(self):
        return []

    def get_roles(self):
        return dict(self.roles)


def is_restored_note(note, rounds):
    """角色表的恢复提示：带上轮数、不是失败提示。措辞与颜色不钉死"""
    return bool(note) and str(rounds) in note[0] and corrupt_reason not in note[0]


def make_handler(directory, roles):
    """真实的 LLMHandler 方法绑在只有记忆相关属性的对象上"""
    h = handler_mod.LLMHandler.__new__(handler_mod.LLMHandler)
    h.memory_store = MemoryStore(directory)
    h.role_loader = FakeLoader(roles)
    h.roles = h.role_loader.get_roles()
    h.client_pool = types.SimpleNamespace(clear=lambda: None)
    h.context_managers = {}
    h.memory_notes = {}
    h._init_context_managers()
    return h


with tempfile.TemporaryDirectory(dir=os.path.join(REPO, 'tools')) as tmp:
    base = Path(tmp)

    # ================================================================ 1
    section('MemoryStore')
    store = MemoryStore(base / 's1')
    check(store.load('甲') is None, '没有文件时 load 返回 None')
    hist = [{'timestamp': 1.0, 'role': 'user', 'content': '你好'},
            {'timestamp': 1.0, 'role': 'assistant', 'content': '<state:微笑>嗯'}]
    store.save('甲', '甲角色', hist)
    check(store.load('甲') == hist, '保存后读回逐条相同')
    data = json.loads(store.path('甲').read_text(encoding='utf-8'))
    check(data.get('display_name') == '甲角色', '文件里记下显示名，方便人辨认', repr(data))
    check(sorted(p.name for p in store.directory.iterdir()) == ['甲.json'], '不留临时文件')
    for raw, label in (
            (b'{not json', 'JSON 语法错误'),
            (json.dumps({'version': 99, 'history': []}).encode(), '格式版本不符'),
            (json.dumps({'version': 1, 'history': [
                {'timestamp': 1, 'role': 'system', 'content': 'x'}]}).encode(), '未知消息角色'),
            (json.dumps({'version': 1, 'history': [
                {'role': 'user', 'content': 'x'}]}).encode(), '缺时间戳'),
            (b'\xff\xfe\x00', '不是 UTF-8')):
        store.path('坏').write_bytes(raw)
        reason = None
        try:
            store.load('坏')
        except memory.MemoryFileError as e:
            reason = e.reason
        check(reason == '文件已损坏', '%s 判为文件已损坏' % label, repr(reason))
    corrupt_reason = reason
    check(store.clear() == [] and list(store.directory.iterdir()) == [], 'clear 删除目录下全部文件')
    check(MemoryStore(base / '不存在').clear() == [], '目录不存在时 clear 不报错')

    # ================================================================ 2
    section('ContextManager 落盘与恢复')
    store2 = MemoryStore(base / 's2')
    saved_tails = []
    real_save = store2.save
    store2.save = lambda key, name, history: (saved_tails.append(history[-1]['role']),
                                              real_save(key, name, history))
    cm = ContextManager(max_length=100000, store=store2, key='乙', display_name='乙')
    cm.add_turn(image_msg('看这张图'), '看到了')
    # 每次落盘时最后一条都是回复：磁盘上不会出现只有提问的半轮（不管落盘几次）
    check(saved_tails and all(r == 'assistant' for r in saved_tails),
          '磁盘上不出现只有提问的半轮', repr(saved_tails))
    disk = store2.load('乙')
    check([m['role'] for m in disk] == ['user', 'assistant'], '磁盘上是完整的一轮')
    check(PH in disk[0]['content'] and '看这张图' in disk[0]['content'] and 'base64' not in disk[0]['content'],
          '磁盘上图片换成占位符', repr(disk[0]['content']))
    check(isinstance(cm.history[0]['content'], list), '内存里仍是真图')

    cm_b = ContextManager(max_length=100000, store=store2, key='乙', display_name='乙')
    check(cm_b.restore() == (1, None), '新建管理器恢复 1 轮')
    check(cm_b.get_history() == [{'role': m['role'], 'content': m['content']} for m in disk],
          '恢复的内容与磁盘一致')
    check(cm_b.last_interaction == disk[-1]['timestamp'], 'last_interaction 取最后一条的时间')
    for i in range(20):
        cm_b.add_turn('问题%d ' % i + '字' * 200, '回答' * 100)
    small = ContextManager(max_length=500, store=store2, key='乙', display_name='乙')
    rounds, failure = small.restore()
    total = sum(constants.estimate_tokens(m['content']) for m in small.history)
    check(failure is None and 0 < rounds < 21, '容量调小后读回会裁剪', repr((rounds, failure)))
    check(total <= 500 * constants.ContextConstants.TRIM_THRESHOLD_RATIO,
          '裁剪后不超过新容量的阈值', repr(total))

    store3 = MemoryStore(base / 's3')
    store3.directory.mkdir()
    store3.path('丙').write_text('{坏了', encoding='utf-8')
    cm_c = ContextManager(max_length=1000, store=store3, key='丙', display_name='丙')
    check(cm_c.restore() == (0, '文件已损坏'), '损坏文件：从空记忆开始并给出原因')
    kept = [p.name for p in store3.directory.iterdir()]
    check(len(kept) == 1 and kept[0].startswith('丙.bad-') and kept[0].endswith('.json'),
          '损坏文件改名保留', repr(kept))
    cm_c.add_turn('新的提问', '新的回复')
    check(store3.load('丙') is not None and len(list(store3.directory.iterdir())) == 2,
          '之后照常保存，不覆盖改名保留的文件')

    store4 = MemoryStore(base / 's4')
    store4.directory.mkdir()
    store4.path('丁').write_text('{坏了', encoding='utf-8')

    def deny(_key):
        raise OSError('拒绝访问')

    store4.quarantine = deny
    LOG.records.clear()
    cm_d = ContextManager(max_length=1000, store=store4, key='丁', display_name='丁')
    cm_d.restore()
    cm_d.add_turn('问', '答')
    check(store4.path('丁').read_text(encoding='utf-8') == '{坏了', '改名失败时不再保存，原文件不被覆盖')
    check(LOG.messages('warning'), '并在终端告警')
    check(len(cm_d.history) == 2, '内存里的记忆照常使用')

    store5 = MemoryStore(base / 's5')

    def disk_full(*_a, **_k):
        raise OSError('磁盘已满')

    store5.save = disk_full
    LOG.records.clear()
    cm_e = ContextManager(max_length=1000, store=store5, key='戊', display_name='戊')
    cm_e.add_turn('问', '答')
    check(len(cm_e.history) == 2, '保存失败不影响内存记忆')
    check(LOG.messages('warning'), '保存失败在终端告警')

    # ================================================================ 3
    section('_record_turn：空回复不写入')
    p = proc.LLMProcessor.__new__(proc.LLMProcessor)
    assistant = role('助理', '助理')
    store6 = MemoryStore(base / 's6')
    cm_f = ContextManager(max_length=100000, store=store6, key='助理', display_name='助理')
    p._record_turn(assistant, cm_f, [{'role': 'user', 'content': '第一问'}], '第一答')
    p._record_turn(assistant, cm_f, [{'role': 'user', 'content': '第二问'}], '')
    p._record_turn(assistant, cm_f, [{'role': 'user', 'content': '第三问'}], '  \n')
    check([m['content'] for m in cm_f.history] == ['第一问', '第一答'], '空回复整轮不写',
          repr([m['content'] for m in cm_f.history]))
    check(len(store6.load('助理')) == 2, '磁盘上同样没有空回复的那一轮')
    p._record_turn(assistant, cm_f, [{'role': 'user', 'content': '被打断的提问'}], '已经输出的半句')
    check(cm_f.history[-1]['content'] == '已经输出的半句', '打断前已输出的部分照常写入')
    p._record_turn(role('翻译', '翻译', history=False), cm_f,
                   [{'role': 'user', 'content': 'x'}], 'y')
    check(len(cm_f.history) == 4, '未开启历史的角色不写')

    # ================================================================ 4
    section('LLMHandler：按文件名区分、热重载、清除记忆')
    mem_dir = base / 'llm_memory'
    h = make_handler(mem_dir, {'雾切响子': role('雾切响子', '雾切响子|雾切妹妹')})
    check(list(h.context_managers) == ['LLM.雾切响子'], '管理器按 module_name 区分',
          repr(list(h.context_managers)))
    check(h.memory_notes == {}, '没有记忆文件时不显示恢复提示')
    h.context_managers['LLM.雾切响子'].add_turn('你好', '……是你啊')
    check((mem_dir / '雾切响子.json').exists(), '记忆文件按 .py 文件名命名')

    # 重启，并且改了显示名（调整别名顺序）
    h = make_handler(mem_dir, {'雾切妹妹': role('雾切响子', '雾切妹妹|雾切响子')})
    kyoko = h.context_managers['LLM.雾切响子']
    check(len(kyoko.history) == 2, '改显示名后重启，记忆仍按文件名恢复')
    check(is_restored_note(h.memory_notes.get('LLM.雾切响子'), 1), '角色表的恢复提示',
          repr(h.memory_notes))

    captured = {}

    def build_messages(role_config, content, context_manager, **_kw):
        captured['cm'] = context_manager
        return [{'role': 'user', 'content': content}]

    h.monitor = types.SimpleNamespace(should_stop=lambda: False, mark_output_started=lambda: None)
    h.message_builder = types.SimpleNamespace(build_messages=build_messages)
    h.processor = types.SimpleNamespace(process=lambda **_kw: ('答', 1, 0.1))
    h.app = types.SimpleNamespace(state=None)
    h.process(h.roles['雾切妹妹'], '问', selection=types.SimpleNamespace(text=''))
    check(captured.get('cm') is kyoko, 'process 按文件名取到这个角色的记忆')

    h.role_loader.roles = {'响子': role('雾切响子', '响子', cap=50000)}
    h.reload_roles()
    check(h.context_managers.get('LLM.雾切响子') is kyoko, '热重载沿用同一个管理器对象')
    check(kyoko.max_length == 50000 and kyoko.display_name == '响子', '容量与显示名随配置更新')
    check(h.memory_notes == {}, '热重载沿用的记忆不显示恢复提示')

    h.role_loader.roles = {'响子': role('雾切响子', '响子', history=False)}
    h.reload_roles()
    check('LLM.雾切响子' not in h.context_managers, '关掉历史后不再有管理器')
    check((mem_dir / '雾切响子.json').exists(), '关掉历史不删除记忆文件')
    h.role_loader.roles = {'响子': role('雾切响子', '响子')}
    h.reload_roles()
    check(len(h.context_managers['LLM.雾切响子'].history) == 2, '重新开启历史时从磁盘读回')
    check(is_restored_note(h.memory_notes.get('LLM.雾切响子'), 1), '读回时同样给出恢复提示')

    (mem_dir / '新岛真.json').write_text('{', encoding='utf-8')
    h2 = make_handler(mem_dir, {'新岛真': role('新岛真', '新岛真')})
    note = h2.memory_notes.get('LLM.新岛真')
    check(bool(note) and corrupt_reason in note[0], '损坏时的提示写明原因',
          repr(h2.memory_notes))

    MemoryStore(mem_dir).save('旧角色', '旧角色', [{'timestamp': 1.0, 'role': 'user', 'content': 'x'}])
    archive = base / '03-雾切响子.md'
    archive.write_text('# LLM 对话记录', encoding='utf-8')
    SELECTION_CLEARED.clear()
    check(h.clear_history() is True, '清除记忆返回成功')
    check(list(mem_dir.iterdir()) == [], '删除全部记忆文件，含暂时没人用的与改名保留的')
    check(all(not c.history for c in h.context_managers.values()), '内存里的记忆清空')
    check(SELECTION_CLEARED == [1], '选区去重记录一并清掉')
    check(archive.exists(), '记忆目录之外的文件（如对话归档）不受影响')
    h.context_managers['LLM.雾切响子'].add_turn('重新开始', '嗯')
    check(len(MemoryStore(mem_dir).load('雾切响子')) == 2, '清除后的新对话照常保存')

    h.memory_store.clear = lambda: ['雾切响子.json']
    LOG.records.clear()
    check(h.clear_history() is False, '有文件删不掉时返回失败')
    check(LOG.messages('warning'), '并在终端告警')

# ================================================================ 5
section('角色表一行的格式')
Formatter = formatter.RoleFormatter
shown = role('助理', '助理', provider='deepseek', enable_read_selection=True, output_mode='toast')
shown.model = 'deepseek-flash'
plain = Formatter.format_status('助理', shown).plain
note = ('已恢复3轮对话', 'green')
noted = Formatter.format_status('助理', shown, note)
# 用户要求：没有提示时这一行与以前完全相同；有提示时只是在「读选区」后面插入提示本身
check(noted.plain.replace(note[0], '') == plain, '没有提示时不多出任何字符', repr((plain, noted.plain)))
check(noted.plain.index('读选区') < noted.plain.index(note[0]) < noted.plain.index('('),
      '提示紧跟在「读选区」后面、模型信息之前', repr(noted.plain))
check(note in noted.spans, '提示带上样式')

seen = []
real_print = Formatter.print_status
Formatter.print_status = staticmethod(
    lambda name, role_config, prefix='  ', memory_note=None: seen.append(memory_note))
try:
    w = watcher.LLMFileWatcher(on_roles_reload=lambda: None, get_roles=lambda: {},
                               get_memory_note=lambda role_config: ('已恢复2轮对话', 'green'))
    w._print_role_info('助理', shown)
    plain_watcher = watcher.LLMFileWatcher(on_roles_reload=lambda: None, get_roles=lambda: {})
    plain_watcher._print_role_info('助理', shown)
finally:
    Formatter.print_status = real_print
check(seen == [('已恢复2轮对话', 'green'), None], '角色表经 watcher 拿到恢复提示，未提供时为空',
      repr(seen))

# ================================================================ 6
section('角色显示名重复告警')
RL = loader.RoleLoader
rl = RL.__new__(RL)
rl.roles_registry = {}
first = types.SimpleNamespace(name='响子', provider='p', model='m')
second = types.SimpleNamespace(name='响子|别名', provider='p', model='m')
LOG.records.clear()
rl.register_role('LLM.甲', first)
rl.register_role('LLM.甲', first)
check(not LOG.messages('warning'), '同一文件重新注册不告警')
rl.register_role('LLM.乙', second)
warns = LOG.messages('warning')
check(len(warns) == 1 and '响子' in warns[0] and '甲.py' in warns[0] and '乙.py' in warns[0]
      and '\n' not in warns[0], '显示名重复时告警一行，写明两个文件', repr(warns))
check(rl.roles_registry['响子'].module_name == 'LLM.乙', '覆盖规则不变：后加载的生效')

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
