# coding: utf-8
"""
视觉输入子模块

按下截图快捷键时读取剪贴板当前内容里的图片暂存于内存，随下一句话一并
上传给支持视觉的 LLM 角色。完整约束见 PROJECT_INDEX.md 约定 21。
"""

from .. import logger
from core.client.vision.clipboard_watcher import ClipboardWatcher, current_sequence
from core.client.vision.notice import notify
from core.client.vision.clipboard_image import (
    PendingImage,
    grab_clipboard_image,
    MAX_EDGE_PX,
    MAX_IMAGE_BYTES,
    JPEG_FALLBACK_QUALITY,
)
from core.client.vision.vision_manager import VisionManager

__all__ = [
    'logger',
    'ClipboardWatcher',
    'current_sequence',
    'notify',
    'PendingImage',
    'grab_clipboard_image',
    'MAX_EDGE_PX',
    'MAX_IMAGE_BYTES',
    'JPEG_FALLBACK_QUALITY',
    'VisionManager',
]
