# coding: utf-8
"""选区读取的安全边界、超时与请求/控制台接线。键盘、剪贴板、网络均为桩。"""
from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, field
import importlib.util
import inspect
import itertools
import logging
from pathlib import Path
import sys
import threading
import time
import types
from typing import List, Optional, Tuple
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for name in ('core', 'core.tools', 'core.client', 'core.client.llm',
             'core.client.output', 'core.client.clipboard', 'core.ui'):
    module = types.ModuleType(name)
    module.__path__ = []
    module.logger = logging.getLogger('selection-test')
    sys.modules[name] = module

config_module = types.ModuleType('config_client')
config_module.ClientConfig = types.SimpleNamespace(selection_mode='uia',
                                                  restore_clip=True, trash_punc='。')
sys.modules['config_client'] = config_module
keyboard = types.ModuleType('keyboard')
keyboard.press_and_release = Mock()
keyboard.write = Mock()
sys.modules['keyboard'] = keyboard
clipboard = types.ModuleType('core.client.llm.llm_clipboard')
clipboard.try_paste = Mock(return_value='旧剪贴板')
clipboard.safe_paste = Mock(return_value='新选区')
clipboard.restore_clipboard = Mock(return_value=True)
sys.modules[clipboard.__name__] = clipboard

native = load('core.tools.windows_selection', 'core/tools/windows_selection.py')
selection = load('core.client.llm.llm_get_selection', 'core/client/llm/llm_get_selection.py')
roles = load('core.client.llm.llm_role_config', 'core/client/llm/llm_role_config.py')
thread_tool = load('core.tools.asyncio_to_thread', 'core/tools/asyncio_to_thread.py')
RoleConfig = roles.RoleConfig
TARGET = (123, 456)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.role = RoleConfig(name='助理', enable_read_selection=True, enable_history=True)
        self.state = types.SimpleNamespace(last_output_text='')
        selection.clear_selection_usage()
        config_module.ClientConfig.selection_mode = 'uia'
        keyboard.press_and_release.reset_mock()
        for mock in (clipboard.try_paste, clipboard.safe_paste, clipboard.restore_clipboard):
            mock.reset_mock()
            mock.side_effect = None
        clipboard.try_paste.return_value = '旧剪贴板'
        clipboard.safe_paste.return_value = '新选区'
        self.target_patch = patch.object(selection, 'get_input_target', return_value=TARGET)
        self.target_patch.start()
        self.sleep_patch = patch.object(selection, 'time', types.SimpleNamespace(sleep=lambda _: None))
        self.sleep_patch.start()

    def tearDown(self):
        self.target_patch.stop()
        self.sleep_patch.stop()

    def capture(self, status='selected', text='选中文字'):
        with patch.object(selection, 'read_selection', return_value=native.SelectionRead(status, text, TARGET)):
            return selection.capture_selection(self.role, self.state)

    def test_default_success_never_touches_keyboard_or_clipboard(self):
        result = self.capture(text='旧剪贴板')
        self.assertEqual(result.text, '旧剪贴板')
        self.assertEqual(result.preview, result.text)
        keyboard.press_and_release.assert_not_called()
        clipboard.try_paste.assert_not_called()
        clipboard.safe_paste.assert_not_called()
        clipboard.restore_clipboard.assert_not_called()

    def test_default_failure_never_copies(self):
        for status in ('unsupported', 'error', 'timeout', 'empty', 'changed', 'blocked'):
            with self.subTest(status=status):
                self.assertEqual(self.capture(status, '').text, '')
        keyboard.press_and_release.assert_not_called()
        clipboard.try_paste.assert_not_called()

    def test_fallback_copies_when_reader_returns_no_text(self):
        config_module.ClientConfig.selection_mode = 'uia_then_ctrl_c'
        for status in ('unsupported', 'error', 'timeout', 'empty'):
            with self.subTest(status=status):
                keyboard.press_and_release.reset_mock()
                self.assertEqual(self.capture(status, '').text, '新选区')
                keyboard.press_and_release.assert_called_once_with('ctrl+c')
                clipboard.restore_clipboard.assert_called_with('旧剪贴板')

    def test_changed_and_blocked_do_not_fallback_even_when_enabled(self):
        config_module.ClientConfig.selection_mode = 'uia_then_ctrl_c'
        for status in ('changed', 'blocked'):
            self.assertEqual(self.capture(status, '').text, '')
        keyboard.press_and_release.assert_not_called()
        clipboard.try_paste.assert_not_called()

    def test_role_switch_still_disables_all_selection_access(self):
        self.role.enable_read_selection = False
        with patch.object(selection, 'read_selection') as read:
            for mode in ('uia', 'uia_then_ctrl_c', 'ctrl_c'):
                config_module.ClientConfig.selection_mode = mode
                self.assertEqual(selection.capture_selection(self.role, self.state).preview, '')
            read.assert_not_called()
        keyboard.press_and_release.assert_not_called()

    def test_fallback_keeps_successful_read_without_copying(self):
        config_module.ClientConfig.selection_mode = 'uia_then_ctrl_c'
        self.assertEqual(self.capture().text, '选中文字')
        keyboard.press_and_release.assert_not_called()
        clipboard.try_paste.assert_not_called()

    def test_fallback_copies_for_whitespace_result(self):
        config_module.ClientConfig.selection_mode = 'uia_then_ctrl_c'
        self.assertEqual(self.capture('empty', ' \r\n\t').text, '新选区')
        keyboard.press_and_release.assert_called_once_with('ctrl+c')

    def test_direct_copy_bypasses_reader_and_keeps_limit_preview_and_dedup(self):
        config_module.ClientConfig.selection_mode = 'ctrl_c'
        self.role.selection_max_length = 2
        with patch.object(selection, 'read_selection') as read:
            first = selection.capture_selection(self.role, self.state)
            self.assertEqual(first.text, '新选')
            self.assertEqual(first.preview, first.text)
            keyboard.press_and_release.assert_called_once_with('ctrl+c')
            clipboard.restore_clipboard.assert_called_once_with('旧剪贴板')
            selection.record_selection_usage(self.role, first.text)
            repeated = selection.capture_selection(self.role, self.state)
            self.assertEqual(repeated.text, '')
            self.assertEqual(repeated.preview, first.preview)
            self.assertTrue(repeated.note)
            read.assert_not_called()

    def test_direct_copy_skips_when_target_changes_before_copy(self):
        config_module.ClientConfig.selection_mode = 'ctrl_c'
        with patch.object(selection, 'read_selection') as read, \
                patch.object(selection, 'get_input_target', side_effect=[TARGET, (999, 456)]):
            self.assertEqual(selection.capture_selection(self.role, self.state).text, '')
            read.assert_not_called()
        keyboard.press_and_release.assert_not_called()
        clipboard.try_paste.assert_not_called()

    def test_nonpositive_limit_skips_all_modes(self):
        with patch.object(selection, 'read_selection') as read:
            for mode in ('uia', 'uia_then_ctrl_c', 'ctrl_c'):
                config_module.ClientConfig.selection_mode = mode
                for limit in (0, -1):
                    self.role.selection_max_length = limit
                    self.assertEqual(selection.capture_selection(self.role, self.state).text, '')
            read.assert_not_called()
        keyboard.press_and_release.assert_not_called()

    def test_legacy_fallback_rechecks_focus_after_clipboard_read(self):
        with patch.object(selection, 'get_input_target', side_effect=[TARGET, (123, 789)]):
            self.assertEqual(selection._read_by_copy(TARGET), '')
        keyboard.press_and_release.assert_not_called()
        clipboard.restore_clipboard.assert_not_called()

    def test_legacy_fallback_refuses_missing_or_changed_target(self):
        for target in ((0, 0), (999, 456), None):
            self.assertEqual(selection._read_by_copy(target), '')
        keyboard.press_and_release.assert_not_called()
        clipboard.try_paste.assert_not_called()

    def test_legacy_fallback_preserves_nontext_clipboard(self):
        clipboard.try_paste.return_value = None
        clipboard.safe_paste.return_value = ''
        self.assertEqual(selection._read_by_copy(TARGET), '')
        clipboard.restore_clipboard.assert_not_called()

    def test_legacy_fallback_restores_even_if_read_fails(self):
        clipboard.safe_paste.side_effect = RuntimeError('clipboard busy')
        with self.assertRaises(RuntimeError):
            selection._read_by_copy(TARGET)
        clipboard.restore_clipboard.assert_called_once_with('旧剪贴板')

    def test_history_dedup_keeps_preview_and_notice(self):
        first = self.capture()
        selection.record_selection_usage(self.role, first.text)
        repeated = self.capture()
        self.assertEqual(repeated.text, '')
        self.assertEqual(repeated.preview, first.preview)
        self.assertTrue(repeated.note)
        selection.clear_selection_usage()
        self.assertEqual(self.capture().text, first.text)

    def test_role_histories_are_independent(self):
        selection.record_selection_usage(self.role, '选中文字')
        self.role.name = '另一角色'
        self.assertEqual(self.capture().text, '选中文字')

    def test_history_disabled_keeps_repeated_selection(self):
        selection.record_selection_usage(self.role, '选中文字')
        self.role.enable_history = False
        self.assertEqual(self.capture().text, '选中文字')

    def test_payload_retains_line_breaks_and_role_length_limit(self):
        self.role.selection_max_length = 8
        result = self.capture(text='第一行\n第二行\n第三行')
        self.assertEqual(result.text, '第一行\n第二行\n')
        preview = selection.format_selection_preview(result)
        self.assertIn('第一行 第二行', preview)        # 发给模型的保留换行，预览压成一行
        self.assertNotIn('\n', preview)

    def test_preview_is_single_line_bounded_and_has_no_control_characters(self):
        result = selection.SelectionContext(preview='[red]\n\t\x1b\x07' + '长' * 80, note='某条提示')
        text = selection.format_selection_preview(result)
        self.assertIn('[red]', text)                    # 方括号原样保留，交给 markup=False 处理
        self.assertIn(result.note, text)
        self.assertFalse(any(c in text for c in '\n\r\t\x1b\x07'))
        self.assertLess(text.count('长'), 80)           # 长选区被截断

    def test_whitespace_only_does_not_produce_preview(self):
        self.assertEqual(self.capture(text=' \r\n\t').preview, '')
        self.assertEqual(selection.format_selection_preview(selection.SelectionContext()), '')

    def test_last_output_filter_has_accurate_notice(self):
        # 「与上次输出相同」和「已在历史」是两种原因，提示不能混用（不比对措辞）
        selection.record_selection_usage(self.role, '选中文字')
        in_history = self.capture().note
        selection.clear_selection_usage()
        self.state.last_output_text = '选中文字'
        result = self.capture()
        self.assertEqual(result.text, '')
        self.assertTrue(result.note)
        self.assertNotEqual(result.note, in_history)

    def test_invalid_length_or_reader_error_does_not_abort_request(self):
        self.role.selection_max_length = 'bad'
        self.assertEqual(self.capture().text, '')
        self.role.selection_max_length = 1000
        with patch.object(selection, 'read_selection', side_effect=RuntimeError('provider failed')):
            self.assertEqual(selection.capture_selection(self.role, self.state).text, '')
        keyboard.press_and_release.assert_not_called()

    def test_missing_mode_uses_readonly_path(self):
        del config_module.ClientConfig.selection_mode
        try:
            self.assertEqual(self.capture('empty', '').text, '')
            self.assertEqual(self.capture().text, '选中文字')
            keyboard.press_and_release.assert_not_called()
            clipboard.try_paste.assert_not_called()
        finally:
            config_module.ClientConfig.selection_mode = 'uia'


