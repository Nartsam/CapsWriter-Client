# coding: utf-8
"""
Companion WebSocket 服务器

管理与 Electron 伴侣应用的 WebSocket 连接。
当有 Electron 客户端连接时，CapsWriter 将语音识别结果
（去前缀后的文本）直接转发给 Electron，不再本地调用 LLM。
"""
from __future__ import annotations

import asyncio
import socket
import logging
from typing import TYPE_CHECKING, Set

from .companion_protocol import EventType, make_event

if TYPE_CHECKING:
    from core.client.app import CapsWriterClient

logger = logging.getLogger(__name__)


class CompanionServer:
    """Companion WebSocket 服务器"""

    def __init__(self, app: CapsWriterClient):
        self.app = app
        self._clients: Set = set()
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def has_connections(self) -> bool:
        return len(self._clients) > 0

    async def start(self) -> None:
        """启动 WebSocket 服务器，端口被占用则跳过"""
        from config_client import ClientConfig as Config

        addr = Config.companion_addr
        port = Config.companion_port

        if not self._check_port(addr, port):
            logger.warning(f"Companion 端口 {addr}:{port} 被占用，已禁用 Companion 功能")
            return

        try:
            import websockets
            self._loop = asyncio.get_running_loop()
            self._server = await websockets.serve(
                self._handler, addr, port,
                ping_interval=20,
                ping_timeout=20,
            )
            logger.info(f"Companion WebSocket 服务器已启动: ws://{addr}:{port}")
        except Exception as e:
            logger.error(f"Companion 服务器启动失败: {e}")

    def stop(self) -> None:
        """停止服务器"""
        if self._server:
            self._server.close()
            logger.info("Companion WebSocket 服务器已停止")

    async def broadcast(self, message: str) -> None:
        """向所有已连接的客户端广播消息"""
        if not self._clients:
            return
        await asyncio.gather(
            *(client.send(message) for client in self._clients),
            return_exceptions=True,
        )

    def broadcast_threadsafe(self, message: str) -> None:
        """从工作线程安全地广播消息"""
        if not self._clients or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(message), self._loop)

    # --- 事件发送便捷方法 ---

    async def emit_recording_start(self) -> None:
        await self.broadcast(make_event(EventType.RECORDING_START))

    async def emit_recording_end(self) -> None:
        await self.broadcast(make_event(EventType.RECORDING_END))

    async def emit_recording_cancel(self) -> None:
        await self.broadcast(make_event(EventType.RECORDING_CANCEL))

    async def emit_user_message(self, text: str, role: str) -> None:
        """发送去前缀后的用户文本，由 Electron 端接管后续处理"""
        await self.broadcast(make_event(
            EventType.USER_MESSAGE,
            text=text,
            role=role,
        ))

    def emit_recording_start_threadsafe(self) -> None:
        if not self._clients or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.emit_recording_start(), self._loop)

    def emit_recording_end_threadsafe(self) -> None:
        if not self._clients or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.emit_recording_end(), self._loop)

    def emit_recording_cancel_threadsafe(self) -> None:
        if not self._clients or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.emit_recording_cancel(), self._loop)

    # --- 内部方法 ---

    async def _handler(self, websocket) -> None:
        """处理单个 WebSocket 连接"""
        self._clients.add(websocket)
        remote = websocket.remote_address
        logger.info(f"Companion 客户端已连接: {remote}")

        try:
            await websocket.send(make_event(EventType.STATUS, state='connected'))
            async for message in websocket:
                logger.debug(f"收到 Companion 消息: {message}")
        except Exception as e:
            logger.debug(f"Companion 连接异常: {e}")
        finally:
            self._clients.discard(websocket)
            logger.info(f"Companion 客户端已断开: {remote}")

    @staticmethod
    def _check_port(addr: str, port: int) -> bool:
        """检测端口是否可用"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((addr, port))
            return True
        except OSError:
            return False
        finally:
            sock.close()
