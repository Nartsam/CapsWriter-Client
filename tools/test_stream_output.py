# coding: utf-8
"""流式上屏：逐句打字、角色延后处理、挂图角色预览、打断与旧服务端回退；
另含任务在控制台的第一行（结果到达时才打、每个任务一行、打断与断线清理）。

ResultProcessor 的相关方法按源码抽出来，绑到桩对象上运行；打字、热词、LLM、指示器均为桩。
"""
from __future__ import annotations

import ast
import importlib.util
import logging
from pathlib import Path
import sys
import time
import types
from typing import Dict, Optional
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for name in ('core', 'core.client', 'core.client.llm', 'core.client.output', 'core.client.ui'):
    module = types.ModuleType(name)
    module.__path__ = []
    module.logger = logging.getLogger('stream-test')
    sys.modules[name] = module

roles = load('core.client.llm.llm_role_config', 'core/client/llm/llm_role_config.py')
load('core.client.llm.llm_interfaces', 'core/client/llm/llm_interfaces.py')
detector_mod = load('core.client.llm.llm_role_detector', 'core/client/llm/llm_role_detector.py')
stream_mod = load('core.client.output.stream_output', 'core/client/output/stream_output.py')
task_line = load('core.client.task_line', 'core/client/task_line.py')
RoleConfig = roles.RoleConfig
indicator_mod = load('core.client.ui.recording_indicator', 'core/client/ui/recording_indicator.py')
with patch.dict(sys.modules, {'rich': types.ModuleType('rich'),
                             'rich.console': types.SimpleNamespace(Console=Mock()),
                             'rich.theme': types.SimpleNamespace(Theme=Mock())}):
    state_mod = load('core.client.state', 'core/client/state.py')

METHODS = {'_handle_message', '_after_output', '_paste_for_foreground', '_correct_text',
           '_on_stream_delta', '_preview_image_role', '_decide_stream_mode', '_type_piece', '_finish_stream',
           '_print_task_line', '_cleanup'}


def extract_methods(namespace):
    tree = ast.parse((ROOT / 'core/client/output/result_processor.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ResultProcessor')
    nodes = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in METHODS]
    assert {n.name for n in nodes} == METHODS, {n.name for n in nodes} ^ METHODS
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'result_processor.py', 'exec'), namespace)


def shortcut_method(name, namespace):
    path = ROOT / 'core/client/shortcut/task.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ShortcutTask')
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


class StreamingConfigTests(unittest.TestCase):
    def test_image_preview_obeys_shortcut_switch(self):
        want = shortcut_method('_want_streaming', {})
        for switch in (None, False, True):
            for sticky in (None, '助理'):
                for default in (False, True):
                    for enabled in (False, True):
                        with self.subTest(switch=switch, sticky=sticky, default=default, llm=enabled):
                            shortcut = types.SimpleNamespace()
                            if switch is not None:
                                shortcut.streaming = switch
                            task = types.SimpleNamespace(shortcut=shortcut, app=types.SimpleNamespace(
                                llm=types.SimpleNamespace(default_role_enabled=lambda: default)))
                            config = types.SimpleNamespace(ClientConfig=types.SimpleNamespace(llm_enabled=enabled))
                            snapshot = state_mod.ContinuousChatSnapshot(sticky_role=sticky)
                            with patch.dict(sys.modules, {'config_client': config}):
                                self.assertEqual(want(task, snapshot, object()), bool(switch and enabled))
                                self.assertEqual(want(task, snapshot, None),
                                                 bool(switch and not sticky and not (enabled and default)))


class FakeLoader:
    """角色加载器桩：助理、新岛真|真学姐（具名），默认角色可开关"""

    def __init__(self, default_enabled=False):
        self.roles = {'助理': RoleConfig(name='助理'), '新岛真': RoleConfig(name='新岛真|真学姐'),
                      '翻译': RoleConfig(name='翻译', enabled=False)}
        self.default = RoleConfig(name=RoleConfig.DEFAULT_ROLE_NAME, enabled=default_enabled)

    def get_roles(self):
        return dict(self.roles)

    def get_default_role(self):
        return self.default


