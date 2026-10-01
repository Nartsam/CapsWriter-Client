# coding: utf-8
"""
流式上屏的单次录音状态

快捷键配置了 streaming 时，服务端按停顿切句，每识别完一句就回一条带 delta 的非最终结果
（协议见 core/protocol.py 的 RecognitionMessage.delta）。ResultProcessor 收到后逐句打字上屏，
最终结果只带最后一句。已发出的 delta 服务端不会再改，所以上屏的字不需要回删。

第一句决定这次录音还要不要边说边上屏：开头喊了角色名（或默认角色已启用），整次录音改为
松手后按原流程处理，此后的 delta 只攒着不上屏，最终结果里的 text 就是全文。
第一句恰好停在角色名中间（「新岛」之于「新岛真」）时先不上屏，等下一句接上再判断。

服务端不支持流式（旧版本）时不会有 delta，最终结果照常整段处理，与非流式完全相同。
"""

from dataclasses import dataclass, field
from typing import List

# pending：第一句还没判定；typing：逐句上屏；deferred：改为松手后按原流程处理
PENDING, TYPING, DEFERRED = 'pending', 'typing', 'deferred'


@dataclass
class StreamSession:
    """一次流式录音在客户端的上屏状态：第一条 delta 到达时创建，最终结果到达时取走"""

    task_id: str
    mode: str = PENDING
    held: str = ''              # pending 期间攒下、还没上屏的文字（已做繁简、热词与规则替换）
    typed: str = ''             # 已上屏的文字
    pieces: int = 0             # 已上屏的段数
    matches: List[tuple] = field(default_factory=list)  # 各段的热词完全匹配，最终打印用


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
