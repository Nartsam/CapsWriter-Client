# coding: utf-8
"""快捷键钩子：前台进程让键、打断键连发、远控软件注入的修饰键同步。

ShortcutManager 的键盘过滤函数、事件处理器和 ShortcutTask.abort 按源码抽出来绑到桩对象上运行，
按键序列取自 tools/probe_keys.py 在 RayLink 下实录的日志。
"""
import ast
import ctypes
import importlib.util
import logging
from pathlib import Path
import sys
import time
import types
import unittest
from typing import Tuple
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
DOWN, UP = 0x100, 0x101
MOUSE_DOWN, MOUSE_UP = 0x20B, 0x20C
MANAGER = 'core/client/shortcut/shortcut_manager.py'
HANDLER = 'core/client/shortcut/event_handler.py'


def class_method(path, class_name, method, ns):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method)
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), ns)
    return ns[method]


def module_constant(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == name)
    return ast.literal_eval(node.value)


MODIFIER_FAMILY = module_constant('core/client/shortcut/key_mapper.py', 'MODIFIER_FAMILY')


class CancelKeyRepeatTests(unittest.TestCase):
    """打断键按住不放的连发：沿用第一次按下的决策，不能在打断后被放行给前台程序"""

    def setUp(self):
        ns = dict(KEY_DOWN_MESSAGES=(DOWN,), KEY_UP_MESSAGES=(UP,))
        handle = class_method(MANAGER, 'ShortcutManager', '_handle_cancel_key', ns)
        self.state = types.SimpleNamespace(pending_task_id='t', recording=True)
        self.task = Mock(is_recording=True)

        def abort():
            # 与真实 abort 的效果一致：录音停止、待处理登记被清掉
            self.task.is_recording = False
            self.state.pending_task_id = None
            self.state.recording = False
        self.task.abort.side_effect = abort
        self.mgr = types.SimpleNamespace(state=self.state, tasks={'ctrl_r': self.task}, _cancel_suppressed=False,
                                         _cancel_key_down=False, _abort_pending_result=Mock())
        self.handle = lambda msg: handle(self.mgr, msg)

    def test_repeat_after_abort_stays_suppressed(self):
        self.assertTrue(self.handle(DOWN))       # 打断，拦截
        self.assertTrue(self.handle(DOWN))       # 连发：此时已没有待处理任务，仍要拦截
        self.assertTrue(self.handle(DOWN))
        self.assertTrue(self.handle(UP))
        self.assertEqual(self.task.abort.call_count, 1)
        self.assertFalse(self.handle(DOWN))      # 之后的普通按键照常放行
        self.assertFalse(self.handle(UP))

    def test_idle_press_passes_through(self):
        self.state.pending_task_id = None
        self.assertFalse(self.handle(DOWN))
        self.assertFalse(self.handle(DOWN))
        self.assertFalse(self.handle(UP))
        self.task.abort.assert_not_called()