class RoleDetectorTests(unittest.TestCase):
    def test_name_prefix(self):
        d = detector_mod.RoleDetector(FakeLoader())
        self.assertTrue(d.is_name_prefix('新岛'))
        self.assertTrue(d.is_name_prefix('真学，'))      # 别名的开头，末尾标点忽略
        self.assertFalse(d.is_name_prefix('新岛真'))      # 完整角色名交给 detect
        self.assertFalse(d.is_name_prefix('翻'))          # 禁用的角色不算
        self.assertFalse(d.is_name_prefix('今天'))
        self.assertFalse(d.is_name_prefix('，'))

    def test_default_role_enabled(self):
        self.assertFalse(detector_mod.RoleDetector(FakeLoader()).default_role_enabled())
        self.assertTrue(detector_mod.RoleDetector(FakeLoader(True)).default_role_enabled())


class TrimTests(unittest.TestCase):
    def test_trim(self):
        trim = stream_mod.trim_for_strip_punc
        self.assertEqual(trim('你好', '，再见。', '你好，再见'), '，再见')
        self.assertEqual(trim('你好', '，再见。', '你好，再见。'), '，再见。')
        self.assertEqual(trim('你好。', '', '你好'), '')        # 已上屏的改不了


def message(task_id='t', delta='', text='', is_final=False):
    return types.SimpleNamespace(task_id=task_id, delta=delta, text=text, is_final=is_final,
                                 time_complete=10.0, time_submit=9.5, time_start=1.0, duration=3.0)


class ProcessorHarness(unittest.IsolatedAsyncioTestCase):
    """把 ResultProcessor 的真实方法绑到桩对象上；本身不含测试"""

    def make(self, llm_enabled=True, cancelled=False, default_role=False):
        self.config = types.SimpleNamespace(
            llm_enabled=llm_enabled, traditional_convert=False, hot=True, paste=False,
            paste_apps=[], save_audio=False)
        self.console = Mock()
        self.note = Mock()
        self.progress = Mock()
        self.typed = []
        ns = dict(Optional=Optional, Dict=Dict, Config=self.config, RecognitionMessage=object,
                  console=self.console, logger=logging.getLogger('stream-test'), time=time,
                  TextOutput=types.SimpleNamespace(strip_punc=lambda t: t[:-1] if t.endswith('。') and len(t) < 6 else t),
                  show_processing_indicator=Mock(), hide_recording_indicator=Mock(),
                  update_recording_indicator_progress=self.progress,
                  update_recording_indicator_stream_note=self.note,
                  get_active_window_info=lambda: {'process_name': 'Code.exe'},
                  broadcast_output_udp=Mock(), RoleConfig=RoleConfig, zhconv_convert=None,
                  format_selection_preview=lambda s: '',
                  StreamSession=stream_mod.StreamSession, trim_for_strip_punc=stream_mod.trim_for_strip_punc,
                  PENDING=stream_mod.PENDING, TYPING=stream_mod.TYPING, DEFERRED=stream_mod.DEFERRED,
                  format_task_line=task_line.format_task_line, aborted_note=task_line.aborted_note,
                  no_result_note=task_line.no_result_note, DISCONNECTED=task_line.DISCONNECTED)
        extract_methods(ns)

        self.loader = FakeLoader(default_role)
        detector = detector_mod.RoleDetector(self.loader)
        llm = types.SimpleNamespace(detect_role=detector.detect, is_role_name_prefix=detector.is_name_prefix,
                                    resolve_role=lambda name: self.loader.roles.get(name),
                                    process_and_output=AsyncMock(return_value=None))
        state = Mock()
        state.is_task_cancelled.return_value = cancelled
        state.pending_task_id = 't'
        state.streamed_task_ids = []
        state.image_snapshots = {}
        state.continuous_snapshots = {}
        state.pop_image_snapshot.return_value = None
        state.pop_continuous_snapshot.return_value = None
        # 等待结果的登记表照真实语义：取出即删
        self.awaiting = {}
        state.pop_awaiting_result.side_effect = lambda tid: self.awaiting.pop(tid, None)

        def drain():
            items = list(self.awaiting.items())
            self.awaiting.clear()
            return items

        state.drain_awaiting_results.side_effect = drain
        hotword = Mock()
        # 热词：把「鱿鱼」纠正成「由于」，并记一条完全匹配
        hotword.get_phoneme_corrector.return_value.correct.side_effect = lambda t, k=10: types.SimpleNamespace(
            text=t.replace('鱿鱼', '由于'), matchs=[('鱿鱼', '由于', 1.0)] if '鱿鱼' in t else [], similars=[])
        hotword.get_rule_corrector.return_value.substitute.side_effect = lambda t: t

        async def output(text, paste=None):
            if text:
                self.typed.append(text)

        p = types.SimpleNamespace(state=state, hotword=hotword, app=types.SimpleNamespace(llm=llm),
                                  output=types.SimpleNamespace(output=output), _streams={},
                                  _resolve_forced_role=Mock(return_value=None), _report_image_usage=Mock(),
                                  _update_continuous_state=Mock(), _log_modifier_key_state=Mock())
        for name in METHODS:
            fn = ns[name]
            if isinstance(fn, staticmethod):
                setattr(p, name, fn.__func__)
            else:
                setattr(p, name, (lambda f: lambda *a, **k: f(p, *a, **k))(fn))
        self.p, self.llm, self.state = p, llm, state
        return p

    async def feed(self, *msgs):
        for m in msgs:
            await self.p._handle_message(m)


