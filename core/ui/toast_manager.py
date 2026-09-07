from __future__ import annotations
"""
Toast 消息管理器模块

提供 ToastMessageManager 单例类，管理所有 Toast 窗口的生命周期。
"""
import logging
import threading
import tkinter as tk
from queue import Queue
from dataclasses import dataclass
from typing import Literal, Optional, Callable, Any, List, TYPE_CHECKING
import sys
import os

# 直接运行时，将项目根目录添加到 sys.path
if __name__ == "__main__":
    file_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(file_dir))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from core.ui.toast_text import ToastWindowText
    from core.ui.toast_label import ToastWindowLabel
    from core.ui.toast_constants import (
        QUEUE_POLL_INTERVAL_MS,
        DEFAULT_DURATION_MS,
        DEFAULT_MIN_WIDTH_PX,
        DEFAULT_MAX_WIDTH_PX,
        DEFAULT_MIN_WIDTH_RATIO,
        DEFAULT_MAX_WIDTH_RATIO,
        TK_SCALING_FACTOR,
    )
    from core.ui.toast_logger import get_toast_logger
else:
    from .toast_text import ToastWindowText
    from .toast_label import ToastWindowLabel
    from .toast_constants import (
        QUEUE_POLL_INTERVAL_MS,
        DEFAULT_DURATION_MS,
        DEFAULT_MIN_WIDTH_PX,
        DEFAULT_MAX_WIDTH_PX,
        DEFAULT_MIN_WIDTH_RATIO,
        DEFAULT_MAX_WIDTH_RATIO,
        TK_SCALING_FACTOR,
    )
    from .toast_logger import get_toast_logger

# 用于类型注解的前向引用
if TYPE_CHECKING:
    from .toast_base import ToastWindowBase


# 配置日志（智能检测主程序配置）
logger = get_toast_logger(__name__)


# ============================================================
# 数据类
# ============================================================

@dataclass
class ToastMessage:
    """Toast 消息配置数据类

    Attributes:
        text: 消息文本内容
        font_size: 字体大小（像素）
        font_family: 字体名称，空字符串使用系统默认
        bg: 背景颜色（十六进制或颜色名）
        fg: 前景色（文字颜色）
        duration: 显示时长（毫秒）
        min_width_px: 最小宽度像素值，合法时优先于比例值
        max_width_px: 最大宽度像素值，合法时优先于比例值
        min_width_ratio: 最小宽度的屏幕比例回退值
        max_width_ratio: 最大宽度的屏幕比例回退值
        initial_height: 初始高度，0 表示自动计算
        streaming: 是否为流式模式
        window_type: 窗口类型 ('text' 或 'label')
        stop_callback: 窗口关闭时的回调函数
        markdown: 是否启用 Markdown 渲染
    """
    text: str
    font_size: int = 14
    font_family: str = ''
    bg: str = '#075077'
    fg: str = 'white'
    duration: int = DEFAULT_DURATION_MS
    min_width_px: Any = DEFAULT_MIN_WIDTH_PX
    max_width_px: Any = DEFAULT_MAX_WIDTH_PX
    min_width_ratio: Any = DEFAULT_MIN_WIDTH_RATIO
    max_width_ratio: Any = DEFAULT_MAX_WIDTH_RATIO
    initial_height: int = 0
    streaming: bool = False
    window_type: Literal['text', 'label'] = 'text'
    stop_callback: Optional[Callable[[], None]] = None
    markdown: bool = False
    editable: bool = False  # Markdown 渲染后是否允许编辑
    window_alpha: float = 1.0  # 窗口透明度，1.0 表示不透明
    corner_radius: int = 0     # 窗口圆角半径，0 表示不启用圆角
    smart_duration: bool = False  # 智能时长模式（LLM 角色白色对话框）：时长随字数、
                                  # 悬停不顺延且到期后移开即关、拖拽后永不自动关闭；
                                  # 普通 toast 保持旧行为
    reminder_status: bool = False  # 内部提醒通知，固定底部、固定时长、不可拖动


