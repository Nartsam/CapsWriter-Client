"""提醒内部状态通知：底部固定位置，鼠标交互不改变位置或倒计时。"""
from .toast_text import ToastWindowText


REMINDER_FONT_SIZE = 18
REMINDER_BG = '#FFF4D6'
REMINDER_FG = '#332B1A'
REMINDER_BOTTOM_OFFSET = 130   # 窗口底边距主屏底边的像素数


class ToastWindowReminder(ToastWindowText):
    @classmethod
    def _resolve_width_range(cls, min_width_px, max_width_px,
                             min_width_ratio, max_width_ratio, screen_width):
        # 提醒内部状态有独立宽度策略，不继承通用通知 800px 的上限。
        maximum = max(1, min(1600, int(screen_width * 0.8)))
        return min(480, maximum), maximum

    def _measure_text_width(self, text, text_font):
        # 为字体测量和布局取整留少量余量，避免末字因边界误差单独换行。
        return super()._measure_text_width(text, text_font) + 8

    def _set_geometry(self, width, height, x, y):
        # 最终排版与增宽都会经过这里，保证多行通知仍贴住底部。
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        height = min(height, max(60, screen_height - 120))
        super()._set_geometry(width, height, (screen_width - width) // 2,
                              max(0, screen_height - height - REMINDER_BOTTOM_OFFSET))

    def _on_mouse_enter(self, event):
        pass

    def _on_mouse_leave(self, event):
        pass

    def _on_drag_start(self, event):
        pass

    def _on_drag_motion(self, event):
        pass

    def _on_mouse_wheel(self, event):
        # 长的列表可以滚动正文，但窗口本身始终固定。
        delta = getattr(event, 'delta', 0)
        self.text_area.yview_scroll(-1 if delta > 0 else 1, 'units')
        return 'break'
