# coding: utf-8
"""
单次麦克风录音的识别与上屏状态。

launch 同步预留 task_id、路由和连接。开启流式与 LLM 的录音是 revisable：服务端保留原始
音频，由客户端在开头确认角色后 request_full（用整段普通识别的结果请求角色），或确定是
普通听写后 commit_stream 再逐句上屏。音频结束不等于任务结束：没决定的任务会收到一条
decision（流式候选全文），据此再做一次决定；decision 不上屏、不消费快照。
"""

from dataclasses import dataclass, field
from typing import Any, List

# pending：还没开始逐句上屏（角色未定或挂图）；typing：逐句上屏中
PENDING, TYPING = 'pending', 'typing'


@dataclass
class StreamSession:
    """一次麦克风录音的识别与上屏状态；launch 登记，唯一终局后释放。"""

    task_id: str
    mode: str = PENDING
    held: str = ''              # pending 期间攒下、还没上屏的原始增量
    typed: str = ''             # 已上屏的文字
    pieces: int = 0             # 已上屏的段数
    matches: List[tuple] = field(default_factory=list)  # 各段的热词完全匹配，最终打印用
    streaming: bool = False
    revisable: bool = False
    start_time: float = 0.0
    connection: Any = None      # launch 时的识别连接；断线重连后不改投新连接
    recorder: Any = None
    owner: Any = None           # 发起录音的 ShortcutTask，服务端报错时据此停止采集
    decision: str = ''          # '' 未决 / 'stream' / 'full'；只在 asyncio 线程更新
    role_name: str = ''         # 预览锁定的角色显示名及当时命中的触发别名
    role_alias: str = ''
    sent_audio: bool = False    # 是否发过音频：短按没发过，不创建服务端任务、也不用发取消
    terminal: bool = False      # 终局已被认领；认领与检查之间不得有 await


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
