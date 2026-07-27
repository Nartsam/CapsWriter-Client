# coding: utf-8
"""
Companion 通信协议

定义 CapsWriter 与 Electron 伴侣应用之间的 WebSocket 消息格式。
这是两个项目之间唯一的耦合点。

CapsWriter 只负责：ASR 识别 → 前缀清洗 → 转发文本
Electron 负责：LLM 调用 / AstrBot 对话 / 渲染 / TTS 等
"""
import json


class EventType:
    """出站事件类型（CapsWriter → Electron）"""
    STATUS = 'status'
    RECORDING_START = 'recording_start'
    RECORDING_END = 'recording_end'
    # 录音被主动打断：与 RECORDING_END 分开，让对端能区分「说完了」和「作废了」
    RECORDING_CANCEL = 'recording_cancel'
    USER_MESSAGE = 'user_message'


def make_event(event_type: str, **kwargs) -> str:
    """构造出站事件 JSON 字符串"""
    payload = {'type': event_type, **kwargs}
    return json.dumps(payload, ensure_ascii=False)
