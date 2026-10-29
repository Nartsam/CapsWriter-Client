# coding: utf-8
"""
通信协议模块

定义客户端与服务端之间的消息协议数据类。
这些类同时用于服务端和客户端，确保消息格式一致。
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict, fields
from typing import List, Literal
import json


def _from_dict(cls, data: dict):
    """按字段名取值：默认值只写在字段定义里，对端新增的未知字段忽略"""
    known = {f.name for f in fields(cls) if f.init}
    return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class AudioMessage:
    """
    客户端 -> 服务端：音频数据消息
    
    Attributes:
        task_id: 任务唯一标识
        source: 音频来源 ('mic' 麦克风 或 'file' 文件)
        data: Base64 编码的音频数据 (float32, 16kHz, mono)
        is_final: 是否为当前任务的最后一个数据包
        time_start: 录音/音频开始时间戳
        seg_duration: 分段时长（秒）
        seg_overlap: 重叠时长（秒）
        streaming: 流式上屏（仅麦克风任务）。服务端按停顿切句、逐句回传新增文字
                   （RecognitionMessage.delta）；流式阶段忽略 seg_duration / seg_overlap，
                   revisable 任务选择 full 后按这两个参数重新切片
        revisable: 保留流式麦克风原始音频，等待客户端选择 stream 或 full 终局
    """
    task_id: str
    source: Literal['mic', 'file']
    data: str                    # base64 编码的音频
    is_final: bool
    time_start: float
    seg_duration: float = 15.0
    seg_overlap: float = 2.0
    context: str = ''
    language: str = 'auto'
    streaming: bool = False
    revisable: bool = False

    def to_json(self) -> str:
        """序列化为 JSON 字符串"""
        return json.dumps(asdict(self), ensure_ascii=False)
    
    @classmethod
    def from_dict(cls, data: dict) -> AudioMessage:
        """从字典创建实例"""
        return _from_dict(cls, data)


@dataclass
class RecognitionControlMessage:
    """同一连接上对原 task_id 的决定；音频结束包仅表示输入结束。"""
    task_id: str
    action: Literal['commit_stream', 'request_full', 'cancel']
    type: str = field(default='recognition_control', init=False)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> RecognitionControlMessage:
        if data['type'] != 'recognition_control':
            raise ValueError('未知控制消息类型')
        if data['action'] not in ('commit_stream', 'request_full', 'cancel'):
            raise ValueError('未知识别控制动作')
        return _from_dict(cls, data)


@dataclass
class RecognitionMessage:
    """
    服务端 -> 客户端：识别结果消息
    
    Attributes:
        task_id: 任务唯一标识
        is_final: 是否为最终结果（所有片段识别完成）
        duration: 已处理的音频总时长（秒）
        time_start: 录音/音频开始时间戳
        time_submit: 最后一个片段的提交时间戳
        time_complete: 识别完成时间戳
        
        text: 主要输出 - 简单文本拼接结果（不依赖时间戳）
        text_accu: 精确输出 - 基于时间戳去重的拼接结果（用于字幕生成）
        tokens: 字级 token 列表（与 timestamps 对应）
        timestamps: 字级时间戳列表（秒）

        delta: 流式任务中本条消息新增的文字，接在此前各条的 delta 之后；
               partial 和成功 stream final 的 delta 按序拼成 text；decision 的尾段
               delta 暂不上屏，commit_stream 后的 final 会再次携带它。full 恒为空
        phase: partial 进度、decision 等待选择、final 唯一终局
        recognition_mode: stream 流式候选，full 普通识别
        status: ok / error / cancelled；后两者仅用于终局，text 和 delta 为空
        error: status=error 时的可读原因
    """
    task_id: str
    is_final: bool
    duration: float
    time_start: float
    time_submit: float
    time_complete: float
    
    # 主要输出（简单文本拼接）
    text: str
    
    # 精确输出（时间戳拼接）
    text_accu: str = ''
    tokens: List[str] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)

    # 流式上屏的增量文字
    delta: str = ''
    phase: Literal['partial', 'decision', 'final'] = 'partial'
    recognition_mode: Literal['stream', 'full'] = 'full'
    status: Literal['ok', 'error', 'cancelled'] = 'ok'
    error: str = ''
    
    def to_json(self) -> str:
        """序列化为 JSON 字符串"""
        return json.dumps(asdict(self), ensure_ascii=False)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> RecognitionMessage:
        """从字典创建实例"""
        return _from_dict(cls, data)
