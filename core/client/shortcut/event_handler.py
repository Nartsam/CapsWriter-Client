# coding: utf-8
"""
事件处理器

处理键盘和鼠标事件的逻辑
"""

import time
from . import logger
from core.client.shortcut.key_mapper import MODIFIER_KEYS



class ShortcutEventHandler:
    """
    快捷键事件处理器

    处理按键按下和释放的逻辑，包括录音启动、取消、完成等
    """

    def __init__(self, tasks, pool, emulator):
        """
        初始化事件处理器

        Args:
            tasks: 快捷键任务字典
            pool: 线程池
            emulator: 快捷键模拟器
        """
        self.tasks = tasks
        self.pool = pool
        self.emulator = emulator

    def handle_keydown(self, key_name, task) -> None:
        """处理按键按下事件"""
        # 长按模式
        if task.shortcut.hold_mode:
            if not task.is_recording:
                task.launch()
            return

        # 单击模式
        if task.released:
            from threading import Event
            task.pressed = True
            task.released = False
            task.event = Event()  # 创建新事件对象
            self.pool.submit(self._count_down, task)
            self.pool.submit(self._manage_task, task)

    def handle_keyup(self, key_name, task) -> None:
        """处理按键释放事件"""
        # 单击模式
        if not task.shortcut.hold_mode:
            if task.pressed:
                task.pressed = False
                task.released = True
                task.event.set()
            self._release_suppressed_modifier(key_name, task)
            return

        # 长按模式
        if not task.is_recording:
            self._release_suppressed_modifier(key_name, task)
            return

        duration = time.time() - task.recording_start_time
        logger.debug(f"[{key_name}] 松开，持续时间: {duration:.2f}s")

        if duration < task.threshold:
            self._handle_short_press(key_name, task)
        else:
            try:
                task.finish()
            finally:
                self._release_suppressed_modifier(key_name, task)

    def _handle_short_press(self, key_name, task) -> None:
        """处理短按情况"""
        cancel_start = time.perf_counter()
        task.cancel()
        cancel_time = (time.perf_counter() - cancel_start) * 1000
        logger.debug(f"[{key_name}] task.cancel() 耗时: {cancel_time:.2f}ms")

        if task.shortcut.suppress:
            logger.debug(f"[{key_name}] 安排异步补发按键")
            self.pool.submit(self.emulator.emulate_key, key_name)

    def _release_suppressed_modifier(self, key_name, task) -> None:
        """释放被阻塞的修饰键，防止系统保留 Ctrl/Alt/Shift 按下状态"""
        if not task.shortcut.suppress or key_name not in MODIFIER_KEYS:
            return

        logger.debug(f"[{key_name}] 安排异步释放修饰键")
        self.pool.submit(self.emulator.release_key, key_name)

    def _count_down(self, task) -> None:
        """倒计时（单击模式）"""
        time.sleep(task.threshold)
        task.event.set()

    def _manage_task(self, task) -> None:
        """管理录音任务（单击模式）"""
        was_recording = task.is_recording

        if not was_recording:
            task.launch()

        if task.event.wait(timeout=task.threshold * 0.8):
            if task.is_recording and was_recording:
                task.finish()
        else:
            if not was_recording:
                task.cancel()