# ============================================================
# Toast 消息管理器
# ============================================================

class ToastMessageManager:
    """Toast 消息管理器（单例模式）

    在独立的线程中运行 Tkinter 主循环，管理所有 Toast 窗口的生命周期。

    Features:
        - 单例模式，确保只有一个 Tkinter 主循环
        - 消息队列，支持并发添加消息
        - 活动窗口跟踪，支持流式输出更新
        - UUID 消息标识，精确匹配和操作
    """

    _instance: Optional[ToastMessageManager] = None
    _lock = threading.Lock()

    def __new__(cls) -> ToastMessageManager:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        self._initialized = True
        self.message_queue: Queue[ToastMessage] = Queue()
        self.is_running = False
        self.active_windows: List = []  # 运行时类型，避免循环导入
        self.root: Optional[tk.Tk] = None

        # 在子线程中启动 Tkinter
        self.manager_thread = threading.Thread(
            target=self._run_manager,
            daemon=True,
            name="ToastManagerThread"
        )
        self.manager_thread.start()

    def _run_manager(self) -> None:
        """在子线程中运行 Tkinter 主循环"""
        # 创建隐藏的主窗口
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.tk.call('tk', 'scaling', TK_SCALING_FACTOR)

        # 设置窗口关闭时的行为
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 开始处理队列
        self.is_running = True
        self._process_queue()

        # 启动 Tkinter 主循环
        self.root.mainloop()

    def _on_close(self) -> None:
        """关闭所有窗口并退出"""
        self.is_running = False

        for window in self.active_windows[:]:
            try:
                window.window.destroy()
            except tk.TclError:
                pass

        self.active_windows.clear()

        if self.root:
            self.root.quit()

    def _process_queue(self) -> None:
        """处理队列中的消息"""
        try:
            if not self.message_queue.empty():
                msg = self.message_queue.get_nowait()
                msg_id = getattr(msg, '_id', 'unknown')

                # 根据 window_type 选择窗口类
                WindowClass = ToastWindowLabel if msg.window_type == 'label' else ToastWindowText
                if msg.reminder_status:
                    from .toast_reminder import ToastWindowReminder
                    WindowClass = ToastWindowReminder

                toast_window = WindowClass(
                    self.root,
                    msg.text,
                    msg.font_size,
                    msg.font_family,
                    msg.bg,
                    msg.fg,
                    msg.duration,
                    msg.min_width_px,
                    msg.max_width_px,
                    msg.min_width_ratio,
                    msg.max_width_ratio,
                    msg.initial_height,
                    streaming=msg.streaming,
                    stop_callback=msg.stop_callback,
                    markdown=msg.markdown,
                    editable=msg.editable,
                    window_alpha=msg.window_alpha,
                    corner_radius=msg.corner_radius,
                    smart_duration=msg.smart_duration
                )

                # 保存消息ID到窗口对象
                toast_window._msg_id = msg_id
                self.active_windows.append(toast_window)

                # 设置窗口销毁时的回调。注意：Tk 的 <Destroy> 事件在任意子 widget 销毁时
                # 也会触发绑定到 Toplevel 上的回调（Markdown 转换会销毁 text_area），必须
                # 校验被销毁的是 Toplevel 本身。否则已 finish 的窗口会被误从 active_windows
                # 移除——窗口还显示着，但后续 close_llm_windows 遍历不到它、顶不掉了。
                toast_window.window.bind(
                    '<Destroy>',
                    lambda e, w=toast_window: (
                        self._remove_window(w) if str(e.widget) == str(w.window) else None
                    )
                )

            # 清理已销毁的窗口
            self.active_windows = [
                w for w in self.active_windows
                if self._window_exists(w)
            ]

        except Exception as e:
            # 带上 traceback：这里吞掉的是窗口构造异常，只打一行消息的话
            # 现象只剩下调用方「等待窗口超时」，根因（哪一行、哪个模块）完全丢失
            logger.warning(f"处理队列消息时出错: {e}", exc_info=True)

        # 继续处理队列
        if self.is_running and self.root:
            self.root.after(QUEUE_POLL_INTERVAL_MS, self._process_queue)

    def _window_exists(self, window) -> bool:
        """检查窗口是否存在"""
        try:
            return window.window.winfo_exists()
        except tk.TclError:
            return False

    def _remove_window(self, window) -> None:
        """从活动窗口列表中移除窗口"""
        if window in self.active_windows:
            self.active_windows.remove(window)

    def add_message(self, msg: ToastMessage) -> Optional[str]:
        """添加 ToastMessage 对象到队列

        Args:
            msg: Toast 消息配置对象

        Returns:
            消息唯一标识符（用于后续更新、完成、关闭操作）
        """
        import uuid
        msg_id = str(uuid.uuid4())
        msg._id = msg_id  # 添加唯一标识符
        self.message_queue.put(msg)
        return msg_id

    def update_toast(self, msg_id: str, new_text: str) -> None:
        """更新指定 ID 的 Toast 文字

        Args:
            msg_id: 消息唯一标识符
            new_text: 新的完整文本内容
        """
        for window in self.active_windows:
            if getattr(window, '_msg_id', None) == msg_id:
                window.update_text(new_text)
                return
        logger.warning(f"未找到消息 ID: {msg_id[:8]}")

    def finish_toast(self, msg_id: str) -> None:
        """完成指定 ID 的 Toast 的流式输出

        Args:
            msg_id: 消息唯一标识符
        """
        for window in self.active_windows:
            if getattr(window, '_msg_id', None) == msg_id:
                if window.streaming:
                    window.finish()
                return
        logger.warning(f"未找到消息 ID: {msg_id[:8]}")

    def close_toast(self, msg_id: str) -> None:
        """关闭指定 ID 的 Toast

        Args:
            msg_id: 消息唯一标识符
        """
        for window in self.active_windows[:]:
            if getattr(window, '_msg_id', None) == msg_id:
                try:
                    window.window.destroy()
                    self.active_windows.remove(window)
                except (tk.TclError, ValueError):
                    pass
                return
        logger.warning(f"未找到消息 ID: {msg_id[:8]}")

    def close_llm_windows(self) -> None:
        """关闭所有旧的 LLM 白色对话框。

        新 LLM 回复窗口创建前调用：上一轮 LLM 回复窗口（无论是否被拖拽）都应由
        新窗口取代，避免新旧叠加——用户说新的话时旧回复窗口应立即消失，而不是
        等到新窗口有文字才被察觉。范围限定 smart_duration 窗口（pause_and_resume
        标志），普通 toast（托盘/错误/录音）不受影响；OpenMate 接管时不走本方法
        （handle_toast_mode 在 OpenMate 路径提前返回，旧窗口保留）。
        """
        for window in self.active_windows[:]:
            if getattr(window, 'pause_and_resume', False):
                # 与 _destroy_window 的 ESC 路径语义对称：顶掉旧窗口时也触发其停止回调。
                # 串行处理下旧请求的 LLM 已完成、回调无害；防未来并发处理时旧请求继续烧 token
                stop_cb = getattr(window, 'stop_callback', None)
                if stop_cb:
                    try:
                        stop_cb()
                    except Exception:
                        pass
                try:
                    window.window.destroy()
                except tk.TclError:
                    pass

    async def wait_for_window(self, msg_id: str, timeout: float = 1.0) -> Optional[ToastWindowBase]:
        """异步等待指定 ID 的窗口创建完成

        Args:
            msg_id: 消息唯一标识符
            timeout: 超时时间（秒）

        Returns:
            窗口对象，如果超时则返回 None
        """
        import asyncio
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            for window in self.active_windows:
                if getattr(window, '_msg_id', None) == msg_id:
                    return window
            await asyncio.sleep(0.01)  # 10ms 轮询间隔
        logger.warning(f"等待窗口超时: {msg_id[:8]}")
        return None
