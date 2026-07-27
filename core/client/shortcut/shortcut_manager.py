# coding: utf-8
"""
快捷键管理器（重构版）

统一管理多个快捷键，处理键盘和鼠标事件，支持：
1. 多快捷键并发处理
2. 防止不同按键互相干扰
3. restore 功能的防自捕获逻辑
4. hold_mode 和 click_mode 支持
"""
from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Dict, List, Optional

from pynput import keyboard, mouse

from . import logger
from core.client.shortcut.key_mapper import *
from core.client.shortcut.key_mapper import KeyMapper
from core.client.shortcut.emulator import ShortcutEmulator
from core.client.shortcut.event_handler import ShortcutEventHandler
from core.client.shortcut.task import ShortcutTask

if TYPE_CHECKING:
    from core.client.shortcut.shortcut_config import Shortcut
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient



class ShortcutManager:
    """
    快捷键管理器

    统一管理多个快捷键，使用 pynput 监听键盘和鼠标事件。
    所有事件处理都在 win32_event_filter 中完成，确保高性能和低延迟。
    """

    def __init__(self, app: CapsWriterClient, shortcuts: List[Shortcut]):
        """
        初始化快捷键管理器

        Args:
            app: 客户端 App 实例
            shortcuts: 快捷键配置列表
        """
        self.app = app
        self.shortcuts = shortcuts

        # 监听器
        self.keyboard_listener: Optional[keyboard.Listener] = None
        self.mouse_listener: Optional[mouse.Listener] = None

        # 快捷键任务映射（key -> ShortcutTask）
        self.tasks: Dict[str, ShortcutTask] = {}

        # 线程池
        self._pool = ThreadPoolExecutor(max_workers=4)

        # 按键模拟器
        self._emulator = ShortcutEmulator()

        # 按键恢复状态追踪
        self._restoring_keys = set()

        # 打断键：录音中或等待识别结果时按下，本次输入作废。留空则禁用
        from config_client import ClientConfig as Config
        self._cancel_key = (getattr(Config, 'cancel_recording_key', '') or '').lower().strip()
        # 打断键抑制决策的锁存位，保证同一次按键的按下与松开处理一致
        self._cancel_suppressed = False

        # 事件处理器
        self._event_handler = ShortcutEventHandler(self.tasks, self._pool, self._emulator)

        # 初始化快捷键任务
        self._init_tasks()

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _init_tasks(self) -> None:
        """初始化所有快捷键任务"""
        from config_client import ClientConfig as Config

        for shortcut in self.shortcuts:
            if not shortcut.enabled:
                continue

            task = ShortcutTask(self.app, shortcut)
            task._manager_ref = lambda: self  # 弱引用，用于回调
            task.pool = self._pool
            task.threshold = shortcut.get_threshold(Config.threshold)
            self.tasks[shortcut.key] = task

    # ========== 监听器创建 ==========

    def create_keyboard_filter(self):
        """创建键盘事件过滤器"""
        def win32_event_filter(msg, data):
            # 只处理 KEYDOWN 和 KEYUP 消息
            if msg not in KEYBOARD_MESSAGES:
                return True

            key_name = KeyMapper.vk_to_name(data.vkCode)

            # 防自捕获检查
            if self._check_emulating(key_name, msg):
                return True
            if self._check_restoring(key_name, msg):
                return True

            # 打断键单独处理，不进入 tasks 状态机
            if self._cancel_key and key_name == self._cancel_key:
                try:
                    suppress = self._handle_cancel_key(msg)
                except Exception as e:
                    logger.error(f"[{key_name}] 打断键处理异常: {e}")
                    # 异常时沿用锁存值，保证按下与松开的抑制决策仍然配对
                    suppress = self._cancel_suppressed
                if suppress and self.keyboard_listener:
                    self.keyboard_listener.suppress_event()
                return True

            # 查找匹配的快捷键
            if key_name not in self.tasks:
                return True

            task = self.tasks[key_name]

            # 处理按键事件
            try:
                if msg in KEY_DOWN_MESSAGES:
                    self._event_handler.handle_keydown(key_name, task)
                elif msg in KEY_UP_MESSAGES:
                    self._event_handler.handle_keyup(key_name, task)
            except Exception as e:
                logger.error(f"[{key_name}] 事件处理异常: {e}")
            finally:
                if task.shortcut.suppress and self.keyboard_listener:
                    self.keyboard_listener.suppress_event()

            return True

        return win32_event_filter

    def create_mouse_filter(self):
        """创建鼠标事件过滤器"""
        def win32_event_filter(msg, data):
            # 只处理 XBUTTON 消息
            if msg not in MOUSE_MESSAGES:
                return True

            # 获取按键标识
            xbutton = (data.mouseData >> 16) & 0xFFFF
            button_name = 'x1' if xbutton == XBUTTON1 else 'x2'

            # 防自捕获检查
            if self._check_emulating(button_name, msg, is_mouse=True):
                return True

            # 查找匹配的快捷键
            if button_name not in self.tasks:
                return True

            task = self.tasks[button_name]

            # 处理鼠标事件
            try:
                if msg == WM_XBUTTONDOWN:
                    self._event_handler.handle_keydown(button_name, task)
                elif msg == WM_XBUTTONUP:
                    self._handle_mouse_keyup(button_name, task)
            except Exception as e:
                logger.error(f"[{button_name}] 事件处理异常: {e}")
            finally:
                if task.shortcut.suppress and self.mouse_listener:
                    self.mouse_listener.suppress_event()

            return True

        return win32_event_filter

    def _handle_mouse_keyup(self, button_name: str, task) -> None:
        """处理鼠标按键释放事件"""
        # 单击模式
        if not task.shortcut.hold_mode:
            if task.pressed:
                task.pressed = False
                task.released = True
                task.event.set()
            return

        # 长按模式
        if not task.is_recording:
            return

        duration = time.time() - task.recording_start_time
        logger.debug(f"[{button_name}] 松开按键，持续时间: {duration:.3f}s")

        if duration < task.threshold:
            task.cancel()
            if task.shortcut.suppress:
                logger.debug(f"[{button_name}] 安排异步补发鼠标按键")
                self._pool.submit(self._emulator.emulate_mouse_click, button_name)
        else:
            task.finish()

    # ========== 打断键处理 ==========

    def _handle_cancel_key(self, msg: int) -> bool:
        """
        处理打断键事件

        Args:
            msg: Windows 键盘消息

        Returns:
            本次事件是否需要拦截（不传给前台程序）

        抑制决策在 KEYDOWN 时锁存、KEYUP 时无条件沿用：判据（是否正在录音）会被
        打断动作本身翻转，若两次各自判断就会「吞掉按下、放行松开」，前台程序会收到
        没有配对按下的松开事件，pynput 组合键监听器的内部按键集合也会随之失衡。
        """
        if msg in KEY_DOWN_MESSAGES:
            # 没有待处理的输入时完全不介入，按键照常传给前台程序
            if not self.state.pending_task_id:
                self._cancel_suppressed = False
                return False

            # 只在录音阶段拦截。等待识别结果阶段放行：那个状态依赖远程服务端回包，
            # 一旦服务端不回，拦截会让打断键全局失灵
            self._cancel_suppressed = self.state.recording

            # 长按会产生连续 KEYDOWN，abort 自身幂等，重复触发会被忽略
            for task in self.tasks.values():
                if task.is_recording:
                    task.abort()
                    break
            else:
                # 按键已松开、正在等结果：没有录音可停，只需丢弃即将到来的结果
                self._abort_pending_result()

            return self._cancel_suppressed

        if msg in KEY_UP_MESSAGES:
            suppressed = self._cancel_suppressed
            self._cancel_suppressed = False
            return suppressed

        return False

    def _abort_pending_result(self) -> None:
        """打断已进入等待识别结果阶段的任务：录音早已结束，只需丢弃结果"""
        task_id = self.state.pending_task_id
        if not task_id:
            return
        # 钩子线程只做派发，实际处理交给线程池，避免低级钩子超时被系统摘掉
        self._pool.submit(self._finish_pending_abort, task_id)

    def _finish_pending_abort(self, task_id: str) -> None:
        """等待阶段打断的实际处理，在线程池中执行"""
        from core.client.ui import show_cancelled_indicator

        try:
            logger.info(f"手动打断等待中的识别结果，任务ID: {task_id}")
            self.state.mark_task_cancelled(task_id)
            show_cancelled_indicator(self._cancel_key)

            if self.app.companion.has_connections:
                self.app.companion.emit_recording_cancel_threadsafe()
        except Exception as e:
            logger.error(f"打断等待中的识别结果失败: {e}", exc_info=True)

    # ========== 按键恢复管理 ==========

    def schedule_restore(self, key: str) -> None:
        """
        安排按键恢复（延迟执行，避免在事件处理中阻塞）

        Args:
            key: 要恢复的按键

        注意：标志清除只在按键释放事件中处理（_check_restoring），
        避免在线程中提前清除导致主线程收到重复消息。
        """
        from pynput import keyboard

        self._restoring_keys.add(key)

        def do_restore():
            import time
            time.sleep(0.05)  # 延迟 50ms
            if key == 'caps_lock':
                controller = keyboard.Controller()
                controller.press(keyboard.Key.caps_lock)
                controller.release(keyboard.Key.caps_lock)

        self._pool.submit(do_restore)

    def is_restoring(self, key: str) -> bool:
        """检查是否正在恢复指定按键"""
        return key in self._restoring_keys

    def clear_restoring_flag(self, key: str) -> None:
        """清除恢复标志"""
        self._restoring_keys.discard(key)

    # ========== 防自捕获检查 ==========

    def _check_emulating(self, key_name: str, msg: int, is_mouse: bool = False) -> bool:
        """检查是否正在模拟按键"""
        if not self._emulator.is_emulating(key_name):
            return False

        # 松开时清除标志
        if is_mouse:
            if msg == WM_XBUTTONUP:
                self._emulator.clear_emulating_flag(key_name)
        else:
            if msg in (WM_KEYUP, WM_SYSKEYUP):
                self._emulator.clear_emulating_flag(key_name)

        return True  # 放行

    def _check_restoring(self, key_name: str, msg: int) -> bool:
        """检查是否正在恢复按键"""
        if not self.is_restoring(key_name):
            return False

        if msg in (WM_KEYUP, WM_SYSKEYUP):
            self.clear_restoring_flag(key_name)

        return True  # 放行

    # ========== 公共接口 ==========

    def start(self) -> None:
        """启动所有监听器"""
        has_keyboard = any(s.type == 'keyboard' for s in self.shortcuts if s.enabled)
        has_mouse = any(s.type == 'mouse' for s in self.shortcuts if s.enabled)

        if has_keyboard:
            if self.keyboard_listener and self.keyboard_listener.is_alive():
                logger.debug("键盘监听器已在运行，跳过启动")
            else:
                self.keyboard_listener = keyboard.Listener(
                    win32_event_filter=self.create_keyboard_filter()
                )
                self.keyboard_listener.start()
                logger.info("键盘监听器已启动")

        if has_mouse:
            if self.mouse_listener and self.mouse_listener.is_alive():
                logger.debug("鼠标监听器已在运行，跳过启动")
            else:
                self.mouse_listener = mouse.Listener(
                    win32_event_filter=self.create_mouse_filter()
                )
                self.mouse_listener.start()
                logger.info("鼠标监听器已启动")

        # 打印所有启用的快捷键
        for shortcut in self.shortcuts:
            if shortcut.enabled:
                mode = "长按" if shortcut.hold_mode else "单击"
                toggle = "可恢复" if shortcut.is_toggle_key() else "普通键"
                logger.info(f"  [{shortcut.key}] {mode}模式, 阻塞:{shortcut.suppress}, {toggle}")

        if self._cancel_key:
            logger.info(f"  [{self._cancel_key}] 打断键，录音中阻塞、等待结果时放行")

    def stop(self) -> None:
        """停止所有监听器和清理资源"""
        if self.keyboard_listener:
            try:
                self.keyboard_listener.stop()
                logger.debug("键盘监听器已停止")
            except Exception:
                pass
            finally:
                self.keyboard_listener = None
                
        if self.mouse_listener:
            try:
                self.mouse_listener.stop()
                logger.debug("鼠标监听器已停止")
            except Exception:
                pass
            finally:
                self.mouse_listener = None

        # 取消所有任务
        for task in self.tasks.values():
            if task.is_recording:
                task.cancel()

        # 关闭线程池
        self._pool.shutdown(wait=False)
        logger.debug("快捷键管理器线程池已关闭")
