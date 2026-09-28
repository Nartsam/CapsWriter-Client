# coding: utf-8
"""
剪贴板变更时刻观测

Windows 剪贴板**不携带任何时间戳**：`GetClipboardSequenceNumber` 只是个变更计数器，
`GetClipboardOwner` 只给 HWND，二者都答不了「这份数据是什么时候放进来的」。
Win+V 的 `ClipboardHistoryItem.Timestamp` 确有时间，但云剪贴板历史只收录部分格式、
应用还能主动排除（JPEGView 那类 Ctrl+C 就不进历史），而且要 WinRT——内嵌的
Python 3.8 运行时里没有。所以时间只能自己记。

做法：后台线程按 `POLL_INTERVAL` 轮询序号，一变就记下 `time.monotonic()`。
序号读取不需要 `OpenClipboard`，与其他程序零争用，实测单次约 0.5 微秒。
没用事件驱动的 `AddClipboardFormatListener`：那要建 message-only 窗口加一条消息泵
线程，而轮询的成本已经在噪声底下（见 POLL_INTERVAL 的注释）。

两个必须一起做的细节：
1. **检查时要再同步读一次序号**。用户截完图 0.2s 就按快捷键时，轮询可能还没看到
   变化，`_changed_at` 还停在更早那次，会把新图误判成过期。同步补读一次，与上次
   已知值不同就当作「刚刚变的」，误差因此被限制在一个轮询周期内。
2. **客户端启动时剪贴板里已有的内容，时刻是「未知」而不是「现在」**。既然规则是
   「必须是近 N 秒内写入的」，证不出来就不算——否则重启客户端会让一张放了一天的
   旧图凭空变新。代价是重启后需要重新复制一次，属于低频摩擦。
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Optional

from . import logger

# 轮询周期（秒）。实测单次序号读取 0.455 微秒，按此周期算下来累计 CPU 时间约
# 0.33 毫秒/小时，成本几乎全在线程唤醒本身，已在噪声底下（对比：录音指示器录音
# 期间每 100ms 一次 _tick 且含 Tk 重绘，热词 watchdog 更是常驻文件监视）。
#
# 上界由误差方向决定：同步补读只会把「其实已经放了一会儿」的内容当成刚刚变的，
# 也就是**只会高估新鲜度、不会误杀新图**，高估上限正好是一个周期。所以周期必须
# 显著小于时效阈值——5s 对 60s 是 12 倍余量（最坏放行 65s 前的图）。
# 再往上调（如 30s）会让这个误差变得可观，收益却只有每小时几分之一毫秒。
POLL_INTERVAL = 5.0

_user32 = ctypes.windll.user32
_user32.GetClipboardSequenceNumber.restype = ctypes.c_uint


def _sequence_number() -> int:
    """当前剪贴板序号。返回 0 表示本进程没有剪贴板访问权限。"""
    try:
        return _user32.GetClipboardSequenceNumber()
    except Exception:
        return 0


def current_sequence() -> int:
    """当前剪贴板序号；0 表示读不到（本进程没有剪贴板访问权限）

    序号只是**变更计数器**，不含时间信息，但「两次读到的值相同」足以断定剪贴板
    内容没有被重写过——连按快捷键时用它判断这次是不是纯重复。调用不需要
    OpenClipboard，零争用、单次 0.455µs。
    """
    return _sequence_number()


class ClipboardWatcher:
    """记录剪贴板最近一次变更的时刻"""

    def __init__(self, poll_interval: float = POLL_INTERVAL):
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_seq = 0
        self._changed_at: Optional[float] = None   # None = 未知
        self._available = True

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        seq = _sequence_number()
        if seq == 0:
            # 拿不到序号就无从判断新鲜度。此时不该把功能连坐禁掉，
            # 而是退回「不检查时效」的旧行为，并且只告警一次
            self._available = False
            logger.warning("无法读取剪贴板序号，图片时效检查已停用（其余功能不受影响）")
            return
        with self._lock:
            self._last_seq = seq
            self._changed_at = None
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name='clipboard-watcher', daemon=True)
        self._thread.start()
        logger.debug(f"剪贴板变更观测已启动，轮询周期 {self.poll_interval}s")

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._refresh()
            self._stop.wait(self.poll_interval)

    def _refresh(self) -> None:
        """读一次序号，与上次不同则把变更时刻更新为此刻"""
        seq = _sequence_number()
        if not seq:
            return
        with self._lock:
            if seq != self._last_seq:
                self._last_seq = seq
                self._changed_at = time.monotonic()

    def seconds_since_change(self) -> Optional[float]:
        """剪贴板内容写入至今的秒数

        Returns:
            秒数；无法判断时返回 None（观测不可用，或客户端启动后剪贴板一直没变过）
        """
        if not self._available:
            return None
        # 同步补读：轮询可能还没赶上刚刚发生的变化
        self._refresh()
        with self._lock:
            if self._changed_at is None:
                return None
            return time.monotonic() - self._changed_at

    @property
    def available(self) -> bool:
        """观测是否可用。为 False 时调用方应跳过时效检查而不是拒绝挂载。"""
        return self._available