class NativeWorkerTests(unittest.TestCase):
    def test_nonpositive_limit_never_reads_or_uses_unlimited_gettext(self):
        with patch.object(native, 'get_input_target') as target:
            for limit in (0, -1):
                self.assertEqual(native.read_selection(limit).status, 'empty')
            target.assert_not_called()

    def test_timeout_does_not_accumulate_workers_or_reuse_late_text(self):
        release = threading.Event()
        started = threading.Event()

        def slow(target, limit):
            started.set()
            release.wait(2)
            return native.SelectionRead('selected', '旧窗口的文字', target)

        with patch.object(native, 'get_input_target', return_value=TARGET), \
                patch.object(native, '_read_native', side_effect=slow) as read, \
                patch.object(native, 'READ_TIMEOUT_S', 0.02):
            try:
                self.assertEqual(native.read_selection(100).status, 'timeout')
                self.assertTrue(started.is_set())
                self.assertEqual(native.read_selection(100).status, 'timeout')
                read.assert_called_once()
            finally:
                release.set()
                deadline = time.monotonic() + 2
                while native._read_lock.locked() and time.monotonic() < deadline:
                    time.sleep(0.001)
            read.side_effect = lambda target, limit: native.SelectionRead('selected', '新文字', target)
            self.assertEqual(native.read_selection(100).text, '新文字')

    def test_focus_change_discards_completed_result(self):
        with patch.object(native, 'get_input_target', side_effect=[TARGET, (999, 456)]), \
                patch.object(native, '_read_native', return_value=native.SelectionRead('selected', '文字', TARGET)):
            result = native.read_selection(100)
        self.assertEqual(result.status, 'changed')
        self.assertEqual(result.text, '')

    def test_native_exception_is_reported_without_raising(self):
        with patch.object(native, 'get_input_target', return_value=TARGET), \
                patch.object(native, '_read_native', side_effect=RuntimeError('provider failure')):
            result = native.read_selection(100)
        self.assertEqual(result.status, 'error')
        self.assertIn('provider failure', result.detail)

    def test_destroyed_element_is_discarded_without_copy_fallback(self):
        with patch.object(native, 'get_input_target', return_value=TARGET), \
                patch.object(native, '_read_native', side_effect=native._UIAError(0x80040201)):
            result = native.read_selection(100)
        self.assertEqual(result.status, 'changed')
        self.assertEqual(result.text, '')

    def test_thread_start_failure_releases_gate(self):
        with patch.object(native, 'get_input_target', return_value=TARGET), \
                patch.object(native.threading.Thread, 'start', side_effect=RuntimeError('cannot start')):
            with self.assertRaises(RuntimeError):
                native.read_selection(100)
        self.assertFalse(native._read_lock.locked())


