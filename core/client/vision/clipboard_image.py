# coding: utf-8
"""
剪贴板图片读取

从 Windows 剪贴板的**当前内容**取出一张图片，缩放、编码成 PNG 并转 base64 data URL。

为什么直接读剪贴板而不是自己实现框选：系统截图工具（Win+Shift+S 等）本身就是
框选，落进剪贴板的已经是用户圈定的那块区域，自绘一套框选拿到的图完全一样，
却要额外背上全屏遮罩、虚拟屏空洞、DPI、第三个 Tk 解释器这一堆边界。

格式优先级（取第一个成功的）：
    "PNG" 注册格式  → 浏览器与部分截图工具，字节即 PNG，最干净
    CF_DIBV5 (17)   → 现代应用，alpha 通道可靠
    CF_DIB   (8)    → JPEGView 这类传统应用
CF_HDROP（资源管理器里对文件 Ctrl+C）刻意不支持：那只是文件引用，要读盘，而本
模块的调用点对延迟敏感（见 vision_manager 的线程说明）。

注意 Win+V 的剪贴板历史是另一套机制（云剪贴板只收录部分格式，应用还能主动排除），
本模块走 OpenClipboard/GetClipboardData 读当前剪贴板，不受其影响。
"""

from __future__ import annotations

import base64
import ctypes
import io
import struct
import time
from ctypes import wintypes
from typing import NamedTuple, Optional

from . import logger
from .notice import notify

# ---- 剪贴板格式常量 ----
CF_DIB = 8
CF_DIBV5 = 17

# 最长边上限：超过则等比缩小。DeepSeek 单图最多 384 token、服务端必然大幅降采样，
# 保留更高分辨率没有收益，只会拖慢上传
MAX_EDGE_PX = 1920

# 编码后的字节上限，PNG 与 JPEG 兜底共用这一个上限，超过即丢弃。
# 原值 1MB 是按「截图类内容 PNG 压缩率极好」定的（4K 全屏实测约 350KB），但那只对
# 界面类截图成立。照片、立绘、多图并排比对这类内容 PNG 几乎压不动：2000×1380 缩到
# 1920 宽后稳定落在 1.3~1.9MB，2026-09-12 实机日志里连续 16 次挂载全被这条挡下
MAX_IMAGE_BYTES = 3 * 1024 * 1024

# PNG 超限时的 JPEG 兜底质量。兜底只走「缩放后 PNG 仍然超限」这一条罕见路径——
# 没超限就原样发 PNG，不为它多付一次编码时间（这条链路对延迟敏感，见 vision_manager
# 的线程说明）。85 对模型侧足够：单图最多 384 token，服务端本就会大幅降采样
JPEG_FALLBACK_QUALITY = 85

# 打开剪贴板的退避重试，语义与 core/client/clipboard 里那一组一致
CLIPBOARD_OPEN_ATTEMPTS = 5
CLIPBOARD_OPEN_RETRY_S = 0.02

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# HANDLE 是 64 位，ctypes 默认 restype 为 c_int 会把高位截断，
# 拿着被截断的句柄去 GlobalLock 会直接触发访问违例
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
_kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_kernel32.GlobalSize.argtypes = [wintypes.HANDLE]


class PendingImage(NamedTuple):
    """一张已就绪、等待随下一句话上传的图片"""

    data_url: str      # data:image/png;base64,... 或 data:image/jpeg;base64,...
    width: int
    height: int
    byte_size: int     # 编码后字节数（不含 base64 膨胀）
    source: str        # 命中的剪贴板格式名，仅用于日志
    encoding: str = 'PNG'   # 实际编码格式（PNG / JPEG），仅用于日志
    archive_path: str = ''  # 归档到 logs/vision/ 的文件路径，空串表示未归档


def _read_clipboard_bytes(fmt: int) -> Optional[bytes]:
    """读出指定剪贴板格式的原始字节；格式不存在或读取失败返回 None"""
    handle = _user32.GetClipboardData(fmt)
    if not handle:
        return None
    size = _kernel32.GlobalSize(handle)
    if not size:
        return None
    ptr = _kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        return ctypes.string_at(ptr, size)
    finally:
        _kernel32.GlobalUnlock(handle)


