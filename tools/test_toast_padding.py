# coding: utf-8
"""
Toast 顶部留白分岔的真 Tk 冒烟测试

验证 2026-09-05 的改动：40px 顶部空白拖动区只给 LLM 角色白色对话框
（`smart_duration=True`），托盘/错误这类普通 toast 顶部与底部对齐（15px）。

**本脚本会真的构造 Tk 窗口**（无边框、置顶，屏幕上会闪一下），因此不进
`run_tests.py` 的流水线——那条流水线要能在没有桌面会话的环境里跑。改完
core/ui/toast_*.py 后手动跑一次。

用法（在仓库根目录执行）：python tools/test_toast_padding.py
"""
from __future__ import annotations

import importlib
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


def geometry_size(window):
    """从 wm geometry 字符串读回尺寸；winfo_height 在窗口未映射时返回 1"""
    spec = window.geometry().split('+')[0]
    w, h = spec.split('x')
    return int(w), int(h)


# ====================================================================
# 0. 常量本身
# ====================================================================
print('[常量]')
check('COMPACT 与底部对齐', COMPACT_PADDING_TOP == DEFAULT_PADDING_BOTTOM,
      '%r != %r' % (COMPACT_PADDING_TOP, DEFAULT_PADDING_BOTTOM))
check('LLM 顶部留白仍是 40', DEFAULT_PADDING_TOP == 40, repr(DEFAULT_PADDING_TOP))
check('两者确实不同', DEFAULT_PADDING_TOP != COMPACT_PADDING_TOP)

root = tk.Tk()
root.withdraw()
root.tk.call('tk', 'scaling', TK_SCALING_FACTOR)   # 与 ToastMessageManager 一致

windows = []


def make(cls, smart, text='测试文本', **kw):
    w = cls(
        root, text,
        font_size=kw.pop('font_size', 14),
        font_family='Microsoft YaHei UI',
        bg='#f7fbff', fg='black',
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
    # ================================================================
    # 1. 两类窗口的留白取值
    # ================================================================
    print()
    print('[Text 窗口的留白分岔]')

    llm = make(ToastWindowText, True)
    plain = make(ToastWindowText, False)

    check('LLM padding_top=40', llm.padding_top == DEFAULT_PADDING_TOP, repr(llm.padding_top))
    check('LLM height_padding=55',
          llm.height_padding == DEFAULT_PADDING_TOP + DEFAULT_PADDING_BOTTOM,
          repr(llm.height_padding))
    check('普通 padding_top=15', plain.padding_top == COMPACT_PADDING_TOP, repr(plain.padding_top))
    check('普通 height_padding=30',
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

    check('普通窗口比 LLM 窗口矮 25px（40-15）',
          llm_h - plain_h == DEFAULT_PADDING_TOP - COMPACT_PADDING_TOP,
          'llm=%r plain=%r line_height=%r' % (llm_h, plain_h, line))

    # 选 15 而不是 0 的理由：0 会让单行窗口撞上 MIN_WINDOW_HEIGHT 兜底，
    # 省下的留白原样挪到底部。这里断言兜底没有生效
    check('普通单行窗口没有被 MIN_WINDOW_HEIGHT 兜底', plain_h > MIN_WINDOW_HEIGHT,
          'plain=%r floor=%r（若不成立，顶部留白会被兜底转移到底部）'
          % (plain_h, MIN_WINDOW_HEIGHT))
    check('普通窗口高度 = 行高 + 30',
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
    check('三行窗口 = 3*行高 + 30', multi_h == line * 3 + multi.height_padding,
          'multi=%r line=%r' % (multi_h, line))

    # ================================================================
    # 6. Label 版本走同一条分岔
    # ================================================================
    print()
    print('[Label 窗口]')

    lbl_llm = make(ToastWindowLabel, True)
    lbl_plain = make(ToastWindowLabel, False)

    check('Label LLM padding_top=40', lbl_llm.padding_top == DEFAULT_PADDING_TOP,
          repr(lbl_llm.padding_top))
    check('Label 普通 padding_top=15', lbl_plain.padding_top == COMPACT_PADDING_TOP,
          repr(lbl_plain.padding_top))

    _, lbl_llm_h = geometry_size(lbl_llm.window)
    _, lbl_plain_h = geometry_size(lbl_plain.window)
    check('Label 普通窗口同样矮 25px',
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
