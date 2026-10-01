# coding: utf-8
"""
音频录制模块

提供 AudioRecorder 类用于管理录音会话，包括开始录音、
发送音频数据到服务端、结束录音等功能。
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path
from subprocess import Popen
from typing import TYPE_CHECKING, Optional

import numpy as np
import websockets

from config_client import ClientConfig as Config
from core.client.state import console
from core.client.audio.file_manager import AudioFileManager
from core.client.connection import WebSocketManager
from core.protocol import AudioMessage
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

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    @property
    def _ws_manager(self) -> WebSocketManager:
        """快捷访问桥接到 app.ws"""
        return self.app.ws
    
    async def _send_message(self, message: AudioMessage, quiet: bool = False) -> None:
        """
        发送消息到服务端

        Args:
            message: 待发送的音频协议消息
            quiet: 静默模式，发送失败不弹错误提示。用于手动打断——用户已经明确
                   表示不要这次结果，服务端连没连上都无所谓，弹错误只会把
                   「已手动打断」的提示顶掉
        """
        if not self._ws_manager.is_connected:
            if message.is_final:
                self.state.pop_audio_file(message.task_id)
                console.print('    服务端未连接，无法发送\n')
                logger.warning("服务端未连接，无法发送音频数据")
                if not quiet:
                    # 最终包发送失败时，悬浮提示改为错误状态并自动隐藏。
                    from core.client.ui import show_recording_indicator_error
                    show_recording_indicator_error("服务端未连接，无法发送音频")
            return

        # 使用 WebSocketManager 发送协议消息
        success = await self._ws_manager.send(message)
        if not success and message.is_final:
            self.state.pop_audio_file(message.task_id)
            if not quiet:
                # WebSocketManager 返回发送失败时，用悬浮提示反馈给用户。
                from core.client.ui import show_recording_indicator_error
                show_recording_indicator_error("音频发送失败")
            # 具体错误日志由 WebSocketManager 记录
    
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
        )

    async def record_and_send(self, streaming: bool = False) -> None:
        """
        录音并发送数据
        
        从队列中读取音频数据，保存到文件（如果启用），
        并发送到服务端进行识别。

        Args:
            streaming: 请求流式上屏，服务端会按停顿切句、逐句回传（RecognitionMessage.delta）
        """
        try:
            self._streaming = streaming
            # 生成唯一任务 ID
            self.task_id = str(uuid.uuid1())
            # 登记为待处理任务，打断键据此判断当前是否有可打断的输入
            self.state.pending_task_id = self.task_id
            # 连续对话的路由快照在 launch() 时就建好了（那时粘滞角色已确定），
            # 这里补上 task_id 索引，供结果到达时查回本次录音的路由意图
            self.state.bind_continuous_snapshot(self.task_id)
            # 图片快照同理：launch() 时已把待挂图片移入本次录音，这里补索引
            self.state.bind_image_snapshot(self.task_id)
            logger.debug(f"创建录音任务，任务ID: {self.task_id}")
            
            self._start_time = 0.0
            self._duration = 0.0
            self._cache = []
            
            # 音频文件管理
            file_path = None
            if Config.save_audio:
                self._file_manager = AudioFileManager()
            
            # 从队列读取数据
            while task := await self.state.queue_in.get():
                self.state.queue_in.task_done()
                
                if task['type'] == 'begin':
                    self._start_time = task['time']
                    logger.debug(f"录音开始，时间戳: {self._start_time}")
                    
                elif task['type'] == 'data':
                    # 在阈值之前积攒音频数据
                    if task['time'] - self._start_time < Config.threshold:
                        self._cache.append(task['data'])
                        continue
                    
                    # 创建音频文件
                    if Config.save_audio and self._file_manager and file_path is None:
                        file_path, _ = self._file_manager.create(
                            task['data'].shape[1],
                            self._start_time
                        )
                        self.state.register_audio_file(self.task_id, file_path)
                        logger.debug(f"创建音频文件: {file_path}")
                    
                    # 获取音频数据
                    if self._cache:
                        data = np.concatenate(self._cache)
                        self._cache.clear()
                    else:
                        data = task['data']
                    
                    # 保存音频至本地文件
                    self._duration += len(data) / 48000
                    if Config.save_audio and self._file_manager:
                        self._file_manager.write(data)
                    
                    # 发送音频数据用于识别
                    asyncio.create_task(self._send_message(self._audio_message(data, False)))
                    
                elif task['type'] == 'finish':
                    # 如果有缓存的数据未发送，先发送缓存
                    if self._cache:
                        data = np.concatenate(self._cache)
                        self._cache.clear()
                        
                        self._duration += len(data) / 48000
                        if Config.save_audio and self._file_manager:
                            self._file_manager.write(data)

                        asyncio.create_task(self._send_message(self._audio_message(data, False)))

                    # 完成写入本地文件
                    if Config.save_audio and self._file_manager:
                        self._file_manager.finish()
                        logger.debug("完成音频文件写入")
                    
                    console.print(f'任务标识：{self.task_id}')
                    console.print(f'    录音时长：{self._duration:.2f}s')
                    logger.info(f"录音任务完成，任务ID: {self.task_id}, 时长: {self._duration:.2f}s")
                    
                    # 告诉服务端音频片段结束了
                    asyncio.create_task(self._send_message(self._audio_message(None, True)))
                    break

                elif task['type'] == 'abort':
                    # 手动打断：未发送的缓存直接丢弃，但仍要发一个 is_final。
                    # 服务端的 AudioCache 是按连接持有的，只有 is_final 才会 reset，
                    # 不发的话这次录音的残余音频会拼到下一次录音前面去。
                    self._cache.clear()
                    self._discard_audio_file()

                    console.print(f'任务标识：{self.task_id}')
                    console.print('    已手动打断，识别结果将被丢弃\n')
                    logger.info(f"录音任务被打断，任务ID: {self.task_id}, 已录 {self._duration:.2f}s")

                    asyncio.create_task(self._send_message(self._audio_message(None, True), quiet=True))
                    break

        except Exception as e:
            logger.error(f"录音任务错误: {e}", exc_info=True)

    def _discard_audio_file(self) -> None:
        """关闭并删除本次录音文件（手动打断时调用）"""
        if self.task_id:
            self.state.pop_audio_file(self.task_id)

        if not self._file_manager:
            return

        # finish() 会把 file_handle 置空，先取出来供后面判断是否需要等子进程退出
        handle = self._file_manager.file_handle
        file_path = self._file_manager.finish()
        self._file_manager = None
        if not file_path:
            return

        asyncio.create_task(self._delete_audio_file(Path(file_path), handle))

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
