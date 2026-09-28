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
# 手动打断是用户主动行为而非故障，用提亮的浅蓝而不是错误红。
CANCEL_ACCENT = "#7FB9E8"

# 连续对话的面板配色。只换底色不换圆点：底色已经是足够强的状态指示，
# 圆点再跟着变会让同一个信息重复两遍。
# 浅黄 = 本次录音正在改变连续对话状态（进入或退出，松手才生效，再按一次可反悔）。
CONTINUOUS_PENDING_BG = "#FFF8E1"
CONTINUOUS_PENDING_FROST = "#FFFCF0"
CONTINUOUS_PENDING_BORDER = "#E8DCB0"
# 浅绿 = 已处在连续对话中。这是防「忘了自己在连续对话里」的主要视觉信号。
CONTINUOUS_ACTIVE_BG = "#E8F5E9"
CONTINUOUS_ACTIVE_FROST = "#F1FAF2"
CONTINUOUS_ACTIVE_BORDER = "#BFDFC4"

# 本次录音挂着待上传的图片：在面板内侧单独画一圈描边，与连续对话的底色
# 完全正交，两个信号可以同时出现（例：浅绿底 + 彩圈）。
# 这是视觉输入唯一的持续可见信号，刻意不弹窗：它出现在唯一还来得及补救的时刻
# （录音刚开始、话还没说）。它只回答「这次带了图」，回答不了「带的是哪张」。
IMAGE_BORDER = "#3D75E6"
# 挂图标记单独画一圈内嵌描边，不能只把最外层 outline 换色：那圈是 Tk 默认的
# 1 物理像素，而面板尺寸是物理像素、字体是磅值，200% 缩放下它细到几乎看不见
# （2026-09-04 实机反馈「看不出特殊效果」的根因）。内嵌避开了 Tk 描边以路径为
# 中心、外侧一半会被窗口裁掉的问题，画在磨砂层之上、文字之下，不影响任何布局。
IMAGE_BORDER_WIDTH = 6
IMAGE_BORDER_INSET = 2

# 面板尺寸：布局仍是像素级固定，只有宽度会按标题内容加宽。
# PANEL_W 是最小宽度而非固定宽度——面板宽度是物理像素、不随 DPI 放大，而字体是
# 磅值、会随 DPI 放大，200% 缩放下 450px 的盒子只有设计意图的一半宽，长标题必然溢出。
# 降字号治不了（要降到比详情行还小）；整个面板按 DPI 等比放大才是根因修法，但会让
# 悬浮框物理尺寸翻倍，暂不做。
MARGIN = 0
PANEL_W = 450
PANEL_H = 140
CANVAS_W = PANEL_W + MARGIN * 2
CANVAS_H = PANEL_H + MARGIN * 2
RADIUS = 34
DOT_R = 6

# 加宽上限占屏幕宽度的比例，超过就交给 _fit_text 截断兜底。
PANEL_MAX_W_RATIO = 0.9
# 文字右侧留白。
PADDING_RIGHT = 32

TITLE_FONT_SIZE     = 14
DETAIL_FONT_SIZE    = 10

