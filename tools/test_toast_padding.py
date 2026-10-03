# coding: utf-8
"""
Toast 顶部留白分岔的真 Tk 冒烟测试

验证 2026-09-05 的改动：顶部空白拖动区只给 LLM 角色白色对话框
（`smart_duration=True`），托盘/错误这类普通 toast 顶部与底部对齐。
留白的具体像素是可调常量，这里只验它们确实落到了画布坐标和窗口高度上。

**本脚本会真的构造 Tk 窗口**（无边框、置顶，屏幕上会闪一下），因此不进
`run_tests.py` 的流水线——那条流水线要能在没有桌面会话的环境里跑。改完
core/ui/toast_*.py 后手动跑一次。

用法（在仓库根目录执行）：python tools/test_toast_padding.py
"""
from __future__ import annotations

import importlib
import ctypes
import os
import sys
import types

sys.dont_write_bytecode = True

# 仓库根目录按本文件位置推导，脚本从任何工作目录跑都成立
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

PASS = []
FAIL = []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    if not cond:
        print('  [×] %s  %s' % (name, detail))


# ====================================================================
# 桩模块
# ====================================================================

class _StubLogger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def install_stubs():
    """本目录是打包后的 Release，系统 Python 没有 markdown / tkhtmlview。

    toast_base 在模块顶层 import 它们，但本测试全程 markdown=False、走不到
    渲染分支，桩掉即可。core / core.ui 也要预注入：`import core` 会在包
    __init__ 里连锁导入 rich 而失败。给 core.ui 的桩挂上真实 __path__，
    让 toast_base 里的 `from .toast_constants import ...` 仍然加载真代码。
    """
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

# 在加载 toast_base 的进程 DPI 声明之前设置，避免文字度量被系统虚拟化。
if os.name == 'nt':
    ctypes.windll.shcore.SetProcessDpiAwareness(2)

import tkinter as tk  # noqa: E402

from core.ui.toast_constants import (  # noqa: E402
    COMPACT_PADDING_TOP,
    DEFAULT_PADDING_BOTTOM,
    DEFAULT_PADDING_TOP,
    DEFAULT_PADDING_X,
    MIN_WINDOW_HEIGHT,
    TK_SCALING_FACTOR,
)

ToastWindowText = importlib.import_module('core.ui.toast_text').ToastWindowText
ToastWindowLabel = importlib.import_module('core.ui.toast_label').ToastWindowLabel
ToastWindowReminder = importlib.import_module('core.ui.toast_reminder').ToastWindowReminder


def geometry_size(window):
    """从 wm geometry 字符串读回尺寸；winfo_height 在窗口未映射时返回 1"""
    spec = window.geometry().split('+')[0]
    w, h = spec.split('x')
    return int(w), int(h)


# 留白常量的具体数值不在这里钉：它们是可调参数，下面只验「取值落到了窗口上」

root = tk.Tk()
root.withdraw()
root.tk.call('tk', 'scaling', TK_SCALING_FACTOR)   # 与 ToastMessageManager 一致

windows = []


def make(cls, smart, text='测试文本', **kw):
    w = cls(
        root, text,
        font_size=kw.pop('font_size', 14),
        font_family='Microsoft YaHei UI',
        bg=kw.pop('bg', '#f7fbff'), fg=kw.pop('fg', 'black'),
        duration=kw.pop('duration', 60000),        # 不会触发：本测试从不进 mainloop
        initial_height=kw.pop('initial_height', 0),
        streaming=kw.pop('streaming', False),
        markdown=False,
        smart_duration=smart,
        **kw
    )
    windows.append(w)
    w.window.update_idletasks()
    return w


