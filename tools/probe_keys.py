# coding: utf-8
"""按键、系统按键状态与 Scintilla 选区的时序探针（诊断用，只读）。

记录下面几类事件到控制台和 logs/probe_keys.log，时间格式与客户端日志相同，便于对照：
  KEY    低级键盘钩子看到的每个事件。本探针在客户端之后装钩子，排在钩子链最前，
         所以客户端会拦截的事件、客户端与远控软件注入的事件都看得到（I=注入）
  MOUSE  鼠标按键与滚轮（不记移动）
  STATE  系统按键状态（GetAsyncKeyState）里修饰键与 Esc 的变化。被钩子拦截的事件不改变它，
         所以它反映的是前台程序实际收到的按键
  FG     前台窗口（类名、进程名）、焦点控件类名、菜单模式的变化
  SCI    焦点在 Scintilla（Notepad4 等）上时，选区个数、模式、主选区的变化

不拦截、不发送任何按键，不读剪贴板，不读编辑器正文与窗口标题；
字母、数字、符号键只记「字符键」，不记是哪个键。

用法：先启动客户端，再运行 `python tools/probe_keys.py`，复现问题后回到本窗口按 Ctrl+C。
客户端若在探针运行期间重启，它的钩子会排到前面，客户端拦截的事件就看不到了，需重开探针。
"""
import argparse
import collections
import ctypes
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
import sys
import threading
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
KEY_MSGS = {0x100: '按下', 0x101: '松开', 0x104: '按下(SYS)', 0x105: '松开(SYS)'}
MOUSE_MSGS = {0x201: '左键按下', 0x202: '左键松开', 0x204: '右键按下', 0x205: '右键松开',
              0x207: '中键按下', 0x208: '中键松开', 0x20A: '滚轮', 0x20B: '侧键按下', 0x20C: '侧键松开'}

VK_NAMES = {
    0x08: 'BACK', 0x09: 'TAB', 0x0D: 'ENTER', 0x10: 'SHIFT', 0x11: 'CTRL', 0x12: 'ALT',
    0x13: 'PAUSE', 0x14: 'CAPS', 0x1B: 'ESC', 0x20: 'SPACE', 0x21: 'PGUP', 0x22: 'PGDN',
    0x23: 'END', 0x24: 'HOME', 0x25: 'LEFT', 0x26: 'UP', 0x27: 'RIGHT', 0x28: 'DOWN',
    0x2D: 'INS', 0x2E: 'DEL', 0x5B: 'LWIN', 0x5C: 'RWIN', 0x5D: 'APPS',
    0xA0: 'LSHIFT', 0xA1: 'RSHIFT', 0xA2: 'LCTRL', 0xA3: 'RCTRL', 0xA4: 'LALT', 0xA5: 'RALT',
    0xE5: 'PROCESSKEY', 0xE7: 'PACKET',
}
VK_NAMES.update({0x70 + i: f'F{i + 1}' for i in range(24)})
# 会暴露输入内容的键：字母、数字、小键盘、符号
CHAR_VKS = set(range(0x30, 0x3A)) | set(range(0x41, 0x5B)) | set(range(0x60, 0x70)) \
    | set(range(0xBA, 0xC1)) | set(range(0xDB, 0xE0)) | {0xE2}
STATE_VKS = [(0xA2, 'LCTRL'), (0xA3, 'RCTRL'), (0xA0, 'LSHIFT'), (0xA1, 'RSHIFT'),
             (0xA4, 'LALT'), (0xA5, 'RALT'), (0x5B, 'LWIN'), (0x5C, 'RWIN'), (0x1B, 'ESC')]