class RemoteModifierSyncTests(unittest.TestCase):
    """按住右 Ctrl 录音时 RayLink 注入的左 Ctrl 按下要被拦截，否则左 Ctrl 卡住、单击变成 Ctrl+单击"""

    VK = {0xA2: 'ctrl_l', 0xA3: 'ctrl_r', 0x1B: 'esc', 0x43: 'c', 0x7B: 'f12'}

    def setUp(self, hold_mode=True):
        self.foreground = Mock(return_value='notepad.exe')
        ns = dict(KEYBOARD_MESSAGES=(DOWN, UP, 0x104, 0x105), KEY_DOWN_MESSAGES=(DOWN, 0x104),
                  KEY_UP_MESSAGES=(UP, 0x105), MOUSE_MESSAGES=(MOUSE_DOWN, MOUSE_UP),
                  WM_XBUTTONDOWN=MOUSE_DOWN, WM_XBUTTONUP=MOUSE_UP, XBUTTON1=1,
                  KeyMapper=types.SimpleNamespace(vk_to_name=self.VK.get), MODIFIER_FAMILY=MODIFIER_FAMILY,
                  MODIFIER_KEYS=set(MODIFIER_FAMILY), logger=logging.getLogger('shortcut-test'), time=time,
                  Tuple=Tuple, get_foreground_process_name=self.foreground)
        make_filter = class_method(MANAGER, 'ShortcutManager', 'create_keyboard_filter', ns)
        make_mouse_filter = class_method(MANAGER, 'ShortcutManager', 'create_mouse_filter', ns)
        mouse_up = class_method(MANAGER, 'ShortcutManager', '_handle_mouse_keyup', ns)
        route = class_method(MANAGER, 'ShortcutManager', '_route_shortcut', ns)
        is_sync = class_method(MANAGER, 'ShortcutManager', '_is_modifier_sync', ns)
        cancel = class_method(MANAGER, 'ShortcutManager', '_handle_cancel_key', ns)
        keydown = class_method(HANDLER, 'ShortcutEventHandler', 'handle_keydown', ns)
        keyup = class_method(HANDLER, 'ShortcutEventHandler', 'handle_keyup', ns)
        release = class_method(HANDLER, 'ShortcutEventHandler', '_release_suppressed_modifier', ns)
        short_press = class_method(HANDLER, 'ShortcutEventHandler', '_handle_short_press', ns)
        manage_task = class_method(HANDLER, 'ShortcutEventHandler', '_manage_task', ns)
        abort = class_method('core/client/shortcut/task.py', 'ShortcutTask', 'abort', ns)

        self.state = types.SimpleNamespace(pending_task_id=None, recording=False)
        task = self.task = types.SimpleNamespace(
            shortcut=types.SimpleNamespace(key='ctrl_r', hold_mode=hold_mode, suppress=True),
            is_recording=False, wait_release=False, pressed=False, released=True, event=Mock(), pool=None,
            recording_start_time=0.0, threshold=0.0, finish=Mock())

        def launch():
            task.is_recording = True
            task.recording_start_time = time.time()
            self.state.recording, self.state.pending_task_id = True, 't'

        def finish_abort():
            task.is_recording = False
            self.state.recording, self.state.pending_task_id = False, None
        task.launch = Mock(side_effect=launch)
        task.finish.side_effect = finish_abort
        task.cancel = Mock(side_effect=lambda: setattr(task, 'is_recording', False))
        task._finish_abort = finish_abort
        task.abort = lambda: abort(task)

        handler = types.SimpleNamespace(pool=Mock(), emulator=Mock())
        handler.handle_keydown = lambda key, t: keydown(handler, key, t)
        handler.handle_keyup = lambda key, t: keyup(handler, key, t)
        handler._release_suppressed_modifier = lambda key, t: release(handler, key, t)
        handler._handle_short_press = lambda key, t: short_press(handler, key, t)
        handler._count_down = Mock()
        self.manage_task = handler._manage_task = lambda t: manage_task(handler, t)

        self.listener = Mock()
        mgr = self.mgr = types.SimpleNamespace(
            state=self.state, tasks={'ctrl_r': task}, keyboard_listener=self.listener, _event_handler=handler,
            _cancel_key='esc', _chat_key='', _cancel_suppressed=False, _cancel_key_down=False,
            _check_emulating=lambda *a, **k: False, _check_restoring=lambda *a: False,
            _abort_pending_result=Mock(), _passthrough_apps=set(), _shortcut_routes={},
            _pool=handler.pool, _emulator=handler.emulator, mouse_listener=Mock())
        mgr._route_shortcut = lambda key, down: route(mgr, key, down)
        mgr._handle_mouse_keyup = lambda key, t: mouse_up(mgr, key, t)
        mgr._is_modifier_sync = lambda key: is_sync(mgr, key)
        mgr._handle_cancel_key = lambda msg: cancel(mgr, msg)
        self.filter = make_filter(mgr)
        self.mouse_filter = make_mouse_filter(mgr)

    def key(self, msg, vk):
        """送一个事件进过滤函数，返回它是否被拦截"""
        self.listener.suppress_event.reset_mock()
        self.assertTrue(self.filter(msg, types.SimpleNamespace(vkCode=vk)))
        return self.listener.suppress_event.called

    def test_raylink_abort_sequence(self):
        # 取自 logs/probe_keys.log：右 Ctrl 按下（连发）→ RayLink 补左 Ctrl 按下 → Esc → 松开右 Ctrl
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(DOWN, 0xA2))      # 同步注入的左 Ctrl：拦截
        self.assertTrue(self.key(DOWN, 0x1B))      # Esc 打断
        self.assertFalse(self.task.is_recording)
        self.assertTrue(self.key(DOWN, 0xA2))      # 打断后右 Ctrl 仍按着，再来的同步照样拦截
        self.assertTrue(self.key(UP, 0x1B))
        self.assertFalse(self.key(UP, 0xA2))       # 松开放行：多发无害
        self.assertTrue(self.key(UP, 0xA3))
        self.mgr._event_handler.pool.submit.assert_called_once()  # 补发右 Ctrl 释放
        self.assertEqual(self.task.launch.call_count, 1)

        self.assertFalse(self.key(DOWN, 0xA2))     # 录音键松开后，左 Ctrl 恢复正常
        self.assertFalse(self.key(DOWN, 0x43))
        self.assertFalse(self.key(UP, 0x43))
        self.assertFalse(self.key(UP, 0xA2))

    def test_finish_releases_guard(self):
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(DOWN, 0xA2))
        self.assertTrue(self.key(UP, 0xA3))        # 正常松手结束录音
        self.task.finish.assert_called_once()
        self.task.is_recording = False             # finish 是桩，补上它的效果
        self.assertFalse(self.key(DOWN, 0xA2))

    def test_click_mode_keeps_left_ctrl(self):
        # 单击模式录音时键并没有按着，用户此时按左 Ctrl 是真要用它
        self.setUp(hold_mode=False)
        self.task.is_recording = True
        self.assertFalse(self.key(DOWN, 0xA2))
        self.assertFalse(self.key(UP, 0xA2))


