# coding: utf-8
"""
麦克风任务在控制台的第一行

形如 `任务标识：…  录音时长：3.21s  转录时延：0.45s  热词时延：0.01s`，下面接识别结果等缩进行。

一个任务的全部终端输出排在一起，不和别的任务交错。这一行只在结果处理器里打印：成功终局
沿接收循环串行处理（接收循环要等整条流水线、含 LLM 回复走完才读下一条）；打断、出错、
断线由 ResultProcessor._fail_task 在循环之外收尾，与成功输出共用一把输出锁。录音协程
只登记录音时长（ClientState.register_awaiting_result），不打印——松手时就打的话，等上一句
角色回复期间说的下一句会插进上一句的输出中间。每个任务只有一个收尾认领者，所以只打一次。
"""

from typing import Optional

ABORTED = '已手动打断，识别结果已丢弃'
ABORTED_PARTIAL = '已手动打断，已上屏的文字保留，其余丢弃'
NOT_CONNECTED = '服务端未连接，无法发送'
SEND_FAILED = '音频发送失败，未收到识别结果'


def format_task_line(task_id: str, duration: Optional[float], *items: str) -> str:
    """拼出任务的第一行

    Args:
        task_id: 任务标识
        duration: 录音时长（秒）；None 表示没登记过，这一项不显示
        items: 后续各项，如「转录时延：0.45s」或打断、出错的说明
    """
    parts = [f'任务标识：{task_id}']
    if duration is not None:
        parts.append(f'录音时长：{duration:.2f}s')
    parts.extend(items)
    return '  '.join(parts)


def aborted_note(partial: bool) -> str:
    """被打断任务的说明；partial 表示流式上屏已经打出过文字（打出去的收不回来）"""
    return ABORTED_PARTIAL if partial else ABORTED