class ProcessorTests(ProcessorHarness):
    async def test_output_mode_for_whole_and_streaming_results(self):
        for streaming in (False, True):
            for default, apps, expected in ((False, [], False), (False, ['CODE.exe'], True),
                                            (False, ['Other.exe'], False), (True, [], True)):
                with self.subTest(streaming=streaming, default=default, apps=apps):
                    self.make(llm_enabled=False)
                    self.config.paste = default
                    self.config.paste_apps = apps
                    self.p.output.output = AsyncMock()
                    if streaming:
                        await self.feed(message(delta='第一句'),
                                        message(delta='第二句', text='第一句第二句', is_final=True))
                    else:
                        await self.feed(message(text='完整文字', is_final=True))
                    self.assertEqual(self.p.output.output.await_count, 2 if streaming else 1)
                    for call in self.p.output.output.await_args_list:
                        self.assertIs(call.kwargs['paste'], expected)

    async def test_llm_receives_foreground_output_mode(self):
        for apps, expected in (([], False), (['CODE.exe'], True)):
            with self.subTest(apps=apps):
                self.make()
                self.config.paste_apps = apps
                await self.feed(message(text='助理，你好', is_final=True))
                self.assertIs(self.llm.process_and_output.await_args.kwargs['paste'], expected)

    async def test_typing_sentence_by_sentence(self):
        self.make()
        await self.feed(message(delta='鱿鱼今天下雨'), message(delta='，我们改在室内'))
        self.assertEqual(self.typed, ['由于今天下雨', '，我们改在室内'])
        self.assertEqual(self.state.streamed_task_ids, ['t'])
        await self.feed(message(delta='开会。', text='鱿鱼今天下雨，我们改在室内开会。', is_final=True))
        self.assertEqual(self.typed, ['由于今天下雨', '，我们改在室内', '开会。'])
        self.state.set_output_text.assert_called_once_with('由于今天下雨，我们改在室内开会。')
        self.llm.process_and_output.assert_not_called()
        self.p._update_continuous_state.assert_called_once()
        self.assertEqual(self.p._streams, {})
        # 各句的热词替换要汇总到终端那一行（只看原词出现了，不管排版）
        printed = ' '.join(str(c.args[0]) for c in self.console.print.call_args_list)
        self.assertIn('鱿鱼', printed)

    async def test_role_at_start_defers_to_release(self):
        self.make()
        await self.feed(message(delta='助理，帮我查一下'), message(delta='明天的天气'))
        self.assertEqual(self.typed, [])
        self.note.assert_called()
        self.assertIn('助理', self.note.call_args.args[0])
        full = '助理，帮我查一下明天的天气。'
        await self.feed(message(delta='。', text=full, is_final=True))
        self.llm.process_and_output.assert_awaited_once()
        self.assertEqual(self.llm.process_and_output.call_args.args[0], full)

    async def test_role_name_split_waits_for_next_sentence(self):
        self.make()
        await self.feed(message(delta='新岛'))
        self.assertEqual(self.typed, [])
        await self.feed(message(delta='真，你好'))
        self.assertEqual(self.typed, [])
        await self.feed(message(delta='。', text='新岛真，你好。', is_final=True))
        self.assertEqual(self.typed, [])
        self.assertEqual(self.llm.process_and_output.call_args.args[0], '新岛真，你好。')

    async def test_name_prefix_that_turns_out_not_a_role(self):
        self.make()
        await self.feed(message(delta='新岛'), message(delta='这个地方很好'))
        self.assertEqual(self.typed, ['新岛这个地方很好'])

    async def test_pending_until_release_goes_whole(self):
        self.make()
        await self.feed(message(delta='新岛'), message(delta='', text='新岛。', is_final=True))
        self.assertEqual(self.typed, [])
        self.assertEqual(self.llm.process_and_output.call_args.args[0], '新岛')   # 整段流程照常去末尾标点

    async def test_llm_disabled_never_defers(self):
        self.make(llm_enabled=False)
        await self.feed(message(delta='助理，你好'))
        self.assertEqual(self.typed, ['助理，你好'])

    async def test_cancelled_task_types_nothing(self):
        self.make(cancelled=True)
        await self.feed(message(delta='你好'), message(delta='。', text='你好。', is_final=True))
        self.assertEqual(self.typed, [])
        self.state.release_cancelled_task.assert_called_once_with('t')

    async def test_old_server_without_delta_uses_whole_flow(self):
        self.make()
        await self.feed(message(text='第一段', is_final=False))
        self.progress.assert_called_once()
        await self.feed(message(text='第一段第二段。', is_final=True))
        self.assertEqual(self.llm.process_and_output.call_args.args[0], '第一段第二段。')

    async def test_strip_punc_applies_to_last_piece(self):
        self.make()
        await self.feed(message(delta='好的'), message(delta='。', text='好的。', is_final=True))
        self.assertEqual(self.typed, ['好的'])          # 全文短，末尾句号按配置去掉


