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
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from pynput import keyboard, mouse

from . import logger
from core.client.shortcut.key_mapper import *
from core.client.shortcut.key_mapper import KeyMapper, MODIFIER_KEYS, MODIFIER_FAMILY
from core.client.shortcut.emulator import ShortcutEmulator
from core.client.shortcut.event_handler import ShortcutEventHandler
from core.client.shortcut.task import ShortcutTask
from core.tools.window_detector import get_foreground_process_name

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

        self._passthrough_apps = self._resolve_passthrough_apps()
        # 每轮按键事件的（交给前台、抑制原始事件），KEYUP 后移除。
        self._shortcut_routes: Dict[str, Tuple[bool, bool]] = {}

        # 打断键：录音中或等待识别结果时按下，本次输入作废。留空则禁用
        from config_client import ClientConfig as Config
        self._cancel_key = (getattr(Config, 'cancel_recording_key', '') or '').lower().strip()
        # 打断键抑制决策的锁存位，保证同一次按键的按下与松开处理一致
        self._cancel_suppressed = False
        # 打断键是否处于按下状态：按住不放会连发 KEYDOWN，只有第一次参与判定
        self._cancel_key_down = False

        # 连续对话切换键：录音期间按下即切换粘滞路由。留空或配置非法则禁用
        self._chat_key = self._resolve_chat_key()
        self._chat_suppressed = False
        # 按住不放会连发 KEYDOWN，而切换不是幂等操作，用它保证一次物理按下只切换一次
        self._chat_key_down = False

        # 事件处理器
        self._event_handler = ShortcutEventHandler(self.tasks, self._pool, self._emulator)

        # 初始化快捷键任务
        self._init_tasks()

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _resolve_passthrough_apps(self):
        from config_client import ClientConfig as Config

        apps = getattr(Config, 'shortcut_passthrough_apps', [])
        if (not isinstance(apps, (list, tuple))
                or any(not isinstance(name, str) for name in apps)):
            logger.warning("shortcut_passthrough_apps 应为进程名列表，已关闭录音键让行")
            return set()
        return {name.strip().lower() for name in apps if name.strip()}

    def _route_shortcut(self, key_name: str, is_keydown: bool) -> Tuple[bool, bool]:
        """返回（交给前台、抑制原始事件）；一次按下到松开只判定一次。"""
        task = self.tasks[key_name]
        if not self._passthrough_apps:
            return False, task.shortcut.suppress
        if not is_keydown:
            # 没见过按下的孤立松开直接放行，例如客户端启动时用户已经按着 Ctrl。
            return self._shortcut_routes.pop(key_name, (True, False))
        if key_name not in self._shortcut_routes:
            passthrough = False
            stopping = task.is_recording and not task.shortcut.hold_mode
            # 已有本机录音保留结束入口，打断后也保持原归属直到物理松开。
            if not task.is_recording and not task.wait_release:
                name = get_foreground_process_name()
                passthrough = not name or name.lower() in self._passthrough_apps
            suppress = not passthrough and (task.shortcut.suppress or stopping)
            self._shortcut_routes[key_name] = passthrough, suppress
        return self._shortcut_routes[key_name]

    def _resolve_chat_key(self) -> str:
        """
        解析并校验连续对话切换键

        非法配置一律禁用功能而不是勉强运行：这个键会在录音期间被拦截，
        配错了要么抢掉录音键、要么把修饰键卡在按下状态，都比功能不可用严重。

        Returns:
            合法的按键名；配置为空或不合法时返回空字符串
        """
        from config_client import ClientConfig as Config

        key = (getattr(Config, 'continuous_chat_key', '') or '').lower().strip()
        if not key:
            return ''

        if not getattr(Config, 'llm_enabled', False):
            logger.info("llm_enabled 为 False，连续对话功能不启用")
            return ''

        if key == self._cancel_key:
            logger.warning(f"连续对话切换键与打断键同为 [{key}]，已禁用连续对话功能")
            return ''

        if key in {s.key for s in self.shortcuts if s.enabled}:
            logger.warning(f"连续对话切换键 [{key}] 与录音快捷键冲突，已禁用连续对话功能")
            return ''

        # 修饰键被抑制时有卡在「按下」状态的风险（见 key_mapper.MODIFIER_KEYS 上方注释），
        # 录音键为此专门补发合成释放事件；切换键没有非用修饰键不可的理由，直接拒绝
        if key in MODIFIER_KEYS:
            logger.warning(f"连续对话切换键不支持修饰键 [{key}]，已禁用连续对话功能")
            return ''

        if key in ('x1', 'x2'):
            logger.warning(f"连续对话切换键不支持鼠标键 [{key}]，已禁用连续对话功能")
            return ''

        return key

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

            # 连续对话切换键同样不进 tasks 状态机
            if self._chat_key and key_name == self._chat_key:
                try:
                    suppress = self._handle_chat_key(msg)
                except Exception as e:
                    logger.error(f"[{key_name}] 连续对话切换键处理异常: {e}")
                    # 异常时沿用锁存值，保证按下与松开的抑制决策仍然配对
                    suppress = self._chat_suppressed
                if suppress and self.keyboard_listener:
                    self.keyboard_listener.suppress_event()
                return True

            task = self.tasks.get(key_name)
            if task is not None:
                passthrough, suppress = self._route_shortcut(key_name, msg in KEY_DOWN_MESSAGES)
                if passthrough:
                    # 命中名单的配置录音键优先让行；同族同步保护只处理其余按键。
                    # 包括 KEYUP：不能进入短按补发、修饰键补发或录音状态机。
                    return True

            if msg in KEY_DOWN_MESSAGES and self._is_modifier_sync(key_name):
                if self.keyboard_listener:
                    self.keyboard_listener.suppress_event()
                return True

            # 查找匹配的快捷键
            if task is None:
                return True

            # 处理按键事件
            try:
                if msg in KEY_DOWN_MESSAGES:
                    self._event_handler.handle_keydown(key_name, task)
                elif msg in KEY_UP_MESSAGES:
                    self._event_handler.handle_keyup(key_name, task)
            except Exception as e:
                logger.error(f"[{key_name}] 事件处理异常: {e}")
            finally:
                if suppress and self.keyboard_listener:
                    # 单击录音的结束键即使原配为非阻塞，也要拦截并清理修饰键状态。
                    # 原本阻塞的键已由 event_handler 补发，避免重复释放。
                    if (msg in KEY_UP_MESSAGES and not task.shortcut.suppress
                            and key_name in MODIFIER_KEYS):
                        self._pool.submit(self._emulator.release_key, key_name)
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
            passthrough, suppress = self._route_shortcut(button_name, msg == WM_XBUTTONDOWN)
            if passthrough:
                return True

            # 处理鼠标事件
            try:
                if msg == WM_XBUTTONDOWN:
                    self._event_handler.handle_keydown(button_name, task)
                elif msg == WM_XBUTTONUP:
                    self._handle_mouse_keyup(button_name, task)
            except Exception as e:
                logger.error(f"[{button_name}] 事件处理异常: {e}")
            finally:
                if suppress and self.mouse_listener:
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
            task.wait_release = False
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

    # ========== 远控软件的修饰键同步 ==========

    def _is_modifier_sync(self, key_name: str) -> bool:
        """
        是否为录音键按住期间、同族修饰键的按下（如按住右 Ctrl 录音时来了一个左 Ctrl 按下）

        录音键的按下被拦截，系统里这个修饰键一直是松开的。RayLink 转发其他按键（如打断键 Esc）时
        发现两边状态不一致，会先注入一个左 Ctrl 按下来「同步」，事后却不补发松开：左 Ctrl 卡在按下
        状态，之后的单击都成了 Ctrl+单击（Notepad4 里会多出一个光标）。这类按下一并拦截；松开照常
        放行，修饰键的释放多发无害。只看长按模式：单击模式录音时键并没有按着，左 Ctrl 是用户真按的。
        """
        family = MODIFIER_FAMILY.get(key_name)
        if family is None:
            return False
        for key, task in self.tasks.items():
            if (key != key_name and MODIFIER_FAMILY.get(key) == family
                    and task.shortcut.suppress and task.shortcut.hold_mode
                    and (task.is_recording or task.wait_release)):
                return True
        return False

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
            # 按住不放的连发沿用第一次的决策：打断会清掉 pending_task_id，若让连发重新判定，
            # 后续连发和松开都会被放行，前台程序收到一个没头没尾的 Esc（微信里会关掉聊天窗口）
            if self._cancel_key_down:
                return self._cancel_suppressed
            self._cancel_key_down = True

            # 没有待处理的输入时完全不介入，按键照常传给前台程序
            if not self.state.pending_task_id:
                self._cancel_suppressed = False
                return False

            # 只在录音阶段拦截。等待识别结果阶段放行：那个状态依赖远程服务端回包，
            # 一旦服务端不回，拦截会让打断键全局失灵
            self._cancel_suppressed = self.state.recording

            for task in self.tasks.values():
                if task.is_recording:
                    task.abort()
                    break
            else:
                # 按键已松开、正在等结果：没有录音可停，只需丢弃即将到来的结果
                self._abort_pending_result()

            return self._cancel_suppressed

        if msg in KEY_UP_MESSAGES:
            self._cancel_key_down = False
            suppressed = self._cancel_suppressed
            self._cancel_suppressed = False
            return suppressed

        return False

    # ========== 连续对话切换键处理 ==========

    def _handle_chat_key(self, msg: int) -> bool:
        """
        处理连续对话切换键事件

        Args:
            msg: Windows 键盘消息

        Returns:
            本次事件是否需要拦截（不传给前台程序）

        与打断键同构：抑制决策在 KEYDOWN 锁存、KEYUP 无条件沿用（原因见 _handle_cancel_key）。
        录音期间必须拦截——录音键 ctrl_r 的 KEYDOWN 虽然被吞了，但物理上它确实按着，
        放行的空格会被系统当成真正的 Ctrl+Space，也就是输入法中英文切换热键。
        """
        if msg in KEY_DOWN_MESSAGES:
            # 长按会连发 KEYDOWN。打断是幂等的所以无所谓，切换不是，只认第一次
            if self._chat_key_down:
                return self._chat_suppressed
            self._chat_key_down = True

            # 只在录音期间介入，其余时候这个键与本程序无关
            self._chat_suppressed = self.state.recording
            if not self._chat_suppressed:
                return False

            start_time = self.state.recording_start_time
            snapshot = self.state.toggle_continuous()
            if snapshot is not None:
                # 钩子线程只做状态翻转和派发：低级钩子超过 300ms 会被系统摘掉。
                # 这里只是往指示器的队列里塞一条命令，实际渲染在 Tk 线程
                from core.client.ui import update_recording_indicator_continuous
                update_recording_indicator_continuous(
                    snapshot.indicator_mode(), snapshot.sticky_role or '', start_time
                )
                logger.debug(f"连续对话切换键：{snapshot.indicator_mode() or '普通录音'}")
            return True

        if msg in KEY_UP_MESSAGES:
            self._chat_key_down = False
            suppressed = self._chat_suppressed
            self._chat_suppressed = False
            return suppressed

        return False

    def _abort_pending_result(self) -> None:
        task_id = self.state.pending_task_id
        session = self.state.recognition_sessions.get(task_id)
        if session is None or session.terminal:
            return
        # 在钩子中先锁存取消意图，避免排队期间最终包抢先进入角色流水线。
        self.state.mark_task_cancelled(task_id)
        self.app.loop.call_soon_threadsafe(self.app.result_processor.fail_task_soon, task_id, '')

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

        if self._chat_key:
            logger.info(f"  [{self._chat_key}] 连续对话切换键，录音期间阻塞")

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