try:
    print('[提醒状态窗口]')
    reminder_style = importlib.import_module('core.ui.toast_reminder')
    reminder = make(ToastWindowReminder, False,
                    text='提醒已设置：今天 12:39:56「助理」会提醒你站起来活动一下',
                    font_size=reminder_style.REMINDER_FONT_SIZE,
                    bg=reminder_style.REMINDER_BG, fg=reminder_style.REMINDER_FG,
                    window_alpha=1.0, corner_radius=12)
    x, y = reminder.window.winfo_x(), reminder.window.winfo_y()
    width, height = geometry_size(reminder.window)
    check('提醒窗口位于主屏底部',
          y + height == reminder.window.winfo_screenheight() - reminder_style.REMINDER_BOTTOM_OFFSET,
          'bottom=%r screen=%r' % (y + height, reminder.window.winfo_screenheight()))
    check('提醒窗口水平居中', x == (reminder.window.winfo_screenwidth() - width) // 2)
    check('提醒字号用的是传入的值', reminder.my_font.cget('size') == reminder_style.REMINDER_FONT_SIZE)
    # 宽度策略的具体数字是可调的，这里只验两条性质：不受通用 800px 上限约束、不超出屏幕
    screen_w = root.winfo_screenwidth()
    check('提醒最大宽度不受通用 800px 上限约束', screen_w < 1100 or reminder.max_width > 800,
          'screen=%r max=%r' % (screen_w, reminder.max_width))
    check('提醒最大宽度不超出屏幕', reminder.max_width <= screen_w)
    lo, hi = ToastWindowReminder._resolve_width_range(320, 800, .2, .5, 400)
    check('小屏宽度不越界', lo <= hi <= 400, (lo, hi))
    required_width = reminder._measure_text_width(reminder.full_text, reminder.my_font)
    if required_width <= reminder.max_width:
        line = reminder.text_area.dlineinfo('1.0')
        last = reminder.text_area.dlineinfo('end-2c')
        check('示例确认文案完整显示一行', line is not None and last is not None and line[1] == last[1],
              'width=%s required=%s first=%s last=%s' % (width, required_width, line, last))
    event = types.SimpleNamespace(x_root=x + 50, y_root=y + 50, delta=120)
    timer = reminder.timer_id
    reminder._on_mouse_enter(event)
    reminder._on_mouse_leave(event)
    reminder._on_drag_start(event)
    event.x_root += 100
    reminder._on_drag_motion(event)
    reminder._on_mouse_wheel(event)
    reminder.window.update_idletasks()
    check('提醒窗口悬停不重置计时器', reminder.timer_id == timer)
    check('拖动和滚轮不移动提醒窗口', (reminder.window.winfo_x(), reminder.window.winfo_y()) == (x, y))
    check('提醒窗口不转为常驻', not reminder._never_auto_close and not reminder.pause)
    # 真实 Tk 定时器验证：悬停后仍按原时长销毁，不依赖手工调用 destroy。
    expiring = make(ToastWindowReminder, False, duration=120)
    expiring._on_mouse_enter(event)
    root.after(180, root.quit)
    root.mainloop()
    check('提醒窗口悬停期间到期销毁', not expiring.window.winfo_exists())

    # ================================================================
    # 1. 两类窗口的留白取值
    # ================================================================
    print()
    print('[Text 窗口的留白分岔]')

    llm = make(ToastWindowText, True)
    plain = make(ToastWindowText, False)

    check('LLM 顶部留白取拖动区高度', llm.padding_top == DEFAULT_PADDING_TOP, repr(llm.padding_top))
    check('LLM 上下留白之和',
          llm.height_padding == DEFAULT_PADDING_TOP + DEFAULT_PADDING_BOTTOM,
          repr(llm.height_padding))
    check('普通 toast 顶部留白取紧凑值', plain.padding_top == COMPACT_PADDING_TOP, repr(plain.padding_top))
    check('普通 toast 上下留白之和',
          plain.height_padding == COMPACT_PADDING_TOP + DEFAULT_PADDING_BOTTOM,
          repr(plain.height_padding))

    # ================================================================
    # 2. 留白真的落到了画布坐标上（不只是记了个属性）
    # ================================================================
    print()
    print('[Canvas 内容项坐标]')

    for label, w in (('LLM', llm), ('普通', plain)):
        x, y = w.content_canvas.coords(w._content_window_item)
        check('%s 内容项 y == padding_top' % label, int(y) == w.padding_top,
              'y=%r padding_top=%r' % (y, w.padding_top))
        check('%s 左边距不受影响' % label, int(x) == DEFAULT_PADDING_X, 'x=%r' % x)

        win_w, win_h = geometry_size(w.window)
        item_h = int(w.content_canvas.itemcget(w._content_window_item, 'height'))
        check('%s 内容高度 = 窗口高 - 上下留白' % label,
              item_h == max(1, win_h - w.height_padding),
              'item=%r window=%r padding=%r' % (item_h, win_h, w.height_padding))

    # ================================================================
    # 3. 窗口高度按各自的 height_padding 收缩
    # ================================================================
    print()
    print('[窗口高度]')

    _, llm_h = geometry_size(llm.window)
    _, plain_h = geometry_size(plain.window)
    line = llm.line_height

    check('普通窗口比 LLM 窗口矮出两种顶部留白之差',
          llm_h - plain_h == DEFAULT_PADDING_TOP - COMPACT_PADDING_TOP,
          'llm=%r plain=%r line_height=%r' % (llm_h, plain_h, line))

    # 选 15 而不是 0 的理由：0 会让单行窗口撞上 MIN_WINDOW_HEIGHT 兜底，
    # 省下的留白原样挪到底部。这里断言兜底没有生效
    check('普通单行窗口没有被 MIN_WINDOW_HEIGHT 兜底', plain_h > MIN_WINDOW_HEIGHT,
          'plain=%r floor=%r（若不成立，顶部留白会被兜底转移到底部）'
          % (plain_h, MIN_WINDOW_HEIGHT))
    check('普通窗口高度 = 行高 + 上下留白',
          plain_h == line * 1 + plain.height_padding,
          'plain=%r line=%r' % (plain_h, line))

    # ================================================================
    # 4. initial_height 仍然是下限（错误框改成 0 前的旧语义不能被破坏）
    # ================================================================
    print()
    print('[initial_height 仍是下限]')

    tall = make(ToastWindowText, False, initial_height=200)
    _, tall_h = geometry_size(tall.window)
    check('initial_height 大于计算值时取 initial_height', tall_h == 200, repr(tall_h))

    # ================================================================
    # 5. 多行时留白只叠加一次
    # ================================================================
    print()
    print('[多行文本]')

    multi = make(ToastWindowText, False, text='第一行\n第二行\n第三行')
    _, multi_h = geometry_size(multi.window)
    check('三行窗口 = 3 倍行高 + 上下留白', multi_h == line * 3 + multi.height_padding,
          'multi=%r line=%r' % (multi_h, line))

    # ================================================================
    # 6. Label 版本走同一条分岔
    # ================================================================
    print()
    print('[Label 窗口]')

    lbl_llm = make(ToastWindowLabel, True)
    lbl_plain = make(ToastWindowLabel, False)

    check('Label LLM 顶部留白取拖动区高度', lbl_llm.padding_top == DEFAULT_PADDING_TOP,
          repr(lbl_llm.padding_top))
    check('Label 普通 toast 顶部留白取紧凑值', lbl_plain.padding_top == COMPACT_PADDING_TOP,
          repr(lbl_plain.padding_top))

    _, lbl_llm_h = geometry_size(lbl_llm.window)
    _, lbl_plain_h = geometry_size(lbl_plain.window)
    check('Label 普通窗口同样矮出留白之差',
          lbl_llm_h - lbl_plain_h == DEFAULT_PADDING_TOP - COMPACT_PADDING_TOP,
          'llm=%r plain=%r' % (lbl_llm_h, lbl_plain_h))

    for label, w in (('Label LLM', lbl_llm), ('Label 普通', lbl_plain)):
        _, y = w.content_canvas.coords(w._content_window_item)
        check('%s 内容项 y == padding_top' % label, int(y) == w.padding_top,
              'y=%r padding_top=%r' % (y, w.padding_top))

    # ================================================================
    # 7. 判别器与 close_llm_windows 用的是同一个标志
    # ================================================================
    print()
    print('[判别器一致性]')

    check('LLM 窗口 pause_and_resume 为真', llm.pause_and_resume is True)
    check('普通窗口 pause_and_resume 为假', plain.pause_and_resume is False)
    check('padding_top 与 pause_and_resume 同源',
          all((w.padding_top == DEFAULT_PADDING_TOP) == w.pause_and_resume
              for w in windows),
          '两者出现了分歧，close_llm_windows 的语义会和留白对不上')

finally:
    for w in windows:
        try:
            w.window.destroy()
        except tk.TclError:
            pass
    try:
        root.destroy()
    except tk.TclError:
        pass

print()
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