SCI_GETSELECTIONS = 2570
SCI_GETMAINSELECTION = 2575
SCI_GETSELECTIONNCARET = 2577
SCI_GETSELECTIONNANCHOR = 2579
SCI_GETSELECTIONMODE = 2423
SCI_SELECTIONISRECTANGLE = 2372
SCI_LINEFROMPOSITION = 2166
SCI_GETCOLUMN = 2129
SCI_GETCARETFORE = 2138
SCI_GETADDITIONALCARETFORE = 2605
SCI_GETMULTIPLESELECTION = 2564
SEL_MODES = {0: '流式', 1: '矩形', 2: '整行', 3: '细矩形'}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', wintypes.POINT), ('mouseData', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD), ('hwndActive', wintypes.HWND),
                ('hwndFocus', wintypes.HWND), ('hwndCapture', wintypes.HWND), ('hwndMenuOwner', wintypes.HWND),
                ('hwndMoveSize', wintypes.HWND), ('hwndCaret', wintypes.HWND), ('rcCaret', wintypes.RECT)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = LRESULT
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

events = collections.deque()


def now() -> str:
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


def key_name(vk: int) -> str:
    if vk in VK_NAMES:
        return VK_NAMES[vk]
    if vk in CHAR_VKS:
        return '字符键'
    return f'VK_{vk:02X}'


def key_flags(flags: int) -> str:
    text = ''.join(c for bit, c in ((0x10, 'I'), (0x02, 'L'), (0x01, 'E'), (0x20, 'A')) if flags & bit)
    return text or '-'


def hook_thread(ready: threading.Event) -> None:
    """装两个低级钩子并泵消息；回调只入队，不做任何耗时操作（超时会被系统摘掉）"""
    def on_key(code, wparam, lparam):
        if code == 0:
            data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            events.append((now(), 'KEY', wparam, data.vkCode, data.scanCode, data.flags, data.dwExtraInfo))
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def on_mouse(code, wparam, lparam):
        if code == 0 and wparam in MOUSE_MSGS:
            data = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            events.append((now(), 'MOUSE', wparam, data.mouseData, data.flags, data.dwExtraInfo))
        return user32.CallNextHookEx(None, code, wparam, lparam)

    key_proc, mouse_proc = HOOKPROC(on_key), HOOKPROC(on_mouse)
    module = kernel32.GetModuleHandleW(None)
    hooks = [user32.SetWindowsHookExW(WH_KEYBOARD_LL, key_proc, module, 0),
             user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_proc, module, 0)]
    if not all(hooks):
        events.append((now(), 'ERROR', f'安装钩子失败，错误码 {ctypes.get_last_error()}'))
    ready.set()
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass


def process_name(pid: int, cache={}) -> str:
    if pid not in cache:
        name = '?'
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = Path(buf.value).name
            kernel32.CloseHandle(handle)
        cache[pid] = name
    return cache[pid]


def class_name(hwnd) -> str:
    if not hwnd:
        return '-'
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def sci(hwnd, msg: int, wparam: int = 0, lparam: int = 0):
    result = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, 0x0002, 50, ctypes.byref(result))  # SMTO_ABORTIFHUNG
    return ctypes.c_ssize_t(result.value).value if ok else None


def colour(bgr) -> str:
    if bgr is None:
        return '?'
    return f'#{bgr & 0xFF:02X}{(bgr >> 8) & 0xFF:02X}{(bgr >> 16) & 0xFF:02X}'


def line_col(hwnd, pos) -> str:
    if pos is None:
        return '?'
    return f'{sci(hwnd, SCI_LINEFROMPOSITION, pos)}:{sci(hwnd, SCI_GETCOLUMN, pos)}'


def scintilla_key(hwnd):
    """只取个数、主选区和模式，供每轮比较；位置在变化时才查（打字时光标每次都动）"""
    count = sci(hwnd, SCI_GETSELECTIONS)
    if count is None:
        return None
    return count, sci(hwnd, SCI_GETMAINSELECTION), sci(hwnd, SCI_GETSELECTIONMODE), sci(hwnd, SCI_SELECTIONISRECTANGLE)


def scintilla_detail(hwnd, key) -> str:
    count, main, mode, rect = key
    parts = []
    for i in range(min(count, 4)):
        caret = sci(hwnd, SCI_GETSELECTIONNCARET, i)
        anchor = sci(hwnd, SCI_GETSELECTIONNANCHOR, i)
        parts.append(f'[{i}{"*" if i == main else ""} 光标{line_col(hwnd, caret)} 锚点{line_col(hwnd, anchor)}]')
    return (f'选区 {count} 个，模式 {SEL_MODES.get(mode, mode)}，矩形={rect}，'
            f'主选区 {main} ' + ' '.join(parts) + ('（只列前 4 个）' if count > 4 else ''))