class NativeBoundaryTests(unittest.TestCase):
    """执行真实原生读取控制流；COM 对象为内存桩，输入/剪贴板不参与。"""
    def setUp(self):
        self.dll = Mock()
        self.dll.CoInitializeEx.return_value = 0
        self.dll.CoCreateInstance.return_value = 0
        self.focus = self.element()
        self.window = self.element()
        self.walker = self.element()
        self.walker.interface.return_value = self.window
        self.automation = self.element()
        self.automation.interface.side_effect = lambda slot, *args: {
            8: self.focus, 6: self.window, 16: self.walker}[slot]

    def element(self):
        element = MagicMock()
        element.pointer = element
        element.__enter__.return_value = element
        element.integer.return_value = 0
        return element

    def read(self):
        with patch.object(native.ctypes, 'WinDLL', return_value=self.dll, create=True), \
                patch.object(native, '_ComPointer', return_value=self.automation), \
                patch.object(native, '_same_element', side_effect=lambda _, a, b: a is b), \
                patch.object(native, 'get_input_target', return_value=TARGET), \
                patch.object(native, '_selected_text', return_value='选区') as text:
            result = native._read_native(TARGET, 100)
        self.dll.CoUninitialize.assert_called_once()
        self.automation.__exit__.assert_called_once()
        return result, text

    def test_password_ancestor_blocks_even_when_focus_has_textpattern(self):
        self.window.integer.return_value = 1
        result, text = self.read()
        self.assertEqual(result.status, 'blocked')
        text.assert_not_called()
        self.focus.interface.assert_not_called()

    def test_unknown_password_attribute_blocks_read_and_fallback(self):
        self.focus.integer.side_effect = native._UIAError(0x80004005)
        result, text = self.read()
        self.assertEqual(result.status, 'blocked')
        text.assert_not_called()

    def test_missing_walker_is_unsupported_and_releases_interfaces(self):
        self.walker = None
        result, text = self.read()
        self.assertEqual(result.status, 'unsupported')
        text.assert_not_called()
        self.focus.__exit__.assert_called_once()
        self.window.__exit__.assert_called_once()

    def test_window_at_parent_depth_limit_can_be_read(self):
        parents = [self.element() for _ in range(native.MAX_PARENT_DEPTH - 1)] + [self.window]
        self.walker.interface.side_effect = parents
        result, text = self.read()
        self.assertEqual(result.text, '选区')
        text.assert_called_once()

    def test_vscode_thirty_level_parent_chain_keeps_focused_selection(self):
        parents = [self.element() for _ in range(29)] + [self.window]
        self.walker.interface.side_effect = parents
        result, text = self.read()
        self.assertEqual(result.status, 'selected')
        self.assertEqual(result.text, '选区')
        text.assert_called_once()

    def test_deeper_parent_chain_is_bounded_without_reading_other_windows(self):
        self.walker.interface.side_effect = [self.element() for _ in range(native.MAX_PARENT_DEPTH)]
        result, text = self.read()
        self.assertEqual(result.status, 'unsupported')
        text.assert_not_called()
        self.assertLessEqual(self.walker.interface.call_count, native.MAX_PARENT_DEPTH)


