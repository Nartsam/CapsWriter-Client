# coding: utf-8
"""
进程 DPI 感知声明

`SetProcessDpiAwareness` 每个进程只能成功调用一次，之后的调用返回
E_ACCESSDENIED。历史上本项目有三处各自调用（toast_base / dialogs 在模块导入时
声明 1，tray 在函数体内声明 2），谁先被导入谁生效——实际链路是 app.py 先拉起
toast_base，所以 tray 里那句 2 从来没有生效过，是被 `except` 吞掉的死代码。
现统一到本模块，行为等同于此前实际生效的那一条。

为什么固定 PROCESS_SYSTEM_DPI_AWARE(1) 而不是 PER_MONITOR(2)：
现有 Tk 界面（Toast、录音悬浮框）的布局全是硬编码物理像素、不响应 WM_DPICHANGED，
换成 2 只会在混合 DPI 环境下得到「清晰但尺寸错误」的窗口，并不比 1 的「按系统 DPI
拉伸」更正确，却会改变已经调好的现有观感。真正的修法是整套布局按 DPI 缩放，
属于另一件事。注意 docs/DEV_NOTES.md 要求 Tk 文字度量的冒烟测试声明 2——本机两块
屏同为 192 DPI，1 与 2 表现一致，故既有测试结论仍然成立。
"""

import ctypes

# PROCESS_DPI_AWARENESS 枚举值
PROCESS_DPI_UNAWARE = 0
PROCESS_SYSTEM_DPI_AWARE = 1
PROCESS_PER_MONITOR_DPI_AWARE = 2

# 本项目统一采用的级别
TARGET_AWARENESS = PROCESS_SYSTEM_DPI_AWARE

_declared = False


def ensure_dpi_awareness() -> None:
    """声明进程 DPI 感知级别。可重复调用，只有第一次会真正生效。"""
    global _declared
    if _declared:
        return
    _declared = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(TARGET_AWARENESS)
    except (OSError, AttributeError):
        # Windows 7 / 非 Windows / 已被更早的调用方（如宿主程序）设定过，
        # 三种情况都无法也无需补救
        pass
