# coding: utf-8
"""
WebSocket 连接管理模块

提供 WebSocketManager 类用于管理与服务端的 WebSocket 连接，
包括连接建立、重连、消息发送和连接状态检查。
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Tuple

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from config_client import ClientConfig as Config
from core.protocol import AudioMessage, RecognitionMessage
from ..state import console
from .. import logger
import asyncio


if TYPE_CHECKING:
    from core.client.state import ClientState
    from ..app import CapsWriterClient


# 探测超时的兜底值（秒），仅在配置项缺失或非法时使用
DEFAULT_PROBE_TIMEOUT = 3.0
DEFAULT_PROBE_TIMEOUT_LAN = 2.0

# 「疑似内网」判定用的网段：RFC1918 三段 + 回环。
# 刻意不用 ipaddress.is_private——它还涵盖 169.254/16、198.18/15、240/4 等保留段，
# 口径比这里想要的宽得多
_LAN_NETWORKS = (
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('127.0.0.0/8'),
)


@dataclass(frozen=True)
class ServerEndpoint:
    """一个候选服务端地址"""
    url: str        # 完整 ws:// URL，用于建立连接
    display: str    # host:port，用于日志与启动提示
    is_lan: bool    # 是否内网/回环 IP 字面量，决定探测超时档位


def _is_lan_host(host: str) -> bool:
    """
    判断主机字符串是否为内网或回环 IP 字面量

    只看字面量、不做 DNS 解析：分档发生在建立连接之前，为了分档先去解析域名，
    可能自己就先挂住几秒，得不偿失。因此主机名与域名一律返回 False（走保守档）。

    Args:
        host: 配置里写的主机部分

    Returns:
        是否属于 RFC1918 私网或回环地址
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # IPv4Network.__contains__ 遇到 IPv6 地址直接返回 False，无需额外判断版本
    return any(ip in network for network in _LAN_NETWORKS)


def _split_host_port(entry: str, default_port: str) -> Tuple[str, str]:
    """
    从候选项里拆出主机与端口

    仅当恰好一个冒号、且右侧是 1-65535 的纯 ASCII 数字时才认作端口覆盖，否则整体
    当主机名——这样 'example.com:abc' 不会被误拆成非法端口。代价是 IPv6 字面量
    （无论是否带方括号）不受支持，会拼出一个明显畸形的 URL 并在日志里原样暴露。

    Args:
        entry: 去过空白的单个候选项
        default_port: 未自带端口时采用的端口

    Returns:
        (主机, 端口) 二元组
    """
    if entry.count(':') == 1:
        host, _, port = entry.partition(':')
        if host and port.isascii() and port.isdigit() and 1 <= int(port) <= 65535:
            return host, port
    return entry, default_port


def resolve_server_endpoints() -> List[ServerEndpoint]:
    """
    把 Config.addr 归一化成候选服务端列表

    addr 可写成单个字符串（旧写法），也可写成字符串列表按序探测。列表项能用
    'host:port' 单独覆盖 Config.port；空白项丢弃，重复项保序去重。

    Returns:
        候选列表，保持配置中的书写顺序
    """
    raw = getattr(Config, 'addr', '')
    if isinstance(raw, str):
        entries = [raw]
    elif isinstance(raw, (list, tuple)):
        entries = list(raw)
    else:
        entries = []

    default_port = str(getattr(Config, 'port', '')).strip()

    endpoints: List[ServerEndpoint] = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, str):
            continue
        entry = entry.strip()
        if not entry:
            continue

        host, port = _split_host_port(entry, default_port)
        display = f"{host}:{port}"
        if display in seen:
            continue
        seen.add(display)
        endpoints.append(
            ServerEndpoint(url=f"ws://{display}", display=display, is_lan=_is_lan_host(host))
        )

    return endpoints


class CommunicationError(Exception):
    """通信层通用异常"""
    pass


