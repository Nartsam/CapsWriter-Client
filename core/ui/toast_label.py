"""
Toast Label 窗口模块

基于 Label 组件的浮动消息窗口，适合普通提示消息。
"""
import logging
import tkinter as tk
from tkinter import font
from typing import Optional, Callable, Any

from .toast_base import (
    ToastWindowBase,
    add_zero_width_for_chinese,
)
from .toast_constants import (
    DEFAULT_FONT_FAMILY,
    DEFAULT_PADDING_X,
    DEFAULT_MIN_WIDTH_PX,
    DEFAULT_MAX_WIDTH_PX,
    DEFAULT_MIN_WIDTH_RATIO,
    DEFAULT_MAX_WIDTH_RATIO,
    MIN_WINDOW_HEIGHT,
    LABEL_HEIGHT_PADDING,
)
from .toast_logger import get_toast_logger

# 配置日志（智能检测主程序配置）
logger = get_toast_logger(__name__)


class ToastWindowLabel(ToastWindowBase):
    """基于 Label 组件的浮动消息窗口
    
    适合普通提示消息，支持简单的文本更新。
    
    Features:
        - 简单高效的文本显示
        - 自动换行（使用 wraplength）
        - 支持 Markdown 渲染
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
        """创建基于 Label 组件的浮动消息窗口

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

        # 用于增量插入
        self.last_char_count = 0

        # 处理文本：在中文字符后添加零宽空格
        processed_text = add_zero_width_for_chinese(text) if text else text

        self.my_font = font.Font(
            root=self.window, family=self.font_family, size=self.font_size
        )

        # 创建文字标签
        self.label = tk.Label(
            self._get_content_parent(),
            text=processed_text,
            font=self.my_font,
            fg=fg,
            bg=bg,
            justify=tk.LEFT,
            wraplength=self.min_width - (DEFAULT_PADDING_X * 2),
            anchor='nw'
        )

        # 使用 fill=BOTH 和 expand=True 填充窗口
        if self.content_canvas is None:
            self.label.pack(
                side=tk.TOP,
                fill=tk.BOTH,
                expand=True,
                padx=DEFAULT_PADDING_X,
                pady=0
            )
        else:
            self._mount_content_widget(self.label)

        # 初始化字符计数
        if text:
            self.last_char_count = len(text)

        # 强制更新布局
        self.window.update_idletasks()

        # 先以最小宽度居中，再按初始内容对称增宽。
        self._set_window_position(initial=True)
        if text:
            self._grow_to_fit_text(text, self.my_font)
            self.window.update_idletasks()
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

            # 获取 Label 的理想高度
            needed_h = self.label.winfo_reqheight() + LABEL_HEIGHT_PADDING

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

            self._set_geometry(window_width, window_height, x, y)
            self._apply_window_shape()
        except tk.TclError as e:
            logger.warning(f"设置窗口位置失败: {e}")

    def update_text(self, new_text: str) -> None:
        """更新文本内容
        
        Label 不支持增量插入，每次更新都会替换全部文本。
        
        Args:
            new_text: 新的完整文本内容
        """
        if not self.streaming:
            return

        # 计算新增的字符
        current_char_count = len(new_text)
        if current_char_count > self.last_char_count:
            # 保存完整文本
            self.full_text = new_text

            # 处理文本：在中文字符后添加零宽空格
            processed_text = add_zero_width_for_chinese(new_text)

            try:
                # Label 不支持增量插入，只能替换全部文本
                self.label.config(text=processed_text)

                # 强制同步布局计算
                self.window.update_idletasks()

                # 只有真的增宽了才需要再刷一次布局，否则下面的 reqheight 会按旧换行宽度算
                if self._grow_to_fit_text(new_text, self.my_font):
                    self.window.update_idletasks()

                # 计算窗口需要的新高度
                needed_h = self.label.winfo_reqheight() + LABEL_HEIGHT_PADDING
                current_h = self.window.winfo_height()
                current_w = self.window.winfo_width()

                # 如果需要增长高度
                if needed_h > current_h:
                    curr_x = self.window.winfo_x()
                    curr_y = self.window.winfo_y()
                    self._set_geometry(current_w, int(needed_h), curr_x, curr_y)
                    self._apply_window_shape()

                self.last_char_count = current_char_count
            except tk.TclError:
                # 窗口已被销毁
                self.streaming = False

    def _on_width_changed(self, new_width: int) -> None:
        """同步 Label 的换行上限，达到最大宽度后按字符自动换行。"""
        if hasattr(self, 'label'):
            self.label.config(
                wraplength=max(1, new_width - DEFAULT_PADDING_X * 2)
            )

    def _destroy_content_widget(self) -> None:
        """销毁 Label 组件"""
        if hasattr(self, 'label'):
            try:
                self.label.destroy()
            except tk.TclError:
                pass
