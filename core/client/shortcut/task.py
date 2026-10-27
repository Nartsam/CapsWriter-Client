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
from core.tools.task_id import new_task_id
from core.client.output.stream_output import StreamSession
 
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
        # 长按模式下被打断后、录音键松开之前为 True，见 abort()
        self.wait_release: bool = False

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
        """同步预留身份和快照，再派发录音协程。"""
        with self.state.recording_lock:
            if self.state.recording:
                return
            logger.info(f"[{self.shortcut.key}] 触发：开始录音")

            # 记录开始时间
            self.recording_start_time = time.time()
            self.is_recording = True

            task_id = new_task_id(self.recording_start_time)
            recorder = self._get_recorder()
            recorder.task_id = task_id
            self.recorder = recorder

            # 连续对话：先做闲置超时的懒判断，再按此刻生效的粘滞角色建立本次录音的路由快照。
            # 必须排在 start_recording() 之前：切换键以 state.recording 为介入判据，
            # 反过来会留下「已在录音、快照还没换」的窗口，那时的切换会翻到上一次录音
            # 尚未消费的快照上去（UDP 控制录音与键盘钩子不在同一线程，确实可能撞上）
            snapshot = self._prepare_continuous()

            # 待挂图片在这一刻绑定到本次录音。剪贴板读取是异步的，此刻还没读完就是
            # 一次不带图的普通录音——「下一条」从图片挂载成功那一刻起算（见 vision_manager 模块说明）
            pending_image = self.state.begin_image_snapshot()

            streaming = self._want_streaming(snapshot, pending_image)

            from config_client import ClientConfig as Config
            session = StreamSession(
                task_id, streaming=streaming, revisable=streaming and Config.llm_enabled,
                start_time=self.recording_start_time, connection=self.state.websocket,
                recorder=recorder, owner=self)
            self.state.recognition_sessions[task_id] = session
            self.state.bind_continuous_snapshot(task_id)
            self.state.bind_image_snapshot(task_id)
            self.state.pending_task_id = task_id
            self._queue('begin', task_id)
            self.state.start_recording(self.recording_start_time, task_id)

            # 显示录音悬浮提示：传入快捷键名，提示器会转成中文名称。
            from core.client.ui import show_recording_indicator
            show_recording_indicator(
                self.recording_start_time,
                self.shortcut.key,
                snapshot.indicator_mode(),
                snapshot.sticky_role or '',
                pending_image is not None,
                streaming,
            )

            # 通知 Companion 开始录音
            if self.app.companion.has_connections:
                self.app.companion.emit_recording_start_threadsafe()

            # 启动识别任务；保留 recorder 引用，打断时需要用它的 task_id 丢弃结果
            self.task = asyncio.run_coroutine_threadsafe(
                recorder.record_and_send(session, self.threshold),
                self.app.loop,
            )

            # 控制台动画移到线程池，避免在键盘钩子回调中创建线程导致超时
            if self.pool:
                self.pool.submit(self._status.start)
            else:
                self._status.start()

    def _queue(self, kind: str, task_id: str) -> None:
        self.app.loop.call_soon_threadsafe(self.state.queue_in.put_nowait, {
            'type': kind, 'task_id': task_id, 'time': time.time(), 'data': None})

    def _want_streaming(self, snapshot, pending_image) -> bool:
        """已知要调用角色时直接普通识别，其余流式任务再按增量决定。"""
        if not getattr(self.shortcut, 'streaming', False):
            return False
        from config_client import ClientConfig as Config
        llm = getattr(self.app, 'llm', None)
        if snapshot.sticky_role:
            return False
        if Config.llm_enabled and llm is not None and llm.default_role_enabled():
            return False
        return True

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
        """短按也用带 ID 的结束标记收尾，不能遗留队列中的 begin/data。"""
        self._cancel_current(short_press=True)

    def abort(self) -> bool:
        if not self.is_recording:
            return False
        self.wait_release = self.shortcut.hold_mode
        self.pressed = False
        self.released = True
        self.event.set()
        self._cancel_current(short_press=False)
        return True

    def _cancel_current(self, short_press: bool) -> None:
        with self.state.recording_lock:
            recorder = self.recorder
            if recorder is None:
                return
            task_id = recorder.task_id
            session = self.state.recognition_sessions.get(task_id)
            if session is None or session.terminal:
                return
            self.is_recording = False
            self.state.stop_recording(task_id)
            self.state.mark_task_cancelled(task_id)
            self.state.rollback_continuous_snapshot(task_id)
            self.state.rollback_image_snapshot(task_id)
            self._queue('abort', task_id)
            if self.pool:
                self.pool.submit(self._status.stop)
            from core.client.ui import hide_recording_indicator, show_cancelled_indicator
            if short_press:
                hide_recording_indicator(session.start_time)
            else:
                from config_client import ClientConfig as Config
                show_cancelled_indicator(Config.cancel_recording_key,
                                         partial=task_id in self.state.streamed_task_ids,
                                         start_time=session.start_time)
            if self.app.companion.has_connections:
                self.app.companion.emit_recording_cancel_threadsafe()
            self.app.loop.call_soon_threadsafe(
                self.app.result_processor.fail_task_soon, task_id, '', short_press)

    def stop_for_error(self, task_id: str) -> None:
        """服务端错误只停止对应录音；文件保留，由结果处理器统一释放登记。"""
        with self.state.recording_lock:
            if self.recorder is None or self.recorder.task_id != task_id or not self.is_recording:
                return
            self.is_recording = False
            self.wait_release = self.shortcut.hold_mode
            self.pressed = False
            self.released = True
            self.event.set()
            self.state.stop_recording(task_id)
            self._queue('error', task_id)
            if self.pool:
                self.pool.submit(self._status.stop)

    def finish(self) -> None:
        """完成本次采集；结束标记与音频块在同一个锁内按序入队。"""
        with self.state.recording_lock:
            logger.info(f"[{self.shortcut.key}] 释放：完成录音")

            self.is_recording = False
            task_id = self.recorder.task_id
            duration = self.state.stop_recording(task_id)
            if self.pool:
                self.pool.submit(self._status.stop)

            # 退出连续对话在松手时落地：录音已确认有效，下一次录音的指示器立刻恢复正常配色，
            # 不必等服务端回包。等待阶段再按打断键会通过 rollback 把它恢复回去
            exited = self.state.commit_continuous_exit()
            if exited:
                logger.info(f"已退出连续对话: {exited}")

            # 松开快捷键后进入等待服务端最终结果阶段。
            from core.client.ui import show_waiting_indicator
            show_waiting_indicator(duration, self.recording_start_time)

            # 通知 Companion 结束录音
            if self.app.companion.has_connections:
                self.app.companion.emit_recording_end_threadsafe()

            self._queue('finish', task_id)

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