class WebSocketManager:
    """
    WebSocket 连接管理器

    负责管理与识别服务端的 WebSocket 连接，提供自动重连和
    错误处理功能。

    配置了多个候选地址时，首次连接成功的那个会被锁定，此后（含断线重连）只连它，
    不会改投其他候选，换机需重启客户端。几条取舍：

    - 锁定发生在「首次连接成功」而不是「启动那一刻」：客户端常随开机自启，那时
      服务端往往还没起来，按启动时刻定终身会锁死在不可达地址上。反过来，一次
      瞬时握手成功就会锁定，哪怕服务端下一秒崩溃，此后也只等它恢复。
    - 探测循环写在 connect() 内部：ResultProcessor 的重连循环与
      FileTranscriber.check() 两个调用点不用改，文件转录模式同样支持多候选。
      一轮全败由调用方 2s 后重扫，不做指数退避（会拖慢服务端恢复后的重连）。
    - 不提供托盘「重新选择服务端」：解锁要处理正在录音/等结果时跨线程断开连接的
      竞态，而换服务器是低频事件，重启客户端的代价很小（各角色的记忆会落盘并恢复）。

    Attributes:
        app: 客户端 App 实例
    """

    def __init__(self, app: CapsWriterClient):
        """
        初始化 WebSocket 管理器

        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self._connect_fail_logged = False  # 断联后只记一次失败日志
        self._locked: Optional[ServerEndpoint] = None  # 首次连上的候选，进程内不再更换
        self._probed = False               # 锁定是否经由多候选探测得来（决定断线告警措辞）
        self._timeout_warned = False       # 探测超时配置非法只警告一次

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state
    
    @property
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.state.is_connected
    
    async def connect(self) -> bool:
        """
        建立 WebSocket 连接

        尚未锁定时按配置顺序逐个探测候选，首个连上的即被锁定；此后（含断线重连）
        只连锁定的那个，不会改投其他候选。调用方的重试节奏不变。

        Returns:
            连接是否成功
        """
        # 如果已连接，直接返回
        if self.is_connected:
            return True

        # 清理旧连接
        if self.state.websocket is not None:
            self.state.websocket = None

        # 已锁定：只等它恢复
        if self._locked is not None:
            if await self._try_endpoint(self._locked):
                return True
            if not self._connect_fail_logged:
                # 「明明另一台开着却不切过去」是刻意设计但反直觉，日志里必须自证；
                # 单候选配置下这句话没有意义，所以只在探测锁定的情况下打
                if self._probed:
                    logger.warning(
                        f"已锁定服务端 {self._locked.display}，正在等待其恢复；"
                        f"按设计不会切换到其他候选，如需换机请重启客户端"
                    )
                self._connect_fail_logged = True
            return False

        endpoints = resolve_server_endpoints()
        if not endpoints:
            if not self._connect_fail_logged:
                logger.warning("未配置服务端地址：config_client.py 的 addr 为空，无法连接")
                self._connect_fail_logged = True
            return False

        # 单候选不分档也不设 open_timeout，行为与改造前逐字一致
        probing = len(endpoints) > 1

        for endpoint in endpoints:
            open_timeout = self._probe_timeout(endpoint) if probing else None
            if await self._try_endpoint(endpoint, open_timeout):
                self._locked = endpoint
                self._probed = probing
                if probing:
                    logger.info(f"已锁定服务端 {endpoint.display}，本次运行期间不再更换")
                return True

        if not self._connect_fail_logged:
            if probing:
                names = '、'.join(endpoint.display for endpoint in endpoints)
                logger.warning(f"所有候选服务端均不可达，将持续重试：{names}")
            self._connect_fail_logged = True

        return False

    async def _try_endpoint(
        self, endpoint: ServerEndpoint, open_timeout: Optional[float] = None
    ) -> bool:
        """
        尝试连接单个候选地址

        Args:
            endpoint: 候选服务端
            open_timeout: 握手超时（秒）。None 表示沿用 websockets 的默认值（10s），
                          用于「已锁定」与「单候选」两种情形

        Returns:
            是否连接成功
        """
        try:
            if not self._connect_fail_logged:
                logger.debug(f"正在连接服务端 {endpoint.url}")

            kwargs = dict(
                uri=endpoint.url,
                subprotocols=["binary"],
                max_size=None,
                max_queue=None,  # 防止文件过大时，只发送，来不及消费结果，接收队列填满导致 pause_reading
            )

            # 探测阶段用短超时快速跳过离线主机。注意不能传 None 代替默认值——
            # websockets 把 open_timeout=None 理解为「永不超时」，与默认的 10s 不是一回事
            if open_timeout is not None:
                kwargs["open_timeout"] = open_timeout

            # websockets>=16.0 默认走代理，本地连接需显式禁用，但 14 才引入这个参数
            if tuple(int(v) for v in websockets.__version__.split(".")) >= (14,):
                kwargs["proxy"] = None

            self.state.websocket = await websockets.connect(**kwargs)
            synchronizer = getattr(self.app, 'hotword_sync', None)
            if synchronizer is not None:
                synchronizer.connect(endpoint.url)

            console.print(f'[bold green]已连接服务端: {endpoint.url}[/bold green]\n')
            logger.info(f"WebSocket 建立成功: {endpoint.url}")
            self._connect_fail_logged = False
            return True

        except (ConnectionRefusedError, TimeoutError, asyncio.TimeoutError):
            # open_timeout 触发时抛的是 asyncio.TimeoutError，3.8 下它与内建 TimeoutError 不是同一个类
            if not self._connect_fail_logged:
                logger.debug(f"连接服务端 {endpoint.url} 被拒绝或超时")
        except Exception as e:
            if not self._connect_fail_logged:
                logger.debug(f"连接服务端 {endpoint.url} 失败: {e}")

        return False

    def _probe_timeout(self, endpoint: ServerEndpoint) -> float:
        """
        取该候选的探测超时：内网/回环走短档，其余走保守档

        分档只看配置里写的字符串（见 _is_lan_host），域名与主机名一律归入保守档。

        Args:
            endpoint: 待探测的候选服务端

        Returns:
            握手超时秒数
        """
        if endpoint.is_lan:
            raw = getattr(Config, 'addr_probe_timeout_lan', DEFAULT_PROBE_TIMEOUT_LAN)
            fallback = DEFAULT_PROBE_TIMEOUT_LAN
        else:
            raw = getattr(Config, 'addr_probe_timeout', DEFAULT_PROBE_TIMEOUT)
            fallback = DEFAULT_PROBE_TIMEOUT

        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            timeout = 0.0

        if timeout <= 0:
            if not self._timeout_warned:
                logger.warning(f"探测超时配置非法（{raw!r}），已回退为 {fallback}s")
                self._timeout_warned = True
            return fallback

        return timeout

    async def send(self, message: AudioMessage) -> bool:
        """
        发送消息到服务端
        
        Args:
            message: 要发送的 AudioMessage 对象
            
        Returns:
            发送是否成功
        """
        if not self.is_connected:
            logger.warning("无法发送消息：WebSocket 未连接")
            return False
        
        try:
            await self.state.websocket.send(message.to_json())
            return True
            
        except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosedOK):
            self.state.websocket = None
            raise CommunicationError("发送失败：连接已断开")
            
        except Exception as e:
            raise CommunicationError(f"发送消息时发生未知错误: {e}")
    
    async def receive(self) -> Optional[RecognitionMessage]:
        """
        接收服务端消息
        
        Returns:
            解析后的 RecognitionMessage 对象，如果失败返回 None
        """
        if not self.is_connected:
            logger.warning("无法接收消息：WebSocket 未连接")
            return None
        
        try:
            raw_message = await self.state.websocket.recv()
            data = json.loads(raw_message)
            return RecognitionMessage.from_dict(data)
            
        except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosedOK):
            self.state.websocket = None
            raise CommunicationError("接收失败：连接已断开")
            
        except json.JSONDecodeError as e:
            raise CommunicationError(f"消息解析失败: {e}")
            
        except Exception as e:
            raise CommunicationError(f"接收消息时发生未知错误: {e}")
    
    async def close(self) -> None:
        """关闭 WebSocket 连接"""
        if self.state.websocket is not None:
            await self.state.websocket.close()
            self.state.websocket = None
            logger.info("WebSocket 连接已关闭")

    def close_sync(self) -> None:
        """
        从同步上下文（如 teardown）关闭连接
        
        使用 run_coroutine_threadsafe 安全地将关闭操作调度到已有的事件循环。
        如果事件循环未运行，则直接置空连接引用。
        """
        if self.state.websocket is None:
            return

        loop = self.app.loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.close(), loop)
            logger.debug("已调度 WebSocket 关闭（threadsafe）")
        else:
            # 事件循环已停止，直接清空引用
            self.state.websocket = None
            logger.debug("事件循环已停，直接置空 WebSocket 引用")
