# coding: utf-8
"""
麦克风任务在控制台的第一行

形如 `任务标识：…  录音时长：3.21s  转录时延：0.45s  热词时延：0.01s`，下面接识别结果等缩进行。

结果处理器统一打印任务行。成功终局沿接收循环串行处理，失败收尾与成功输出共用
输出锁，避免插入上一轮 LLM 回复中间。录音协程仅登记时长，不打印。
用户取消、本地失败与断线均由按任务 ID 的唯一收尾认领；旧回包不再重复打印。
"""

from typing import Optional

ABORTED = '已手动打断，识别结果已丢弃'
ABORTED_PARTIAL = '已手动打断，已上屏的文字保留，其余丢弃'
NOT_CONNECTED = '服务端未连接，无法发送'
SEND_FAILED = '音频发送失败，未收到识别结果'
DISCONNECTED = '连接已断开，未收到识别结果'


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


def no_result_note(state, task_id: str, reason: str) -> str:
    """等不到结果时的说明：已被打断的任务如实说打断，其余写原因"""
    if state.is_task_cancelled(task_id):
        return aborted_note(task_id in state.streamed_task_ids)
    return reason
