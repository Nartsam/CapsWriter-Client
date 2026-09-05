"""
Toast Text 窗口模块

基于 Text 组件的浮动消息窗口，适合流式输出场景（如 LLM 实时显示）。
"""
import logging
import tkinter as tk
from tkinter import font
from typing import Optional, Callable, Any

from .toast_base import (
    ToastWindowBase,
)
from .toast_constants import (
    DEFAULT_MIN_WIDTH_PX,
    DEFAULT_MAX_WIDTH_PX,
    DEFAULT_MIN_WIDTH_RATIO,
    DEFAULT_MAX_WIDTH_RATIO,
    MIN_WINDOW_HEIGHT,
    STREAMING_TEXT_HEIGHT,
    NON_STREAMING_TEXT_HEIGHT,
    TEXT_WRAP_MODE,
)
from .toast_logger import get_toast_logger

# 配置日志（智能检测主程序配置）
logger = get_toast_logger(__name__)


class ToastWindowText(ToastWindowBase):
    """基于 Text 组件的浮动消息窗口
    
    适合流式输出场景，支持增量插入文本，窗口高度自动增长。
    
    Features:
        - 支持流式文本输出（逐字显示）
        - 窗口高度根据内容自动调整
        - 支持 Markdown 渲染（流式完成后转换）
    """

    def __init__(
        self,
        parent_root: tk.Tk,
        text: str,
        font_size: int = 14,
        font_family: str = '',
        bg: str = '#075077',
        fg: str = 'white',
        duration: int = 3000,
        min_width_px: Any = DEFAULT_MIN_WIDTH_PX,
        max_width_px: Any = DEFAULT_MAX_WIDTH_PX,
        min_width_ratio: Any = DEFAULT_MIN_WIDTH_RATIO,
        max_width_ratio: Any = DEFAULT_MAX_WIDTH_RATIO,
        initial_height: int = 0,
        streaming: bool = False,
        stop_callback: Optional[Callable[[], None]] = None,
        markdown: bool = False,
        editable: bool = False,
        window_alpha: float = 1.0,
        corner_radius: int = 0,
        smart_duration: bool = False
    ) -> None:
        """创建基于 Text 组件的浮动消息窗口

        Args:
            parent_root: 父窗口（Tk 主窗口）
            text: 初始文本内容
            font_size: 字体大小（像素）
            font_family: 字体名称，空字符串使用默认字体
            bg: 背景颜色
            fg: 前景色（文字颜色）
            duration: 自动关闭时长（毫秒）
            min_width_px: 最小宽度像素值，合法时优先于比例值
            max_width_px: 最大宽度像素值，合法时优先于比例值
            min_width_ratio: 最小宽度的屏幕比例回退值
            max_width_ratio: 最大宽度的屏幕比例回退值
            initial_height: 初始高度，0 表示自动计算
            streaming: 是否为流式输出模式
            stop_callback: 窗口关闭时的回调函数
            markdown: 是否启用 Markdown 渲染
            editable: Markdown 渲染后是否允许编辑
            window_alpha: 窗口透明度，1.0 表示不透明
            corner_radius: 窗口圆角半径，0 表示不启用圆角
            smart_duration: 智能时长模式（LLM 角色白色对话框），见 ToastWindowBase
        """
        # 初始化基类
        super().__init__(
            parent_root, text, font_size, font_family, bg, fg,
            duration, min_width_px, max_width_px,
            min_width_ratio, max_width_ratio, initial_height, streaming,
            stop_callback, markdown, editable, window_alpha, corner_radius,
            smart_duration
        )

        # 创建字体对象用于计算行高
        # root 必须显式指向本窗口：命名字体默认建在全进程第一个 Tk（_default_root，
        # 通常是录音指示器线程）上，跨解释器传到本窗口会退化成无效家族名 -> 小号宋体
        self.my_font = font.Font(root=self.window, family=self.font_family, size=self.font_size)
        self.line_height = self.my_font.metrics('linespace')
        self.last_char_count = 0

        # 创建 Text 组件
        text_height = STREAMING_TEXT_HEIGHT if streaming else NON_STREAMING_TEXT_HEIGHT
        
        self.text_area = tk.Text(
            self._get_content_parent(),
            font=self.my_font,
            fg=self.fg,
            bg=self.bg,
            padx=0,
            pady=0,
            borderwidth=0,
            highlightthickness=0,
            wrap=TEXT_WRAP_MODE,
            insertofftime=0,
            state=tk.DISABLED,
            cursor="xterm",
            height=text_height
        )

        # 锚定在左上角，使用 fill=BOTH 填充窗口
        self._mount_content_widget(self.text_area)
        self._bind_selectable_text_widget(self.text_area)

        # 禁用 Text 组件的默认滚轮行为，让其传播到窗口
        # 通过将事件绑定到空函数来阻止 Text 的内置滚动，但不阻止传播
        def pass_to_window(event):
            # 让窗口处理这个事件
            self.window.event_generate('<MouseWheel>', x=event.x, y=event.y, delta=event.delta)
            return "break"

        self.text_area.bind('<MouseWheel>', pass_to_window)
        self.text_area.bind('<Button-4>', pass_to_window)  # Linux
        self.text_area.bind('<Button-5>', pass_to_window)  # Linux

        # 初始化时插入初始文本
        if text:
            self.text_area.config(state=tk.NORMAL)
            self.text_area.insert(tk.END, text)
            self.text_area.config(state=tk.DISABLED)
            self.last_char_count = len(text)

        # 强制更新布局
        self.window.update_idletasks()

        # 先以最小宽度居中，再按初始内容对称增宽。
        self._set_window_position(initial=True)
        if text:
            self._grow_to_fit_text(text, self.my_font)
            self.window.update_idletasks()
            result = self.text_area.count('1.0', 'end', 'displaylines')
            actual_lines = result[0] if result else 1
            self.text_area.config(height=actual_lines)
            self._set_window_position()
        
        # 如果是非流式模式且启用了 Markdown，立即转换
        if not streaming and markdown:
            self.window.update()
            self._switch_to_markdown()

    def _set_window_position(self, initial: bool = False) -> None:
        """设置窗口位置

        Args:
            initial: 是否为初始位置（屏幕中央，单行高度）
        """
        try:
            screen_width = self.window.winfo_screenwidth()
            screen_height = self.window.winfo_screenheight()

            # 更新窗口以确保获取正确的尺寸
            self.window.update_idletasks()

            # 获取实际渲染的行数
            result = self.text_area.count('1.0', 'end', 'displaylines')
            current_lines = result[0] if result else 1

            # 精确计算高度：行数 * 行高 + 上下 Padding
            needed_h = (current_lines * self.line_height) + self.height_padding

            # 如果设置了初始高度，使用较大值
            if self.initial_height > 0:
                window_height = max(self.initial_height, needed_h)
            else:
                window_height = needed_h

            # 限制最小高度
            window_height = max(window_height, MIN_WINDOW_HEIGHT)
            window_width = self.current_width

            if initial:
                # 初始位置：水平居中，顶部在屏幕中间
                x = (screen_width - window_width) // 2
                y = screen_height // 2
            else:
                # 保持当前位置，只更新大小
                x = self.window.winfo_x()
                y = self.window.winfo_y()

            self._set_geometry(window_width, int(window_height), x, y)
            self._apply_window_shape()
        except tk.TclError as e:
            logger.warning(f"设置窗口位置失败: {e}")

    def update_text(self, new_text: str) -> None:
        """更新文本内容（增量插入模式）
        
        仅在流式模式下有效。增量插入新字符到 Text 组件末尾，
        并根据内容自动调整窗口高度。
        
        Args:
            new_text: 完整的新文本内容
        """
        if not self.streaming:
            return

        # 检查窗口是否还存在
        try:
            if not self.window.winfo_exists():
                self.streaming = False
                return
        except tk.TclError:
            self.streaming = False
            return

        # 计算新增的字符
        current_char_count = len(new_text)
        if current_char_count > self.last_char_count:
            # 保存完整文本
            self.full_text = new_text
            
            # 获取新增部分
            new_chars = new_text[self.last_char_count:]

            try:
                # 更新 Text 组件
                self.text_area.config(state=tk.NORMAL)
                self.text_area.insert(tk.END, new_chars)
                self.text_area.config(state=tk.DISABLED)

                # 强制同步布局计算
                self.window.update_idletasks()

                # 只有真的增宽了才需要再刷一次布局，否则下面的 displaylines 会按旧宽度计数
                if self._grow_to_fit_text(new_text, self.my_font):
                    self.window.update_idletasks()

                # 计算需要的窗口高度
                result = self.text_area.count('1.0', 'end', 'displaylines')
                current_lines = result[0] if result else 1
                needed_h = (current_lines * self.line_height) + self.height_padding
                current_h = self.window.winfo_height()
                current_w = self.window.winfo_width()

                # 更新 Text 组件的高度，使其能显示所有行
                self.text_area.config(height=current_lines)

                # 如果需要增长高度
                if needed_h > current_h:
                    curr_x = self.window.winfo_x()
                    curr_y = self.window.winfo_y()
                    self._set_geometry(current_w, int(needed_h), curr_x, curr_y)
                    self._apply_window_shape()

                self.last_char_count = current_char_count
            except tk.TclError:
                # 窗口已被销毁，停止流式输出
                self.streaming = False

    def _destroy_content_widget(self) -> None:
        """销毁 Text 组件"""
        if hasattr(self, 'text_area'):
            try:
                self.text_area.destroy()
            except tk.TclError:
                pass