class ImagePreviewTests(ProcessorHarness):
    def prepare(self, sticky=None, **kwargs):
        self.make(**kwargs)
        self.state = self.p.state = state_mod.ClientState()
        self.state.start_recording(1.0)
        self.state.pending_task_id = 't'
        self.image = types.SimpleNamespace(data_url='data:image/png;base64,AAA')
        self.state.set_pending_image(self.image)
        self.state.begin_image_snapshot()
        self.state.bind_image_snapshot('t')
        self.state.continuous_role = sticky
        self.snapshot = self.state.begin_continuous_snapshot()
        self.state.bind_continuous_snapshot('t')
        self.p._resolve_forced_role = lambda s: self.llm.resolve_role(s.forced_role) if s and s.forced_role else None

    async def test_preview_then_send_whole_text_and_bound_image_once(self):
        self.prepare()
        first = '助理，看看这张图'
        await self.feed(message(delta=first, text=first))
        self.assertIn('助理', self.note.call_args.args[0])
        self.assertEqual(self.note.call_args.args[1], 1.0)
        self.assertEqual(self.typed, [])
        self.assertEqual(list(self.state.streamed_task_ids), [])
        self.llm.process_and_output.assert_not_called()
        self.assertIs(self.state.image_snapshots['t'], self.image)
        # 下一张待挂图片不能替换当前任务已绑定的图片。
        next_image = object()
        self.state.set_pending_image(next_image)
        full = first + '，解释报错原因。'
        await self.feed(message(delta='，解释报错原因。', text=full, is_final=True))
        self.llm.process_and_output.assert_awaited_once()
        call = self.llm.process_and_output.call_args
        self.assertEqual(call.args[0], full)
        self.assertEqual(call.kwargs['image_data'], self.image.data_url)
        self.assertIs(self.state.pending_image, next_image)
        self.assertEqual(self.state.image_snapshots, {})
        self.assertEqual(self.p._streams, {})

    async def test_no_role_and_disabled_llm_never_type_before_release(self):
        for enabled in (False, True):
            self.prepare(llm_enabled=enabled)
            await self.feed(message(delta='这是普通听写', text='这是普通听写'))
            self.assertEqual(self.typed, [])
            self.assertIs(self.state.image_snapshots['t'], self.image)
            await self.feed(message(delta='。', text='这是普通听写。', is_final=True))
            if enabled:
                self.llm.process_and_output.assert_awaited_once()
                self.assertEqual(self.note.call_args.args[0], '')
            else:
                self.assertEqual(self.typed, ['这是普通听写。'])
                self.note.assert_not_called()

    async def test_split_name_uses_accumulated_text(self):
        self.prepare()
        await self.feed(message(delta='新岛', text='新岛'))
        self.assertEqual(self.note.call_args.args[0], '')
        await self.feed(message(delta='真，帮我看图', text='新岛真，帮我看图'))
        self.assertIn('新岛真', self.note.call_args.args[0])
        self.assertEqual(self.typed, [])

    async def test_rule_across_chunks_matches_final_role(self):
        self.prepare()
        self.p.hotword.get_rule_corrector.return_value.substitute.side_effect = lambda t: t.replace('语音助手', '助理')
        await self.feed(message(delta='语音助', text='语音助'))
        self.assertEqual(self.note.call_args.args[0], '')
        await self.feed(message(delta='手，看图', text='语音助手，看图'))
        self.assertIn('助理', self.note.call_args.args[0])
        await self.feed(message(delta='解释一下', text='语音助手，看图解释一下', is_final=True))
        self.assertEqual(self.llm.process_and_output.call_args.args[0], '助理，看图解释一下')

    async def test_continuous_route_uses_task_snapshot_and_tracks_exit(self):
        self.prepare(sticky='助理')
        self.state.continuous_role = '新岛真'
        await self.feed(message(delta='新岛真，看图', text='新岛真，看图'))
        self.assertIn('助理', self.note.call_args.args[0])
        self.assertEqual(self.note.call_args.kwargs['continuous_mode'], self.snapshot.indicator_mode())
        self.state.toggle_continuous()
        await self.feed(message(delta='解释一下', text='新岛真，看图解释一下'))
        self.assertIn('新岛真', self.note.call_args.args[0])
        self.assertEqual(self.note.call_args.kwargs['continuous_mode'], self.snapshot.indicator_mode())
        self.assertEqual(self.typed, [])

    async def test_default_role_preview_and_role_change_clear_old_note(self):
        self.prepare(default_role=True)
        await self.feed(message(delta='看图', text='看图'))
        self.assertIn(RoleConfig.DEFAULT_ROLE_NAME, self.note.call_args.args[0])
        self.loader.default.enabled = False
        await self.feed(message(delta='解释一下', text='看图解释一下'))
        self.assertEqual(self.note.call_args.args[0], '')

    async def test_cancel_returns_image_and_late_results_do_not_preview_or_send(self):
        self.prepare()
        await self.feed(message(delta='助理，看图', text='助理，看图'))
        self.state.mark_task_cancelled('t')
        self.state.rollback_continuous_snapshot('t')
        self.state.rollback_image_snapshot('t')
        self.note.reset_mock()
        await self.feed(message(delta='继续', text='助理，看图继续'),
                        message(delta='。', text='助理，看图继续。', is_final=True))
        self.assertIs(self.state.pending_image, self.image)
        self.assertEqual(self.state.image_snapshots, {})
        self.assertEqual(self.typed, [])
        self.assertEqual(self.p._streams, {})
        self.note.assert_not_called()
        self.llm.process_and_output.assert_not_called()

    async def test_no_delta_keeps_whole_flow_without_preview(self):
        for partial in (False, True):
            self.prepare()
            if partial:
                await self.feed(message(text='助理，看图'))
            await self.feed(message(text='助理，看图解释一下。', is_final=True))
            self.note.assert_not_called()
            self.llm.process_and_output.assert_awaited_once()
            self.assertEqual(self.llm.process_and_output.call_args.kwargs['image_data'], self.image.data_url)

    async def test_next_pending_image_does_not_defer_plain_recording(self):
        self.make(llm_enabled=False)
        self.state.pending_image = object()
        await self.feed(message(delta='普通听写', text='普通听写'))
        self.assertEqual(self.typed, ['普通听写'])