class PassthroughTests(RemoteModifierSyncTests):
    """复用真实过滤器和事件处理器，同时重跑已存在的 RayLink 回归序列。"""

    def setUp(self, hold_mode=True):
        super().setUp(hold_mode)
        self.mgr._passthrough_apps = {'gameviewer.exe'}

    def test_remote_press_keeps_route_across_focus_change(self):
        self.foreground.return_value = 'GameViewer.EXE'
        self.assertFalse(self.key(DOWN, 0xA3))
        self.foreground.return_value = 'notepad.exe'
        self.assertFalse(self.key(DOWN, 0xA3))
        self.assertFalse(self.key(UP, 0xA3))
        self.task.launch.assert_not_called()
        self.task.cancel.assert_not_called()
        self.mgr._pool.submit.assert_not_called()  # 连短按/修饰键补发也不应排入队列
        self.foreground.assert_called_once()
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(UP, 0xA3))
        self.task.launch.assert_called_once()

    def test_local_press_finishes_after_focus_moves_to_remote(self):
        self.assertTrue(self.key(DOWN, 0xA3))
        self.foreground.return_value = 'gameviewer.exe'
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(UP, 0xA3))
        self.task.finish.assert_called_once()
        self.foreground.assert_called_once()
        self.assertFalse(self.key(DOWN, 0xA3))
        self.assertFalse(self.key(UP, 0xA3))

    def test_failed_query_passes_entire_press(self):
        self.foreground.return_value = ''
        self.assertFalse(self.key(DOWN, 0xA3))
        self.foreground.return_value = 'notepad.exe'
        self.assertFalse(self.key(DOWN, 0xA3))
        self.assertFalse(self.key(UP, 0xA3))
        self.task.launch.assert_not_called()
        self.mgr._pool.submit.assert_not_called()
        self.foreground.assert_called_once()

    def test_empty_list_uses_old_behavior_without_queries(self):
        self.mgr._passthrough_apps = set()
        self.foreground.return_value = ''
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(UP, 0xA3))
        self.task.finish.assert_called_once()
        self.foreground.assert_not_called()

    def test_unrelated_keys_do_not_query_foreground(self):
        self.assertFalse(self.key(DOWN, 0x43))
        self.assertFalse(self.key(UP, 0x43))
        self.foreground.assert_not_called()

    def test_process_name_requires_exact_match(self):
        self.foreground.return_value = 'other-gameviewer.exe'
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(UP, 0xA3))

    def test_syskey_and_other_configured_key(self):
        self.task.shortcut.key = 'f12'
        self.mgr.tasks = {'f12': self.task}
        self.foreground.return_value = 'gameviewer.exe'
        self.assertFalse(self.key(0x104, 0x7B))
        self.assertFalse(self.key(0x105, 0x7B))
        self.task.launch.assert_not_called()

    def test_local_short_press_still_restores_key(self):
        self.task.threshold = 100
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(UP, 0xA3))
        self.task.cancel.assert_called_once()
        self.mgr._pool.submit.assert_called_once_with(self.mgr._emulator.emulate_key, 'ctrl_r')

    def test_cancelled_local_press_does_not_change_route(self):
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(DOWN, 0x1B))
        self.foreground.return_value = 'gameviewer.exe'
        self.assertTrue(self.key(DOWN, 0xA3))
        self.assertTrue(self.key(UP, 0xA3))
        self.task.launch.assert_called_once()
        self.foreground.assert_called_once()

    def test_passthrough_repeat_is_not_modifier_sync(self):
        self.foreground.return_value = 'gameviewer.exe'
        self.assertFalse(self.key(DOWN, 0xA3))
        # 另一条本机录音随后启动，同族拦截不能改变已经放行的右 Ctrl。
        self.mgr.tasks['ctrl_l'] = types.SimpleNamespace(
            shortcut=types.SimpleNamespace(suppress=True, hold_mode=True), is_recording=True)
        self.assertFalse(self.key(DOWN, 0xA3))
        self.assertFalse(self.key(UP, 0xA3))
        self.task.launch.assert_not_called()

    def test_unpaired_keyup_passes_without_synthetic_release(self):
        self.assertFalse(self.key(UP, 0xA3))
        self.foreground.assert_not_called()
        self.mgr._pool.submit.assert_not_called()

    def test_second_configured_modifier_can_pass_during_local_recording(self):
        self.assertTrue(self.key(DOWN, 0xA3))
        other = types.SimpleNamespace(
            shortcut=types.SimpleNamespace(suppress=True, hold_mode=True),
            is_recording=False, wait_release=False, launch=Mock())
        self.mgr.tasks['ctrl_l'] = other
        self.foreground.return_value = 'gameviewer.exe'
        self.assertFalse(self.key(DOWN, 0xA2))
        self.foreground.return_value = 'notepad.exe'
        self.assertFalse(self.key(DOWN, 0xA2))
        self.assertFalse(self.key(UP, 0xA2))
        other.launch.assert_not_called()
        self.assertTrue(self.key(UP, 0xA3))
        self.task.finish.assert_called_once()

    def test_synthetic_key_does_not_consume_physical_route(self):
        self.foreground.return_value = 'gameviewer.exe'
        self.assertFalse(self.key(DOWN, 0xA3))
        self.mgr._check_emulating = lambda *a, **k: True
        self.assertFalse(self.key(UP, 0xA3))
        self.mgr._check_emulating = lambda *a, **k: False
        self.foreground.return_value = 'notepad.exe'
        self.assertFalse(self.key(DOWN, 0xA3))
        self.assertFalse(self.key(UP, 0xA3))
        self.foreground.assert_called_once()

    def test_click_recording_stop_takes_priority(self):
        for suppress in (True, False):
            with self.subTest(suppress=suppress):
                self.setUp(hold_mode=False)
                self.task.shortcut.suppress = suppress
                self.task.threshold = 1
                # 第一击在本机开始录音。工作项在按键序列后手动运行，避免睡眠等待线程。
                self.assertEqual(self.key(DOWN, 0xA3), suppress)
                self.assertEqual(self.key(UP, 0xA3), suppress)
                self.manage_task(self.task)
                self.assertTrue(self.task.is_recording)
                self.mgr._pool.reset_mock()
                self.foreground.reset_mock()
                self.foreground.return_value = 'gameviewer.exe'
                self.assertTrue(self.key(DOWN, 0xA3))
                self.assertTrue(self.key(DOWN, 0xA3))
                self.assertTrue(self.key(UP, 0xA3))
                self.manage_task(self.task)
                self.task.finish.assert_called_once()
                self.assertFalse(self.task.is_recording)
                self.foreground.assert_not_called()
                self.assertFalse(self.key(DOWN, 0xA3))
                self.assertFalse(self.key(UP, 0xA3))
                self.task.launch.assert_called_once()

    def mouse_key(self, msg):
        listener = self.mgr.mouse_listener
        listener.suppress_event.reset_mock()
        self.assertTrue(self.mouse_filter(msg, types.SimpleNamespace(mouseData=2 << 16)))
        return listener.suppress_event.called

    def test_mouse_press_preserves_route_both_directions(self):
        self.mgr.tasks = {'x2': self.task}
        self.task.shortcut.key = 'x2'
        self.foreground.return_value = 'gameviewer.exe'
        self.assertFalse(self.mouse_key(MOUSE_DOWN))
        self.foreground.return_value = 'notepad.exe'
        self.assertFalse(self.mouse_key(MOUSE_UP))
        self.task.launch.assert_not_called()
        self.mgr._pool.submit.assert_not_called()
        self.assertTrue(self.mouse_key(MOUSE_DOWN))
        self.foreground.return_value = 'gameviewer.exe'
        self.assertTrue(self.mouse_key(MOUSE_UP))
        self.task.finish.assert_called_once()

    def test_mouse_click_recording_can_stop_in_remote_window(self):
        self.setUp(hold_mode=False)
        self.mgr.tasks = {'x2': self.task}
        self.task.shortcut.key = 'x2'
        self.task.is_recording = True
        self.task.threshold = 1
        self.foreground.return_value = 'gameviewer.exe'
        self.assertTrue(self.mouse_key(MOUSE_DOWN))
        self.assertTrue(self.mouse_key(MOUSE_UP))
        self.manage_task(self.task)
        self.task.finish.assert_called_once()
        self.foreground.assert_not_called()
        self.assertFalse(self.mouse_key(MOUSE_DOWN))
        self.assertFalse(self.mouse_key(MOUSE_UP))