class ScintillaTests(unittest.TestCase):
    def window_api(self, class_name='Scintilla'):
        dll = Mock()
        def get_class(hwnd, buffer, size):
            buffer.value = class_name
            return len(class_name)
        dll.GetClassNameW.side_effect = get_class
        dll.IsChild.return_value = True
        query = self.query('text', [(0, 4)])
        def send(hwnd, message, argument, pointer, flags, timeout, output):
            self.assertEqual(hwnd, TARGET[1])
            self.assertEqual(pointer, 0)
            self.assertTrue(flags & 0x2)             # SMTO_ABORTIFHUNG：目标窗口卡死时不能跟着卡
            self.assertTrue(0 < timeout <= 100)
            output._obj.value = query(message, argument)
            return 1
        dll.SendMessageTimeoutW.side_effect = send
        return dll

    def query(self, text, ranges, codepage=65001):
        data = text.encode('utf-8' if codepage == 65001 else f'cp{codepage}')
        self.positions = []

        def send(message, argument=0):
            if message == native.SCI_GETLENGTH:
                return len(data)
            if message == native.SCI_GETSELECTIONS:
                return len(ranges)
            if message == native.SCI_GETSELECTIONNSTART:
                return ranges[argument][0]
            if message == native.SCI_GETSELECTIONNEND:
                return ranges[argument][1]
            if message == native.SCI_GETCODEPAGE:
                return codepage
            if message == native.SCI_GETCHARAT:
                self.positions.append(argument)
                return data[argument]
            raise AssertionError(f'不是只读消息：{message}')
        return send

    def test_unicode_selection_reads_only_selected_bytes(self):
        query = self.query('before 中文🙂 after', [(7, 17)])
        result = native._scintilla_text(query, 1000, TARGET)
        self.assertEqual(result.text, '中文🙂')
        self.assertEqual(self.positions, list(range(7, 17)))

    def test_length_limit_never_splits_multibyte_character(self):
        query = self.query('中文🙂后文', [(0, 16)])
        result = native._scintilla_text(query, 3, TARGET)
        self.assertEqual(result.text, '中文🙂')
        self.assertEqual(self.positions, list(range(10)))

    def test_multiselection_skips_unselected_gaps_and_empty_carets(self):
        query = self.query('aa gap bb', [(7, 9), (3, 3), (0, 2)])
        self.assertEqual(native._scintilla_text(query, 100, TARGET).text, 'aa\nbb')
        self.assertEqual(self.positions, [0, 1, 7, 8])

    def test_multiselection_obeys_total_character_limit(self):
        query = self.query('aa gap bb', [(0, 2), (7, 9)])
        self.assertEqual(native._scintilla_text(query, 4, TARGET).text, 'aa\nb')

    def test_caret_does_not_read_document_text(self):
        query = self.query('unselected text', [(4, 4)])
        self.assertEqual(native._scintilla_text(query, 100, TARGET).status, 'empty')
        self.assertEqual(self.positions, [])

    def test_legacy_codepage_and_signed_bytes(self):
        query = self.query('中文', [(0, 4)], 936)
        def signed(message, argument=0):
            value = query(message, argument)
            return value - 256 if message == native.SCI_GETCHARAT and value > 127 else value
        self.assertEqual(native._scintilla_text(signed, 100, TARGET).text, '中文')

    def test_changed_selection_discards_text(self):
        ranges = [(0, 4)]
        query = self.query('text', ranges)
        def moving(message, argument=0):
            value = query(message, argument)
            if message == native.SCI_GETCHARAT:
                ranges[:] = [(0, 0)]
            return value
        self.assertEqual(native._scintilla_text(moving, 100, TARGET).status, 'changed')

    def test_invalid_ranges_and_too_many_ranges_do_not_read_text(self):
        for ranges in ([(-1, 2)], [(0, 999)], [(0, 1)] * 65):
            query = self.query('abc', ranges)
            with self.assertRaises(ValueError):
                native._scintilla_text(query, 100, TARGET)
            self.assertEqual(self.positions, [])

    def test_compatibility_path_only_runs_when_uia_is_unsupported(self):
        for status in ('selected', 'empty', 'blocked', 'changed'):
            result = native.SelectionRead(status, target=TARGET)
            with patch.object(native, '_read_native', return_value=result), \
                    patch.object(native, '_read_scintilla') as read:
                self.assertIs(native._read_compatible(TARGET, 100), result)
                read.assert_not_called()
        expected = native.SelectionRead('selected', 'text', TARGET)
        with patch.object(native, '_read_native', return_value=native.SelectionRead('unsupported')), \
                patch.object(native, '_read_scintilla', return_value=expected):
            self.assertIs(native._read_compatible(TARGET, 100), expected)

    def test_other_window_classes_never_receive_scintilla_messages(self):
        dll = self.window_api('Chrome_WidgetWin_1')
        with patch.object(native.ctypes, 'WinDLL', return_value=dll, create=True):
            self.assertIsNone(native._read_scintilla(TARGET, 100))
        dll.SendMessageTimeoutW.assert_not_called()

    def test_child_outside_original_window_never_receives_messages(self):
        dll = self.window_api()
        dll.IsChild.return_value = False
        with patch.object(native.ctypes, 'WinDLL', return_value=dll, create=True), \
                patch.object(native, 'get_input_target', return_value=TARGET):
            self.assertEqual(native._read_scintilla(TARGET, 100).status, 'changed')
        dll.SendMessageTimeoutW.assert_not_called()

    def test_focus_change_discards_readonly_message_result(self):
        dll = self.window_api()
        with patch.object(native.ctypes, 'WinDLL', return_value=dll, create=True), \
                patch.object(native, 'get_input_target', side_effect=[TARGET, (999, 888)]):
            result = native._read_scintilla(TARGET, 100)
        self.assertEqual(result.status, 'changed')
        self.assertEqual(result.text, '')

    def test_scalar_message_failure_is_bounded(self):
        dll = self.window_api()
        dll.SendMessageTimeoutW.side_effect = None
        dll.SendMessageTimeoutW.return_value = 0
        with patch.object(native.ctypes, 'WinDLL', return_value=dll, create=True), \
                patch.object(native, 'get_input_target', return_value=TARGET):
            with self.assertRaises(OSError):
                native._read_scintilla(TARGET, 100)
        dll.SendMessageTimeoutW.assert_called_once()

    def test_total_deadline_stops_before_sending_another_message(self):
        dll = self.window_api()
        with patch.object(native.ctypes, 'WinDLL', return_value=dll, create=True), \
                patch.object(native, 'get_input_target', return_value=TARGET), \
                patch.object(native.time, 'monotonic', side_effect=itertools.chain(
                    [0], itertools.repeat(native.SCINTILLA_TIMEOUT_S + 1))):
            with self.assertRaises(TimeoutError):
                native._read_scintilla(TARGET, 100)
        dll.SendMessageTimeoutW.assert_not_called()


