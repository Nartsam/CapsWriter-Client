# coding: utf-8
"""
剪贴板工具模块

提供统一的剪贴板操作接口，包括：
1. 安全读取剪贴板（支持多种编码）
2. 安全写入剪贴板
3. 剪贴板保存/恢复上下文管理器
4. 粘贴文本（模拟 Ctrl+V）
"""
import asyncio
import platform
from contextlib import contextmanager
from typing import Optional
import pyclip
from pynput import keyboard
from . import logger


# 支持的编码列表
CLIPBOARD_ENCODINGS = ['utf-8', 'gbk', 'utf-16', 'latin1']


def try_paste() -> Optional[str]:
    """
    从剪贴板读取文本，能区分「读不到」与「本来就是空文本」

    Returns:
        解码后的文本；剪贴板里不是文本（典型是图片）或解码失败时返回 None

    **凡是之后要把读到的内容写回剪贴板的调用方，必须用本函数而不是 safe_paste()**：
    后者把失败也折叠成空字符串，拿它写回去会先 EmptyClipboard，把原内容
    （典型是一张待用的截图）直接清掉。
    """
    try:
        clipboard_data = pyclip.paste()
    except Exception as e:
        # 剪贴板里是图片等非文本内容时 pyclip 必然抛异常，这是预期情况而非故障。
        # 走 WARNING 会直接刷到运行终端（logger.py 把控制台处理器固定在 WARNING 级），
        # 而带图对话时每句都要触发两次
        logger.debug(f"剪贴板中没有可读的文本内容: {e}")
        return None

    # 尝试多种编码方式
    for encoding in CLIPBOARD_ENCODINGS:
        try:
            return clipboard_data.decode(encoding)
        except (UnicodeDecodeError, AttributeError):
            continue

    logger.debug(f"剪贴板解码失败，尝试了编码: {CLIPBOARD_ENCODINGS}")
    return None


def safe_paste() -> str:
    """
    安全地从剪贴板读取并解码文本

    尝试多种编码方式，确保能够正确读取

    Returns:
        解码后的文本字符串，失败返回空字符串
    """
    text = try_paste()
    return text if text is not None else ""


def safe_copy(content: str) -> bool:
    """
    安全地复制内容到剪贴板

    Args:
        content: 要复制的内容

    Returns:
        是否成功
    """
    if not content:
        return False

    try:
        pyclip.copy(content)
        logger.debug(f"剪贴板写入成功，长度: {len(content)}")
        return True
    except Exception as e:
        logger.warning(f"剪贴板写入失败: {e}")
        return False


def copy_to_clipboard(content: str):
    """
    复制内容到剪贴板（兼容旧 API）

    Args:
        content: 要复制的内容
    """
    safe_copy(content)


@contextmanager
def save_and_restore_clipboard():
    """
    剪贴板保存/恢复上下文管理器

    用法:
        with save_and_restore_clipboard():
            # 在这里操作剪贴板
            pyclip.copy("临时内容")
        # 退出后剪贴板恢复原内容
    """
    original = safe_paste()
    try:
        yield
    finally:
        if original:
            pyclip.copy(original)
            logger.debug("剪贴板已恢复")


async def paste_text(text: str, restore_clipboard: bool = True):
    """
    通过模拟 Ctrl+V 粘贴文本

    Args:
        text: 要粘贴的文本
        restore_clipboard: 粘贴后是否恢复原剪贴板内容
    """
    # 保存剪切板
    original = ''
    if restore_clipboard:
        try:
            original = safe_paste()
        except:
            pass

    # 复制要粘贴的文本
    pyclip.copy(text)
    logger.debug(f"已复制文本到剪贴板，长度: {len(text)}")

    # 粘贴结果（使用 pynput 模拟 Ctrl+V）
    controller = keyboard.Controller()
    if platform.system() == 'Darwin':
        # macOS: Command+V
        with controller.pressed(keyboard.Key.cmd):
            controller.tap('v')
    else:
        # Windows/Linux: Ctrl+V
        with controller.pressed(keyboard.Key.ctrl):
            controller.tap('v')
    
    logger.debug("已发送粘贴命令 (Ctrl+V)")

    # 还原剪贴板
    if restore_clipboard and original:
        await asyncio.sleep(0.1)
        pyclip.copy(original)
        logger.debug("剪贴板已恢复")
