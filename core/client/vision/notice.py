# coding: utf-8
"""
视觉输入的终端提示（连续重复的只打一次）

按下快捷键的结果必须当场可见，但连按时每次的结果往往一模一样——
剪贴板没变，连按 9 次就是 9 行一字不差的提示（2026-09-12 实机截图）。刷屏本身
会把有用信息顶出屏幕，所以这里做一层「与上一条完全相同就不再打」的去重。

**去重只能用在「什么都没发生」的提示上**（没有图、体积超限、时效过期这一类）。
挂载成功要传 `dedupe=False`：那代表状态真的变了，而两张不同的图完全可能渲染出
一模一样的一行字，吞掉第二条就等于让用户以为新图没挂上。同一张图被反复挂载导致
的刷屏不归这里管——那在更前面就被剪贴板序号拦掉了（见 `VisionManager._capture`）。

**只去重终端输出，文件日志不受影响**：日志每次都记，事后仍能数清按了几次。

为什么用 console.print 而不是提高日志级别：logger.py 把控制台固定在 WARNING，
视觉链路的日志全是 INFO，于是按下快捷键后成功与失败在终端上一模一样（都没有
任何输出），要等下一次录音看有没有描边才知道——那时话都快说完了。诊断仍走
INFO 不升级（升级等于往运行终端刷字）；按快捷键是明确的用户动作、一次一行，
所以结果直接 console.print。挂载失败的具体原因由 clipboard_image 自己打，
新增失败分支时要一并补这一行。
"""

from __future__ import annotations

import threading

from core.client.state import console

_lock = threading.Lock()
_last_message = ''


def notify(message: str, *, dedupe: bool = True) -> None:
    """打印一条结果提示

    Args:
        message: 要打印的内容
        dedupe: 与上一条完全相同时是否跳过。**只有「什么都没发生」的提示才能用
            默认的 True**——挂载成功意味着状态真的变了，必须每次都打：两张不同的图
            完全可能有一样的尺寸、体积和来源（都是 401×169 的 1KB PNG 截图并不罕见，
            而体积还是按整数 KB 显示的），靠文本去重会把第二次挂载整条吞掉，
            用户就看不出新图到底挂上没有。

    无论是否跳过打印，都会刷新「上一条」——否则中间插了一条不去重的消息后，
    再出现与更早那条相同的内容会被误吞。
    """
    global _last_message
    with _lock:
        skip = dedupe and message == _last_message
        _last_message = message
    if not skip:
        console.print(message)


def reset() -> None:
    """清掉去重状态，让下一条无论如何都会打印"""
    global _last_message
    with _lock:
        _last_message = ''