class IndicatorPreviewTests(unittest.TestCase):
    def setUp(self):
        # 绕过 Tk 单例初始化，运行真实命令处理和文案逻辑；不建窗口。
        self.indicator = object.__new__(indicator_mod.RecordingIndicator)
        self.indicator.state = indicator_mod.IndicatorState()
        self.indicator._tick_index = 0
        self.indicator._get_anchor = Mock(return_value=((0, 0), 'near'))
        for name in ('_ensure_window', '_render', '_show_now'):
            setattr(self.indicator, name, Mock())
        self.handle = self.indicator._handle_command
        self.handle('recording', dict(start_time=1.0, has_image=True, streaming=True))

    def test_old_recording_and_finished_recording_reject_late_note(self):
        self.handle('recording', dict(start_time=2.0, has_image=True, streaming=True))
        self.handle('stream_note', dict(start_time=1.0, note='旧角色'))
        self.assertEqual(self.indicator.state.stream_note, '')
        self.handle('stream_note', dict(start_time=2.0, note='当前角色'))
        self.assertEqual(self.indicator._compose_text()[1], '当前角色')
        self.handle('waiting', {})
        self.handle('stream_note', dict(start_time=2.0, note='迟到更新'))
        self.assertEqual(self.indicator.state.mode, 'waiting')
        self.assertEqual(self.indicator.state.stream_note, '当前角色')

    def test_route_toggle_clears_preview_until_next_delta(self):
        self.handle('stream_note', dict(start_time=1.0, note='旧角色'))
        self.handle('continuous', dict(start_time=1.0, mode='exited', role='旧角色'))
        self.assertEqual(self.indicator.state.stream_note, '')
        self.assertEqual(self.indicator.state.start_time, 1.0)
        self.assertTrue(self.indicator.state.has_image)

    def test_old_route_preview_after_toggle_is_rejected(self):
        self.handle('continuous', dict(start_time=1.0, mode='exited', role='助理'))
        self.handle('stream_note', dict(start_time=1.0, note='旧路由', continuous_mode='active'))
        self.assertEqual(self.indicator.state.stream_note, '')
        self.handle('stream_note', dict(start_time=1.0, note='新路由', continuous_mode='exited'))
        self.assertEqual(self.indicator.state.stream_note, '新路由')

    def test_old_recording_toggle_cannot_change_new_recording(self):
        self.handle('recording', dict(start_time=2.0, has_image=True, streaming=True))
        self.handle('stream_note', dict(start_time=2.0, note='新角色', continuous_mode=''))
        self.handle('continuous', dict(start_time=1.0, mode='exited', role='旧角色'))
        self.assertEqual(self.indicator.state.continuous_mode, '')
        self.assertEqual(self.indicator.state.stream_note, '新角色')

    def test_role_preview_replaces_and_clears_to_recording_detail(self):
        # 默认说明允许共用文案；只验证角色提示能替换，并能恢复到当前录音的说明。
        for image in (False, True):
            self.handle('recording', dict(start_time=1.0, has_image=image, streaming=True))
            detail = self.indicator._compose_text()[1]
            self.handle('stream_note', dict(start_time=1.0, note='测试角色'))
            self.assertEqual(self.indicator._compose_text()[1], '测试角色')
            self.handle('stream_note', dict(start_time=1.0, note=''))
            self.assertEqual(self.indicator._compose_text()[1], detail)

    def test_public_updates_forward_recording_and_route_to_queue(self):
        self.indicator._post = Mock()
        with patch.object(indicator_mod, 'RecordingIndicator', return_value=self.indicator):
            indicator_mod.update_recording_indicator_stream_note('角色提示', 1.0, continuous_mode='active')
            indicator_mod.update_recording_indicator_continuous('exited', '助理', 1.0)
        calls = self.indicator._post.call_args_list
        self.assertEqual(calls[0].args, ('stream_note',))
        self.assertEqual(calls[0].kwargs, dict(note='角色提示', start_time=1.0, continuous_mode='active'))
        self.assertEqual(calls[1].args, ('continuous',))
        self.assertEqual(calls[1].kwargs, dict(mode='exited', role='助理', start_time=1.0))


