# coding: utf-8
"""
视觉输入子模块

按下截图快捷键时读取剪贴板当前内容里的图片暂存于内存，随下一句话一并
上传给支持视觉的 LLM 角色。各环节的约束写在对应文件头：vision_manager（线程、
连按合并、重复挂载）、clipboard_image（格式、缩放、体积）、clipboard_watcher（时效）、
image_archive（落盘归档）、notice（终端提示去重）；图片随录音的绑定与回滚见
core/client/state.py 的「待挂图片」一节。
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
