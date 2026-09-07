"""只读 Win32 探测：当前前台显示器的全屏、锁屏和屏保延后提醒。"""
import ctypes
from ctypes import wintypes
import os


SHELL_CLASSES = {'Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'Progman', 'WorkerW',
                 'MultitaskingViewFrame', 'ForegroundStaging', 'XamlExplorerHostIslandWindow'}


def is_fullscreen(rect, monitor, caption=False):
    return (not caption and rect[0] <= monitor[0] and rect[1] <= monitor[1]
            and rect[2] >= monitor[2] and rect[3] >= monitor[3])


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]


def should_defer():
    if os.name != 'nt':
        return False
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = wintypes.HWND
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    desktop = user.OpenInputDesktop(0, False, 1)
    if not desktop:
        return True
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
            return True
        if name.value.lower() != 'default':
            return True
    finally:
        user.CloseDesktop(desktop)
    screensaver = wintypes.BOOL()
    if user.SystemParametersInfoW(0x0072, 0, ctypes.byref(screensaver), 0) and screensaver.value:
        return True
    window = user.GetForegroundWindow()
    if not window:
        return True
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(window, ctypes.byref(pid))
    if pid.value == os.getpid():
        return False
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    name = ctypes.create_unicode_buffer(256)
    user.GetClassNameW(window, name, 256)
    if name.value in SHELL_CLASSES:
        return False
    # 仅改变本次探测线程的 DPI 上下文，确保矩形和显示器边界使用同一物理像素坐标。
    previous = None
    change_dpi = getattr(user, 'SetThreadDpiAwarenessContext', None)
    if change_dpi:
        change_dpi.argtypes = [ctypes.c_void_p]
        change_dpi.restype = ctypes.c_void_p
        previous = change_dpi(ctypes.c_void_p(-4))
    try:
        user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user.MonitorFromWindow.restype = wintypes.HANDLE
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        rect = wintypes.RECT()
        if not user.GetMonitorInfoW(user.MonitorFromWindow(window, 2), ctypes.byref(info)):
            return True
        if not user.GetWindowRect(window, ctypes.byref(rect)):
            return True
        dwm = ctypes.windll.dwmapi
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        frame = wintypes.RECT()
        if dwm.DwmGetWindowAttribute(window, 9, ctypes.byref(frame), ctypes.sizeof(frame)) == 0:
            rect = frame
        # SHQueryUserNotificationState 没有显示器归属，不能用后台另一屏的游戏
        # 覆盖当前前台文档的判断；提醒严格以此前台窗口的几何范围为准。
        edges = lambda value: (value.left, value.top, value.right, value.bottom)
        return is_fullscreen(edges(rect), edges(info.rcMonitor),
                             bool(user.GetWindowLongW(window, -16) & 0x00C00000))
    finally:
        if previous:
            change_dpi(previous)