TID = 'a3f91c07_10-20_17-54-20'


class TaskLineTests(ProcessorHarness):
    """任务的第一行：结果到达时才打、排在本任务输出的最前面、每个任务只打一次"""

    def printed(self):
        return [str(c.args[0]) for c in self.console.print.call_args_list if c.args]

    def lines_with(self, tid):
        return [line for line in self.printed() if tid in line]

    def assert_one_task_line(self, tid, duration):
        lines = self.lines_with(tid)
        self.assertEqual(len(lines), 1, self.printed())
        self.assertIn(f'{duration:.2f}s', lines[0])
        self.assertNotIn(tid, self.awaiting)

    async def test_whole_result_line_comes_first(self):
        self.make()
        self.awaiting[TID] = 2.5
        await self.feed(message(task_id=TID, text='今天天气不错', is_final=True))
        self.assert_one_task_line(TID, 2.5)
        self.assertIn(TID, self.printed()[0])

    async def test_streaming_line_waits_for_final(self):
        self.make()
        self.awaiting[TID] = 3.0
        await self.feed(message(task_id=TID, delta='今天下雨'), message(task_id=TID, delta='，改在室内'))
        self.assertEqual(self.lines_with(TID), [])
        await self.feed(message(task_id=TID, delta='开会。', text='今天下雨，改在室内开会。', is_final=True))
        self.assert_one_task_line(TID, 3.0)
        self.assertIn(TID, self.printed()[0])

    async def test_cancelled_task_prints_one_line_on_final(self):
        self.make(cancelled=True)
        self.awaiting[TID] = 1.2
        await self.feed(message(task_id=TID, text='你好', is_final=False))
        self.assertEqual(self.printed(), [])
        await self.feed(message(task_id=TID, text='你好。', is_final=True))
        self.assert_one_task_line(TID, 1.2)

    async def test_cancelled_after_streaming_says_so(self):
        lines = []
        for streamed in ([], [TID]):
            self.make(cancelled=True)
            self.state.streamed_task_ids = streamed
            await self.feed(message(task_id=TID, text='你好。', is_final=True))
            lines.append(self.lines_with(TID)[0])
        self.assertNotEqual(lines[0], lines[1])

    async def test_cleanup_prints_every_waiting_task(self):
        self.make()
        other = 'b0c1d2e3_10-20_17-54-30'
        self.awaiting.update({TID: 1.0, other: 2.0})
        self.state.is_task_cancelled.side_effect = lambda tid: tid == other
        self.p._cleanup()
        self.assert_one_task_line(TID, 1.0)
        self.assert_one_task_line(other, 2.0)
        # 被打断的与单纯断线的说明彼此不同（不比对措辞）
        notes = [line.replace(TID, '').replace(other, '').replace('1.00s', '').replace('2.00s', '')
                 for line in self.printed()]
        self.assertNotEqual(notes[0], notes[1])


class RoleLoaderErrorTests(unittest.TestCase):
    """角色文件导入失败要记日志；出自 secrets.py 的异常只记类型，不记信息"""

    def setUp(self):
        sys.modules['config_client'] = types.SimpleNamespace(BASE_DIR=str(ROOT))
        self.loader_mod = load('core.client.llm.llm_role_loader', 'core/client/llm/llm_role_loader.py')

    def raise_in(self, filename, exc_code):
        code = compile(exc_code, filename, 'exec')
        try:
            exec(code, {})
        except Exception as e:
            return e

    def test_secrets_error_is_redacted(self):
        e = self.raise_in(str(ROOT / 'LLM' / 'secrets.py'), "KEY = sk_live_abc123\n")
        text = self.loader_mod._describe_error(e)
        self.assertIn('NameError', text)
        self.assertNotIn('sk_live_abc123', text)

    def test_other_error_keeps_message(self):
        e = self.raise_in(str(ROOT / 'LLM' / 'backends.py'),
                          "raise ImportError(\"cannot import name 'CUSTOM_API_KEY' from 'LLM.secrets'\")\n")
        self.assertIn("cannot import name 'CUSTOM_API_KEY'", self.loader_mod._describe_error(e))


if __name__ == '__main__':
    unittest.main()
