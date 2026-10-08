"""
Toast 窗口基础模块

提供 Toast 窗口的抽象基类和通用工具函数。
"""
import logging
import math
import tkinter as tk
from tkinter import font
from typing import Optional, Callable, Any, Tuple
from abc import ABC, abstractmethod

import markdown
from tkhtmlview import HTMLLabel

from .toast_constants import (
    DEFAULT_FONT_FAMILY,
    DEFAULT_PADDING_X,
    DEFAULT_PADDING_TOP,
    DEFAULT_PADDING_BOTTOM,
    COMPACT_PADDING_TOP,
    DEFAULT_MIN_WIDTH_PX,
    DEFAULT_MAX_WIDTH_PX,
    MIN_WINDOW_HEIGHT,
    MARKDOWN_MIN_HEIGHT,
    SCROLL_STEP,
    DESTROY_DELAY_MS,
    SMART_DURATION_SCALE,
    SMART_DURATION_CAP_MS,
)
from . import logger
from .dpi import ensure_dpi_awareness

TRANSPARENT_COLOR = "#010203"

# DPI 感知：统一由 core/ui/dpi.py 声明，避免多处各自调用时「谁先导入谁生效」
ensure_dpi_awareness()


# ============================================================
# 工具函数
# ============================================================

def add_zero_width_for_chinese(text: str) -> str:
    """在中文字符后添加零宽空格，强制 Label 按字符换行。
    
    这样可以避免中英混合时，Label 的单词边界换行导致不均匀。
    
    Args:
        text: 原始文本
        
    Returns:
        处理后的文本，每个中文字符后都添加了零宽空格
    """
    result = []
    for char in text:
        result.append(char)
        # 在中文字符（及全角字符）后插入零宽空格
        if ord(char) > 127:
            result.append('\u200B')  # 零宽空格
    return ''.join(result)


# ============================================================
# Toast 窗口抽象基类
# ============================================================

