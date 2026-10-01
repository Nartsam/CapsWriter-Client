# coding: utf-8
"""快捷键钩子：打断键按住连发、远控软件在录音键按住期间注入的修饰键同步。

ShortcutManager 的键盘过滤函数、事件处理器和 ShortcutTask.abort 按源码抽出来绑到桩对象上运行，
按键序列取自 tools/probe_keys.py 在 RayLink 下实录的日志。
"""
import ast
import logging
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import Mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
DOWN, UP = 0x100, 0x101
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

    VK = {0xA2: 'ctrl_l', 0xA3: 'ctrl_r', 0x1B: 'esc', 0x43: 'c'}

    def setUp(self, hold_mode=True):
        ns = dict(KEYBOARD_MESSAGES=(DOWN, UP), KEY_DOWN_MESSAGES=(DOWN,), KEY_UP_MESSAGES=(UP,),
                  KeyMapper=types.SimpleNamespace(vk_to_name=self.VK.get), MODIFIER_FAMILY=MODIFIER_FAMILY,
                  MODIFIER_KEYS=set(MODIFIER_FAMILY), logger=logging.getLogger('shortcut-test'), time=time)
        make_filter = class_method(MANAGER, 'ShortcutManager', 'create_keyboard_filter', ns)
        is_sync = class_method(MANAGER, 'ShortcutManager', '_is_modifier_sync', ns)
        cancel = class_method(MANAGER, 'ShortcutManager', '_handle_cancel_key', ns)
        keydown = class_method(HANDLER, 'ShortcutEventHandler', 'handle_keydown', ns)
        keyup = class_method(HANDLER, 'ShortcutEventHandler', 'handle_keyup', ns)
        release = class_method(HANDLER, 'ShortcutEventHandler', '_release_suppressed_modifier', ns)
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
            self.state.recording, self.state.pending_task_id = False, None
        task.launch = Mock(side_effect=launch)
        task._finish_abort = finish_abort
        task.abort = lambda: abort(task)

        handler = types.SimpleNamespace(pool=Mock(), emulator=Mock())
        handler.handle_keydown = lambda key, t: keydown(handler, key, t)
        handler.handle_keyup = lambda key, t: keyup(handler, key, t)
        handler._release_suppressed_modifier = lambda key, t: release(handler, key, t)

        self.listener = Mock()
        mgr = self.mgr = types.SimpleNamespace(
            state=self.state, tasks={'ctrl_r': task}, keyboard_listener=self.listener, _event_handler=handler,
            _cancel_key='esc', _chat_key='', _cancel_suppressed=False, _cancel_key_down=False,
            _check_emulating=lambda *a, **k: False, _check_restoring=lambda *a: False,
            _abort_pending_result=Mock())
        mgr._is_modifier_sync = lambda key: is_sync(mgr, key)
        mgr._handle_cancel_key = lambda msg: cancel(mgr, msg)
        self.filter = make_filter(mgr)

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


if __name__ == '__main__':
    unittest.main()