# Canvas 内部布局坐标。TITLE_Y 和 DETAIL_Y 是两行文字的基线位置。
PANEL_X = MARGIN
PANEL_Y = MARGIN
DOT_X = PANEL_X + 38
TITLE_X = DOT_X + 24
TITLE_Y = PANEL_Y + 45
DETAIL_Y = PANEL_Y + 99
# 最小宽度下的可用文字宽度；实际每次渲染按当前面板宽度重算（_text_max_w）。
TEXT_MAX_W = PANEL_W - (TITLE_X - PANEL_X) - PADDING_RIGHT


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

    # hidden / recording / waiting / progress / processing / cancelled / error
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
    # 触发手动打断的按键名，同样渲染前转成中文名称。
    cancel_key: str = ""
    # 连续对话显示态：""（无）/ armed（即将进入）/ active（进行中）/ exited（已退出）。
    # 仅在 mode == "recording" 时参与渲染。
    continuous_mode: str = ""
    # 连续对话的粘滞角色名，仅 continuous_mode == "active" 时用于文案。
    continuous_role: str = ""
    # 本次录音是否绑定了待上传的图片，只影响最外圈描边颜色。
    has_image: bool = False
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

        # 当前面板宽度。按标题内容可增可减（见 _fit_panel_to_title），每次新录音复位。
        self._panel_w = PANEL_W

    # ------------------------------------------------------------------
    # 对外命令
    # ------------------------------------------------------------------

    def show_recording(
        self,
        start_time: Optional[float] = None,
        shortcut_key: str = "",
        continuous_mode: str = "",
        continuous_role: str = "",
        has_image: bool = False,
    ) -> None:
        """显示录音状态。"""
        self._post(
            "recording",
            start_time=start_time or time.time(),
            shortcut_key=shortcut_key,
            continuous_mode=continuous_mode,
            continuous_role=continuous_role,
            has_image=has_image,
        )

    def update_continuous(self, mode: str = "", role: str = "") -> None:
        """录音过程中切换连续对话显示态。

        必须是独立命令而不是重发 show_recording：后者会重建 IndicatorState，
        把 start_time 一并刷新，录音计时会被清零。
        """
        self._post("continuous", mode=mode, role=role)

    def show_waiting(self, recorded_duration: Optional[float] = None) -> None:
        """显示等待服务端返回结果状态。"""
        self._post("waiting", recorded_duration=recorded_duration or 0.0)

    def show_processing(self) -> None:
        """显示本地处理最终结果状态。"""
        self._post("processing")

    def update_progress(self, duration: Optional[float] = None, preview: str = "") -> None:
        """更新服务端已返回的识别进度。"""
        self._post("progress", duration=duration or 0.0, preview=preview or "")

    def show_cancelled(self, cancel_key: str = "", duration_ms: int = 1200) -> None:
        """显示手动打断状态，短暂停留后自动隐藏。"""
        self._post("cancelled", cancel_key=cancel_key, duration_ms=duration_ms)

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

        # 连续对话显示态只在录音期间有意义。松手后到达的切换命令直接丢弃，
        # 否则会把已经进入等待状态的提示框又拉回录音态。
        if action == "continuous" and self.state.mode != "recording":
            return

        self._ensure_window()

        if action == "recording":
            # 新一次听写开始，面板宽度复位，避免上一句的长角色名一直把窗口撑着。
            self._panel_w = PANEL_W
            # 按下快捷键时锁定锚点，避免提示框在录音过程中跳动。
            anchor, source = self._get_anchor()
            self.state = IndicatorState(
                mode="recording",
                start_time=payload["start_time"],
                shortcut_key=payload.get("shortcut_key", ""),
                continuous_mode=payload.get("continuous_mode", ""),
                continuous_role=payload.get("continuous_role", ""),
                has_image=payload.get("has_image", False),
                anchor=anchor,
                anchor_source=source,
            )
        elif action == "continuous":
            # 只改连续对话相关字段，start_time 和锚点保持不动。
            self.state.continuous_mode = payload.get("mode", "")
            self.state.continuous_role = payload.get("role", "")
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
            # 录音仍在进行时只记录数据、不切换模式：麦克风是边录边发的，服务端每攒够
            # mic_seg_duration + mic_seg_overlap * 2（默认 68s）才切一段回传一次进度，
            # 切到 progress 会让实时录音计时被一个一分钟内都不再变化的「已处理」时长顶掉。
            if self.state.mode != "recording":
                self.state.mode = "progress"
            self.state.progress_duration = payload.get("duration", 0.0)
            self.state.preview_text = payload.get("preview", "")
            self.state.auto_hide_at = 0.0
        elif action == "cancelled":
            # 手动打断：沿用当前锚点让提示原地换文案，短暂停留后自动隐藏。
            if not self.state.anchor:
                self.state.anchor, self.state.anchor_source = self._get_anchor()
            self.state.mode = "cancelled"
            self.state.cancel_key = payload.get("cancel_key", "")
            self.state.progress_duration = 0.0
            self.state.preview_text = ""
            self.state.auto_hide_at = time.time() + payload.get("duration_ms", 1200) / 1000
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
        self._panel_w = PANEL_W

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

        # 先生成文案，把面板调整到刚好容纳标题，再按像素宽度截断兜底。
        title, detail, accent = self._compose_text()
        stable_title, _, _ = self._compose_text(stable=True)
        resized = self._fit_panel_to_title(stable_title)
        panel_w = self._panel_w
        max_w = self._text_max_w(panel_w)
        title = self._fit_text(title, self.title_font, max_w)
        detail = self._fit_text(detail, self.detail_font, max_w)

        self.canvas.configure(width=panel_w + MARGIN * 2, height=CANVAS_H)
        self.canvas.delete("all")

        x1, y1 = PANEL_X, PANEL_Y
        x2, y2 = PANEL_X + panel_w, PANEL_Y + PANEL_H

        panel_bg, frost_mid, border = self._panel_colors()

        # 扁平化毛玻璃模拟：无阴影/发光，只用浅色层制造磨砂层次。
        self._rounded_rect(x1, y1, x2, y2, RADIUS, fill=panel_bg, outline=border)
        # 内层浅色整面叠加，模拟磨砂玻璃的层次；不画横向分隔线，避免割裂上下两行。
        self._rounded_rect(x1 + 3, y1 + 3, x2 - 3, y2 - 3, RADIUS - 3, fill=frost_mid, outline="")

        # 本次录音挂着待上传的图片：内嵌一圈描边（颜色/粗细/内嵌量见文件头常量）
        if self.state.mode == "recording" and self.state.has_image:
            inset = IMAGE_BORDER_INSET
            self._rounded_rect(
                x1 + inset, y1 + inset, x2 - inset, y2 - inset, RADIUS - inset,
                fill="", outline=IMAGE_BORDER, width=IMAGE_BORDER_WIDTH,
            )

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

        # 宽度变了必须重排窗口：_tick 每 100ms 只调 _render，
        # 光把画布配宽而不改 wm geometry，多出来的部分是看不见的。
        if resized:
            self._place_window()

    @staticmethod
    def _text_max_w(panel_w: int) -> int:
        """给定面板宽度时，单行文字可用的像素宽度。"""
        return panel_w - (TITLE_X - PANEL_X) - PADDING_RIGHT

    def _fit_panel_to_title(self, stable_title: str) -> bool:
        """把面板宽度调整到刚好容纳标题，返回宽度是否变化。

        只让标题参与测宽：progress 状态的详情行是「已处理 xxs | 识别文本预览」，
        预览文本可以任意长，让它驱动宽度会把面板撑到横跨整个屏幕。

        宽度可增可减，但**必须用 stable 版标题测量**：省略号动画每 100ms 变一次、
        实测差 22px，拿动画帧测宽会让窗口每秒抖十次。秒数不需要特殊处理——
        字体里数字等宽（各 23px），且同一次录音内只增不减。
        """
        if not self.title_font:
            return False

        needed = (TITLE_X - PANEL_X) + self.title_font.measure(stable_title) + PADDING_RIGHT

        cap = PANEL_W
        try:
            if self.window:
                cap = max(PANEL_W, int(self.window.winfo_screenwidth() * PANEL_MAX_W_RATIO))
        except tk.TclError:
            pass

        new_w = max(PANEL_W, min(needed, cap))
        if new_w == self._panel_w:
            return False

        self._panel_w = new_w
        return True

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

    def _panel_colors(self) -> Tuple[str, str, str]:
        """按连续对话状态选择面板配色，返回 (主背景, 内层磨砂, 边框)。

        只有录音阶段染色：等待/进度/处理/打断/错误描述的是识别流水线状态，
        与连续对话正交，染上去反而会让「哪个状态」变得难读。
        """
        if self.state.mode == "recording":
            mode = self.state.continuous_mode
            if mode in ("armed", "exited"):
                bg, frost, border = CONTINUOUS_PENDING_BG, CONTINUOUS_PENDING_FROST, CONTINUOUS_PENDING_BORDER
            elif mode == "active" and self.state.continuous_role:
                bg, frost, border = CONTINUOUS_ACTIVE_BG, CONTINUOUS_ACTIVE_FROST, CONTINUOUS_ACTIVE_BORDER
            else:
                bg, frost, border = PANEL_BG, FROST_MID, BORDER
            return bg, frost, border
        return PANEL_BG, FROST_MID, BORDER

    def _compose_text(self, stable: bool = False) -> Tuple[str, str, str]:
        """生成当前状态的文案。

        Args:
            stable: 用于测量面板宽度时置 True，把省略号动画固定成最长的三个点。
                    否则窗口宽度会跟着每 100ms 一次的动画脉动（实测 22px）。
        """
        dots = "..." if stable else "." * ((self._tick_index % 3) + 1)

        if self.state.mode == "recording":
            elapsed = max(0.0, time.time() - self.state.start_time)
            shortcut_name = self._format_shortcut_name(self.state.shortcut_key)
            detail = f"松开{shortcut_name}后开始识别"
            mode = self.state.continuous_mode
            if mode == "armed":
                return f"即将开始连续对话 {elapsed:.1f}s", detail, ACCENT
            if mode == "active" and self.state.continuous_role:
                # 角色名加引号，避免「正在与助理连续对话」读起来断不开词
                return f"正在与“{self.state.continuous_role}”连续对话 {elapsed:.1f}s", detail, ACCENT
            if mode == "exited":
                # 按下切换键的那一刻这句话就已经改回正常角色检测了，所以是「已结束」
                return f"已结束连续对话 {elapsed:.1f}s", detail, ACCENT
            return f"正在录音 {elapsed:.1f}s", detail, ACCENT

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

        if self.state.mode == "cancelled":
            cancel_name = self._format_shortcut_name(self.state.cancel_key)
            return f"已按 {cancel_name}手动打断", "识别结果已丢弃", CANCEL_ACCENT

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
        width, height = self._panel_w + MARGIN * 2, CANVAS_H
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
            "tab": "制表键",
            "backspace": "退格键",
            "delete": "删除键",
            "insert": "插入键",
            "esc": "退出键",
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


def show_recording_indicator(
    start_time: Optional[float] = None,
    shortcut_key: str = "",
    continuous_mode: str = "",
    continuous_role: str = "",
    has_image: bool = False,
) -> None:
    RecordingIndicator().show_recording(
        start_time, shortcut_key, continuous_mode, continuous_role, has_image
    )


def update_recording_indicator_continuous(mode: str = "", role: str = "") -> None:
    RecordingIndicator().update_continuous(mode, role)


def show_waiting_indicator(recorded_duration: Optional[float] = None) -> None:
    RecordingIndicator().show_waiting(recorded_duration)


def show_processing_indicator() -> None:
    RecordingIndicator().show_processing()


def update_recording_indicator_progress(duration: Optional[float] = None, preview: str = "") -> None:
    RecordingIndicator().update_progress(duration, preview)


def show_cancelled_indicator(cancel_key: str = "", duration_ms: int = 1200) -> None:
    RecordingIndicator().show_cancelled(cancel_key, duration_ms)


def show_recording_indicator_error(text: str, duration_ms: int = 2000) -> None:
    RecordingIndicator().show_error(text, duration_ms)


def hide_recording_indicator() -> None:
    RecordingIndicator().hide()