class ToastWindowBase(ABC):
    """Toast 窗口抽象基类
    
    提供窗口创建、拖动、鼠标事件处理、定时销毁等通用功能。
    子类需要实现 update_text 和 _destroy_content_widget 方法。
    
    Attributes:
        window: tkinter Toplevel 窗口对象
        streaming: 是否为流式输出模式
        markdown: 是否启用 Markdown 渲染
        full_text: 完整的文本内容
    """

    def __init__(
        self,
        parent_root: tk.Tk,
        text: str,
        font_size: int,
        font_family: str,
        bg: str,
        fg: str,
        duration: int,
        min_width_px: Any,
        max_width_px: Any,
        min_width_ratio: Any,
        max_width_ratio: Any,
        initial_height: int,
        streaming: bool,
        stop_callback: Optional[Callable[[], None]],
        markdown_enabled: bool,
        editable: bool = False,
        window_alpha: float = 1.0,
        corner_radius: int = 0,
        smart_duration: bool = False
    ) -> None:
        """初始化 Toast 窗口基类
        
        Args:
            parent_root: 父窗口（Tk 主窗口）
            text: 初始文本内容
            font_size: 字体大小（Tk 磅值，随 DPI 缩放）
            font_family: 字体名称，空字符串使用默认字体
            bg: 背景颜色
            fg: 前景色（文字颜色）
            duration: 自动关闭时长（毫秒）
            min_width_px: 最小宽度像素值，合法时优先于比例值
            max_width_px: 最大宽度像素值，合法时优先于比例值
            min_width_ratio: 像素值无效时使用的最小屏幕宽度比例
            max_width_ratio: 像素值无效时使用的最大屏幕宽度比例
            initial_height: 初始高度，0 表示自动计算
            streaming: 是否为流式输出模式
            stop_callback: 窗口关闭时的回调函数
            markdown_enabled: 是否启用 Markdown 渲染
            editable: Markdown 渲染后是否允许编辑
            window_alpha: 窗口透明度，1.0 表示不透明
            corner_radius: 窗口圆角半径，0 表示不启用圆角
            smart_duration: 智能时长模式（LLM 角色白色对话框）。时长按文本字数计算；
                悬停不暂停倒计时，到期后在鼠标移开时关闭；拖拽后永不自动关闭。
                False 时保持旧的固定时长 + 悬停重置行为
        """
        # 保存基本属性
        self.parent_root = parent_root
        self.stop_callback = stop_callback
        self.streaming = streaming
        self.markdown = markdown_enabled
        self.editable = editable
        self.duration = duration
        self.initial_height = initial_height
        self.window_alpha = max(0.2, min(float(window_alpha), 1.0))
        self.corner_radius = max(0, int(corner_radius))
        
        # 状态标志
        self.pause = False
        self._escape_armed = False
        self.mouse_inside = False
        self.timer_id: Optional[str] = None

        # 智能时长模式（LLM 角色白色对话框）：时长随字数、悬停不延长截止时间、
        # 拖拽永不自动关闭。
        # 普通 toast 为 False，完全保持旧的固定时长 + 悬停取消/移开重置行为。
        self.pause_and_resume = smart_duration
        self._timer_expired = False                  # 截止时间已到；悬停时延迟到移开再关闭
        self._never_auto_close = False               # 拖拽后置 True，窗口转为永不自动关闭

        # 顶部留白按窗口种类取值。smart_duration 在本项目里事实上等同于「这是 LLM 角色
        # 白色对话框」（close_llm_windows 同样拿它做判别），只有这类窗口需要 40px 纯空白
        # 拖动抓手；托盘/错误这类几秒即逝的提示不可拖拽也无需抓手，顶部与底部对齐即可。
        # 复用这个标志而不另加字段：同一个概念有两个开关迟早对不齐。代价是「智能时长」
        # 与「顶部拖动区」绑在一起，真出现只要其一的窗口种类时得先把标志拆成两个。
        # height_padding 是上下留白之和，供子类按行数反推窗口高度
        self.padding_top = DEFAULT_PADDING_TOP if smart_duration else COMPACT_PADDING_TOP
        self.height_padding = self.padding_top + DEFAULT_PADDING_BOTTOM
        
        # 拖动锚点：按下时同时记录指针屏幕坐标和窗口左上角，
        # 拖动中用「指针位移 + 起始窗口位置」计算，避免逐帧累加误差
        self._dragging_from_text_blank = False
        self._user_dragged = False
        self._drag_pointer_x = 0
        self._drag_pointer_y = 0
        self._drag_window_x = 0
        self._drag_window_y = 0
        
        # 保存完整文本和样式配置（用于 Markdown 渲染）
        self.full_text = text
        self.font_size = font_size
        self.font_family = self._resolve_font_family(font_family)
        self.bg = bg
        self.fg = fg
        
        # 创建窗口
        self.window = tk.Toplevel(parent_root)
        self.window.hang_on = False
        self.content_canvas: Optional[tk.Canvas] = None
        self._content_window_item: Optional[int] = None
        self._background_item: Optional[int] = None
        
        # 设置窗口属性
        self.window.overrideredirect(True)  # 无边框模式
        self.window.attributes('-topmost', True)  # 保持置顶
        self.window.attributes('-alpha', self.window_alpha)
        self.window.configure(bg=TRANSPARENT_COLOR if self.corner_radius > 0 else bg)
        self.window.resizable(True, True)
        self.window.pack_propagate(False)
        self._init_content_host()

        screen_width = self.window.winfo_screenwidth()
        self.min_width, self.max_width = self._resolve_width_range(
            min_width_px,
            max_width_px,
            min_width_ratio,
            max_width_ratio,
            screen_width,
        )
        self.current_width = self.min_width
        
        # 绑定通用事件
        self._bind_common_events()
        
        # 显示窗口
        self.window.deiconify()
        
        # 如果不是流式模式，设置定时销毁；智能时长模式用字数计算时长
        if not self.streaming:
            if self.pause_and_resume:
                self._start_smart_destroy_timer()
            else:
                self._start_destroy_timer()

    def _bind_common_events(self) -> None:
        """绑定通用事件（拖动、鼠标进入/离开、滚轮、ESC、复制）"""
        self.window.bind('<ButtonPress-1>', self._on_drag_start)
        self.window.bind('<ButtonRelease-1>', self._on_drag_stop)
        self.window.bind('<B1-Motion>', self._on_drag_motion)
        self.window.bind('<Escape>', self._on_escape)
        self.window.bind('<Enter>', self._on_mouse_enter)
        self.window.bind('<Leave>', self._on_mouse_leave)
        self.window.bind('<MouseWheel>', self._on_mouse_wheel)
        self.window.bind('<Button-4>', self._on_mouse_wheel)  # Linux 向上滚动
        self.window.bind('<Button-5>', self._on_mouse_wheel)  # Linux 向下滚动
        self.window.bind('<Control-c>', self._on_copy)        # Ctrl+C 复制

    def _has_keyboard_focus(self) -> bool:
        focused = self.window.focus_displayof()
        return focused is not None and focused.winfo_toplevel() == self.window

    def _focus_from_click(self, event: tk.Event) -> None:
        if self.pause_and_resume:
            self._escape_armed = True
            event.widget.focus_set()

    def _on_escape(self, event: tk.Event) -> str:
        # 角色白框必须由用户点过，且此刻焦点仍在本窗口；新窗口不继承旧窗口的武装状态。
        if self.pause_and_resume and (not self._escape_armed or not self._has_keyboard_focus()):
            return 'break'
        self._destroy_window(event)
        return 'break'

    @staticmethod
    def _valid_number(value: Any) -> Optional[float]:
        """解析有限正数；bool 虽是 int 子类，但不接受为尺寸配置。"""
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @classmethod
    def _resolve_width_bound(
        cls,
        pixel_value: Any,
        ratio_value: Any,
        screen_width: int,
        fallback: int,
    ) -> int:
        """按“像素 → 屏幕比例 → 内置像素值”的顺序解析宽度边界。"""
        pixel = cls._valid_number(pixel_value)
        if pixel is not None and pixel >= 1:
            return int(pixel)

        ratio = cls._valid_number(ratio_value)
        if ratio is not None and ratio <= 1:
            return max(1, int(screen_width * ratio))

        return fallback

    @classmethod
    def _resolve_width_range(
        cls,
        min_width_px: Any,
        max_width_px: Any,
        min_width_ratio: Any,
        max_width_ratio: Any,
        screen_width: int,
    ) -> Tuple[int, int]:
        """解析最小/最大宽度；反向区间整体回退，等宽区间允许固定宽度。"""
        min_width = cls._resolve_width_bound(
            min_width_px, min_width_ratio, screen_width, DEFAULT_MIN_WIDTH_PX
        )
        max_width = cls._resolve_width_bound(
            max_width_px, max_width_ratio, screen_width, DEFAULT_MAX_WIDTH_PX
        )
        if min_width > max_width:
            logger.warning(
                "Toast 最小宽度大于最大宽度，已回退到 "
                f"{DEFAULT_MIN_WIDTH_PX}～{DEFAULT_MAX_WIDTH_PX}px"
            )
            return DEFAULT_MIN_WIDTH_PX, DEFAULT_MAX_WIDTH_PX
        return min_width, max_width

    @staticmethod
    def _invert_color(hex_color: str) -> str:
        """计算十六进制颜色的反色
        
        Args:
            hex_color: 十六进制颜色值（如 '#075077'）
            
        Returns:
            反色的十六进制颜色值
        """
        # 去掉 # 前缀
        hex_color = hex_color.lstrip('#')
        # 解析 RGB 值
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        # 计算反色
        inv_r = 255 - r
        inv_g = 255 - g
        inv_b = 255 - b
        return f"#{inv_r:02x}{inv_g:02x}{inv_b:02x}"

    @staticmethod
    def _resolve_font_family(font_family: str) -> str:
        """把用户可读的中文字体名映射为 Tk 在 Windows 上更稳定识别的字体名。"""
        if not font_family:
            return DEFAULT_FONT_FAMILY

        aliases = {
            "微软雅黑": "Microsoft YaHei UI",
            "Microsoft YaHei": "Microsoft YaHei UI",
        }
        return aliases.get(font_family, font_family)

    def _init_content_host(self) -> None:
        """创建 Canvas 内容宿主，为顶部空白拖动区和圆角背景提供统一布局。"""
        if self.corner_radius > 0:
            try:
                self.window.attributes('-transparentcolor', TRANSPARENT_COLOR)
            except tk.TclError as e:
                logger.debug(f"当前 Tk 环境不支持 transparentcolor: {e}")

        self.content_canvas = tk.Canvas(
            self.window,
            bg=TRANSPARENT_COLOR if self.corner_radius > 0 else self.bg,
            highlightthickness=0,
            bd=0
        )
        self.content_canvas.pack(fill=tk.BOTH, expand=True)
        self._bind_drag_surface(self.content_canvas)

    def _get_content_parent(self) -> tk.Misc:
        """获取内容控件父级；圆角窗口使用 Canvas 承载内容控件。"""
        return self.content_canvas if self.content_canvas is not None else self.window

    def _mount_content_widget(self, widget: tk.Widget) -> None:
        """挂载内容控件；圆角模式下在 Canvas 内留出可拖动边距。"""
        if self.content_canvas is None:
            widget.pack(side=tk.TOP, anchor='nw', fill=tk.BOTH, expand=True)
            return

        if self._content_window_item is not None:
            self.content_canvas.delete(self._content_window_item)

        self._content_window_item = self.content_canvas.create_window(
            DEFAULT_PADDING_X,
            self.padding_top,
            anchor='nw',
            window=widget,
        )
        self._apply_window_shape()

    def _bind_drag_surface(self, widget: tk.Widget) -> None:
        """让非文本区域可以拖动窗口。"""
        widget.bind('<ButtonPress-1>', self._on_drag_start)
        widget.bind('<ButtonRelease-1>', self._on_drag_stop)
        widget.bind('<B1-Motion>', self._on_drag_motion)
        widget.bind('<Enter>', self._on_mouse_enter)
        widget.bind('<Leave>', self._on_mouse_leave)
        widget.bind('<MouseWheel>', self._on_mouse_wheel)
        widget.bind('<Button-4>', self._on_mouse_wheel)
        widget.bind('<Button-5>', self._on_mouse_wheel)

    def _apply_window_shape(self) -> None:
        """按当前窗口尺寸刷新圆角背景和内容控件尺寸。

        通过透明色 Canvas 绘制圆角矩形，不使用 SetWindowRgn，避免圆角处出现黑底。
        """
        if self.content_canvas is None:
            return

        try:
            self.window.update_idletasks()
            width = self.window.winfo_width()
            height = self.window.winfo_height()
            if width <= 1 or height <= 1:
                return

            self.content_canvas.config(width=width, height=height)
            if self.corner_radius > 0:
                self.content_canvas.delete(self._background_item)

                radius = max(1, min(self.corner_radius, width // 2, height // 2))
                points = [
                    radius, 0, width - radius, 0,
                    width, 0, width, radius,
                    width, height - radius, width, height,
                    width - radius, height, radius, height,
                    0, height, 0, height - radius,
                    0, radius, 0, 0,
                ]
                self._background_item = self.content_canvas.create_polygon(
                    points,
                    smooth=True,
                    splinesteps=18,
                    fill=self.bg,
                    outline="",
                )
                self.content_canvas.tag_lower(self._background_item)

            if self._content_window_item is not None:
                content_width = max(1, width - DEFAULT_PADDING_X * 2)
                content_height = max(
                    1, height - self.height_padding
                )
                self.content_canvas.coords(
                    self._content_window_item,
                    DEFAULT_PADDING_X,
                    self.padding_top,
                )
                self.content_canvas.itemconfigure(
                    self._content_window_item,
                    width=content_width,
                    height=content_height,
                )
        except tk.TclError as e:
            logger.debug(f"应用 Toast 圆角失败: {e}")

    def _measure_text_width(self, text: str, text_font: font.Font) -> int:
        """测量最长可视行需要的总窗口宽度。"""
        lines = text.splitlines() or ['']
        content_width = max(text_font.measure(line.expandtabs(4)) for line in lines)
        return content_width + DEFAULT_PADDING_X * 2

    def _on_width_changed(self, _new_width: int) -> None:
        """宽度变化钩子，供需要显式设置换行宽度的子类覆盖。"""

    # 窗口位置/尺寸一律经过这两个方法。几何字符串必须写成 f"+{x}+{y}"，负坐标得到
    # "+-100+50"：wm geometry 把 x、y 前面的 +/- 当作「从左上边缘量」还是「从右下边缘
    # 量」的标志位消费掉，其后的数字再按有符号数解析。f"{x:+d}" 对负数产出的
    # "-100+50" 会被理解成「距屏幕右边缘 100px」，窗口拖出左/上边界时会跳到另一侧。
    def _set_geometry(self, width: int, height: int, x: int, y: int) -> None:
        """设置尺寸和位置。"""
        self.window.geometry(
            f"{int(width)}x{int(height)}+{int(x)}+{int(y)}"
        )

    def _set_position(self, x: int, y: int) -> None:
        """只设置位置。"""
        try:
            self.window.geometry(f"+{int(x)}+{int(y)}")
        except tk.TclError:
            # 窗口已被销毁（如被新 LLM 回复顶掉时用户仍在拖动，B1-Motion 仍会到达），忽略
            pass

    def _grow_to_fit_text(self, text: str, text_font: font.Font) -> bool:
        """按文本需要增宽窗口；窗口生命周期内只增不减。"""
        current_width = max(self.current_width, self.window.winfo_width())
        # 已经顶到最大宽度就不必再测量：流式输出每来一个分片都会调用这里，
        # 而测量成本随全文长度线性增长，不早退会退化成 O(n²)。
        if current_width >= self.max_width:
            return False

        desired_width = min(
            self.max_width,
            max(self.min_width, self._measure_text_width(text, text_font)),
        )
        if desired_width <= current_width:
            return False

        current_x = self.window.winfo_x()
        current_y = self.window.winfo_y()
        current_height = self.window.winfo_height()
        if self._user_dragged:
            new_x = current_x
        else:
            new_x = int(current_x + (current_width - desired_width) / 2)

        self.current_width = desired_width
        self._set_geometry(desired_width, current_height, new_x, current_y)
        self._apply_window_shape()
        self._on_width_changed(desired_width)
        return True

    def _bind_selectable_text_widget(self, widget: tk.Widget) -> None:
        """让文本控件保留选择/复制能力，同时阻止事件继续触发窗口拖动。"""
        # bindtag 不能用控件路径（如 ".!toplevel.!text"）拼接；
        # 以 "." 开头会被 Tk 当成窗口路径，导致创建 Toast 时抛出 bad window path name。
        tag = f"toast_selectable_{id(widget)}"
        bindtags = widget.bindtags()
        if tag not in bindtags:
            insert_at = 2 if len(bindtags) > 1 else 1
            widget.bindtags(bindtags[:insert_at] + (tag,) + bindtags[insert_at:])

        widget.bind_class(tag, '<ButtonPress-1>', self._on_selectable_press)
        widget.bind_class(tag, '<B1-Motion>', self._on_selectable_motion)
        widget.bind_class(tag, '<ButtonRelease-1>', self._on_selectable_release)
        widget.bind_class(tag, '<Control-c>', self._on_copy)

    def _on_selectable_press(self, event: tk.Event) -> str:
        """文字上拖动选择文本；文本控件空白处拖动窗口。"""
        self._focus_from_click(event)
        self._dragging_from_text_blank = not self._is_text_hit(event)
        if self._dragging_from_text_blank:
            self._on_drag_start(event)
        return "break"

    def _on_selectable_motion(self, event: tk.Event) -> str:
        """根据按下位置决定是选择文字还是移动窗口。"""
        if self._dragging_from_text_blank:
            self._on_drag_motion(event)
        return "break"

    def _on_selectable_release(self, event: tk.Event) -> str:
        """结束文本空白处窗口拖动。"""
        if self._dragging_from_text_blank:
            self._on_drag_stop(event)
        self._dragging_from_text_blank = False
        return "break"

    def _is_text_hit(self, event: tk.Event) -> bool:
        """判断鼠标是否点在真实文字字符上。"""
        widget = event.widget
        try:
            index = widget.index(f"@{event.x},{event.y}")
            line = index.split(".", 1)[0]
            line_end = f"{line}.end"
            if widget.compare(index, ">=", line_end):
                return False
            return widget.bbox(index) is not None
        except tk.TclError:
            return False

    # --------------------------------------------------------
    # 鼠标事件处理
    # --------------------------------------------------------

    def _on_mouse_enter(self, event: tk.Event) -> None:
        """鼠标进入窗口；LLM Toast 保持绝对截止时间不变。"""
        self.mouse_inside = True
        if self.timer_id and not self.pause_and_resume:
            self.window.after_cancel(self.timer_id)
            self.timer_id = None

    def _on_mouse_leave(self, event: tk.Event) -> None:
        """鼠标离开窗口；已到期的 LLM Toast 在确认真正移出后关闭。"""
        self.mouse_inside = False
        if self.pause_and_resume:
            if self._timer_expired and not self._never_auto_close:
                # Toplevel 与子控件都绑定了 Enter/Leave；在内部控件之间移动时 Tk 会先
                # 发送 Leave 再发送 Enter。排到当前事件批次末尾，避免把内部移动误判
                # 为离开整个 Toast。
                try:
                    self.window.after_idle(self._destroy_if_expired_outside)
                except tk.TclError:
                    pass
            return
        if not self.streaming:
            self._start_destroy_timer()

    def _on_drag_start(self, event: tk.Event) -> None:
        """拖动开始"""
        self._focus_from_click(event)
        self.pause = True
        self._drag_pointer_x = event.x_root
        self._drag_pointer_y = event.y_root
        self._drag_window_x = self.window.winfo_x()
        self._drag_window_y = self.window.winfo_y()

    def _on_drag_stop(self, event: tk.Event) -> None:
        """拖动结束"""
        self.pause = False

    def _on_drag_motion(self, event: tk.Event) -> None:
        """拖动中，更新窗口位置"""
        deltax = event.x_root - self._drag_pointer_x
        deltay = event.y_root - self._drag_pointer_y
        x = self._drag_window_x + deltax
        y = self._drag_window_y + deltay
        if x != self._drag_window_x or y != self._drag_window_y:
            if not self._user_dragged:
                # 首次发生位移才算真正拖拽；智能时长模式下窗口转为永不自动关闭
                self._on_never_auto_close()
            self._user_dragged = True
        self._set_position(x, y)

    def _on_mouse_wheel(self, event: tk.Event) -> str:
        """滚轮调整窗口垂直位置

        允许在屏幕中线到屏幕底边之间滚动：
        - 窗口底部超出屏幕时，可以向上滚动（减小 y）
        - 窗口顶部超过屏幕中线时，可以向下滚动（增加 y）

        Returns:
            "break" 阻止事件继续传播
        """
        try:
            self.window.update_idletasks()
            current_y = self.window.winfo_y()
            window_height = self.window.winfo_height()
            screen_height = self.window.winfo_screenheight()
            screen_middle = screen_height // 2

            # 判定滚动方向
            delta = getattr(event, 'delta', 0)
            num = getattr(event, 'num', 0)

            if delta:
                is_scroll_up = (delta < 0)
            elif num:
                is_scroll_up = (num != 4)
            else:
                return "break"

            # 计算边界
            # top_limit: 窗口在最低位置时的 y 坐标（底部刚好在屏幕底边）
            # bottom_limit: 窗口在最高位置时的 y 坐标（顶部在屏幕中线）
            top_limit = screen_height - window_height
            bottom_limit = screen_middle

            # 检查是否允许滚动
            window_bottom = current_y + window_height
            can_scroll_up = window_bottom > screen_height  # 底部超出屏幕，可向上滚动
            can_scroll_down = current_y < screen_middle    # 顶部在中线之上，可向下滚动

            # 根据滚动方向和位置判断是否执行滚动
            if is_scroll_up and can_scroll_up:
                # 向上滚动（减小 y）
                target_y = max(current_y - SCROLL_STEP, top_limit)
                if target_y != current_y:
                    self._set_position(self.window.winfo_x(), int(target_y))
            elif not is_scroll_up and can_scroll_down:
                # 向下滚动（增加 y）
                target_y = min(current_y + SCROLL_STEP, bottom_limit)
                if target_y != current_y:
                    self._set_position(self.window.winfo_x(), int(target_y))

            return "break"
        except tk.TclError as e:
            logger.warning(f"滚动事件处理失败: {e}")
            return "break"

    def _on_copy(self, _event: tk.Event) -> str:
        """复制文本到剪贴板
        
        优先复制选中的文本（如果有），否则复制全部内容。

        Args:
            _event: 事件对象（未使用）

        Returns:
            "break" 阻止事件继续传播
        """
        try:
            text_to_copy = None
            
            # 如果有 md_label，检查是否有选中文本
            if hasattr(self, 'md_label') and self.md_label:
                try:
                    # Text 组件使用 tag_ranges("sel") 检查选区
                    sel_ranges = self.md_label.tag_ranges("sel")
                    if sel_ranges:
                        # 有选中文本，获取选中内容
                        text_to_copy = self.md_label.get(sel_ranges[0], sel_ranges[1])
                        logger.info("已复制选中的文本到剪贴板")
                except tk.TclError:
                    pass  # 没有选区或其他错误

            # 流式输出阶段使用 Text 组件，也允许复制其中的选区
            if text_to_copy is None and hasattr(self, 'text_area') and self.text_area:
                try:
                    sel_ranges = self.text_area.tag_ranges("sel")
                    if sel_ranges:
                        text_to_copy = self.text_area.get(sel_ranges[0], sel_ranges[1])
                        logger.info("已复制选中的文本到剪贴板")
                except tk.TclError:
                    pass
            
            # 如果没有选中文本，复制全部内容
            if text_to_copy is None:
                text_to_copy = self.full_text
                logger.info("已复制 Toast 全部内容到剪贴板")
            
            self.window.clipboard_clear()
            self.window.clipboard_append(text_to_copy)
        except Exception as e:
            logger.error(f"复制到剪贴板失败: {e}")
        return "break"

    # --------------------------------------------------------
    # 窗口销毁
    # --------------------------------------------------------

    def _start_destroy_timer(self) -> None:
        """启动自动销毁计时器"""
        if self.timer_id:
            self.window.after_cancel(self.timer_id)
        self.timer_id = self.window.after(self.duration, self._destroy_window)

    # --------------------------------------------------------
    # 智能时长（LLM 角色白色对话框）：时长随字数、悬停不顺延、拖拽永不自动关闭
    # --------------------------------------------------------

    @staticmethod
    def _compute_smart_duration(toast_duration: int, text: str) -> int:
        """按文本字数计算自动关闭时长（对数，毫秒）。

        公式：toast_duration + S·ln(1+字数)，封顶 SMART_DURATION_CAP_MS。
        对数而非线性：阅读时间边际递减，线性会在长回复上算出离谱时长。
        """
        chars = len(text or '')
        return int(min(
            toast_duration + SMART_DURATION_SCALE * math.log1p(chars),
            SMART_DURATION_CAP_MS,
        ))

    def _start_smart_destroy_timer(self) -> None:
        """按最终文本字数确定绝对倒计时；悬停不会暂停或顺延截止时间。"""
        if self._never_auto_close:
            return
        self._timer_expired = False
        if self.timer_id:
            self.window.after_cancel(self.timer_id)
            self.timer_id = None
        duration = self._compute_smart_duration(self.duration, self.full_text)
        try:
            self.timer_id = self.window.after(duration, self._on_smart_timer_expired)
        except tk.TclError:
            self.timer_id = None

    def _on_smart_timer_expired(self) -> None:
        """智能时长到期：未悬停则关闭，悬停则等真正移出后关闭。"""
        self.timer_id = None
        if self._never_auto_close:
            return
        self._timer_expired = True
        if not self.mouse_inside:
            self._destroy_window()

    def _destroy_if_expired_outside(self) -> None:
        """Leave 事件批次结束后，确认鼠标确已移出整个 Toast。"""
        if self._timer_expired and not self.mouse_inside and not self._never_auto_close:
            self._destroy_window()

    def _on_never_auto_close(self) -> None:
        """智能时长窗口被拖拽：取消自动关闭计时，转为永不自动关闭。

        结束条件只剩两个：新的 LLM 回复窗口生成（manager.close_llm_windows）
        或命中提前关闭路径（ESC / 异常 / 中断 / Markdown 失败 / 程序退出）。
        """
        if not self.pause_and_resume:
            return
        self._never_auto_close = True
        self._timer_expired = False
        if self.timer_id:
            self.window.after_cancel(self.timer_id)
            self.timer_id = None

    def _destroy_window(self, event: Optional[tk.Event] = None) -> None:
        """销毁窗口
        
        Args:
            event: 事件对象（ESC 键触发时传入）
        """
        try:
            # 角色白框生成完后仅关窗；自动到期也不代表用户放弃回答。
            if self.stop_callback and (not self.pause_and_resume or (event is not None and self.streaming)):
                try:
                    self.stop_callback()
                except Exception as e:
                    logger.warning(f"停止回调执行失败: {e}")

            if self.pause:
                # 如果窗口被暂停（拖动），延迟销毁
                if self.timer_id:
                    self.window.after_cancel(self.timer_id)
                self.timer_id = self.window.after(DESTROY_DELAY_MS, self._destroy_window)
            else:
                if self.timer_id:
                    self.window.after_cancel(self.timer_id)
                    self.timer_id = None
                self.window.destroy()
        except tk.TclError:
            # 窗口可能已被销毁
            pass

    # --------------------------------------------------------
    # Markdown 渲染
    # --------------------------------------------------------

    def _apply_markdown_text_style(self, select_bg: str, select_fg: str) -> None:
        """强制同步 HTMLLabel 的 Tk 文本样式。

        tkhtmlview 对 HTML/CSS 字体支持不完整，完成流式输出切换到 HTMLLabel 后，
        仅写入 font-family/font-size 可能不会生效；这里直接配置 Text 本体和所有
        已生成的 tag，确保 Markdown 最终显示阶段也使用角色配置的字体、字号和颜色。
        """
        # root 必须显式指向本窗口，否则命名字体建到其他线程的 _default_root 上，
        # 下面的 tag 覆盖只会写入无效字体名，静默回退为小号宋体
        self.markdown_font = font.Font(root=self.window, family=self.font_family, size=self.font_size)

        self.md_label.config(
            font=self.markdown_font,
            foreground=self.fg,
            bg=self.bg,
            background=self.bg,
            selectbackground=select_bg,
            selectforeground=select_fg
        )

        for tag in self.md_label.tag_names():
            if tag == "sel":
                continue
            try:
                self.md_label.tag_config(
                    tag,
                    font=self.markdown_font,
                    foreground=self.fg,
                    selectbackground=select_bg,
                    selectforeground=select_fg
                )
            except tk.TclError as e:
                logger.debug(f"同步 Markdown 文本样式失败: tag={tag}, error={e}")

    def _switch_to_markdown(self) -> None:
        """将内容组件切换为 Markdown 渲染"""
        try:
            # 保存当前位置
            cx, cy = self.window.winfo_x(), self.window.winfo_y()

            # 转换 Markdown 为 HTML
            raw_html = markdown.markdown(
                self.full_text,
                extensions=['extra', 'nl2br']
            )

            # 包装为完整的 HTML（不设置 padding，让 HTMLLabel 组件处理）
            full_html = f"""
            <div style="background-color:{self.bg}; color:{self.fg};
                        font-family:{self.font_family}; font-size:{self.font_size}px;">
                {raw_html}
            </div>
            """

            # 先销毁流式 Text，再创建 HTMLLabel，避免 Canvas 中残留旧窗口项。
            had_focus = self._has_keyboard_focus()
            self._destroy_content_widget()

            self.md_label = HTMLLabel(
                self._get_content_parent(),
                html=full_html,
                wrap="char",
                background=self.bg,
                padx=0,
                pady=0
            )
            self._mount_content_widget(self.md_label)
            self._bind_selectable_text_widget(self.md_label)
            if had_focus:
                self.md_label.focus_set()
            
            # 根据 editable 参数设置是否可编辑
            # DISABLED 状态下仍可选择文字，但无法编辑
            # NORMAL 状态下可以选择和编辑文字
            if self.editable:
                self.md_label.config(state=tk.NORMAL)
                
                # 设置光标颜色为背景色的反色
                cursor_color = self._invert_color(self.bg)
                self.md_label.config(insertbackground=cursor_color, insertwidth=2)
            # 否则保持 HTMLLabel 默认的 DISABLED 状态
            
            # 设置选中文字的高亮样式（DISABLED 状态下默认选中样式不可见）
            select_bg = "#3399ff"
            select_fg = "white"
            self._apply_markdown_text_style(select_bg, select_fg)

            # 多次更新以确保布局完全计算
            self.window.update()
            self.window.update_idletasks()

            # 使用 fit_height() 方法获取真实的内容高度
            # 这会自动调整标签高度以适应所有内容
            self.md_label.fit_height()
            self.window.update()
            self.window.update_idletasks()

            # 测量 fit_height() 后的高度
            height_after = self.md_label.winfo_height()
            reqheight_after = self.md_label.winfo_reqheight()

            # 使用较大的高度值（reqheight 可能更准确）
            content_height = max(height_after, reqheight_after)

            # fit_height() 已给出 Markdown 正文的实际高度；这里只叠加明确的
            # 顶部拖动区和底边距，避免按比例放大后在长文本下方留下大块空白。
            final_h = max(
                int(content_height) + self.height_padding,
                MARKDOWN_MIN_HEIGHT,
            )
            # 完成视图保持流式阶段达到的宽度，只重新计算高度。
            final_w = max(self.current_width, self.window.winfo_width())

            self._set_geometry(final_w, int(final_h), cx, cy)
            self._apply_window_shape()

            # 更新布局后再次测量
            self.window.update()
            self.window.update_idletasks()

            logger.info(
                f"Markdown 窗口: {final_w}x{final_h} "
                f"(内容: {content_height}px, 顶部留白: {self.padding_top}px)"
            )

        except Exception as e:
            logger.error(f"Markdown 转换失败: {e}")
            self._destroy_window()

    # --------------------------------------------------------
    # 抽象方法（子类必须实现）
    # --------------------------------------------------------

    @abstractmethod
    def update_text(self, new_text: str) -> None:
        """更新文本内容（由子类实现）
        
        Args:
            new_text: 新的完整文本内容
        """
        pass

    @abstractmethod
    def _destroy_content_widget(self) -> None:
        """销毁内容组件（由子类实现）"""
        pass

    # --------------------------------------------------------
    # 流式输出完成
    # --------------------------------------------------------

    def finish(self) -> None:
        """完成流式输出

        标记流式输出结束，如果启用了 Markdown 则转换渲染，
        然后启动自动销毁计时器。
        """
        if self.streaming:
            self.streaming = False

            # 如果启用了 Markdown，转换渲染
            if self.markdown:
                self._switch_to_markdown()

            # 智能时长模式：从「内容全部生成完成」起算，时长按最终文本字数计算；
            # 悬停不顺延截止时间，到期时仍悬停则移开即关；拖拽过的窗口不再计时
            if self.pause_and_resume:
                self._start_smart_destroy_timer()
            # 普通模式：只有当鼠标不在窗口内时才启动计时器
            elif not self.mouse_inside:
                self._start_destroy_timer()
