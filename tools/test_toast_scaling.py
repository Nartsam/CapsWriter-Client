# coding: utf-8
"""
Toast 线程 Tk 缩放比例被显示配置变更改掉的真 Tk 回归测试

现象：虚拟显示器接入或移除之后，角色 Toast 的字号放大到 4/3，重启客户端才恢复。
原因见 ToastMessageManager._ensure_tk_scaling 的说明。

本脚本启动真实的 ToastMessageManager 线程，只向**本进程**这个线程的顶层窗口
发送 WM_DISPLAYCHANGE（不广播，不影响其他程序），分三步验证：
  A. 关掉修复：消息确实把缩放比例改回系统值，新窗口的字放大——证明本测试真的触发了问题
  B. 打开修复：空闲轮询就把比例恢复，并记一行 INFO
  C. 发消息后紧接着弹窗：同一轮询里先恢复比例再构造窗口，字号正常

**会真的弹出 Toast 窗口**（屏幕中央闪一下），需要桌面会话，因此不进 run_tests.py
的流水线。改完 core/ui/toast_manager.py 后手动跑一次。

用法（在仓库根目录执行）：python tools/test_toast_scaling.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import importlib
import os
import sys
import time
import types

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

PASS = []
FAIL = []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print('  [%s] %s  %s' % ('√' if cond else '×', name, detail))


def finish():
    print()
    print('=' * 60)
    print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
    sys.stdout.flush()
    # Tk 对象属于 Toast 线程，交给解释器退出时在主线程回收会报 Tcl_AsyncDelete，直接退出
    os._exit(1 if FAIL else 0)


if os.name != 'nt':
    print('仅适用于 Windows')
    sys.exit(0)


# ====================================================================
# 桩模块（与 test_toast_padding.py 相同：系统 Python 没有 markdown / tkhtmlview / rich）
# ====================================================================

class _StubLogger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def install_stubs():
    md = types.ModuleType('markdown')
    md.markdown = lambda *a, **k: ''
    sys.modules['markdown'] = md

    htmlview = types.ModuleType('tkhtmlview')

    class HTMLLabel(object):                       # 仅供 toast_base 顶层 import
        def __init__(self, *a, **k):
            raise NotImplementedError('本测试不触发 Markdown 渲染路径')

    htmlview.HTMLLabel = HTMLLabel
    sys.modules['tkhtmlview'] = htmlview

    core = types.ModuleType('core')
    core.__path__ = []
    sys.modules['core'] = core

    ui = types.ModuleType('core.ui')
    ui.__path__ = [os.path.join(REPO, 'core', 'ui')]
    ui.logger = _StubLogger()
    sys.modules['core.ui'] = ui
    core.ui = ui


install_stubs()
ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 在 Tk 之前声明，避免文字度量被虚拟化

tm = importlib.import_module('core.ui.toast_manager')
Manager = tm.ToastMessageManager
FACTOR = tm.TK_SCALING_FACTOR


class _CaptureLogger:
    def __init__(self):
        self.infos = []

    def info(self, msg, *a, **k):
        self.infos.append(msg)

    def __getattr__(self, _name):
        return lambda *a, **k: None


capture = _CaptureLogger()
tm.logger = capture

# ====================================================================
# Win32 辅助
# ====================================================================

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
WM_DISPLAYCHANGE = 0x007E
LOGPIXELSX, BITSPIXEL = 88, 12

hdc = user32.GetDC(None)
NATIVE_SCALING = gdi32.GetDeviceCaps(hdc, LOGPIXELSX) / 72.0
DEPTH = gdi32.GetDeviceCaps(hdc, BITSPIXEL)
user32.ReleaseDC(None, hdc)
SCREEN_LPARAM = (user32.GetSystemMetrics(1) << 16) | user32.GetSystemMetrics(0)

FONT_SIZE = 18
NORMAL_EM = round(FONT_SIZE * FACTOR)


def thread_toplevels(thread_id):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def collect(hwnd, _):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == 'TkTopLevel':
            found.append(hwnd)
        return True

    user32.EnumThreadWindows(thread_id, collect, 0)
    return found


def send_display_change(mgr):
    """模拟显示配置变更：只发给 Toast 线程自己的顶层窗口。SendMessage 同步返回，
    返回时 Tk 已处理完毕。参数沿用当前分辨率与色深，只走「重算毫米宽度」这一支"""
    hwnds = thread_toplevels(mgr.manager_thread.native_id)
    for hwnd in hwnds:
        user32.SendMessageW(hwnd, WM_DISPLAYCHANGE, DEPTH, SCREEN_LPARAM)
    return len(hwnds)


def scaling(mgr):
    return float(mgr.root.tk.call('tk', 'scaling'))


def show(mgr, text):
    """经消息队列弹一个角色 Toast 式窗口，返回窗口对象"""
    msg = tm.ToastMessage(text=text, font_size=FONT_SIZE, font_family='Microsoft YaHei UI',
                          bg='#f7fbff', fg='black', duration=60000, window_type='text')
    msg_id = mgr.add_message(msg)
    deadline = time.time() + 3
    while time.time() < deadline:
        for window in list(mgr.active_windows):
            if getattr(window, '_msg_id', None) == msg_id:
                return window
        time.sleep(0.02)
    return None


def em(window):
    return window.my_font.measure('我')


# ====================================================================
# 测试
# ====================================================================

print('[环境] 系统 DPI 对应的缩放比例 %.3f，Toast 设定 %s，%d 号字正常应为 %dpx'
      % (NATIVE_SCALING, FACTOR, FONT_SIZE, NORMAL_EM))
if abs(NATIVE_SCALING - FACTOR) < 0.05:
    print('本机系统 DPI 恰好等于 Toast 设定，比例被改也看不出差别，跳过')
    sys.exit(0)

mgr = Manager()
deadline = time.time() + 5
while not mgr.is_running and time.time() < deadline:
    time.sleep(0.02)
check('Toast 线程已启动', mgr.is_running)
if not mgr.is_running:
    finish()

try:
    check('启动时缩放比例为设定值', abs(scaling(mgr) - FACTOR) < 0.01, '%.4f' % scaling(mgr))

    print()
    print('[A 关掉修复：确认消息能触发问题]')
    original = Manager._ensure_tk_scaling
    Manager._ensure_tk_scaling = lambda self: None
    count = send_display_change(mgr)
    check('隐藏根窗口也能收到消息', count >= 1, '顶层窗口 %d 个' % count)
    drifted = scaling(mgr)
    check('比例被改回系统值', abs(drifted - NATIVE_SCALING) < 0.01, '%.4f' % drifted)
    big = show(mgr, '关掉修复时的窗口')
    check('新窗口的字放大', big is not None and em(big) > NORMAL_EM,
          'em=%s' % (em(big) if big else None))

    print()
    print('[B 打开修复：空闲轮询即恢复]')
    Manager._ensure_tk_scaling = original
    time.sleep(0.35)                               # 队列轮询间隔 100ms，留足三轮
    check('比例已恢复', abs(scaling(mgr) - FACTOR) < 0.01, '%.4f' % scaling(mgr))
    check('记了一行 INFO', len(capture.infos) == 1, repr(capture.infos))

    print()
    print('[C 消息与弹窗紧挨着：先恢复再构造]')
    capture.infos.clear()
    send_display_change(mgr)
    w = show(mgr, '打开修复后的窗口')
    check('新窗口字号正常', w is not None and em(w) == NORMAL_EM,
          'em=%s' % (em(w) if w else None))
    check('行高回到放大前', w is not None and big is not None and w.line_height < big.line_height,
          '%s vs 放大时 %s' % (w.line_height if w else None, big.line_height if big else None))
    check('恢复动作同样记了日志', len(capture.infos) == 1, repr(capture.infos))
finally:
    try:
        mgr._on_close()
    except Exception:
        pass

finish()
