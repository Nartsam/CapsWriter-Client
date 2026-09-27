"""Windows 选区读取：UIA 与 Scintilla 只读消息，不发送输入或访问剪贴板。

COM 对象只存在于无窗口的 MTA 工作线程，外部只接收文本快照。总等待超时后
不采纳迟到结果；最多一个工作线程在途，避免无响应的应用耗尽线程池。
"""
from __future__ import annotations

import ctypes
import codecs
from ctypes import wintypes
from contextlib import ExitStack
from dataclasses import dataclass
import sys
import threading
import time
import uuid


READ_TIMEOUT_S = 1.0
UIA_TIMEOUT_MS = 300
# Electron/webview 的原始树包含多层布局节点；本机 VS Code 焦点到窗口实测 30 层。
# 仍限制在原前台窗口父链内，并受外层总等待上限约束。
MAX_PARENT_DEPTH = 128
MAX_SELECTION_RANGES = 64
TEXT_PATTERN_ID = 10014
SCINTILLA_TIMEOUT_S = 0.5
SCINTILLA_MAX_BYTES = 16384
# 所有消息只接收/返回整数，不能把 SCI_GETSELTEXT 等指针消息直接发给其他进程。
SCI_GETLENGTH = 2006
SCI_GETCHARAT = 2007
SCI_GETCODEPAGE = 2137
SCI_GETSELECTIONS = 2570
SCI_GETSELECTIONNSTART = 2585
SCI_GETSELECTIONNEND = 2587


@dataclass
class SelectionRead:
    status: str
    text: str = ''
    target: tuple = (0, 0)
    detail: str = ''


class _GUIThreadInfo(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('hwndActive', wintypes.HWND), ('hwndFocus', wintypes.HWND),
                ('hwndCapture', wintypes.HWND), ('hwndMenuOwner', wintypes.HWND),
                ('hwndMoveSize', wintypes.HWND), ('hwndCaret', wintypes.HWND),
                ('rcCaret', wintypes.RECT)]


def get_input_target() -> tuple:
    """前台窗口与实际键盘焦点 HWND；失败时不可用于 Ctrl+C 回退。"""
    if sys.platform != 'win32':
        return (0, 0)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUIThreadInfo)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    foreground = user32.GetForegroundWindow()
    info = _GUIThreadInfo(cbSize=ctypes.sizeof(_GUIThreadInfo))
    if not foreground or not user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return (0, 0)
    return (foreground, info.hwndFocus or 0)


class _GUID(ctypes.Structure):
    _fields_ = [('data1', ctypes.c_uint32), ('data2', ctypes.c_uint16),
                ('data3', ctypes.c_uint16), ('data4', ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


CLSID_UIAUTOMATION8 = _GUID.parse('e22ad333-b25f-460c-83d0-0581107395c9')
IID_UIAUTOMATION2 = _GUID.parse('34723aff-0c9d-49d0-9896-7ab52df8cd8a')
IID_TEXT_PATTERN = _GUID.parse('32eba289-3583-42c9-9c59-3b6d9a1e9b6a')
# 槽位按 Windows SDK UIAutomationClient.h 的声明顺序，IUnknown 占 0/1/2。


class _UIAError(OSError):
    def __init__(self, hresult):
        self.hresult = hresult & 0xffffffff
        super().__init__(f'UIA HRESULT 0x{self.hresult:08X}')


class _ComPointer:
    """仅包装所需的原生 vtable 调用；HRESULT 为 32 位，接口/句柄为指针宽度。"""
    def __init__(self, pointer):
        self.pointer = pointer

    def call(self, slot, argtypes=(), *args):
        table = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, *argtypes)(table[slot])
        result = method(self.pointer, *args)
        if result < 0:
            raise _UIAError(result)

    def interface(self, slot, argtypes=(), *args):
        pointer = ctypes.c_void_p()
        self.call(slot, argtypes + (ctypes.POINTER(ctypes.c_void_p),), *args, ctypes.byref(pointer))
        return _ComPointer(pointer) if pointer.value else None

    def integer(self, slot):
        value = ctypes.c_int()
        self.call(slot, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(value))
        return value.value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        # IUnknown::Release 返回 ULONG，不是 HRESULT。
        table = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(table[2])(self.pointer)
        self.pointer = None


def _same_element(automation, first, second):
    same = ctypes.c_int()
    automation.call(3, (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)),
                    first.pointer, second.pointer, ctypes.byref(same))
    return bool(same.value)


def _password_or_unknown(element):
    try:
        return bool(element.integer(35))  # CurrentIsPassword
    except _UIAError:
        # 不能确认此属性时也不允许 Ctrl+C 回退。
        return True


