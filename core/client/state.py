# coding: utf-8
"""
客户端状态管理模块

提供 ClientState 类用于管理客户端的全局状态。
使用 dataclass 提供类型安全和清晰的状态定义。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Deque, Optional, Dict, Any

if TYPE_CHECKING:
    import sounddevice as sd
    from websockets.legacy.client import WebSocketClientProtocol
    from .app import CapsWriterClient

from rich.console import Console
from rich.theme import Theme

from . import logger


# 配置 Rich console
_theme = Theme({
    'markdown.code': 'cyan',
    'markdown.item.number': 'yellow'
})
console = Console(highlight=False, soft_wrap=True, theme=_theme)


@dataclass
class ClientState:
    """
    客户端运行状态

    管理客户端运行过程中的所有共享状态，包括事件循环、消息队列、
    WebSocket 连接、音频流和录音状态等。

    Attributes:
        loop: asyncio 事件循环
        queue_in: 音频数据输入队列
        queue_out: 处理结果输出队列（保留）
        websocket: WebSocket 客户端连接
        stream: 音频输入流
        recording: 是否正在录音
        recording_start_time: 录音开始时间戳
        audio_files: 任务ID到音频文件路径的映射
        last_recognition_text: 最近一次识别的最终文本（热词替换后），供"添加纠错记录"使用
    """

    queue_in: asyncio.Queue = field(default_factory=asyncio.Queue)
    queue_out: asyncio.Queue = field(default_factory=asyncio.Queue)
    websocket: Optional[WebSocketClientProtocol] = None
    stream: Optional[sd.InputStream] = None
    app: Optional[CapsWriterClient] = None

    recording: bool = False
    recording_start_time: float = 0.0
    audio_files: Dict[str, Path] = field(default_factory=dict)

    # 当前正在录音或等待识别结果的任务 ID：录音任务创建时写入，最终结果到达或被打断时清空。
    # 打断键只在它非空时有效，这样「录音中」和「松开后等结果」两个阶段用同一个判据。
    pending_task_id: Optional[str] = None

    # 已被打断、结果需要丢弃的任务 ID。服务端对每个任务必定回一个最终包（残余音频
    # 不足 0.1s 也会跳过推理直接回），收到即移除；定长队列兜底，避免服务端异常时条目滞留。
    cancelled_task_ids: Deque[str] = field(default_factory=lambda: deque(maxlen=8))

    # 最近一次识别结果（用于手动添加纠错记录）
    last_recognition_text: Optional[str] = None
    
    # 最近一次输出内容（如果是 LLM 润色，则是润色结果；否则是原始识别结果）
    last_output_text: Optional[str] = None
    

    
    def reset(self) -> None:
        """
        重置状态
        
        清理所有状态，关闭连接和流。用于重新初始化或退出时清理。
        """
        logger.debug("正在重置客户端状态...")
        
        # 关闭 WebSocket 连接
        ws = self.websocket
        if ws is not None:
            try:
                if not ws.closed and self.app and self.app.loop and self.app.loop.is_running():
                    asyncio.run_coroutine_threadsafe(ws.close(), self.app.loop)
            except Exception:
                pass
            self.websocket = None
        
        # 关闭音频流
        if self.stream is not None:
            try:
                self.stream.close()
                logger.debug("音频流已关闭")
            except Exception:
                pass
            self.stream = None
        
        # 重置其他状态
        self.recording = False
        self.recording_start_time = 0.0
        self.audio_files.clear()
        self.pending_task_id = None
        self.cancelled_task_ids.clear()

        logger.debug("客户端状态重置完成")
    
    def start_recording(self, start_time: float) -> None:
        """
        开始录音
        
        Args:
            start_time: 录音开始的时间戳
        """
        self.recording = True
        self.recording_start_time = start_time
        logger.debug(f"录音状态已更新: recording=True, start_time={start_time:.2f}")
    
    def stop_recording(self) -> float:
        """
        停止录音
        
        Returns:
            录音持续时间（秒）
        """
        duration = 0.0
        if self.recording_start_time > 0:
            duration = time.time() - self.recording_start_time
        
        self.recording = False
        self.recording_start_time = 0.0
        logger.debug(f"录音状态已更新: recording=False, duration={duration:.2f}s")
        return duration
    
    @property
    def is_connected(self) -> bool:
        """检查 WebSocket 是否已连接"""
        if self.websocket is None:
            return False
        try:
            return not self.websocket.closed
        except AttributeError:
            return self.websocket is not None
    
    def register_audio_file(self, task_id: str, file_path: Path) -> None:
        """
        注册音频文件
        
        Args:
            task_id: 任务ID
            file_path: 音频文件路径
        """
        self.audio_files[task_id] = file_path
        logger.debug(f"注册音频文件: task_id={task_id}, path={file_path}")
    
    def pop_audio_file(self, task_id: str) -> Optional[Path]:
        """
        获取并移除音频文件路径
        
        Args:
            task_id: 任务ID
            
        Returns:
            音频文件路径，如果不存在则返回 None
        """
        file_path = self.audio_files.pop(task_id, None)
        if file_path:
            logger.debug(f"获取音频文件: task_id={task_id}, path={file_path}")
        return file_path

    def mark_task_cancelled(self, task_id: Optional[str]) -> None:
        """
        标记任务已被打断，其识别结果到达后应被丢弃

        Args:
            task_id: 任务ID；为空表示录音尚未拿到任务ID，无需登记
        """
        self.pending_task_id = None
        if not task_id or task_id in self.cancelled_task_ids:
            return
        self.cancelled_task_ids.append(task_id)
        logger.debug(f"任务已标记为打断: {task_id}")

    def is_task_cancelled(self, task_id: str) -> bool:
        """检查任务是否已被打断"""
        return task_id in self.cancelled_task_ids

    def release_cancelled_task(self, task_id: str) -> None:
        """收到被打断任务的最终结果后，将其移出打断名单"""
        try:
            self.cancelled_task_ids.remove(task_id)
            logger.debug(f"打断任务已收尾: {task_id}")
        except ValueError:
            pass

    def set_output_text(self, text: str) -> None:
        """
        设置最近一次输出文本
        
        Args:
            text: 输出文本内容
        """
        self.last_output_text = text



