# coding: utf-8
"""
挂载图片的落盘归档（排查用）

把每一张**成功挂载**的图片存进 `logs/vision/`，等这次录音的结果到达后再按去向
重命名，补上 `task_id` 与去向。

动机来自 2026-09-12 的一次实机排查：日志只能告诉我们「上传了一张 185×83 的图」，
至于那到底是哪一张、内容是什么，只能靠翻截图目录反推。存一份原图，事后直接打开
就能对答案。

文件名形如：

    260912031647_DV5_pend.png                 # 刚挂载，去向未知
    260912031647_DV5_used_<task_id>.png       # 随该次录音上传了
    260912031647_DV5_drop_<task_id>.png       # 没用上（角色不支持视觉、被新图顶掉等）

每一段都是定长，列目录时逐列对齐：时间戳 12 位、来源代号 3 位、去向 4 位。
来源代号是剪贴板格式的缩写（`PNG` / `DV5` = CF_DIBV5 / `DIB` = CF_DIB / `UNK` = 未知）——
格式原名长度不一（3/6/8 位），直接写进文件名会把后面几列挤得错开。原名在日志里仍是全称。
留着这一列是因为 `CF_DIB` 那条路有 `_drop_dead_alpha` 在处理「32 位像素但 alpha 全为 0」
的老毛病，日后再出现「图是一片空白」时，先看来源就能判断是不是又踩了它。

**时间戳必须排在最前面**：`task_id` 是 UUID1，开头的 `time_low` 只是时间戳的低 32 位，
每约 429.5 秒（2^32 × 100ns）回绕一次，按它排序得不到时间顺序——日志里就有现成反例
（03:10 是 `62ccea6f-ae14-…`，03:40 是 `97def774-ae18-…`，中间几次并不单调）。

本模块的任何失败都只记日志、不往外抛：归档是排查辅助，不能反过来影响主流程。
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Optional

from . import logger

# 归档目录（相对客户端根目录）。`logs/` 已在 .gitignore 里
ARCHIVE_SUBDIR = ('logs', 'vision')

# 只清理本模块自己产出的文件。先按这个模式筛一遍再删，避免有人往目录里放了别的东西
# 时被连带删掉——自动删除是不可逆的，宁可少删不可错删
_ARCHIVE_NAME_RE = re.compile(
    r'^\d{12}_[\w.-]+\.(?:png|jpg)$', re.IGNORECASE)

_EXT_BY_ENCODING = {'PNG': 'png', 'JPEG': 'jpg'}

# 剪贴板格式名 → 定长代号。原名 PNG/CF_DIB/CF_DIBV5 长度分别是 3/6/8 位，
# 直接写进文件名会让后面的去向列对不齐
_SOURCE_CODES = {'PNG': 'PNG', 'CF_DIBV5': 'DV5', 'CF_DIB': 'DIB'}
SOURCE_CODE_UNKNOWN = 'UNK'

# 去向标记一律 4 个字符，列目录时能对齐
OUTCOME_PENDING = 'pend'   # 已挂载，还不知道会不会被用上
OUTCOME_USED = 'used'      # 随某次录音上传了
OUTCOME_DROPPED = 'drop'   # 没用上（角色不支持视觉、被新图顶掉等）


def _archive_dir(base_dir) -> Path:
    return Path(base_dir).joinpath(*ARCHIVE_SUBDIR)


def _decode_data_url(data_url: str) -> Optional[bytes]:
    """从 data URL 取回原始字节；格式不对返回 None"""
    marker = ';base64,'
    index = data_url.find(marker)
    if index < 0:
        return None
    try:
        return base64.b64decode(data_url[index + len(marker):])
    except Exception:
        return None


def save_mounted(base_dir, image, keep: int) -> str:
    """存下一张刚挂载成功的图片

    Args:
        base_dir: 客户端根目录
        image: PendingImage
        keep: 最多保留多少张，<= 0 表示关闭归档

    Returns:
        写入的文件路径（字符串）；关闭或失败时返回空串
    """
    if keep <= 0:
        return ''

    payload = _decode_data_url(image.data_url)
    if payload is None:
        logger.debug('[视觉] 归档跳过：data URL 不是预期格式')
        return ''

    ext = _EXT_BY_ENCODING.get(image.encoding, 'png')
    # 时间戳用两位年份且日期与时间连写，固定 12 位；尺寸不进文件名（日志里有，
    # 而且它会把去向那几个字挤得对不齐）
    source = _SOURCE_CODES.get(image.source, SOURCE_CODE_UNKNOWN)
    name = (f"{time.strftime('%y%m%d%H%M%S')}_"
            f"{source}_{OUTCOME_PENDING}.{ext}")

    try:
        directory = _archive_dir(base_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        # 同一秒内连按两次快捷键会撞名，加序号而不是覆盖
        counter = 1
        while path.exists():
            path = directory / name.replace(
                f'_{OUTCOME_PENDING}.', f'_{OUTCOME_PENDING}-{counter}.')
            counter += 1
        path.write_bytes(payload)
    except Exception as e:
        logger.warning(f'[视觉] 归档图片失败: {e}', exc_info=True)
        return ''

    _prune(directory, keep)
    return str(path)


def mark_outcome(path: str, task_id: str, used: bool) -> str:
    """结果到达后按去向重命名，补上 task_id

    Args:
        path: save_mounted 返回的路径
        task_id: 本次录音的任务 ID；空串表示这张图从未绑定到任何一次录音
        used: 图片是否真的随本句上传了

    Returns:
        重命名后的路径；未归档或重命名失败时返回原路径
    """
    if not path:
        return path

    outcome = OUTCOME_USED if used else OUTCOME_DROPPED
    try:
        old = Path(path)
        if not old.exists():
            return path
        # 只替换结尾那个 _pend（含同秒撞名时的 _pend-N），不碰文件名其余部分
        stem = re.sub(rf'_{OUTCOME_PENDING}(-\d+)?$', '', old.stem)
        # task_id 可以为空：被新图顶掉的那张从没绑定过任何一次录音
        suffix = f'_{task_id}' if task_id else ''
        new = old.with_name(f'{stem}_{outcome}{suffix}{old.suffix}')
        old.rename(new)
        return str(new)
    except Exception as e:
        logger.debug(f'[视觉] 归档重命名失败: {e}')
        return path


def _prune(directory: Path, keep: int) -> None:
    """只保留最近 keep 张，超出的从旧到新删掉

    按 mtime 排序而不是文件名：重命名会让 `_pend` 变成 `_used`，但不会改 mtime，
    而文件名里的时间戳位置固定、两种排序在正常情况下一致，mtime 对异常更宽容。
    """
    try:
        files = [p for p in directory.iterdir()
                 if p.is_file() and _ARCHIVE_NAME_RE.match(p.name)]
        if len(files) <= keep:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for stale in files[:len(files) - keep]:
            try:
                stale.unlink()
            except Exception as e:
                logger.debug(f'[视觉] 清理旧归档失败: {stale.name}: {e}')
    except Exception as e:
        logger.debug(f'[视觉] 清理归档目录失败: {e}')