def _dib_to_bmp(dib: bytes) -> Optional[bytes]:
    """给 DIB 字节拼上 14 字节 BITMAPFILEHEADER，使其可被 PIL 当作 BMP 打开

    比手写像素解析可靠得多：位深、调色板、压缩方式的组合全部交给 PIL。
    """
    if len(dib) < 16:
        return None
    header_size = struct.unpack_from('<I', dib, 0)[0]
    # BITMAPINFOHEADER(40) / V4(108) / V5(124)，其余视为不认识的变体
    if header_size not in (40, 52, 56, 108, 124):
        logger.debug(f"[视觉] 不认识的 DIB 头大小: {header_size}")
        return None

    bit_count = struct.unpack_from('<H', dib, 14)[0]
    clr_used = struct.unpack_from('<I', dib, 32)[0] if len(dib) >= 36 else 0
    # 调色板项数：显式给出就用它，否则 8 位及以下按位深推算
    if clr_used:
        palette_entries = clr_used
    elif bit_count <= 8:
        palette_entries = 1 << bit_count
    else:
        palette_entries = 0

    pixel_offset = 14 + header_size + palette_entries * 4
    # 16/32 位可能带 BITFIELDS 掩码，V4 及以上的掩码已计入 header_size
    if header_size == 40 and bit_count in (16, 32):
        compression = struct.unpack_from('<I', dib, 16)[0]
        if compression == 3:  # BI_BITFIELDS
            pixel_offset += 12

    return (b'BM'
            + struct.pack('<IHHI', 14 + len(dib), 0, 0, pixel_offset)
            + dib)


def _grab_from_clipboard():
    """按格式优先级取出一张 PIL Image，返回 (Image, 来源格式名, 原始 PNG 字节)

    第三个返回值只有命中 "PNG" 格式时才非空，供尺寸已合格时跳过重编码。
    调用方必须已经打开剪贴板。
    """
    from PIL import Image

    png_fmt = _user32.RegisterClipboardFormatW('PNG')

    # 1) "PNG" 注册格式：字节本身就是 PNG
    if png_fmt:
        raw = _read_clipboard_bytes(png_fmt)
        if raw:
            try:
                return Image.open(io.BytesIO(raw)), 'PNG', raw
            except Exception as e:
                logger.debug(f"[视觉] PNG 格式解码失败，继续尝试 DIB: {e}")

    # 2) CF_DIBV5 / CF_DIB：拼 BMP 文件头后交给 PIL
    for fmt, name in ((CF_DIBV5, 'CF_DIBV5'), (CF_DIB, 'CF_DIB')):
        raw = _read_clipboard_bytes(fmt)
        if not raw:
            continue
        bmp = _dib_to_bmp(raw)
        if not bmp:
            continue
        try:
            return Image.open(io.BytesIO(bmp)), name, None
        except Exception as e:
            logger.debug(f"[视觉] {name} 解码失败: {e}")

    return None, '', None


def _shrink_if_needed(image):
    """最长边超过 MAX_EDGE_PX 则等比缩小；只缩不放"""
    from PIL import Image

    longest = max(image.size)
    if longest <= MAX_EDGE_PX:
        return image
    ratio = MAX_EDGE_PX / float(longest)
    new_size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
    logger.debug(f"[视觉] 图片过大，{image.size} → {new_size}")
    return image.resize(new_size, Image.LANCZOS)


def _drop_dead_alpha(image):
    """DIB 来源的 32bpp 图若整层 alpha 全为 0，丢掉 alpha 转成 RGB

    不少传统应用往 CF_DIB/CF_DIBV5 里写 32 位像素时根本不填 alpha 字节，PIL 按
    RGBA 读出来就是「全透明」。这类图编码出的 PNG 在模型眼里是一张空白，而且
    本地毫无报错——必须在这里挡掉。
    """
    if image.mode != 'RGBA':
        return image
    try:
        alpha_min, alpha_max = image.getchannel('A').getextrema()
    except Exception:
        return image
    if alpha_max == 0:
        logger.debug("[视觉] DIB 的 alpha 通道全为 0，按不透明处理")
        return image.convert('RGB')
    return image


# 带 alpha 通道的模式，转码时保留透明度
_ALPHA_MODES = ('RGBA', 'LA', 'PA', 'La')


def _encode_png(image) -> bytes:
    """编码成 PNG 字节。带透明通道的转 RGBA，其余统一转 RGB。"""
    if image.mode not in ('RGB', 'RGBA'):
        image = image.convert('RGBA' if image.mode in _ALPHA_MODES else 'RGB')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', optimize=False)
    return buffer.getvalue()


def _encode_jpeg(image) -> bytes:
    """编码成 JPEG 字节，供 PNG 超限时兜底

    JPEG 没有 alpha 通道，带透明度的图必须先合成到白底再编码。不能直接
    `convert('RGB')`：那只是把 alpha 通道丢掉、保留透明像素底下的 RGB 值，
    而截图里那些值往往是黑的，结果是透明区域变成一片黑。
    """
    from PIL import Image

    if image.mode in _ALPHA_MODES:
        rgba = image.convert('RGBA')
        background = Image.new('RGB', rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel('A'))
        image = background
    elif image.mode != 'RGB':
        image = image.convert('RGB')
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=JPEG_FALLBACK_QUALITY, optimize=True)
    return buffer.getvalue()