def _selected_text(pattern, limit, oleaut32):
    ranges = pattern.interface(5)  # IUIAutomationTextPattern::GetSelection
    if ranges is None:
        return ''
    parts = []
    remaining = limit
    with ranges:
        count = ranges.integer(3)
        if count < 0 or count > MAX_SELECTION_RANGES:
            raise ValueError('UIA 选区数量异常')
        for index in range(count):
            span = ranges.interface(4, (ctypes.c_int,), index)
            if span is None:
                continue
            with span:
                text = ctypes.c_void_p()
                try:
                    # BSTR 必须以原始指针接收并由 SysFreeString 释放，不能用 c_wchar_p
                    # 自动转换后丢掉分配地址；SysStringLen 也保留了字符串内的 NUL。
                    span.call(12, (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
                              remaining, ctypes.byref(text))
                    value = ctypes.wstring_at(text, oleaut32.SysStringLen(text)) if text.value else ''
                finally:
                    if text.value:
                        oleaut32.SysFreeString(text)
                value = value[:remaining]
                if value:
                    parts.append(value)
                    remaining -= len(value) + 1
            if remaining <= 0:
                break
    return '\n'.join(parts)[:limit]


def _read_native(target, limit) -> SelectionRead:
    ole32 = ctypes.WinDLL('ole32')
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_int32
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_int32
    oleaut32 = ctypes.WinDLL('oleaut32')
    oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
    oleaut32.SysFreeString.restype = None
    oleaut32.SysStringLen.argtypes = [ctypes.c_void_p]
    oleaut32.SysStringLen.restype = ctypes.c_uint32
    hr = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED；此线程不拥有任何窗口。
    if hr < 0:
        raise OSError(f'COM 初始化失败 0x{hr & 0xffffffff:08X}')
    try:
        pointer = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(CLSID_UIAUTOMATION8), None, 1,
                                    ctypes.byref(IID_UIAUTOMATION2), ctypes.byref(pointer))
        if hr < 0:
            raise OSError(f'UIA 创建失败 0x{hr & 0xffffffff:08X}')
        with ExitStack() as stack:
            automation = stack.enter_context(_ComPointer(pointer))
            # IUIAutomation2：连接和单次跨进程调用的超时；外层另限制整次读取时间。
            automation.call(61, (ctypes.c_uint32,), UIA_TIMEOUT_MS)
            automation.call(63, (ctypes.c_uint32,), UIA_TIMEOUT_MS)
            focus = automation.interface(8)  # GetFocusedElement
            if focus is not None:
                stack.enter_context(focus)
            window = automation.interface(6, (wintypes.HWND,), target[0])  # ElementFromHandle
            if window is not None:
                stack.enter_context(window)
            if focus is None or window is None:
                return SelectionRead('unsupported', target=target, detail='未找到前台焦点元素')
            if _password_or_unknown(focus):
                return SelectionRead('blocked', target=target, detail='密码控件或无法确认密码属性')
            walker = automation.interface(16)  # RawViewWalker
            if walker is None:
                return SelectionRead('unsupported', target=target, detail='无法获取焦点父链遍历器')
            stack.enter_context(walker)
            ancestors = [focus]
            for depth in range(MAX_PARENT_DEPTH + 1):
                if _same_element(automation, ancestors[-1], window):
                    break
                if depth == MAX_PARENT_DEPTH:
                    return SelectionRead('unsupported', target=target, detail='焦点父链超出读取范围')
                parent = walker.interface(3, (ctypes.c_void_p,), ancestors[-1].pointer)
                if parent is None:
                    return SelectionRead('changed', target=target, detail='焦点不属于原前台窗口')
                ancestors.append(stack.enter_context(parent))
                # 即使焦点本身支持 TextPattern，也要确认祖先没有密码属性。
                if _password_or_unknown(parent):
                    return SelectionRead('blocked', target=target, detail='密码控件或无法确认密码属性')

            result = SelectionRead('unsupported', target=target, detail='焦点控件不支持 TextPattern')
            # 只检查焦点及其祖先，不遍历整个窗口去读取其他标签页或终端面板的选区。
            for element in ancestors:
                try:
                    pattern = element.interface(14, (ctypes.c_int, ctypes.POINTER(_GUID)),
                                                TEXT_PATTERN_ID, ctypes.byref(IID_TEXT_PATTERN))
                except _UIAError as e:
                    if e.hresult in (0x80004002, 0x80040204):  # E_NOINTERFACE / UIA_E_NOTSUPPORTED
                        continue
                    raise
                if pattern is None:
                    continue
                with pattern:
                    text = _selected_text(pattern, limit, oleaut32)
                result = SelectionRead('selected' if text.strip() else 'empty', text, target)
                break
            current = automation.interface(8)
            if current is None:
                return SelectionRead('changed', target=target, detail='读取期间焦点失效')
            with current:
                if get_input_target() != target or not _same_element(automation, focus, current):
                    return SelectionRead('changed', target=target, detail='读取期间前台或焦点改变')
            return result
    finally:
        ole32.CoUninitialize()


