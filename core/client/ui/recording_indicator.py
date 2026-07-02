# coding: utf-8
"""
录音悬浮指示器

在录音和等待识别结果期间，在输入光标附近显示一个轻量置顶小组件。
布局使用固定尺寸和像素级截断，避免文字挤压、错位或越界。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from tkinter import font as tkfont
from typing import Optional, Tuple

from .. import logger


# Tk 线程轮询消息队列的间隔，负责响应录音/等待/隐藏等状态切换。
POLL_INTERVAL_MS = 50

# 文案刷新间隔，负责更新录音秒数和等待状态省略号。
TICK_INTERVAL_MS = 100

# 当可以拿到输入光标时，提示框相对输入光标的偏移。
OFFSET_X = 16
OFFSET_Y = 20

# Tk 使用指定颜色作为透明色；实际面板仍然是 #F0F0F0。
TRANSPARENT_BG = "#ff00ff"

# 用户指定的毛玻璃主背景色和扁平化视觉参数。
PANEL_BG = "#F0F0F0"
BORDER = "#D8D8D8"
FROST_MID = "#F5F5F5"
FG_TITLE = "#333333"
FG_DETAIL = "#333333"
ACCENT = "#0078D7"
ERROR_ACCENT = "#dc2626"

# 面板固定尺寸：固定布局能避免文字挤压、重排和越界。
MARGIN = 0
PANEL_W = 450
PANEL_H = 140
CANVAS_W = PANEL_W + MARGIN * 2
CANVAS_H = PANEL_H + MARGIN * 2
RADIUS = 34
DOT_R = 6

TITLE_FONT_SIZE     = 14
DETAIL_FONT_SIZE    = 10

# Canvas 内部布局坐标。TITLE_Y 和 DETAIL_Y 是两行文字的基线位置。
PANEL_X = MARGIN
PANEL_Y = MARGIN
DOT_X = PANEL_X + 38
TITLE_X = DOT_X + 24
TITLE_Y = PANEL_Y + 45
DETAIL_Y = PANEL_Y + 99
TEXT_MAX_W = PANEL_W - (TITLE_X - PANEL_X) - 32


class POINT(ctypes.Structure):
    """Windows API 使用的屏幕坐标点。"""

    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


class RECT(ctypes.Structure):
    """Windows API 使用的矩形区域。"""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class GUITHREADINFO(ctypes.Structure):
    """GetGUIThreadInfo 返回的前台线程 GUI 状态，用于获取文本插入光标。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", RECT),
    ]


@dataclass
class IndicatorState:
    """悬浮指示器的当前状态快照，所有字段都只在 Tk 线程中消费。"""

    # hidden / recording / waiting / progress / processing / error
    mode: str = "hidden"
    # 录音开始时间，用于持续计算录音秒数。
    start_time: float = 0.0
    # 松开快捷键后记录的总录音时长。
    recorded_duration: float = 0.0
    # 服务端非最终结果携带的已处理音频时长。
    progress_duration: float = 0.0
    # 服务端非最终识别文本预览，过长会按像素裁剪。
    preview_text: str = ""
    # 内部快捷键名，渲染前会转成人类可读中文名称。
    shortcut_key: str = ""
    # 错误提示自动隐藏的截止时间戳。
    auto_hide_at: float = 0.0
    # 提示框锚点：输入光标位置或屏幕中心靠下的位置。
    anchor: Optional[Tuple[int, int]] = None
    # near 表示贴近输入光标；center 表示屏幕中下兜底位置。
    anchor_source: str = "near"


