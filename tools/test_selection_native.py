# coding: utf-8
"""真实 Win32/UIA 冒烟：隐藏自有控件，无键盘输入、无剪贴板写入。

替换的只有焦点定位（指向自有控件）；COM 创建、父链、TextPattern、GetSelection、
GetText 与资源释放使用生产代码；另以自有 Scintilla 消息桩验证跨线程只读消息。
后者不是真实 Scintilla 库，不能替代 Notepad4 / Windows Terminal/TUI 的人工验收。
"""
import ctypes
from ctypes import wintypes
import importlib.util
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != 'win32':
        print('SKIP: Windows only')
        return
    spec = importlib.util.spec_from_file_location('selection_native_smoke', ROOT / 'core/tools/windows_selection.py')
    native = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = native
    spec.loader.exec_module(native)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                     ctypes.c_size_t, ctypes.c_ssize_t)

    class WindowClass(ctypes.Structure):
        _fields_ = [('style', wintypes.UINT), ('proc', wndproc_type), ('cls_extra', ctypes.c_int),
                    ('wnd_extra', ctypes.c_int), ('instance', wintypes.HINSTANCE), ('icon', wintypes.HICON),
                    ('cursor', wintypes.HANDLE), ('brush', wintypes.HBRUSH),
                    ('menu', wintypes.LPCWSTR), ('name', wintypes.LPCWSTR)]

    user32.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    source = 'before 中文🙂 after'.encode('utf-8')
    bounds = [7, 17]
    received = []

    @wndproc_type
    def procedure(hwnd, message, wparam, lparam):
        if message in (0x0100, 0x0101, 0x0301):
            received.append(('input_or_copy', message))
        scalars = {native.SCI_GETLENGTH: len(source), native.SCI_GETSELECTIONS: 1,
                   native.SCI_GETSELECTIONNSTART: bounds[0], native.SCI_GETSELECTIONNEND: bounds[1],
                   native.SCI_GETCODEPAGE: 65001}
        if message in scalars or message == native.SCI_GETCHARAT:
            received.append((message, wparam, lparam))
            if message == native.SCI_GETCHARAT:
                return source[wparam] if wparam < len(source) else 0
            return scalars[message]
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    cls = WindowClass(proc=procedure, name='Scintilla')
    if not user32.RegisterClassW(ctypes.byref(cls)):
        raise ctypes.WinError(ctypes.get_last_error())
    before_clipboard = user32.GetClipboardSequenceNumber()
    parent = user32.CreateWindowExW(0, 'STATIC', 'CapsWriter selection test', 0x00CF0000,
                                   0, 0, 400, 200, None, None, None, None)
    if not parent:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        edit = user32.CreateWindowExW(0, 'EDIT', 'before SELECTED after', 0x50000104,
                                     0, 0, 390, 190, parent, None, None, None)
        if not edit:
            raise ctypes.WinError(ctypes.get_last_error())
        original_interface = native._ComPointer.interface

        def focused_interface(pointer, slot, argtypes=(), *args):
            if slot == 8 and not argtypes:  # GetFocusedElement 定位自有隐藏控件。
                return original_interface(pointer, 6, (wintypes.HWND,), edit)
            return original_interface(pointer, slot, argtypes, *args)

        def read(limit):
            result = []
            thread = threading.Thread(target=lambda: result.append(native.read_selection(limit)), daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            msg = wintypes.MSG()
            # UIA 在独立 MTA 线程调用；拥有测试窗口的主线程持续处理消息。
            while (thread.is_alive() or native._read_lock.locked()) and time.monotonic() < deadline:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.001)
            if thread.is_alive() or native._read_lock.locked():
                raise AssertionError('native UIA worker did not finish')
            return result[0]

        with patch.object(native, 'get_input_target', return_value=(parent, edit)), \
                patch.object(native._ComPointer, 'interface', focused_interface):
            user32.SendMessageW(edit, 0x00B1, 7, 15)  # EM_SETSEL 仅操作自有 Edit。
            result = read(100)
            assert result.status == 'selected', (result.status, result.detail)
            assert result.text == 'SELECTED', repr(result.text)
            result = read(3)
            assert result.text == 'SEL', repr(result.text)
            user32.SendMessageW(edit, 0x00B1, 7, 7)
            result = read(100)
            assert result.status == 'empty' and result.text == '', (result.status, result.detail)
        scintilla = user32.CreateWindowExW(0, 'Scintilla', '', 0x50000000,
                                          0, 0, 390, 190, parent, None, None, None)
        if not scintilla:
            raise ctypes.WinError(ctypes.get_last_error())
        edit = scintilla  # 上面的焦点定位桩指向新的自有控件；UIA 和消息发送均走生产代码。
        with patch.object(native, 'get_input_target', return_value=(parent, scintilla)), \
                patch.object(native._ComPointer, 'interface', focused_interface):
            result = read(100)
            assert result.text == '中文🙂', (result.status, result.detail, repr(result.text))
            assert result.detail == 'Scintilla 只读接口', result.detail
            result = read(2)
            assert result.text == '中文', (result.status, result.detail, repr(result.text))
            bounds[:] = [7, 7]
            result = read(100)
            assert result.status == 'empty' and result.text == '', (result.status, result.detail)
        assert all(len(item) == 3 and item[2] == 0 for item in received), received
        assert received, 'Scintilla query path was not exercised'
        assert user32.GetClipboardSequenceNumber() == before_clipboard, 'clipboard changed during smoke test'
        print('PASS: UIA Edit + Win32 Scintilla protocol fixture; Unicode, limit, caret; no keys/copy, clipboard unchanged.')
    finally:
        user32.DestroyWindow(parent)
        user32.UnregisterClassW('Scintilla', None)


if __name__ == '__main__':
    main()
