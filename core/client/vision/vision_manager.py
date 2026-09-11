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

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Optional

from config_client import ClientConfig as Config

from . import logger
from .clipboard_image import grab_clipboard_image
from .image_archive import mark_outcome, save_mounted
from .clipboard_watcher import ClipboardWatcher, current_sequence
from .notice import notify

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
        # 已排队但还没开始执行的捕获。executor 只有一个工作线程，排着的那个跑起来时
        # 会读**那一刻**的剪贴板，因此它必然涵盖此刻之后的任何一次按键——再排一个
        # 只会重复解码、重复归档、重复刷一行终端，拿到的还是同一张图
        self._capture_queued = False
        self._capture_lock = threading.Lock()
        # 上一次成功挂载时的剪贴板序号。连按快捷键时用它判断这次是不是纯重复
        self._last_mounted_seq = 0

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

        with self._capture_lock:
            if self._capture_queued:
                # 已经有一个排着了，它跑起来时读到的就是最新的剪贴板内容
                logger.debug('[视觉] 已有排队中的捕获，本次按键合并')
                return
            self._capture_queued = True

        try:
            executor.submit(self._capture)
        except RuntimeError:
            # 退出过程中 executor 已 shutdown
            with self._capture_lock:
                self._capture_queued = False

    @staticmethod
    def _archive_keep() -> int:
        """归档保留张数，<= 0 表示关闭归档"""
        try:
            return int(getattr(Config, 'vision_archive_keep', 0) or 0)
        except (TypeError, ValueError):
            return 0

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
            notify('    [yellow]剪贴板自客户端启动以来未变动过，未挂载[/yellow]'
                          '（复制一次即可）')
            return False
        if age > max_age:
            logger.info(f"[视觉] 剪贴板内容是 {age:.0f}s 前写入的，"
                        f"超过 {max_age:.0f}s 时效上限，未挂载")
            notify(f'    [yellow]剪贴板内容是 {age:.0f}s 前写入的，'
                          f'超过 {max_age:.0f}s 时效上限，未挂载[/yellow]')
            return False
        return True

    def _capture(self) -> None:
        """工作线程：读剪贴板，成功则存入待挂槽位"""
        # 一开始执行就放闸：此刻之后的按键读到的可能是更新的剪贴板内容，
        # 必须让它能排进来，否则「先按快捷键、再截新图、再按一次」会漏掉新图
        with self._capture_lock:
            self._capture_queued = False

        # 剪贴板没变过、且上次挂上的那张还在待挂槽里 → 这次按键是纯重复，直接返回。
        # 合并闸门挡不住这种：人手速度的连按之间，上一个捕获早就跑完了（剪贴板没变时
        # 时效检查 1ms 内就返回），队列根本来不及堆积。不拦的话连按 N 次就是 N 次解码、
        # N 个归档文件、N 行终端输出，而拿到的是同一张图（2026-09-12 实机 9 连按刷了 9 行）。
        # 图一旦被某次录音消耗掉，pending_image 变空，同一张图就能重新挂上
        seq = current_sequence()
        if (seq and seq == self._last_mounted_seq
                and self.app.state.pending_image is not None):
            logger.debug('[视觉] 剪贴板未变且图片仍在待挂状态，跳过重复挂载')
            return

        if not self._is_fresh():
            return
        try:
            image = grab_clipboard_image()
        except Exception as e:
            logger.warning(f"[视觉] 截取剪贴板图片时出错: {e}", exc_info=True)
            return

        if image is None:
            # 具体原因（没有图 / 解码失败 / 体积超限）已由 grab_clipboard_image 自己
            # 打到控制台和日志，这里只记一条中性的收尾，不要再猜原因——此前这里写死
            # 「剪贴板中没有可用图片」，把体积超限也说成了没有图
            logger.info("[视觉] 本次未挂载图片")
            return

        # 归档在写入待挂槽之前：存盘可能失败，而 archive_path 要跟着这张图一路走到
        # 结果处理那边去改名，不能先挂一个没有路径的版本进去
        archive_path = save_mounted(self.app.base_dir, image, self._archive_keep())
        if archive_path:
            image = image._replace(archive_path=archive_path)

        # 待挂槽只有一个位置，后挂的顶掉先挂的。必须说出来：不然连按两次会看到
        # 两行「已挂载」，读起来像挂了两张，而实际上只有最后一张会被用
        replaced = self.app.state.pending_image
        self.app.state.set_pending_image(image)
        if replaced is not None:
            # 被顶掉的那张从没绑定到任何一次录音，所以没有 task_id
            mark_outcome(getattr(replaced, 'archive_path', ''), '', used=False)

        self._last_mounted_seq = seq

        note = '，已顶掉上一张待挂图片' if replaced is not None else ''
        detail = (f"{image.width}×{image.height}"
                  f"（{image.byte_size / 1024:.0f}KB，{image.encoding}，来源 {image.source}）")
        logger.info(f"[视觉] 已挂载图片 {detail}，将随下一句话上传{note}")
        # dedupe=False：挂载成功是状态变更，必须每次都打。两张不同的图完全可能有
        # 一样的尺寸、体积和来源，按文本去重会把第二次挂载吞掉
        notify(
            f'    [green]已挂载图片[/green] {detail}，将随下一句话上传'
            f'{("[yellow]" + note + "[/yellow]") if note else ""}',
            dedupe=False,
        )
        if image.archive_path:
            logger.info(f'[视觉] 图片已归档: {image.archive_path}')