def extract_methods(path, cls_name, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls_name)
    nodes = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.preview = selection.SelectionContext('选择内容', '选择内容')
        self.capture = Mock(return_value=self.preview)
        self.namespace = dict(Optional=Optional, Tuple=Tuple, RoleConfig=RoleConfig,
                              capture_selection=self.capture, to_thread=thread_tool.to_thread,
                              record_selection_usage=Mock(), clear_selection_usage=Mock(),
                              logger=logging.getLogger('selection-test'), broadcast_output_udp=Mock(),
                              __name__='core.client.llm.selection_handler_test', __package__='core.client.llm')
        source = ast.parse((ROOT / 'core/client/llm/llm_handler.py').read_text(encoding='utf-8'))
        result_cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'LLMResult')
        self.namespace.update(dataclass=dataclass, field=field, List=List)
        exec(compile(ast.Module(body=[result_cls], type_ignores=[]), '<result>', 'exec',
                     dont_inherit=True), self.namespace)
        extract_methods('core/client/llm/llm_handler.py', 'LLMHandler',
                        {'process', 'process_and_output', 'clear_history'}, self.namespace)
        self.role = RoleConfig(name='助理', enable_read_selection=True, enable_vision=True, output_mode='toast')
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(last_output_text='', set_output_text=Mock()),
                    companion=types.SimpleNamespace(has_connections=False, emit_user_message=AsyncMock()),
                    reminders=types.SimpleNamespace(handle=AsyncMock(return_value=None)))
        self.handler = types.SimpleNamespace(app=self.app, monitor=Mock(), context_managers={},
                    detect_role=Mock(return_value=(self.role, '问题')), processor=Mock(), message_builder=Mock(),
                    memory_store=Mock(clear=Mock(return_value=[])))
        self.handler.processor.process.return_value = ('回答', 1, 1)
        self.handler.monitor.toast_cancelled.return_value = False
        self.typing = types.ModuleType('core.client.llm.llm_output_typing')
        self.typing.handle_typing_mode = AsyncMock(return_value=('回答', 1, 1))
        self.typing.output_text = AsyncMock()
        self.toast = types.ModuleType('core.client.llm.llm_output_toast')
        self.toast.handle_toast_mode = AsyncMock(return_value=('回答', 1, 1))
        text = types.ModuleType('core.client.output.text_output')
        text.TextOutput = types.SimpleNamespace(strip_punc=lambda value: value)
        self.modules = patch.dict(sys.modules, {self.typing.__name__: self.typing,
                                 self.toast.__name__: self.toast, text.__name__: text})
        self.modules.start()

    async def asyncTearDown(self):
        self.modules.stop()

    async def test_snapshot_is_read_once_and_preview_precedes_dispatch(self):
        order = []
        def preview(value):
            self.assertIs(value, self.preview)
            order.append('preview')
        async def dispatch(*args):
            self.assertIs(args[-1], self.preview)
            order.append('dispatch')
            return ('回答', 1, 1)
        self.toast.handle_toast_mode.side_effect = dispatch
        await self.namespace['process_and_output'](self.handler, '助理 问题', on_selection=preview)
        self.assertEqual(order, ['preview', 'dispatch'])
        self.capture.assert_called_once()

    async def test_empty_snapshot_does_not_trigger_second_read_in_process(self):
        empty = selection.SelectionContext()
        self.namespace['process'](self.handler, self.role, '问题', selection=empty)
        self.capture.assert_not_called()
        self.assertEqual(self.handler.message_builder.build_messages.call_args.kwargs['selection_text'], '')

    async def test_preview_and_request_use_same_snapshot(self):
        self.namespace['process'](self.handler, self.role, '问题', selection=self.preview)
        self.capture.assert_not_called()
        self.assertEqual(self.handler.message_builder.build_messages.call_args.kwargs['selection_text'], '选择内容')

    async def test_openmate_status_does_not_arm_escape_before_body(self):
        stop = load('core.client.llm.llm_stop_monitor', 'core/client/llm/llm_stop_monitor.py')
        monitor = self.handler.monitor = stop.StopMonitor()
        monitor.begin_request()
        statuses = []

        def process(**kwargs):
            kwargs['status_callback']('检索提示')
            monitor.on_stop_pressed()
            self.assertFalse(monitor.should_stop())
            kwargs['callback']('正文')
            monitor.on_stop_pressed()
            self.assertTrue(monitor.should_stop())
            return '正文', 1, 0.1

        self.handler.processor.process.side_effect = process
        self.namespace['process'](self.handler, self.role, '问题', selection=self.preview,
                                  status_callback=statuses.append)
        self.assertEqual(statuses, ['检索提示'])
        monitor.end_request()

    async def test_cancelled_toast_does_not_mark_selection_as_used(self):
        stop = load('core.client.llm.llm_stop_monitor', 'core/client/llm/llm_stop_monitor.py')
        request = stop.ToastRequest(lambda: None)
        def process(**kwargs):
            request.cancel()
            return '半截回答', 2, 0.1
        self.handler.processor.process.side_effect = process
        self.namespace['process'](self.handler, self.role, '问题', selection=self.preview,
                                  toast_request=request)
        self.namespace['record_selection_usage'].assert_not_called()

    async def test_no_role_reminder_and_companion_skip_selection(self):
        for branch in ('no_role', 'reminder', 'companion'):
            with self.subTest(branch=branch):
                self.handler.detect_role.return_value = (None if branch == 'no_role' else self.role, '问题')
                self.app.reminders.handle.return_value = '已设提醒' if branch == 'reminder' else None
                self.app.companion.has_connections = branch == 'companion'
                on_selection = Mock()
                await self.namespace['process_and_output'](self.handler, '问题', reminder_anchor=1,
                                                          on_selection=on_selection)
                on_selection.assert_called_once_with(None)
        self.capture.assert_not_called()

    async def test_clearing_history_also_clears_selection_dedup(self):
        self.namespace['clear_history'](self.handler)
        self.namespace['clear_selection_usage'].assert_called_once()


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_precedes_recognition_once_with_literal_single_line_output(self):
        await self.run_processor(selection.SelectionContext('正文', '[red]\n正文',
                                                          '已在历史，本轮未重复附加'))

    async def test_no_selection_does_not_add_preview_row(self):
        await self.run_processor(None)

    async def test_exception_before_selection_still_prints_recognition(self):
        await self.run_processor(None, failure=True)

    async def run_processor(self, selected, failure=False):
        console = Mock()
        config = types.SimpleNamespace(llm_enabled=True, traditional_convert=False, hot=False,
                                       paste=False, paste_apps=[], save_audio=False)
        namespace = dict(Optional=Optional, Config=config, RecognitionMessage=object,
            console=console, logger=logging.getLogger('selection-test'), time=time,
            TextOutput=types.SimpleNamespace(strip_punc=lambda text: text),
            show_processing_indicator=Mock(), hide_recording_indicator=Mock(),
            get_active_window_info=lambda: {}, broadcast_output_udp=Mock(),
            format_selection_preview=selection.format_selection_preview,
            format_task_line=load('core.client.task_line', 'core/client/task_line.py').format_task_line)
        extract_methods('core/client/output/result_processor.py', 'ResultProcessor',
                        {'_handle_message', '_after_output', '_paste_for_foreground', '_print_task_line'},
                        namespace)

        async def request(text, **kwargs):
            if failure:
                raise RuntimeError('routing failure')
            kwargs['on_selection'](selected)
            return None

        state = Mock()
        state.is_task_cancelled.return_value = False
        state.pop_image_snapshot.return_value = None
        state.pop_awaiting_result.return_value = None
        hotword = Mock()
        hotword.get_phoneme_corrector.return_value.correct.return_value = types.SimpleNamespace(
            text='识别文字', matchs=[], similars=[])
        hotword.get_rule_corrector.return_value.substitute.side_effect = lambda text: text
        processor = types.SimpleNamespace(state=state, hotword=hotword,
            app=types.SimpleNamespace(llm=types.SimpleNamespace(process_and_output=request)),
            _resolve_forced_role=Mock(return_value=None), _report_image_usage=Mock(),
            _update_continuous_state=Mock(), _log_modifier_key_state=Mock(), _streams={},
            _paste_for_foreground=namespace['_paste_for_foreground'])
        processor._after_output = lambda *args: namespace['_after_output'](processor, *args)
        processor._print_task_line = lambda *args: namespace['_print_task_line'](processor, *args)
        message = types.SimpleNamespace(task_id='test', is_final=True, text='识别文字',
                    time_complete=10, time_submit=9, time_start=5, duration=4)
        if failure:
            with self.assertRaisesRegex(RuntimeError, 'routing failure'):
                await namespace['_handle_message'](processor, message)
        else:
            await namespace['_handle_message'](processor, message)
        # 按内容认行，不按「识别结果：」「选中文本：」这类标签措辞
        calls = console.print.call_args_list
        recognition = [i for i, call in enumerate(calls) if '识别文字' in str(call.args[0])]
        preview = [i for i, call in enumerate(calls) if '正文' in str(call.args[0])]
        self.assertEqual(len(recognition), 1)
        self.assertEqual(len(preview), 1 if selected else 0)
        if selected:
            self.assertLess(preview[0], recognition[0])
            call = calls[preview[0]]
            self.assertNotIn('\n', call.args[0])
            self.assertIn('[red]', call.args[0])
            self.assertIn(selected.note, call.args[0])
            self.assertIs(call.kwargs['markup'], False)      # 选区里的方括号不能被当成样式
            self.assertIs(call.kwargs['no_wrap'], True)      # 预览只占一行


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.snapshot = selection.SelectionContext('选中文字', '选中文字')
        self.calls = []
        self.role = RoleConfig(name='助理', output_mode='toast')
        # 桩按真实 LLMHandler.process 的签名绑定参数、按名字取用，参数增减或换位都不影响
        signature = inspect.signature(extract_methods(
            'core/client/llm/llm_handler.py', 'LLMHandler', {'process'},
            dict(RoleConfig=RoleConfig, Optional=Optional, Tuple=Tuple))['process'])

        def process(*args, **kwargs):
            arguments = signature.bind(None, *args, **kwargs).arguments
            self.calls.append(arguments)
            if arguments.get('callback'):
                arguments['callback']('正文')
            return ('正文', 1, 1)

        monitor = Mock()
        monitor.should_stop.return_value = False
        stop = load('core.client.llm.llm_stop_monitor', 'core/client/llm/llm_stop_monitor.py')
        monitor.create_toast_request.side_effect = stop.ToastRequest
        self.handler = types.SimpleNamespace(process=process, monitor=monitor)
        tags = load('core.client.llm.llm_state_tags', 'core/client/llm/llm_state_tags.py')
        text_output = types.ModuleType('core.client.output.text_output')
        text_output.TextOutput = types.SimpleNamespace(strip_punc=lambda text: text)
        errors = types.ModuleType('core.client.llm.llm_error_handler')
        errors.handle_llm_error = Mock(side_effect=AssertionError('unexpected output fallback'))
        errors.should_fallback_to_original = lambda error: False
        errors.show_error_notification = Mock(side_effect=AssertionError('unexpected output error'))
        toast_ui = types.ModuleType('core.ui.toast')
        self.manager = Mock()
        self.manager.wait_for_window = AsyncMock(return_value=object())
        toast_ui.ToastMessageManager = lambda: self.manager
        toast_ui.ToastMessage = lambda **kwargs: types.SimpleNamespace(**kwargs)
        ui = types.ModuleType('core.client.ui')
        ui.hide_recording_indicator = Mock()
        self.openmate = types.ModuleType('core.client.llm.llm_output_openmate')
        self.openmate.try_openmate_mode = AsyncMock(return_value=None)
        self.modules = patch.dict(sys.modules, {text_output.__name__: text_output,
            errors.__name__: errors, toast_ui.__name__: toast_ui, ui.__name__: ui,
            self.openmate.__name__: self.openmate})
        self.modules.start()
        sys.modules['core.client.clipboard'].paste_text = AsyncMock()
        self.typing = load('core.client.llm.llm_output_typing', 'core/client/llm/llm_output_typing.py')
        self.toast = load('core.client.llm.llm_output_toast', 'core/client/llm/llm_output_toast.py')
        self.tags = tags

    async def asyncTearDown(self):
        self.modules.stop()

    def check_forwarding(self):
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.calls[0].get('selection'), self.snapshot)
        self.assertEqual(self.calls[0].get('image_data'), 'image')

    async def test_typing_stream_forwards_snapshot(self):
        await self.typing.handle_typing_mode(self.handler, '问题', False, [], self.role,
                                             '问题', [], 'image', self.snapshot)
        self.check_forwarding()

    async def test_typing_paste_forwards_snapshot(self):
        await self.typing.handle_typing_mode(self.handler, '问题', True, [], self.role,
                                             '问题', [], 'image', self.snapshot)
        self.check_forwarding()

    async def test_toast_forwards_snapshot(self):
        await self.toast.handle_toast_mode(self.handler, '问题', self.role, [], '问题', [],
                                           'image', self.snapshot)
        self.check_forwarding()
        call = self.openmate.try_openmate_mode.call_args
        self.assertTrue(any(arg is self.snapshot for arg in list(call.args) + list(call.kwargs.values())))

    async def test_openmate_stream_and_segments_forward_snapshot(self):
        source = ast.parse((ROOT / 'core/client/llm/llm_output_openmate.py').read_text(encoding='utf-8'))
        funcs = [node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                 and node.name in {'try_openmate_mode', '_handle_openmate_mode'}]
        sender = Mock(delivered=True)
        sender.finish.return_value = True
        config = config_module.ClientConfig
        config.openmate_enabled = True
        config.openmate_character = ''
        config.openmate_health_timeout = 0.5
        namespace = dict(to_thread=thread_tool.to_thread, logger=logging.getLogger('selection-test'),
            _make_client=lambda: types.SimpleNamespace(is_available=lambda timeout: True),
            _set_typing_safe=lambda client, active: True,
            fetch_emotion_info=lambda client, character: None,
            OpenMateStreamSender=lambda *args: sender, OpenMateSegmentSender=lambda *args: sender,
            strip_state_tags=self.tags.strip_state_tags, __package__='core.client.llm')
        exec(compile(ast.Module(body=funcs, type_ignores=[]), '<openmate>', 'exec'), namespace)
        for split in (False, True):
            with self.subTest(split=split):
                self.role.enable_result_splitter = split
                self.calls.clear()
                result = await namespace['try_openmate_mode'](self.handler, self.role, '问题', [], [],
                                                              'image', self.snapshot)
                self.assertEqual(result[0], '正文')
                self.check_forwarding()
                sender.push_status.reset_mock()
                self.calls[0]['status_callback']('检索提示')
                sender.push_status.assert_called_once_with('检索提示')


if __name__ == '__main__':
    unittest.main(verbosity=2)