class PassthroughConfigTests(unittest.TestCase):
    def resolve(self, **attrs):
        config = types.ModuleType('config_client')
        config.ClientConfig = types.SimpleNamespace(**attrs)
        ns = dict(logger=Mock())
        resolve = class_method(MANAGER, 'ShortcutManager', '_resolve_passthrough_apps', ns)
        with patch.dict(sys.modules, config_client=config):
            return resolve(None)

    def test_missing_or_empty_config_disables_feature(self):
        self.assertEqual(self.resolve(), set())
        self.assertEqual(self.resolve(shortcut_passthrough_apps=[]), set())

    def test_names_are_normalized_and_deduplicated(self):
        self.assertEqual(self.resolve(shortcut_passthrough_apps=[
            ' GameViewer.EXE ', 'gameviewer.exe', '', 'Other.exe']), {'gameviewer.exe', 'other.exe'})

    def test_invalid_config_does_not_become_character_names(self):
        for value in ('GameViewer.exe', None, ['GameViewer.exe', 42]):
            self.assertEqual(self.resolve(shortcut_passthrough_apps=value), set())


class ForegroundProcessTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('window_detector_test', ROOT / 'core/tools/window_detector.py')
        self.detector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.detector)
        self.user32 = types.SimpleNamespace(GetForegroundWindow=Mock(return_value=100),
                                           GetWindowThreadProcessId=Mock(side_effect=self.set_pid))
        self.win_dll = patch.object(ctypes, 'WinDLL', return_value=self.user32, create=True)
        self.win_dll.start()
        self.addCleanup(self.win_dll.stop)
        self.detector._get_process_name = Mock(return_value='GameViewer.exe')

    @staticmethod
    def set_pid(hwnd, pid):
        pid._obj.value = 123
        return 456

    def test_reads_process_without_window_text(self):
        self.assertEqual(self.detector.get_foreground_process_name(), 'GameViewer.exe')
        self.detector._get_process_name.assert_called_once_with(123)

    def test_no_foreground_is_unknown(self):
        self.user32.GetForegroundWindow.return_value = 0
        self.assertEqual(self.detector.get_foreground_process_name(), '')
        self.detector._get_process_name.assert_not_called()

    def test_invalid_window_is_unknown(self):
        self.user32.GetWindowThreadProcessId.side_effect = None
        self.user32.GetWindowThreadProcessId.return_value = 0
        self.assertEqual(self.detector.get_foreground_process_name(), '')
        self.detector._get_process_name.assert_not_called()

    def test_process_lookup_failure_is_unknown(self):
        for result in ('', OSError('process exited')):
            self.detector._get_process_name.side_effect = result if isinstance(result, Exception) else None
            self.detector._get_process_name.return_value = result
            self.assertEqual(self.detector.get_foreground_process_name(), '')

    def test_focus_change_during_lookup_is_unknown(self):
        self.user32.GetForegroundWindow.side_effect = [100, 200]
        self.assertEqual(self.detector.get_foreground_process_name(), '')


if __name__ == '__main__':
    unittest.main()
