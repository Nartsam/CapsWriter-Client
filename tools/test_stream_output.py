# coding: utf-8
"""流式上屏、角色整段识别、挂图与唯一终局；
另含任务在控制台的第一行（结果到达时才打、每个任务一行、打断与断线清理）。

ResultProcessor 的相关方法按源码抽出来，绑到桩对象上运行；打字、热词、LLM、指示器均为桩。
"""
from __future__ import annotations

import ast
import asyncio
import json
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

protocol = load('core.protocol', 'core/protocol.py')


def load_class(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    future = ast.parse('from __future__ import annotations').body
    exec(compile(ast.Module(body=future + [cls], type_ignores=[]), path, 'exec'), namespace)
    return namespace[name]


def shortcut_method(name, namespace):
    path = ROOT / 'core/client/shortcut/task.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ShortcutTask')
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


class StreamingConfigTests(unittest.TestCase):
    def test_known_role_uses_full_recognition_from_start(self):
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
                                self.assertEqual(want(task, snapshot),
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

    def test_default_role_keeps_named_prefix_priority(self):
        detector = detector_mod.RoleDetector(FakeLoader(True))
        self.assertEqual(detector.detect('助理，你好')[0].display_name, '助理')
        self.assertEqual(detector.detect('普通正文')[0].display_name, RoleConfig.DEFAULT_ROLE_NAME)


class TrimTests(unittest.TestCase):
    def test_trim(self):
        trim = stream_mod.trim_for_strip_punc
        self.assertEqual(trim('你好', '，再见。', '你好，再见'), '，再见')
        self.assertEqual(trim('你好', '，再见。', '你好，再见。'), '，再见。')
        self.assertEqual(trim('你好。', '', '你好'), '')        # 已上屏的改不了


def message(task_id='t', delta='', text='', phase='partial', mode='stream', status='ok'):
    return protocol.RecognitionMessage(task_id=task_id, delta=delta, text=text,
        is_final=phase == 'final', phase=phase, recognition_mode=mode, status=status,
        error='ASR failure' if status == 'error' else '',
        time_complete=10.0, time_submit=9.5, time_start=1.0, duration=3.0)


class ProcessorHarness(unittest.IsolatedAsyncioTestCase):
    def make(self, llm_enabled=True, default_role=False):
        self.config = types.SimpleNamespace(llm_enabled=llm_enabled, traditional_convert=False,
            hot=True, paste=False, paste_apps=[], save_audio=False, cancel_recording_key='esc')
        self.console, self.note, self.progress = Mock(), Mock(), Mock()
        self.typed, self.events = [], []
        self.state = state_mod.ClientState()
        self.loader = FakeLoader(default_role)
        detector = detector_mod.RoleDetector(self.loader)
        def resolve(name):
            role = self.loader.roles.get(name)
            return role if role is not None and role.enabled else None
        self.llm = types.SimpleNamespace(role_detector=detector, detect_role=detector.detect,
            is_role_name_prefix=detector.is_name_prefix, resolve_role=resolve,
            default_role_enabled=detector.default_role_enabled,
            process_and_output=AsyncMock(return_value=None))
        async def send(msg, connection=None):
            self.events.append(('control', msg.action))
            return True
        async def output(text, paste=None):
            self.events.append(('output', text))
            if text:
                self.typed.append(text)
        hotword = Mock()
        hotword.get_phoneme_corrector.return_value.correct.side_effect = lambda t, k=10: types.SimpleNamespace(
            text=t.replace('鱿鱼', '由于'), matchs=[('鱿鱼', '由于', 1.0)] if '鱿鱼' in t else [], similars=[])
        hotword.get_rule_corrector.return_value.substitute.side_effect = lambda t: t
        ns = dict(asyncio=asyncio, time=time, Config=self.config, console=self.console,
            logger=logging.getLogger('stream-test'), RecognitionMessage=protocol.RecognitionMessage,
            RecognitionControlMessage=protocol.RecognitionControlMessage, RoleConfig=RoleConfig,
            TextOutput=types.SimpleNamespace(strip_punc=lambda t: t[:-1] if t.endswith('。') and len(t) < 6 else t),
            show_processing_indicator=Mock(), hide_recording_indicator=Mock(),
            show_recording_indicator_error=Mock(), update_recording_indicator_progress=self.progress,
            update_recording_indicator_stream_note=self.note, get_active_window_info=lambda: {'process_name': 'Code.exe'},
            broadcast_output_udp=Mock(), zhconv_convert=lambda t, locale: t,
            format_selection_preview=lambda s: '', AudioFileManager=Mock(), write_llm_md=Mock(),
            mark_outcome=Mock(return_value=''), IMAGE_PLACEHOLDER='[图]',
            StreamSession=stream_mod.StreamSession, trim_for_strip_punc=stream_mod.trim_for_strip_punc,
            PENDING=stream_mod.PENDING, TYPING=stream_mod.TYPING,
            format_task_line=task_line.format_task_line, aborted_note=task_line.aborted_note)
        cls = load_class('core/client/output/result_processor.py', 'ResultProcessor', ns)
        self.app = types.SimpleNamespace(state=self.state, loop=asyncio.get_running_loop(), llm=self.llm,
            ws=types.SimpleNamespace(send=AsyncMock(side_effect=send)), hotword=hotword,
            output=types.SimpleNamespace(output=AsyncMock(side_effect=output)), diary=Mock())
        self.p = cls(self.app)
        self.p._log_modifier_key_state = Mock()
        self.p._after_output = Mock(wraps=self.p._after_output)
        self.p._report_image_usage = Mock(wraps=self.p._report_image_usage)
        self.p._update_continuous_state = Mock(wraps=self.p._update_continuous_state)
        self.ns = ns
        self.connection = object()
        return self.p

    def begin(self, task_id='t', streaming=True, image=False, sticky=None):
        """按 ShortcutTask.launch 的方式登记一次已经松手的录音；录音协程与快捷键用替身"""
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        session = stream_mod.StreamSession(task_id, streaming=streaming,
            revisable=streaming and self.config.llm_enabled, start_time=1.0, connection=self.connection,
            recorder=types.SimpleNamespace(_done=done, delete_recording=AsyncMock()),
            owner=types.SimpleNamespace(stop_for_error=Mock()))
        self.state.recognition_sessions[task_id] = session
        self.state.pending_task_id = task_id
        self.state.continuous_role = sticky
        snapshot = self.state.begin_continuous_snapshot(task_id)
        if image:
            self.image = types.SimpleNamespace(data_url='image', width=1, height=1, archive_path='')
            self.state.set_pending_image(self.image)
        self.state.begin_image_snapshot(task_id)
        self.state.register_awaiting_result(task_id, 3.0)
        return session, snapshot

    async def feed(self, *messages):
        for msg in messages:
            await self.p._handle_message(msg)

    def task_lines(self, task_id='t'):
        return [str(c.args[0]) for c in self.console.print.call_args_list if c.args and str(c.args[0]).split('  ')[0] == '任务标识：' + task_id]


class ProcessorTests(ProcessorHarness):
    async def test_role_locks_and_receives_only_independent_full_text(self):
        for image in (False, True):
            self.make()
            session, _ = self.begin(image=image)
            await self.feed(message(delta='助理，预览措辞', text='助理，预览措辞'))
            self.assertEqual(self.events, [('control', 'request_full')])
            self.llm.process_and_output.assert_not_called()
            self.assertIn('t', self.state.continuous_snapshots)
            self.assertIn('t', self.state.awaiting_results)
            if image:
                self.assertIs(self.state.image_snapshots['t'], self.image)
            await self.feed(message(delta='旧尾巴', text='助理，预览措辞旧尾巴'),
                            message(text='助理，重新识别的全文', phase='final', mode='full'))
            call = self.llm.process_and_output.await_args
            self.assertEqual(call.args[0], '助理，重新识别的全文')
            self.assertEqual(call.kwargs['forced_content'], '重新识别的全文')
            self.assertEqual(call.kwargs['forced_role'].display_name, '助理')
            self.assertEqual(call.kwargs['image_data'], 'image' if image else None)
            self.p._after_output.assert_called_once()
            self.assertFalse(self.p._streams)
            self.assertEqual(len(self.task_lines()), 1)

    async def test_split_prefix_and_final_candidate(self):
        self.make()
        self.begin()
        await self.feed(message(delta='新岛', text='新岛'))
        self.assertEqual(self.events, [])
        await self.feed(message(delta='真，你好', text='新岛真，你好', phase='decision'))
        self.assertEqual(self.events, [('control', 'request_full')])
        await self.feed(message(text='新岛真，完整正文', phase='final', mode='full'))
        self.assertEqual(self.llm.process_and_output.await_args.kwargs['forced_content'], '完整正文')

    async def test_short_recording_first_text_is_decision(self):
        self.make()
        self.begin()
        await self.feed(message(delta='助理，你好', text='助理，你好', phase='decision'))
        self.assertEqual(self.events, [('control', 'request_full')])
        self.assertEqual(self.task_lines(), [])
        self.assertIsNone(self.state.last_recognition_text)

    async def test_locked_role_does_not_change_when_final_prefix_changes(self):
        for final, content in [('真学姐，你好', '你好'), ('助理，你好', '助理，你好')]:
            self.make()
            self.begin()
            await self.feed(message(delta='新岛真，预览', text='新岛真，预览'),
                            message(text=final, phase='final', mode='full'))
            call = self.llm.process_and_output.await_args
            self.assertEqual(call.kwargs['forced_role'].display_name, '新岛真')
            self.assertEqual(call.kwargs['forced_content'], content)

    async def test_locked_role_disabled_or_removed_skips_output(self):
        for remove in (False, True):
            self.make()
            self.begin()
            await self.feed(message(delta='助理，预览', text='助理，预览'))
            if remove:
                del self.loader.roles['助理']
            else:
                self.loader.roles['助理'].enabled = False
            await self.feed(message(text='新岛真，正文', phase='final', mode='full'))
            self.llm.process_and_output.assert_not_called()
            self.assertEqual(self.typed, [])
            self.p._after_output.assert_called_once()

    async def test_commit_precedes_typing_and_final_tail_is_not_duplicated(self):
        self.make()
        self.begin()
        await self.feed(message(delta='鱿鱼今天下雨', text='鱿鱼今天下雨'),
                        message(text='鱿鱼今天下雨，助理你好。', delta='，助理你好。', phase='decision'))
        self.assertEqual(self.events[:2], [('control', 'commit_stream'), ('output', '由于今天下雨')])
        await self.feed(message(text='鱿鱼今天下雨，助理你好。', delta='，助理你好。', phase='final'))
        self.assertEqual(self.typed, ['由于今天下雨', '，助理你好。'])
        self.assertEqual(self.state.last_output_text, '由于今天下雨，助理你好。')
        self.llm.process_and_output.assert_not_called()
        self.assertEqual(self.app.ws.send.await_count, 1)
        self.assertEqual(len(self.task_lines()), 1)

    async def test_decision_never_types_and_final_removes_short_punctuation(self):
        for image in (False, True):
            self.make()
            self.begin(image=image)
            await self.feed(message(delta='好的。', text='好的。', phase='decision'))
            self.assertEqual(self.typed, [])
            await self.feed(message(delta='好的。', text='好的。', phase='final'))
            self.assertEqual(self.typed, ['好的'])
            self.llm.process_and_output.assert_not_called()
            self.assertFalse(self.state.image_snapshots)

    async def test_prefix_that_is_not_role_commits_accumulated_text(self):
        self.make()
        self.begin()
        await self.feed(message(delta='新岛', text='新岛'),
                        message(delta='这个地方很好', text='新岛这个地方很好'))
        self.assertEqual(self.typed, ['新岛这个地方很好'])

    async def test_punctuation_only_trimmed_from_final_piece(self):
        self.make()
        self.begin()
        await self.feed(message(delta='好的', text='好的'),
                        message(delta='。', text='好的。', phase='final'))
        self.assertEqual(self.typed, ['好的'])

    async def test_image_waits_for_role_and_uses_cumulative_rules(self):
        self.make()
        self.begin(image=True)
        self.app.hotword.get_rule_corrector.return_value.substitute.side_effect = lambda t: t.replace('语音助手', '助理')
        await self.feed(message(delta='语音助', text='语音助'))
        self.assertEqual(self.events, [])
        await self.feed(message(delta='手，看看', text='语音助手，看看'))
        self.assertEqual(self.events, [('control', 'request_full')])

    async def test_continuous_exit_uses_normal_prefix_on_full_result(self):
        for exiting in (False, True):
            self.make()
            _, snapshot = self.begin(streaming=False, image=True, sticky='助理')
            if exiting:
                self.state.toggle_continuous()
                self.state.commit_continuous_exit()
            await self.feed(message(text='新岛真，看图', phase='partial', mode='full'),
                            message(text='新岛真，看图', phase='final', mode='full'))
            call = self.llm.process_and_output.await_args
            self.assertEqual(call.kwargs['forced_role'], None if exiting else self.loader.roles['助理'])
            self.assertIsNone(call.kwargs['forced_content'])
            self.app.ws.send.assert_not_called()

    async def test_disabled_llm_uses_pure_stream_and_paste_modes(self):
        for apps, expected in (([], False), (['CODE.exe'], True), (['Other.exe'], False)):
            self.make(llm_enabled=False)
            self.config.paste_apps = apps
            self.begin()
            await self.feed(message(delta='助理，你好', text='助理，你好'),
                            message(delta='正文', text='助理，你好正文', phase='final'))
            self.app.ws.send.assert_not_called()
            self.assertEqual(self.typed, ['助理，你好', '正文'])
            for call in self.app.output.output.await_args_list:
                self.assertEqual(call.kwargs['paste'], expected)

    async def test_full_result_keeps_foreground_paste_choice(self):
        self.make()
        self.config.paste_apps = ['CODE.exe']
        self.begin(streaming=False)
        await self.feed(message(text='助理，你好', phase='final', mode='full'))
        self.assertTrue(self.llm.process_and_output.await_args.kwargs['paste'])

    async def test_duplicate_terminal_and_late_stream_have_no_effect(self):
        self.make()
        self.begin()
        msg = message(text='好的。', delta='好的。', phase='final')
        await self.feed(msg, msg, message(delta='迟到', text='迟到'))
        self.assertEqual(self.typed, ['好的'])
        self.p._after_output.assert_called_once()

    async def test_cancel_and_error_rollback_only_old_task(self):
        for status in ('error', 'cancelled'):
            self.make()
            self.begin(image=True)
            old_image = self.image
            new, snapshot = self.begin('new', image=True)
            new_image = self.image
            await self.feed(message(phase='final', mode='full', status=status),
                            message(phase='final', mode='full', text='迟到成功'))
            self.assertEqual(self.state.pending_task_id, 'new')
            self.assertIs(self.state.image_snapshots['new'], new_image)
            self.assertIs(self.state.continuous_current, snapshot)
            self.assertIs(self.state.pending_image, old_image)
            self.assertIn('new', self.p._streams)
            self.assertEqual(self.typed, [])
            self.llm.process_and_output.assert_not_called()
            self.p._after_output.assert_not_called()
            self.assertEqual(len(self.task_lines()), 1)

    async def test_local_cancel_wins_over_server_success_or_error(self):
        for status in ('ok', 'error'):
            self.make()
            self.begin(image=True)
            self.state.mark_task_cancelled('t')
            await self.feed(message(text='助理，正文' if status == 'ok' else '',
                                    phase='final', mode='full', status=status))
            self.assertEqual(self.typed, [])
            self.llm.process_and_output.assert_not_called()
            self.assertIs(self.state.pending_image, self.image)
            self.assertEqual(len(self.task_lines()), 1)

    async def test_decision_send_failure_does_not_type_or_call_role(self):
        self.make()
        self.begin()
        self.app.ws.send.side_effect = None
        self.app.ws.send.return_value = False
        await self.feed(message(delta='普通听写', text='普通听写'))
        self.assertEqual(self.typed, [])
        self.assertFalse(self.p._streams)
        self.assertEqual(len(self.task_lines()), 1)

    async def test_disconnect_drains_all_live_tasks_of_that_connection_once(self):
        self.make()
        self.begin()
        self.begin('other')
        self.state.mark_task_cancelled('other')
        self.connection, lost = object(), self.connection
        self.begin('later')                       # 重连后开始的录音不受旧连接断开影响
        self.p.connection_failed(lost, '连接已断开')
        self.p.connection_failed(lost, '连接已断开')
        await asyncio.gather(*list(self.p._failures))
        self.assertEqual(list(self.p._streams), ['later'])
        self.assertEqual(list(self.state.awaiting_results), ['later'])
        self.assertEqual(len(self.task_lines()), 1)
        self.assertEqual(len(self.task_lines('other')), 1)

    async def test_full_failure_never_falls_back_to_preview(self):
        self.make()
        self.begin(image=True)
        await self.feed(message(text='助理，预览', delta='助理，预览'),
                        message(phase='final', mode='full', status='error'),
                        message(phase='final', text='助理，迟到的旧结果'))
        self.assertEqual(self.typed, [])
        self.llm.process_and_output.assert_not_called()
        self.app.diary.write.assert_not_called()
        self.assertIs(self.state.pending_image, self.image)

    async def test_output_failure_cleans_only_its_task_and_returns_image(self):
        self.make(llm_enabled=False)
        self.begin(image=True)
        old_image = self.image
        _, snapshot = self.begin('new', image=True)
        self.state.audio_files['t'] = Path('retained.wav')
        self.app.output.output.side_effect = RuntimeError('clipboard busy')
        await self.feed(message(text='普通听写', phase='final', mode='full'))
        self.assertNotIn('t', self.state.audio_files)
        self.assertNotIn('t', self.p._streams)
        self.assertIs(self.state.pending_image, old_image)
        self.assertIs(self.state.continuous_current, snapshot)
        self.assertIn('new', self.p._streams)
        self.assertEqual(self.state.pending_task_id, 'new')

    async def test_partial_output_failure_does_not_abort_other_tasks(self):
        self.make()
        self.begin()
        self.begin('new')
        self.app.output.output.side_effect = RuntimeError('clipboard busy')
        await self.feed(message(text='普通听写', delta='普通听写'))
        self.assertNotIn('t', self.p._streams)
        self.assertIn('new', self.p._streams)
        self.assertEqual(self.state.pending_task_id, 'new')


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

    def test_old_recording_commands_cannot_change_new_recording(self):
        self.handle('recording', dict(start_time=2.0, has_image=True, streaming=True))
        self.handle('stream_note', dict(start_time=2.0, note='新角色'))
        self.handle('continuous', dict(start_time=1.0, mode='exited', role='旧角色'))
        self.handle('error', dict(start_time=1.0, text='旧任务出错'))
        self.handle('hide', dict(start_time=1.0))
        self.assertEqual(self.indicator.state.mode, 'recording')
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
