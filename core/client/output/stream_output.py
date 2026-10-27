# coding: utf-8
"""
单次麦克风录音的识别与上屏状态。

launch 同步预留 task_id、路由和连接；音频结束后任务仍可等待客户端决定。
revisable 任务开头确认角色后 request_full，普通听写则 commit_stream 后逐句打字。
挂图未确定角色时保持待定、不上屏；decision 只是候选，只有 final 才消费快照与归档。
阶段和决定在 asyncio 线程更新，键盘钩子只锁存现有取消意图与采集身份。
"""

from dataclasses import dataclass, field
from typing import Any, List

# pending：第一句还没判定；typing：逐句上屏；deferred：改为松手后按原流程处理
PENDING, TYPING, DEFERRED = 'pending', 'typing', 'deferred'


@dataclass
class StreamSession:
    """一次麦克风录音的识别与上屏状态；launch 预留，唯一终局后释放。"""

    task_id: str
    mode: str = PENDING
    held: str = ''              # pending 期间攒下、还没上屏的文字（已做繁简、热词与规则替换）
    typed: str = ''             # 已上屏的文字
    pieces: int = 0             # 已上屏的段数
    matches: List[tuple] = field(default_factory=list)  # 各段的热词完全匹配，最终打印用
    streaming: bool = False
    revisable: bool = False
    start_time: float = 0.0
    connection: Any = None
    recorder: Any = None
    owner: Any = None
    decision: str = ''          # 未决 / stream / full；决定只在 asyncio 线程更新
    role_name: str = ''
    role_alias: str = ''
    audio_ended: bool = False
    sent_audio: bool = False
    terminal: bool = False      # 在任何 await 之前认领终局


def trim_for_strip_punc(typed: str, piece: str, stripped_full: str) -> str:
    """
    把「整句去末尾标点」的结果落到最后一段上

    去末尾标点（TextOutput.strip_punc）看的是全文长度，但前面各段已经上屏改不了，
    只能从最后一段里去掉；最后一段为空时就不去了。
    """
    cut = len(typed) + len(piece) - len(stripped_full)
    if cut <= 0 or cut > len(piece):
        return piece
    return piece[:len(piece) - cut]
