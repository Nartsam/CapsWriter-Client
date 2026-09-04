# coding: utf-8
"""
快捷键任务模块

管理单个快捷键的录音任务状态
"""

from __future__ import annotations
import asyncio
import time
from threading import Event
from typing import TYPE_CHECKING, Optional

from . import logger
from core.tools.my_status import Status
 
if TYPE_CHECKING:
    from core.client.shortcut.shortcut_config import Shortcut
    from core.client.state import ClientState
    from core.client.audio.recorder import AudioRecorder
    from core.client.app import CapsWriterClient



class ShortcutTask:
    """
    单个快捷键的录音任务

    跟踪每个快捷键独立的录音状态，防止互相干扰。
    """

    def __init__(self, app: CapsWriterClient, shortcut: Shortcut, recorder_class=None):
        """
        初始化快捷键任务

        Args:
            app: 客户端 App 实例
            shortcut: 快捷键配置
            recorder_class: AudioRecorder 类（可选，用于延迟导入）
        """
        self.app = app
        self.shortcut = shortcut
        self._recorder_class = recorder_class

        # 任务状态
        self.task: Optional[asyncio.Future] = None
        self.recorder: Optional[AudioRecorder] = None
        self.recording_start_time: float = 0.0
        self.is_recording: bool = False

        # hold_mode 状态跟踪
        self.pressed: bool = False
        self.released: bool = True
        self.event: Event = Event()

        # 线程池（用于 countdown）
        self.pool = None

        # 录音状态动画
        self._status = Status('开始录音', spinner='point')

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _get_recorder(self) -> AudioRecorder:
        """获取 AudioRecorder 实例"""
        if self._recorder_class is None:
            from core.client.audio.recorder import AudioRecorder
            self._recorder_class = AudioRecorder
        return self._recorder_class(self.app)

    def launch(self) -> None:
        """启动录音任务"""
        logger.info(f"[{self.shortcut.key}] 触发：开始录音")

        # 记录开始时间
        self.recording_start_time = time.time()
        self.is_recording = True

        # 将开始标志放入队列
        asyncio.run_coroutine_threadsafe(
            self.state.queue_in.put({'type': 'begin', 'time': self.recording_start_time, 'data': None}),
            self.app.loop
        )

        # 连续对话：先做闲置超时的懒判断，再按此刻生效的粘滞角色建立本次录音的路由快照。
        # 必须排在 start_recording() 之前：切换键以 state.recording 为介入判据，
        # 反过来会留下「已在录音、快照还没换」的窗口，那时的切换会翻到上一次录音
        # 尚未消费的快照上去（UDP 控制录音与键盘钩子不在同一线程，确实可能撞上）
        snapshot = self._prepare_continuous()

        # 待挂图片在这一刻绑定到本次录音。剪贴板读取是异步的，此刻还没读完就是
        # 一次不带图的普通录音——「下一条」从图片挂载成功那一刻起算（见约定 21）
        pending_image = self.state.begin_image_snapshot()

        # 更新录音状态
        self.state.start_recording(self.recording_start_time)

        # 显示录音悬浮提示：传入快捷键名，提示器会转成中文名称。
        from core.client.ui import show_recording_indicator
        show_recording_indicator(
            self.recording_start_time,
            self.shortcut.key,
            snapshot.indicator_mode(),
            snapshot.sticky_role or '',
            pending_image is not None,
        )

        # 通知 Companion 开始录音
        if self.app.companion.has_connections:
            self.app.companion.emit_recording_start_threadsafe()

        # 启动识别任务；保留 recorder 引用，打断时需要用它的 task_id 丢弃结果
        recorder = self._get_recorder()
        self.recorder = recorder
        self.task = asyncio.run_coroutine_threadsafe(
            recorder.record_and_send(),
            self.app.loop,
        )

        # 控制台动画移到线程池，避免在键盘钩子回调中创建线程导致超时
        if self.pool:
            self.pool.submit(self._status.start)
        else:
            self._status.start()

    def _prepare_continuous(self):
        """录音启动时的连续对话准备：闲置超时懒判断 + 建立路由快照

        不开后台定时器，超时只在这里判定——录音键按下时才需要知道结果，
        而指示器紧接着就会把判定后的状态显示出来，用户自证即可，无需额外提示。
        """
        from config_client import ClientConfig as Config

        timeout = getattr(Config, 'continuous_chat_timeout', 0) or 0
        expired = self.state.expire_continuous_if_idle(timeout)
        if expired:
            logger.info(f"连续对话闲置超过 {timeout}s，已自动退出: {expired}")
        return self.state.begin_continuous_snapshot()

    def cancel(self) -> None:
        """取消录音任务（时间过短）"""
        logger.debug(f"[{self.shortcut.key}] 取消录音任务（时间过短）")

        self.is_recording = False
        self.state.stop_recording()
        self._status.stop()

        # 这次录音不算数，本次录音期间按过的连续对话切换键、挂上的图片一并回滚
        cancel_task_id = self.recorder.task_id if self.recorder else None
        self.state.rollback_continuous_snapshot(cancel_task_id)
        if self.state.rollback_image_snapshot(cancel_task_id):
            logger.info("短按取消，图片已退回待挂状态")
        # 短按取消录音时同步隐藏悬浮提示，避免空提示残留。
        from core.client.ui import hide_recording_indicator
        hide_recording_indicator()

        # 短按时音频还全在客户端缓存里、一个字节都没发出，服务端不会回结果，
        # 所以只需撤销待处理登记，不必登记进丢弃名单。
        self.state.pending_task_id = None

        # 通知 Companion 本次录音作废（与正常结束区分开）
        if self.app.companion.has_connections:
            self.app.companion.emit_recording_cancel_threadsafe()

        self.task.cancel()
        self.task = None
        self.recorder = None

    def abort(self) -> bool:
        """手动打断录音任务（打断键触发）

        本方法在键盘钩子线程里执行，只做状态翻转和派发：Windows 低级键盘钩子有
        300ms 超时，超时会被系统直接摘掉钩子，导致此后所有快捷键失效。

        Returns:
            是否真的发起了打断。未在录音、或按键自动重复导致的重复触发返回 False
        """
        if not self.is_recording:
            return False

        logger.info(f"[{self.shortcut.key}] 手动打断录音")
        self.is_recording = False

        # 单击模式下复位按压状态，避免下一次按键落进半截的状态机
        self.pressed = False
        self.released = True
        self.event.set()

        if self.pool:
            self.pool.submit(self._finish_abort)
        else:
            self._finish_abort()
        return True

    def _finish_abort(self) -> None:
        """打断的实际处理，在线程池中执行"""
        from config_client import ClientConfig as Config

        try:
            # 先停采音，音频回调会立刻停止往队列灌数据
            self.state.stop_recording()
            self._status.stop()

            # 登记进丢弃名单：这次录音已有音频发到服务端，结果仍会回来
            task_id = self.recorder.task_id if self.recorder else None
            self.state.mark_task_cancelled(task_id)

            # 打断同样意味着「这次输入不算数」，切换键跟着回滚
            restored = self.state.rollback_continuous_snapshot(task_id)
            if restored:
                logger.info(f"打断已撤销本次退出，仍在连续对话中: {restored}")
            if self.state.rollback_image_snapshot(task_id):
                logger.info("打断已把图片退回待挂状态")

            from core.client.ui import show_cancelled_indicator
            show_cancelled_indicator(Config.cancel_recording_key)

            if self.app.companion.has_connections:
                self.app.companion.emit_recording_cancel_threadsafe()

            # 通知录音协程收尾：丢弃未发送的缓存、删除录音文件，
            # 但仍发一个 is_final 把服务端的音频缓冲冲掉
            asyncio.run_coroutine_threadsafe(
                self.state.queue_in.put({
                    'type': 'abort',
                    'time': time.time(),
                    'data': None
                }),
                self.app.loop
            )
        except Exception as e:
            logger.error(f"[{self.shortcut.key}] 打断处理失败: {e}", exc_info=True)
        finally:
            self.recorder = None
            self.task = None

    def finish(self) -> None:
        """完成录音任务"""
        logger.info(f"[{self.shortcut.key}] 释放：完成录音")

        self.is_recording = False
        duration = self.state.stop_recording()
        self._status.stop()

        # 退出连续对话在松手时落地：录音已确认有效，下一次录音的指示器立刻恢复正常配色，
        # 不必等服务端回包。等待阶段再按打断键会通过 rollback 把它恢复回去
        exited = self.state.commit_continuous_exit()
        if exited:
            logger.info(f"已退出连续对话: {exited}")

        # 松开快捷键后进入等待服务端最终结果阶段。
        from core.client.ui import show_waiting_indicator
        show_waiting_indicator(duration)

        # 通知 Companion 结束录音
        if self.app.companion.has_connections:
            self.app.companion.emit_recording_end_threadsafe()

        asyncio.run_coroutine_threadsafe(
            self.state.queue_in.put({
                'type': 'finish',
                'time': time.time(),
                'data': None
            }),
            self.app.loop
        )

        # 执行 restore（可恢复按键 + 非阻塞模式）
        # 阻塞模式下按键不会发送到系统，状态不会改变，不需要恢复
        if self.shortcut.is_toggle_key() and not self.shortcut.suppress:
            self._restore_key()

    def _restore_key(self) -> None:
        """恢复按键状态（防自捕获逻辑由 ShortcutManager 处理）"""
        # 通知管理器执行 restore
        # 防自捕获：管理器会设置 flag 再发送按键
        manager = self._manager_ref()
        if manager:
            logger.debug(f"[{self.shortcut.key}] 自动恢复按键状态 (suppress={self.shortcut.suppress})")
            manager.schedule_restore(self.shortcut.key)
        else:
            logger.warning(f"[{self.shortcut.key}] manager 引用丢失，无法 restore")