def _scintilla_text(query, limit, target):
    """只读取各选区中的字节；不取全文、不注入进程内存，保留多选区间的换行。"""
    def snapshot():
        count = query(SCI_GETSELECTIONS)
        if not 1 <= count <= MAX_SELECTION_RANGES:
            raise ValueError('Scintilla 选区数量不支持')
        length = query(SCI_GETLENGTH)
        ranges = tuple((query(SCI_GETSELECTIONNSTART, index), query(SCI_GETSELECTIONNEND, index))
                       for index in range(count))
        if any(not 0 <= start <= end <= length for start, end in ranges):
            raise ValueError('Scintilla 选区位置无效')
        return length, ranges, query(SCI_GETCODEPAGE)

    before = snapshot()
    _, ranges, codepage = before
    encoding = 'utf-8' if codepage == 65001 else ('mbcs' if codepage == 0 else f'cp{codepage}')
    parts = []
    remaining = limit
    byte_count = 0
    for start, end in sorted(ranges):
        if start == end:
            continue
        decoder = codecs.getincrementaldecoder(encoding)(errors='strict')
        chars = []
        for position in range(start, end):
            if byte_count >= SCINTILLA_MAX_BYTES:
                raise ValueError('Scintilla 选区读取超出字节上限')
            value = query(SCI_GETCHARAT, position)
            byte_count += 1
            # Scintilla 的 char 在部分构建中会符号扩展，统一恢复为一个字节。
            chunk = decoder.decode(bytes([value & 0xff]))
            if chunk:
                chars.append(chunk)
                remaining -= len(chunk)
                if remaining <= 0:
                    break
        if remaining > 0:
            chars.append(decoder.decode(b'', final=True))
        parts.append(''.join(chars))
        if remaining <= 0:
            break
        remaining -= 1  # 多选区之间的换行也计入角色长度限制。
        if remaining <= 0:
            break
    if before != snapshot():
        return SelectionRead('changed', target=target, detail='Scintilla 读取期间选区或文档改变')
    text = '\n'.join(parts)[:limit]
    return SelectionRead('selected' if text.strip() else 'empty', text, target, 'Scintilla 只读接口')


def _read_scintilla(target, limit):
    """仅对原前台窗口内的 Scintilla 焦点控件生效；其他控件返回 None。"""
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.IsChild.restype = wintypes.BOOL
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t,
        ctypes.c_ssize_t, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    foreground, focus = target
    if not focus:
        return None
    class_name = ctypes.create_unicode_buffer(128)
    if not user32.GetClassNameW(focus, class_name, len(class_name)) or class_name.value != 'Scintilla':
        return None
    if get_input_target() != target or not user32.IsChild(foreground, focus):
        return SelectionRead('changed', target=target, detail='Scintilla 输入窗口改变')
    deadline = time.monotonic() + SCINTILLA_TIMEOUT_S

    def query(message, argument=0):
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError('Scintilla 读取超时')
        result = ctypes.c_size_t()
        # SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT；不发送带远程缓冲区的消息。
        if not user32.SendMessageTimeoutW(focus, message, argument, 0, 0x22,
                                         min(100, remaining_ms), ctypes.byref(result)):
            raise OSError('Scintilla 只读消息失败或超时')
        return ctypes.c_ssize_t(result.value).value

    result = _scintilla_text(query, limit, target)
    if get_input_target() != target:
        return SelectionRead('changed', target=target, detail='Scintilla 读取期间输入窗口改变')
    return result


def _read_compatible(target, limit):
    result = _read_native(target, limit)
    # 只在 UIA 不支持时补充控件专用查询；空结果是否触发 Ctrl+C 由上层模式决定。
    if result.status == 'unsupported':
        compatible = _read_scintilla(target, limit)
        if compatible is not None:
            return compatible
    return result


_read_lock = threading.Lock()


def read_selection(max_length: int) -> SelectionRead:
    """有界等待；选区工作线程永远只读，即使超时后继续运行也不会发送按键。"""
    max_length = max(0, int(max_length))
    if not max_length:
        return SelectionRead('empty')
    target = get_input_target()
    if not target[0]:
        return SelectionRead('unsupported', target=target, detail='无法确定前台输入窗口')
    if not _read_lock.acquire(blocking=False):
        return SelectionRead('timeout', target=target, detail='上一次选区读取尚未结束')
    done = threading.Event()
    result = []

    def worker():
        try:
            result.append(_read_compatible(target, max_length))
        except _UIAError as e:
            # 元素已销毁/替换时丢弃原目标，不把它当成可用的 Ctrl+C 回退对象。
            status = 'changed' if e.hresult == 0x80040201 else 'error'  # UIA_E_ELEMENTNOTAVAILABLE
            result.append(SelectionRead(status, target=target, detail=str(e)))
        except Exception as e:
            result.append(SelectionRead('error', target=target, detail=str(e)))
        finally:
            _read_lock.release()
            done.set()

    thread = threading.Thread(target=worker, name='selection-reader', daemon=True)
    try:
        thread.start()
    except Exception:
        _read_lock.release()
        raise
    if not done.wait(READ_TIMEOUT_S):
        return SelectionRead('timeout', target=target, detail='选区读取超时')
    if get_input_target() != target:
        return SelectionRead('changed', target=target, detail='读取期间输入窗口改变')
    return result[0]
