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
from core.protocol import AudioMessage, RecognitionControlMessage
from core.tools.asyncio_to_thread import to_thread
from . import logger

if TYPE_CHECKING:
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient

# 日志记录器


class AudioRecorder:
    """
    音频录制器
    
    管理一次完整的录音会话，包括：
    - 从音频流接收数据
    - 可选地保存到本地文件
    - 将音频数据发送到识别服务端
    """
    
    def __init__(self, app: CapsWriterClient):
        """
        初始化录制器
        
        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self.task_id: Optional[str] = None
        self._file_manager: Optional[AudioFileManager] = None
        self._start_time: float = 0.0
        self._duration: float = 0.0
        self._cache: list = []
        self._streaming: bool = False
        self._session = None
        self._done = app.loop.create_future()
        self._last_send = None
        self._file_handle = None

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    @property
    def _ws_manager(self) -> WebSocketManager:
        """快捷访问桥接到 app.ws"""
        return self.app.ws
    
    def _enqueue_audio(self, data, is_final: bool) -> None:
        session = self._session
        if session.connection is None:
            future = asyncio.get_running_loop().create_future()
            future.set_result(False)
        else:
            future = self._ws_manager.enqueue(self._audio_message(data, is_final), session.connection)
        self._last_send = future
        if data is not None:
            session.sent_audio = True

        def sent(done):
            if not done.cancelled() and not done.result():
                processor = getattr(self.app, 'result_processor', None)
                if processor is not None:
                    processor.fail_task_soon(self.task_id, '音频发送失败')
        future.add_done_callback(sent)

    def _write_audio(self, data) -> None:
        self._duration += len(data) / 48000
        if not Config.save_audio:
            return
        if self._file_manager is None:
            self._file_manager = AudioFileManager()
            path, _ = self._file_manager.create(data.shape[1], self._start_time)
            self.state.register_audio_file(self.task_id, path)
        self._file_manager.write(data)

    def _audio_message(self, data: Optional[np.ndarray], is_final: bool) -> AudioMessage:
        """构造本次录音的音频消息；data 是 48kHz 采集块（None 表示不带音频），在这里降到 16kHz 单声道"""
        payload = ''
        if data is not None:
            payload = base64.b64encode(np.mean(data[::3], axis=1).tobytes()).decode('utf-8')
        return AudioMessage(
            task_id=self.task_id,
            source='mic',
            data=payload,
            is_final=is_final,
            time_start=self._start_time,
            seg_duration=Config.mic_seg_duration,
            seg_overlap=Config.mic_seg_overlap,
            context=Config.context,
            language=Config.language,
            streaming=self._streaming,
            revisable=self._session.revisable,
        )

    async def record_and_send(self, session, threshold: float) -> None:
        """跨快捷键串行消费带任务 ID 的采集消息；身份和快照已在 launch 固定。"""
        self._session = session
        self.task_id = session.task_id
        self._streaming = session.streaming
        self._start_time = session.start_time
        try:
            async with self.state.recorder_lock:
                await self._record(threshold)
        except Exception as error:
            logger.error('录音任务错误: %s', error, exc_info=True)
            self.app.result_processor.fail_task_soon(self.task_id, '录音处理失败')
        finally:
            if session.sent_audio and not session.audio_ended:
                self._enqueue_audio(None, True)
                if self._last_send is not None:
                    await self._last_send
                session.audio_ended = True
            if self._file_manager is not None:
                self._file_handle = self._file_manager.file_handle
                self._file_manager.finish()
            if not self._done.done():
                self._done.set_result(None)

    async def _record(self, threshold: float) -> None:
        session = self._session
        while True:
            item = await self.state.queue_in.get()
            self.state.queue_in.task_done()
            # 采集回调可跨过松键后才入队；旧块只能丢弃，不能交给下一句。
            if item['task_id'] != self.task_id:
                continue
            kind = item['type']
            if kind == 'begin':
                continue
            if kind == 'data':
                self._cache.append(item['data'])
                if item['time'] - self._start_time < threshold:
                    continue
                data = np.concatenate(self._cache)
                self._cache.clear()
                self._write_audio(data)
                if not session.terminal and not self.state.is_task_cancelled(self.task_id):
                    self._enqueue_audio(data, False)
                continue

            if kind not in ('finish', 'abort', 'error'):
                continue
            cancelled = self.state.is_task_cancelled(self.task_id)
            if self._cache and not cancelled:
                data = np.concatenate(self._cache)
                self._write_audio(data)
                if not session.terminal:
                    self._enqueue_audio(data, False)
            self._cache.clear()
            # 短按未发过音频，不创建服务器任务；其余情况结束包排在所有音频之后。
            if session.sent_audio:
                self.state.register_awaiting_result(self.task_id, self._duration)
                self._enqueue_audio(None, True)
                if self._last_send is not None:
                    await self._last_send
            session.audio_ended = True
            if not session.sent_audio and kind == 'finish':
                self.app.result_processor.fail_task_soon(self.task_id, '未采集到音频')
            return

    async def cancel_remote(self) -> None:
        """结束发送屏障之后才能取消；未发过音频的短按只做本地收尾。"""
        await self._done
        session = self._session
        if session is not None and session.sent_audio and session.connection is not None:
            await self._ws_manager.send(
                RecognitionControlMessage(self.task_id, 'cancel'), session.connection)

    async def delete_recording(self) -> None:
        await self._done
        path = self.state.pop_audio_file(self.task_id)
        if path is not None:
            await self._delete_audio_file(Path(path), self._file_handle)
        self._file_manager = None

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
    
    def get_file_manager(self) -> Optional[AudioFileManager]:
        """获取当前的文件管理器"""
        return self._file_manager