def main():
    parser = argparse.ArgumentParser(description='按键与 Scintilla 选区时序探针（只读）')
    parser.add_argument('--seconds', type=float, default=0, help='运行多少秒后自动退出，默认直到 Ctrl+C')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Windows only')

    log_path = ROOT / 'logs' / 'probe_keys.log'
    log_path.parent.mkdir(exist_ok=True)
    log = open(log_path, 'w', encoding='utf-8')

    def emit(line: str) -> None:
        print(line, flush=True)
        log.write(line + '\n')
        log.flush()

    ready = threading.Event()
    threading.Thread(target=hook_thread, args=(ready,), daemon=True).start()
    ready.wait(2)
    emit(f'{now()} 探针已启动，日志写入 {log_path}；复现后回到本窗口按 Ctrl+C 退出')

    states = {}
    last_fg = None
    last_sci = None
    sci_seen = set()
    deadline = time.time() + args.seconds if args.seconds > 0 else None
    try:
        while deadline is None or time.time() < deadline:
            while events:
                item = events.popleft()
                if item[1] == 'KEY':
                    t, _, msg, vk, scan, flags, extra = item
                    name = key_name(vk)
                    # PACKET 的 scan 是注入的字符本身，PROCESSKEY 的 scan 是输入法吃掉的那个键
                    scan_text = '' if name in ('字符键', 'PACKET', 'PROCESSKEY') else f' scan={scan:02X}'
                    extra_text = f' extra={extra:X}' if extra else ''
                    emit(f'{t} KEY   {KEY_MSGS.get(msg, hex(msg))} {name}{scan_text} 标记={key_flags(flags)}{extra_text}')
                elif item[1] == 'MOUSE':
                    t, _, msg, data, flags, extra = item
                    injected = '注入' if flags & 0x01 else '-'
                    emit(f'{t} MOUSE {MOUSE_MSGS[msg]} 标记={injected}' + (f' extra={extra:X}' if extra else ''))
                else:
                    emit(f'{item[0]} {item[1]} {item[2]}')

            for vk, name in STATE_VKS:
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                if states.get(vk, False) != down:
                    states[vk] = down
                    held = ' '.join(n for v, n in STATE_VKS if states.get(v)) or '无'
                    emit(f'{now()} STATE {"+" if down else "-"}{name}（当前按下：{held}）')

            hwnd = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) if hwnd else 0
            info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
            if not (tid and user32.GetGUIThreadInfo(tid, ctypes.byref(info))):
                info = GUITHREADINFO()
            menu = [n for bit, n in ((0x04, '菜单模式'), (0x08, '系统菜单'), (0x10, '弹出菜单')) if info.flags & bit]
            focus_class = class_name(info.hwndFocus)
            fg = (hwnd, info.hwndFocus, tuple(menu))
            if fg != last_fg:
                last_fg = fg
                emit(f'{now()} FG    前台 {class_name(hwnd)}（{process_name(pid.value) if hwnd else "-"}）'
                     f' 焦点 {focus_class}' + (f' {"、".join(menu)}' if menu else ''))

            if focus_class == 'Scintilla':
                if info.hwndFocus not in sci_seen:
                    sci_seen.add(info.hwndFocus)
                    emit(f'{now()} SCI   主光标色 {colour(sci(info.hwndFocus, SCI_GETCARETFORE))}，'
                         f'附加光标色 {colour(sci(info.hwndFocus, SCI_GETADDITIONALCARETFORE))}，'
                         f'多选={sci(info.hwndFocus, SCI_GETMULTIPLESELECTION)}')
                key = scintilla_key(info.hwndFocus)
                if key is not None and (info.hwndFocus, key) != last_sci:
                    last_sci = (info.hwndFocus, key)
                    emit(f'{now()} SCI   {scintilla_detail(info.hwndFocus, key)}')
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    emit(f'{now()} 探针退出')
    log.close()


if __name__ == '__main__':
    main()
