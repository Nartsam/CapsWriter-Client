# coding: utf-8
"""
音频录制模块

提供 AudioRecorder 类用于管理录音会话，包括开始录音、
发送音频数据到服务端、结束录音等功能。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from subprocess import Popen
from typing import TYPE_CHECKING, Optional

import numpy as np

from config_client import ClientConfig as Config
from core.client.audio.file_manager import AudioFileManager
from core.client.connection import WebSocketManager
from core.client.task_line import NOT_CONNECTED, SEND_FAILED
from core.protocol import AudioMessage
from core.tools.asyncio_to_thread import to_thread
from . import logger

if TYPE_CHECKING:
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient
    from core.client.output.stream_output import StreamSession


class AudioRecorder:
    """
    音频录制器

    管理一次录音的采集、本地保存与发送。task_id、路由快照和识别连接都在
    ShortcutTask.launch 时固定在 StreamSession 里，这里只按 task_id 消费采集消息。
    _done 在结束包发出、录音文件写完之后完成：取消控制、删除录音、录音改名都要等它。
    """

    def __init__(self, app: CapsWriterClient):
        self.app = app
        self.task_id: Optional[str] = None   # launch 时同步写入，打断与报错按它找回本次任务
        self._session: Optional[StreamSession] = None
        self._file_manager: Optional[AudioFileManager] = None
        self._file_handle = None
        self._duration: float = 0.0
        self._cache: list = []
        self._last_send: Optional[asyncio.Future] = None
        self._done = app.loop.create_future()

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    @property
    def _ws_manager(self) -> WebSocketManager:
        """快捷访问桥接到 app.ws"""
        return self.app.ws

    def _enqueue_audio(self, data: Optional[np.ndarray], is_final: bool) -> None:
        """按序进入连接的发送队列；发送失败只终结本次任务"""
        session = self._session
        if session.connection is None:
            # launch 时没有连接：本次录音整体作废，不改投之后才建立的新连接
            future = asyncio.get_running_loop().create_future()
            future.set_result(False)
        else:
            future = self._ws_manager.enqueue(self._audio_message(data, is_final), session.connection)
        self._last_send = future
        if data is not None:
            session.sent_audio = True
        reason = SEND_FAILED if session.connection is not None else NOT_CONNECTED

        def sent(done: asyncio.Future) -> None:
            if not done.result():
                self.app.result_processor.fail_task_soon(self.task_id, reason)
        future.add_done_callback(sent)

    def _write_audio(self, data) -> None:
        self._duration += len(data) / 48000
        if not Config.save_audio:
            return
        if self._file_manager is None:
            self._file_manager = AudioFileManager()
            path, _ = self._file_manager.create(data.shape[1], self._session.start_time)
            self.state.register_audio_file(self.task_id, path)
        self._file_manager.write(data)

    def _flush(self) -> None:
        """把缓存的采集块写入录音文件并发送；任务已终结或已打断时只写不发"""
        if not self._cache:
            return
        data = np.concatenate(self._cache)
        self._cache.clear()
        self._write_audio(data)
        if not self._session.terminal and not self.state.is_task_cancelled(self.task_id):
            self._enqueue_audio(data, False)

    def _audio_message(self, data: Optional[np.ndarray], is_final: bool) -> AudioMessage:
        """构造本次录音的音频消息；data 是 48kHz 采集块（None 表示不带音频），在这里降到 16kHz 单声道"""
        payload = ''
        if data is not None:
            payload = base64.b64encode(np.mean(data[::3], axis=1).tobytes()).decode('utf-8')
        session = self._session
        return AudioMessage(
            task_id=self.task_id,
            source='mic',
            data=payload,
            is_final=is_final,
            time_start=session.start_time,
            seg_duration=Config.mic_seg_duration,
            seg_overlap=Config.mic_seg_overlap,
            context=Config.context,
            language=Config.language,
            streaming=session.streaming,
            revisable=session.revisable,
        )

    async def record_and_send(self, session: StreamSession, threshold: float) -> None:
        """消费本次录音的采集消息，结束后发出结束包、写完录音文件"""
        self._session = session
        try:
            # 共享 queue_in 的消费者同一时刻只有一个：上一句读到自己的结束标记才轮到这一句
            async with self.state.recorder_lock:
                await self._record(threshold)
        except Exception as error:
            logger.error('录音任务错误: %s', error, exc_info=True)
            self.app.result_processor.fail_task_soon(self.task_id, '录音处理失败')
        finally:
            # 结束包排在本次所有音频之后（取消控制又排在它之后，见 ResultProcessor._fail_task）；
            # 没发过音频的短按不创建服务端任务。出错中断时同样要发，服务端才会结束这次输入
            if session.sent_audio:
                self._enqueue_audio(None, True)
                await self._last_send
            if self._file_manager is not None:
                # finish() 会把 file_handle 置空，删除录音时还要用它等 FFmpeg 子进程退出
                self._file_handle = self._file_manager.file_handle
                self._file_manager.finish()
            self._done.set_result(None)

    async def _record(self, threshold: float) -> None:
        while True:
            item = await self.state.queue_in.get()
            self.state.queue_in.task_done()
            # 采集回调可能在松键后才把最后一块入队；别的任务的块只能丢弃，不能交给这一句
            if item['task_id'] != self.task_id:
                continue
            kind = item['type']
            if kind == 'data':
                # 阈值内的音频先攒着：短按取消时一个字节都不发
                self._cache.append(item['data'])
                if item['time'] - self._session.start_time >= threshold:
                    self._flush()
                continue

            # finish / abort / error：打断时未发出的缓存直接丢弃
            if not self.state.is_task_cancelled(self.task_id):
                self._flush()
            logger.info(f"录音结束（{kind}），任务ID: {self.task_id}, 时长: {self._duration:.2f}s")
            if self._session.sent_audio:
                # 任务行要等结果处理时才打印（见 core/client/task_line.py），录音时长经这里转交
                self.state.register_awaiting_result(self.task_id, self._duration)
            elif kind == 'finish':
                self.app.result_processor.fail_task_soon(self.task_id, '未采集到音频')
            return

    async def delete_recording(self) -> None:
        """删除本次录音文件（用户打断时）；调用方需先等 _done"""
        path = self.state.pop_audio_file(self.task_id)
        if path is not None:
            await self._delete_audio_file(Path(path), self._file_handle)

    async def _delete_audio_file(self, file_path: Path, handle) -> None:
        """等待文件句柄真正释放后删除录音文件

        用 FFmpeg 保存时句柄是子进程，关掉 stdin 后它还要写完文件尾才退出，
        立刻删会因文件被占用而失败。等待放到线程里做，不阻塞事件循环。
        """
        def _wait_and_delete() -> None:
            if isinstance(handle, Popen):
                try:
                    handle.wait(timeout=5)
                except Exception:
                    logger.debug("等待 FFmpeg 退出超时，仍尝试删除录音文件")
            try:
                file_path.unlink()
                logger.debug(f"已删除被打断的录音文件: {file_path}")
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"删除被打断的录音文件失败: {e}")

        await to_thread(_wait_and_delete)
