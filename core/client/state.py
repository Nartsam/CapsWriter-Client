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


# 路由快照最多保留几条。服务端对每个任务必定回一个最终包，正常情况下最多只有
# 一条在飞；定长兜底避免服务端异常时条目无限滞留。
MAX_CONTINUOUS_SNAPSHOTS = 8


@dataclass
class ContinuousChatSnapshot:
    """
    单次录音的连续对话路由快照

    「这句话该发给谁」由说它的那一刻决定，而不是结果到达时的全局状态——两者之间
    隔着一整个服务端往返，期间用户完全可能已经开始说下一句、或者已经切换了状态。
    快照在 launch() 时创建、拿到 task_id 后登记，最终结果到达时消费。

    连续对话只改路由、不碰对话记忆：历史一直由各角色的 enable_history 维护，所以
    进入时不清空历史，enable_history=False 的角色（如翻译）也能进。

    放弃过的更简单方案是「等结果期间禁止再次录音」：pending_task_id 在断线时没人清，
    服务端一崩录音键就永久锁死；而且按下录音键没反应会让整句话凭空消失。

    任何让这次录音作废的路径（短按取消、录音阶段或等待阶段的打断键）都回滚本次
    切换。代价是退出也必须完成一次有效录音（静默长按过阈值再松手即可）。

    Attributes:
        sticky_role: 录音开始时生效的粘滞角色名；None 表示当时不在连续对话中
        toggled: 本次录音期间切换键按了奇数次
        exit_committed: 退出已在松手时落地。等待结果阶段再按打断键要据此回滚
    """

    sticky_role: Optional[str] = None
    toggled: bool = False
    exit_committed: bool = False

    @property
    def is_arming(self) -> bool:
        """本次录音是否在尝试进入连续对话（原本不在，且按了切换键）"""
        return self.toggled and not self.sticky_role

    @property
    def is_exiting(self) -> bool:
        """本次录音是否在退出连续对话（原本在，且按了切换键）"""
        return self.toggled and bool(self.sticky_role)

    @property
    def forced_role(self) -> Optional[str]:
        """这句话要强制发给哪个角色；None 表示走正常的角色前缀检测"""
        return None if self.toggled else self.sticky_role

    def indicator_mode(self) -> str:
        """映射成悬浮指示器的连续对话显示态"""
        if self.is_arming:
            return "armed"
        if self.is_exiting:
            return "exited"
        if self.sticky_role:
            return "active"
        return ""


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

    # 流式上屏已经打出过文字的任务 ID。打断只能丢弃还没上屏的部分，打断提示据此改口
    streamed_task_ids: Deque[str] = field(default_factory=lambda: deque(maxlen=8))

    # 连续对话中的粘滞角色名（RoleConfig.display_name）；None 表示不在连续对话中。
    # 只在「录音正常结束」和「结果确认命中具名角色」两个时刻变更，其余一律回滚
    continuous_role: Optional[str] = None

    # 最后一次连续对话交互结束的时间戳，供闲置超时的懒判断使用
    continuous_last_active: float = 0.0

    # 每次录音的路由快照。current 与字典里的是同一个对象：快照要在 launch() 时就
    # 建好（那时粘滞角色已确定），而 task_id 要等 record_and_send() 协程跑起来才有
    continuous_current: Optional[ContinuousChatSnapshot] = None
    continuous_snapshots: Dict[str, ContinuousChatSnapshot] = field(default_factory=dict)

    # 待挂图片（按下截图快捷键后从剪贴板读入，纯内存、不落盘）。
    # 与连续对话同构地按录音快照绑定：图片归属由「说这句话时它挂着没有」决定，
    # 而不是结果到达时的全局槽位——中间隔着一整个服务端往返（同上面的连续对话快照）
    pending_image: Optional[Any] = None
    image_current: Optional[Any] = None
    image_snapshots: Dict[str, Any] = field(default_factory=dict)

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
        self.streamed_task_ids.clear()
        self.continuous_role = None
        self.continuous_last_active = 0.0
        self.continuous_current = None
        self.continuous_snapshots.clear()
        self.pending_image = None
        self.image_current = None
        self.image_snapshots.clear()

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

    # ========== 连续对话 ==========

    def begin_continuous_snapshot(self) -> ContinuousChatSnapshot:
        """录音启动时创建本次录音的路由快照，记录此刻生效的粘滞角色"""
        snapshot = ContinuousChatSnapshot(sticky_role=self.continuous_role)
        self.continuous_current = snapshot
        return snapshot

    def bind_continuous_snapshot(self, task_id: Optional[str]) -> None:
        """拿到 task_id 后把当前快照登记进字典，供结果到达时按任务查回"""
        snapshot = self.continuous_current
        if not task_id or snapshot is None:
            return
        self.continuous_snapshots[task_id] = snapshot
        # 字典按插入序，超出上限时丢最旧的（Python 3.7+ 保证顺序）
        while len(self.continuous_snapshots) > MAX_CONTINUOUS_SNAPSHOTS:
            self.continuous_snapshots.pop(next(iter(self.continuous_snapshots)))

    def toggle_continuous(self) -> Optional[ContinuousChatSnapshot]:
        """翻转本次录音的连续对话切换位，返回翻转后的快照"""
        snapshot = self.continuous_current
        if snapshot is None:
            return None
        snapshot.toggled = not snapshot.toggled
        return snapshot

    def pop_continuous_snapshot(self, task_id: str) -> Optional[ContinuousChatSnapshot]:
        """取出并移除指定任务的路由快照"""
        return self.continuous_snapshots.pop(task_id, None)

    def commit_continuous_exit(self) -> Optional[str]:
        """
        录音正常结束时落地退出

        Returns:
            被退出的角色名；本次录音不是退出型则返回 None
        """
        snapshot = self.continuous_current
        self.continuous_current = None
        if snapshot is None or not snapshot.is_exiting:
            return None
        snapshot.exit_committed = True
        self.continuous_role = None
        return snapshot.sticky_role

    def rollback_continuous_snapshot(self, task_id: Optional[str] = None) -> Optional[str]:
        """
        本次录音作废：丢弃快照，并把已经落地的退出恢复回去

        「这次输入不算数」要连同这次录音里按过的切换键一起不算数，否则会出现
        「录音阶段按 Esc 能撤销退出、松手后按 Esc 撤销不了」这种要背时序才能解释的差别。

        Returns:
            被恢复的角色名；没有需要恢复的则返回 None
        """
        snapshot = self.continuous_current
        self.continuous_current = None
        if task_id:
            snapshot = self.continuous_snapshots.pop(task_id, None) or snapshot
        if snapshot is None:
            return None
        # continuous_role 非空说明期间已经进入了新的连续对话，此时不该覆盖
        if snapshot.exit_committed and self.continuous_role is None:
            self.continuous_role = snapshot.sticky_role
            return snapshot.sticky_role
        return None

    def enter_continuous(self, role_name: str) -> None:
        """进入与指定角色的连续对话"""
        self.continuous_role = role_name
        self.continuous_last_active = time.time()
        logger.info(f"进入连续对话: {role_name}")

    def exit_continuous(self) -> Optional[str]:
        """无条件退出连续对话，返回原来的角色名"""
        role = self.continuous_role
        self.continuous_role = None
        return role

    def touch_continuous(self) -> None:
        """刷新连续对话的最后活动时间，用于闲置超时判断"""
        if self.continuous_role:
            self.continuous_last_active = time.time()

    def expire_continuous_if_idle(self, timeout: float) -> Optional[str]:
        """
        闲置超时的懒判断：在下一次按下录音键时检查，不开后台定时器

        Args:
            timeout: 超时秒数，<= 0 表示永不超时

        Returns:
            因超时被退出的角色名；未超时则返回 None
        """
        if not self.continuous_role or timeout <= 0:
            return None
        if self.continuous_last_active <= 0:
            return None
        if time.time() - self.continuous_last_active < timeout:
            return None
        return self.exit_continuous()

    # ========== 待挂图片 ==========

    def set_pending_image(self, image: Any) -> None:
        """存入一张待挂图片。连按快捷键时后者覆盖前者，不排队。"""
        self.pending_image = image

    def clear_pending_image(self) -> None:
        """丢弃尚未绑定到任何录音的待挂图片"""
        self.pending_image = None

    def begin_image_snapshot(self) -> Any:
        """录音启动时把待挂图片移入本次录音的快照

        「移入」而非「复制」：图片一旦绑定到某次录音就不再是待挂状态，
        这样「已绑定」与「待挂」互斥，指示器边框和后续录音的归属都不会含糊。
        还没读完剪贴板时槽位为空，本次录音就是一次普通录音——这正是
        「下一条」从**挂载成功那一刻**起算的含义（见 vision_manager 模块说明）。
        """
        image = self.pending_image
        self.pending_image = None
        self.image_current = image
        return image

    def bind_image_snapshot(self, task_id: Optional[str]) -> None:
        """拿到 task_id 后登记图片快照，供结果到达时按任务查回"""
        image = self.image_current
        if not task_id or image is None:
            return
        self.image_snapshots[task_id] = image
        while len(self.image_snapshots) > MAX_CONTINUOUS_SNAPSHOTS:
            self.image_snapshots.pop(next(iter(self.image_snapshots)))

    def pop_image_snapshot(self, task_id: str) -> Any:
        """取出并移除指定任务绑定的图片（消耗）

        **不清 image_current**：上一句的结果到达时，下一次录音可能已经 launch
        但还没跑到 bind（record_and_send 是另一个协程），此刻清掉会把新录音刚
        绑定的图凭空抹掉。image_current 由下一次 begin_image_snapshot 覆盖，
        不会累积。
        """
        return self.image_snapshots.pop(task_id, None)

    def rollback_image_snapshot(self, task_id: Optional[str] = None) -> Any:
        """本次录音作废：把绑定的图片退回待挂状态

        与连续对话的回滚同一条规则——让这次录音作废的任何路径（短按取消、
        录音阶段 ESC、等待阶段 ESC）都不该顺带烧掉一张图，重截的代价比重说一句高。

        Returns:
            被退回的图片；没有可退回的返回 None
        """
        image = self.image_current
        self.image_current = None
        if task_id:
            image = self.image_snapshots.pop(task_id, None) or image
        if image is None:
            return None
        # 期间若已按过快捷键读进新图，就不要用旧图把它顶掉
        if self.pending_image is None:
            self.pending_image = image
            return image
        return None

    def set_output_text(self, text: str) -> None:
        """
        设置最近一次输出文本
        
        Args:
            text: 输出文本内容
        """
        self.last_output_text = text