def _open_clipboard_with_retry() -> bool:
    """打开剪贴板，被别的进程占着时退避重试

    Windows 同一时刻只允许一个窗口打开剪贴板，而本功能的典型用法恰好是「截完图立刻
    按快捷键」——截图工具刚写完往往还占着它。占用是瞬时的，退避几十毫秒基本都能拿到；
    不重试的表现就是「按了没反应，再按一次就好」。本函数跑在工作线程上，这点等待
    不会碰到低级钩子的 300ms 上限（见模块顶部的线程说明）。
    """
    for attempt in range(CLIPBOARD_OPEN_ATTEMPTS):
        if _user32.OpenClipboard(None):
            return True
        if attempt + 1 < CLIPBOARD_OPEN_ATTEMPTS:
            logger.debug(f"[视觉] 剪贴板被占用，{CLIPBOARD_OPEN_RETRY_S * 1000:.0f}ms 后重试"
                         f"（第 {attempt + 1} 次）")
            time.sleep(CLIPBOARD_OPEN_RETRY_S)
    return False


def grab_clipboard_image() -> Optional[PendingImage]:
    """读取剪贴板当前内容里的图片

    Returns:
        就绪的 PendingImage；剪贴板里没有图片、解码失败或超出体积上限时返回 None
        （三种情况都不算错误，调用方一律按「没有图」处理）

    每一条返回 None 的路径都自己往控制台打一行说明原因：按下快捷键后成功与失败
    此前给用户的反馈完全一样——都是什么都没有（日志走 INFO，而控制台处理器固定在
    WARNING），于是「图太大被丢了」在终端上看起来和「快捷键没生效」一模一样。
    调用方只负责汇总，不再重复解释原因。
    """
    if not _open_clipboard_with_retry():
        logger.info("[视觉] 剪贴板被其他进程占用，重试后仍打不开，未挂载")
        notify('    [yellow]剪贴板被其他进程占用，未挂载图片[/yellow]'
                      '（稍后再按一次即可）')
        return None
    try:
        image, source, raw_png = _grab_from_clipboard()
    except Exception as e:
        logger.warning(f"[视觉] 读取剪贴板图片失败: {e}", exc_info=True)
        notify(f'    [red]读取剪贴板图片失败[/red]（{e}）')
        return None
    finally:
        _user32.CloseClipboard()

    if image is None:
        # 剪贴板里没有图片是最常见的情况（复制的是文字、文件或什么都没有），不是错误
        logger.info("[视觉] 剪贴板中没有可用图片，未挂载")
        notify('    [yellow]剪贴板中没有图片，未挂载[/yellow]')
        return None

    try:
        # PIL 打开时只读文件头，此处拿尺寸不会触发整图解码：剪贴板本来就是 PNG
        # 且尺寸已合格时直接复用原字节，省掉一次解码 + 编码（4K 图上这是最慢的一步）
        original_size = image.size
        if raw_png and max(original_size) <= MAX_EDGE_PX:
            data = raw_png
        else:
            if not raw_png:
                image = _drop_dead_alpha(image)
            image = _shrink_if_needed(image)
            data = _encode_png(image)
    except Exception as e:
        logger.warning(f"[视觉] 图片编码失败: {e}", exc_info=True)
        notify(f'    [red]图片编码失败[/red]（{e}）')
        return None

    encoding, mime = 'PNG', 'image/png'

    # 缩放后 PNG 仍然超限才转 JPEG。PNG 压不动照片/立绘这类内容，而同一张图的 JPEG
    # 通常小一个数量级；没超限的那条路原样发 PNG，不付这次重编码的时间
    if len(data) > MAX_IMAGE_BYTES:
        png_bytes = len(data)
        try:
            jpeg = _encode_jpeg(image)
        except Exception as e:
            logger.warning(f"[视觉] JPEG 兜底编码失败: {e}", exc_info=True)
            jpeg = None

        if jpeg is None or len(jpeg) > MAX_IMAGE_BYTES:
            jpeg_desc = f"JPEG {len(jpeg) / 1024:.0f}KB" if jpeg else "JPEG 编码失败"
            logger.info(
                f"[视觉] 图片过大已丢弃：PNG {png_bytes / 1024:.0f}KB、{jpeg_desc}，"
                f"均超过 {MAX_IMAGE_BYTES / 1024:.0f}KB 上限（原始尺寸 {original_size}）"
            )
            notify(
                f'    [yellow]图片过大，未挂载[/yellow]'
                f'（PNG {png_bytes / 1024:.0f}KB、{jpeg_desc}，'
                f'上限 {MAX_IMAGE_BYTES / 1024:.0f}KB）'
            )
            return None

        logger.info(
            f"[视觉] PNG {png_bytes / 1024:.0f}KB 超过上限，"
            f"已改用 JPEG {len(jpeg) / 1024:.0f}KB（质量 {JPEG_FALLBACK_QUALITY}）"
        )
        data, encoding, mime = jpeg, 'JPEG', 'image/jpeg'

    return PendingImage(
        data_url=f'data:{mime};base64,' + base64.b64encode(data).decode('ascii'),
        width=image.width,
        height=image.height,
        byte_size=len(data),
        source=source,
        encoding=encoding,
    )
