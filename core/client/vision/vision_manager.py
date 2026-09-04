# coding: utf-8
"""
视觉输入管理器

注册截图快捷键，按下时把剪贴板当前内容里的图片读进内存待挂槽位。

线程模型是本模块唯一需要小心的地方：`GlobalHotKeys` 是 pynput `keyboard.Listener`
的子类，Windows 上走 `WH_KEYBOARD_LL` 低级钩子，**回调就在钩子线程上同步执行**。
而剪贴板读取这条链路（DIB 解码 + 缩放 + PNG 编码）在 4K 图上可能耗时数百毫秒，
远超系统对低级钩子的 300ms 容忍上限（同约定 12）。一旦超时被系统摘钩，连
`llm_stop_key`（ESC）都会一起失效。所以回调里只做一次 submit，实际读取全在
工作线程完成——「按下快捷键后立刻开始录音，这次录音不带图」正是这个异步的直接
后果，也是「下一条」从**挂载成功那一刻**起算的原因（见约定 21）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Optional

from config_client import ClientConfig as Config

from . import logger
from .clipboard_image import grab_clipboard_image
from .clipboard_watcher import ClipboardWatcher

if TYPE_CHECKING:
    from core.client.app import CapsWriterClient


class VisionManager:
    """截图快捷键与待挂图片的管理者"""

    def __init__(self, app: CapsWriterClient):
        self.app = app
        self.hotkey: str = ''
        self.enabled: bool = False
        self._executor: Optional[ThreadPoolExecutor] = None
        self._watcher = ClipboardWatcher()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """校验配置并注册快捷键。任何一条校验不过就静默禁用功能，不影响录音本身。"""
        hotkey = self._resolve_hotkey()
        if not hotkey:
            return

        self.hotkey = hotkey
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='vision')
        # 观测必须在注册快捷键之前起来：它记的是「剪贴板何时变的」，
        # 起晚了这段空窗期里的变更就永远算不出年龄
        if self._max_age() > 0:
            self._watcher.start()

        try:
            from core.client.global_hotkey import get_global_hotkey_manager
            manager = get_global_hotkey_manager()
            manager.register(hotkey, self._on_hotkey)
            manager.start()
        except Exception as e:
            logger.warning(f"注册截图快捷键失败，视觉输入已禁用: {e}")
            self._watcher.stop()
            self._shutdown_executor()
            return

        self.enabled = True
        logger.info(f"视觉输入已启用，截图快捷键: {hotkey}")

    def stop(self) -> None:
        """注销快捷键并停掉工作线程"""
        if self.enabled and self.hotkey:
            try:
                from core.client.global_hotkey import get_global_hotkey_manager
                get_global_hotkey_manager().unregister(self.hotkey)
            except Exception as e:
                logger.debug(f"注销截图快捷键失败: {e}")
        self.enabled = False
        self._watcher.stop()
        self._shutdown_executor()

    def _shutdown_executor(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    # ------------------------------------------------------------------
    # 配置校验
    # ------------------------------------------------------------------

    def _resolve_hotkey(self) -> str:
        """解析并校验截图快捷键，返回可用的 pynput 快捷键字符串（不可用则空串）"""
        hotkey = (getattr(Config, 'vision_capture_key', '') or '').strip()
        if not hotkey:
            logger.debug("未配置 vision_capture_key，视觉输入功能关闭")
            return ''

        if not getattr(Config, 'llm_enabled', False):
            logger.warning("llm_enabled 为 False，视觉输入功能已禁用（图片只对 LLM 角色有意义）")
            return ''

        # 必须在注册前自行校验：GlobalHotkeyManager 是单例，所有热键共用同一个
        # Listener，一个解析不了的组合会让整个 Listener 启动失败，把 llm_stop_key
        # （ESC 中断 LLM 输出）一起带走，而那个故障现场跟本功能毫无相似之处
        try:
            from pynput import keyboard
            keyboard.HotKey.parse(hotkey)
        except Exception as e:
            logger.warning(
                f"vision_capture_key = {hotkey!r} 不是合法的 pynput 快捷键（{e}），"
                f"视觉输入功能已禁用。格式示例: '<ctrl>+<alt>+d'"
            )
            return ''

        return hotkey

    # ------------------------------------------------------------------
    # 快捷键回调
    # ------------------------------------------------------------------

    def _on_hotkey(self) -> None:
        """快捷键回调。运行在低级键盘钩子线程上，只能派发、不能干活。"""
        executor = self._executor
        if executor is None:
            return
        try:
            executor.submit(self._capture)
        except RuntimeError:
            # 退出过程中 executor 已 shutdown
            pass

    @staticmethod
    def _max_age() -> float:
        """图片时效上限（秒），<= 0 表示不检查"""
        try:
            return float(getattr(Config, 'vision_clipboard_max_age', 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _is_fresh(self) -> bool:
        """剪贴板内容是否在时效内

        放在解码之前：过期就整个跳过，省掉一次可能上百毫秒的解码 + 编码。
        """
        max_age = self._max_age()
        if max_age <= 0:
            return True
        if not self._watcher.available:
            # 序号读不到时退回旧行为（不检查），而不是把功能连坐禁掉
            return True

        age = self._watcher.seconds_since_change()
        if age is None:
            logger.info("[视觉] 剪贴板自客户端启动以来未变动过，无法确认时效，"
                        "未挂载（复制一次即可）")
            return False
        if age > max_age:
            logger.info(f"[视觉] 剪贴板内容是 {age:.0f}s 前写入的，"
                        f"超过 {max_age:.0f}s 时效上限，未挂载")
            return False
        return True

    def _capture(self) -> None:
        """工作线程：读剪贴板，成功则存入待挂槽位"""
        if not self._is_fresh():
            return
        try:
            image = grab_clipboard_image()
        except Exception as e:
            logger.warning(f"[视觉] 截取剪贴板图片时出错: {e}", exc_info=True)
            return

        if image is None:
            # 剪贴板里没有图片是最常见的情况（复制的是文字、文件或什么都没有），
            # 不是错误。用户会在下一次录音时从提示框没有变色看出来
            logger.info("[视觉] 剪贴板中没有可用图片，未挂载")
            return

        self.app.state.set_pending_image(image)
        logger.info(
            f"[视觉] 已挂载图片 {image.width}×{image.height}"
            f"（{image.byte_size / 1024:.0f}KB，来源 {image.source}），"
            f"将随下一句话上传"
        )