class RecordingIndicator:
    """线程安全的录音悬浮指示器单例。"""

    _instance: Optional["RecordingIndicator"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "RecordingIndicator":
        # 单例保证全局只有一个 Tk 主循环和一个提示窗口。
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        # __new__ 会反复返回同一个对象，避免重复初始化线程和队列。
        if self._initialized:
            return

        self._initialized = True
        # 其他线程只往队列投递命令，实际 UI 操作都由 Tk 线程执行。
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None

        # Tk 对象只能在创建它的线程中访问。
        self.root: Optional[tk.Tk] = None
        self.window: Optional[tk.Toplevel] = None
        self.canvas: Optional[tk.Canvas] = None
        self.title_font: Optional[tkfont.Font] = None
        self.detail_font: Optional[tkfont.Font] = None

        # 当前显示状态和动画 tick 计数。
        self.state = IndicatorState()
        self._tick_index = 0

    # ------------------------------------------------------------------
    # 对外命令
    # ------------------------------------------------------------------

    def show_recording(self, start_time: Optional[float] = None, shortcut_key: str = "") -> None:
        """显示录音状态。"""
        self._post("recording", start_time=start_time or time.time(), shortcut_key=shortcut_key)

    def show_waiting(self, recorded_duration: Optional[float] = None) -> None:
        """显示等待服务端返回结果状态。"""
        self._post("waiting", recorded_duration=recorded_duration or 0.0)

    def show_processing(self) -> None:
        """显示本地处理最终结果状态。"""
        self._post("processing")

    def update_progress(self, duration: Optional[float] = None, preview: str = "") -> None:
        """更新服务端已返回的识别进度。"""
        self._post("progress", duration=duration or 0.0, preview=preview or "")

    def show_error(self, text: str, duration_ms: int = 2000) -> None:
        """显示错误并自动隐藏。"""
        self._post("error", text=text, duration_ms=duration_ms)

    def hide(self) -> None:
        """隐藏指示器。"""
        self._post("hide")

    # ------------------------------------------------------------------
    # 线程与 Tk 主循环
    # ------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        # 已有 Tk 线程时复用；没有时懒启动，避免客户端启动阶段额外创建窗口。
        if self._thread and self._thread.is_alive():
            return

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="RecordingIndicatorThread",
        )
        self._thread.start()

    def _post(self, action: str, **payload) -> None:
        # 所有对外 API 都通过队列转交 Tk 线程，避免跨线程直接操作 Tk。
        self._ensure_thread()
        self._queue.put((action, payload))

    def _run(self) -> None:
        try:
            # 隐藏 root，只使用独立的 Toplevel 做真正的悬浮提示框。
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.protocol("WM_DELETE_WINDOW", self.hide)
            # 定时处理跨线程命令，并刷新动态文案。
            self.root.after(POLL_INTERVAL_MS, self._poll_queue)
            self.root.after(TICK_INTERVAL_MS, self._tick)
            self.root.mainloop()
        except Exception as e:
            logger.warning(f"录音悬浮指示器启动失败: {e}")

    def _poll_queue(self) -> None:
        try:
            while True:
                # 每次轮询尽量清空队列，避免频繁状态切换时 UI 滞后。
                action, payload = self._queue.get_nowait()
                self._handle_command(action, payload)
        except queue.Empty:
            pass
        except Exception as e:
            logger.warning(f"录音悬浮指示器处理命令失败: {e}")

        if self.root:
            self.root.after(POLL_INTERVAL_MS, self._poll_queue)

    def _tick(self) -> None:
        try:
            if self.state.mode != "hidden":
                # 错误提示有自动隐藏时间，其他状态由识别流程显式隐藏。
                if self.state.auto_hide_at and time.time() >= self.state.auto_hide_at:
                    self._hide_now()
                else:
                    # tick 用于录音秒数和等待省略号动画。
                    self._tick_index += 1
                    self._render()
        except Exception as e:
            logger.debug(f"录音悬浮指示器刷新失败: {e}")

        if self.root:
            self.root.after(TICK_INTERVAL_MS, self._tick)

    # ------------------------------------------------------------------
    # 命令处理
    # ------------------------------------------------------------------

    def _handle_command(self, action: str, payload: dict) -> None:
        # 隐藏命令不需要创建窗口。
        if action == "hide":
            self._hide_now()
            return

        self._ensure_window()

        if action == "recording":
            # 按下快捷键时锁定锚点，避免提示框在录音过程中跳动。
            anchor, source = self._get_anchor()
            self.state = IndicatorState(
                mode="recording",
                start_time=payload["start_time"],
                shortcut_key=payload.get("shortcut_key", ""),
                anchor=anchor,
                anchor_source=source,
            )
        elif action == "waiting":
            # 松开按键后沿用录音时的锚点，并切换为等待服务端状态。
            anchor, source = self._state_anchor_or_current()
            self.state.mode = "waiting"
            self.state.recorded_duration = payload.get("recorded_duration", 0.0)
            self.state.progress_duration = 0.0
            self.state.preview_text = ""
            self.state.auto_hide_at = 0.0
            self.state.anchor = anchor
            self.state.anchor_source = source
        elif action == "processing":
            # 最终结果已到达，但还在做本地热词/LLM/上屏处理。
            self.state.mode = "processing"
            self.state.auto_hide_at = 0.0
        elif action == "progress":
            # 服务端分段识别返回时刷新已处理时长和文字预览。
            self.state.mode = "progress"
            self.state.progress_duration = payload.get("duration", 0.0)
            self.state.preview_text = payload.get("preview", "")
            self.state.auto_hide_at = 0.0
        elif action == "error":
            # 网络或发送失败时显示短暂错误提示。
            if not self.state.anchor:
                self.state.anchor, self.state.anchor_source = self._get_anchor()
            self.state.mode = "error"
            self.state.preview_text = payload.get("text", "")
            self.state.auto_hide_at = time.time() + payload.get("duration_ms", 2000) / 1000

        self._render()
        self._show_now()

    def _state_anchor_or_current(self) -> Tuple[Tuple[int, int], str]:
        # 优先沿用已有锚点，让同一次听写期间窗口位置稳定。
        if self.state.anchor:
            return self.state.anchor, self.state.anchor_source
        return self._get_anchor()

    # ------------------------------------------------------------------
    # 窗口与视觉
    # ------------------------------------------------------------------

    def _ensure_window(self) -> None:
        # 窗口存在时直接复用，避免反复创建导致闪烁。
        if self.window and self._window_exists():
            return
        if not self.root:
            return

        # Toplevel 做无边框置顶窗口，配合透明色实现圆角外区域透明。
        self.window = tk.Toplevel(self.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.8)
        self.window.configure(bg=TRANSPARENT_BG)
        try:
            self.window.attributes("-transparentcolor", TRANSPARENT_BG)
        except tk.TclError:
            # 不支持透明色时至少保持用户指定的 #F0F0F0 背景。
            self.window.configure(bg=PANEL_BG)

        # root 必须显式指向本线程的 Tk：若命名字体落到其他线程的 _default_root 上，
        # 本窗口只会拿到无效的字体名并静默回退成小号宋体
        self.title_font = tkfont.Font(root=self.root, family="Microsoft YaHei UI", size=TITLE_FONT_SIZE, weight="bold")
        self.detail_font = tkfont.Font(root=self.root, family="Microsoft YaHei UI", size=DETAIL_FONT_SIZE)
        # Canvas 负责全部绘制，确保布局由固定坐标控制。
        self.canvas = tk.Canvas(
            self.window,
            width=CANVAS_W,
            height=CANVAS_H,
            bd=0,
            highlightthickness=0,
            bg=TRANSPARENT_BG,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self._make_no_activate()
        self._apply_windows_visual_effects()

    def _make_no_activate(self) -> None:
        """尽量避免悬浮窗抢走目标输入框焦点。"""
        if not self.window:
            return
        try:
            hwnd = self.window.winfo_id()
            user32 = ctypes.windll.user32
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)

            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TOPMOST = 0x00000008
            WS_EX_NOACTIVATE = 0x08000000

            style = get_long(hwnd, GWL_EXSTYLE)
            set_long(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE)
        except Exception:
            pass

    def _apply_windows_visual_effects(self) -> None:
        """启用 Windows 11 圆角/背景材质；不支持时静默回退到 Canvas 自绘毛玻璃。"""
        if not self.window:
            return
        try:
            hwnd = self.window.winfo_id()
            dwmapi = ctypes.windll.dwmapi

            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(DWMWCP_ROUND),
                ctypes.sizeof(DWMWCP_ROUND),
            )

            DWMWA_SYSTEMBACKDROP_TYPE = 38
            DWMSBT_TRANSIENTWINDOW = ctypes.c_int(3)
            dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_SYSTEMBACKDROP_TYPE,
                ctypes.byref(DWMSBT_TRANSIENTWINDOW),
                ctypes.sizeof(DWMSBT_TRANSIENTWINDOW),
            )
        except Exception:
            pass

    def _show_now(self) -> None:
        if not self.window:
            return
        self._place_window()
        self.window.deiconify()
        self._set_topmost_without_activate()

    def _hide_now(self) -> None:
        if self.window and self._window_exists():
            self.window.withdraw()
        self.state = IndicatorState()

    def _set_topmost_without_activate(self) -> None:
        if not self.window:
            return
        try:
            hwnd = self.window.winfo_id()
            user32 = ctypes.windll.user32
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception:
            pass

    def _window_exists(self) -> bool:
        try:
            return bool(self.window and self.window.winfo_exists())
        except tk.TclError:
            return False

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def _render(self) -> None:
        # 字体对象还没初始化时不绘制，避免 Tk 初始化竞态。
        if not self.canvas or not self.title_font or not self.detail_font:
            return

        # 先生成文案，再按像素宽度截断，保证不会超出边界。
        title, detail, accent = self._compose_text()
        title = self._fit_text(title, self.title_font, TEXT_MAX_W)
        detail = self._fit_text(detail, self.detail_font, TEXT_MAX_W)

        self.canvas.configure(width=CANVAS_W, height=CANVAS_H)
        self.canvas.delete("all")

        x1, y1 = PANEL_X, PANEL_Y
        x2, y2 = PANEL_X + PANEL_W, PANEL_Y + PANEL_H

        # 扁平化毛玻璃模拟：无阴影/发光，只用浅色层制造磨砂层次。
        self._rounded_rect(x1, y1, x2, y2, RADIUS, fill=PANEL_BG, outline=BORDER)
        # 内层浅色整面叠加，模拟磨砂玻璃的层次；不画横向分隔线，避免割裂上下两行。
        self._rounded_rect(x1 + 3, y1 + 3, x2 - 3, y2 - 3, RADIUS - 3, fill=FROST_MID, outline="")

        # 圆点和标题共用 TITLE_Y 基线，保证视觉中心对齐。
        self.canvas.create_oval(
            DOT_X - DOT_R,
            TITLE_Y - DOT_R,
            DOT_X + DOT_R,
            TITLE_Y + DOT_R,
            fill=accent,
            outline=accent,
        )
        self.canvas.create_text(
            TITLE_X,
            TITLE_Y,
            text=title,
            fill=FG_TITLE,
            font=self.title_font,
            anchor="w",
        )
        self.canvas.create_text(
            TITLE_X,
            DETAIL_Y,
            text=detail,
            fill=FG_DETAIL,
            font=self.detail_font,
            anchor="w",
        )

    def _rounded_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs) -> None:
        """用单个平滑 polygon 画圆角矩形，避免内部拼接线。"""
        if not self.canvas:
            return
        # 半径不能超过宽高的一半，否则 Tk 的平滑 polygon 会产生明显变形。
        r = max(1, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        self.canvas.create_polygon(points, smooth=True, splinesteps=16, **kwargs)

    @staticmethod
    def _fit_text(text: str, font: tkfont.Font, max_width: int) -> str:
        """按像素宽度截断，确保单行文字永远不会越过面板边界。"""
        if font.measure(text) <= max_width:
            return text

        ellipsis = "..."
        if font.measure(ellipsis) > max_width:
            return ""

        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + ellipsis) <= max_width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + ellipsis

    def _compose_text(self) -> Tuple[str, str, str]:
        dots = "." * ((self._tick_index % 3) + 1)

        if self.state.mode == "recording":
            elapsed = max(0.0, time.time() - self.state.start_time)
            shortcut_name = self._format_shortcut_name(self.state.shortcut_key)
            return f"正在录音 {elapsed:.1f}s", f"松开{shortcut_name}后开始识别", ACCENT

        if self.state.mode == "waiting":
            detail = "等待服务端返回最终结果"
            if self.state.recorded_duration > 0:
                detail = f"已录音 {self.state.recorded_duration:.1f}s | {detail}"
            return f"等待识别结果{dots}", detail, ACCENT

        if self.state.mode == "progress":
            detail = "服务端正在处理音频"
            if self.state.progress_duration > 0:
                detail = f"已处理 {self.state.progress_duration:.1f}s"
            if self.state.preview_text:
                preview = self.state.preview_text.replace("\n", " ")
                detail = f"{detail} | {preview}"
            return f"识别进行中{dots}", detail, ACCENT

        if self.state.mode == "processing":
            return f"正在处理结果{dots}", "即将输出到当前窗口", ACCENT

        if self.state.mode == "error":
            return "识别未发送", self.state.preview_text or "服务端未连接", ERROR_ACCENT

        return "", "", ACCENT

    # ------------------------------------------------------------------
    # 位置
    # ------------------------------------------------------------------

    def _place_window(self) -> None:
        if not self.window or not self._window_exists():
            return

        # 位置只来自输入光标或屏幕中心靠下，不再使用焦点控件/鼠标兜底。
        anchor, source = self._state_anchor_or_current()
        width, height = CANVAS_W, CANVAS_H
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()

        if source == "center":
            # 屏幕中下兜底时居中显示，接近 Win+H 的浮层位置。
            x = anchor[0] - width // 2
            y = anchor[1]
        else:
            # 拿到输入光标时贴近光标显示，并自动避开屏幕边缘。
            x = anchor[0] + OFFSET_X
            y = anchor[1] + OFFSET_Y
            if x + width > screen_width:
                x = anchor[0] - width - OFFSET_X
            if y + height > screen_height:
                y = anchor[1] - height - OFFSET_Y

        x = max(0, min(x, screen_width - width))
        y = max(0, min(y, screen_height - height))
        self.window.geometry(f"{width}x{height}+{int(x)}+{int(y)}")

    def _get_anchor(self) -> Tuple[Tuple[int, int], str]:
        """输入光标 -> 屏幕中下。"""
        # 先尝试拿真实文本插入光标，这是最接近最终上屏位置的坐标。
        caret = self._get_caret_position()
        if caret:
            return caret, "near"

        # 拿不到输入光标时直接退回屏幕中心靠下，不再使用鼠标或焦点控件。
        return self._get_screen_center_lower_position(), "center"

    def _get_caret_position(self) -> Optional[Tuple[int, int]]:
        try:
            # GetGUIThreadInfo 能获取前台线程中的 hwndCaret 和 caret 矩形。
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None

            thread_id = user32.GetWindowThreadProcessId(hwnd, None)
            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(GUITHREADINFO)
            if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
                return None
            if not info.hwndCaret:
                return None

            point = POINT(info.rcCaret.left, info.rcCaret.bottom)
            # caret 坐标是控件客户区坐标，需要转换成屏幕坐标。
            if user32.ClientToScreen(info.hwndCaret, ctypes.byref(point)):
                return int(point.x), int(point.y)
        except Exception:
            return None
        return None

    def _get_screen_center_lower_position(self) -> Tuple[int, int]:
        # 使用当前主屏尺寸，位置约在 72% 高度处，接近 Win+H 浮层体验。
        if self.root:
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            return int(screen_width / 2), int(screen_height * 0.875)  # 这里设置兜底位置的坐标
        return 960, 760

    # ------------------------------------------------------------------
    # 文案辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _format_shortcut_name(key: str) -> str:
        """把内部按键名转成人类可读的中文名称。"""
        key = (key or "").strip().lower()
        names = {
            "ctrl_l": "左 Ctrl 键",
            "ctrl_r": "右 Ctrl 键",
            "shift": "左 Shift 键",
            "shift_l": "左 Shift 键",
            "shift_r": "右 Shift 键",
            "alt_l": "左 Alt 键",
            "alt_r": "右 Alt 键",
            "alt_gr": "右 Alt 键",
            "cmd": "Windows 键",
            "cmd_l": "左 Windows 键",
            "cmd_r": "右 Windows 键",
            "caps_lock": "大写锁定键",
            "num_lock": "数字锁定键",
            "scroll_lock": "滚动锁定键",
            "space": "空格键",
            "enter": "回车键",
            "tab": "Tab 键",
            "backspace": "退格键",
            "delete": "Delete 键",
            "insert": "Insert 键",
            "esc": "Esc 键",
            "home": "Home 键",
            "end": "End 键",
            "page_up": "Page Up 键",
            "page_down": "Page Down 键",
            "up": "上方向键",
            "down": "下方向键",
            "left": "左方向键",
            "right": "右方向键",
            "x1": "鼠标侧键 1",
            "x2": "鼠标侧键 2",
        }
        if key in names:
            return names[key]
        if len(key) == 1 and key.isalpha():
            return f"{key.upper()} 键"
        if len(key) == 1 and key.isdigit():
            return f"数字 {key} 键"
        if key.startswith("f") and key[1:].isdigit():
            return f"F{key[1:]} 键"
        if key:
            return f"{key.replace('_', ' ')} 键"
        return "快捷键"


def show_recording_indicator(start_time: Optional[float] = None, shortcut_key: str = "") -> None:
    RecordingIndicator().show_recording(start_time, shortcut_key)


def show_waiting_indicator(recorded_duration: Optional[float] = None) -> None:
    RecordingIndicator().show_waiting(recorded_duration)


def show_processing_indicator() -> None:
    RecordingIndicator().show_processing()


def update_recording_indicator_progress(duration: Optional[float] = None, preview: str = "") -> None:
    RecordingIndicator().update_progress(duration, preview)


def show_recording_indicator_error(text: str, duration_ms: int = 2000) -> None:
    RecordingIndicator().show_error(text, duration_ms)


def hide_recording_indicator() -> None:
    RecordingIndicator().hide()
