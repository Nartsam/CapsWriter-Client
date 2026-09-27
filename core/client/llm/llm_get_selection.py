"""获取当前选区，并区分「读取成功」与「本轮需要附加到模型」。"""
import time
from dataclasses import dataclass

import keyboard
from config_client import ClientConfig as Config
from core.tools.windows_selection import read_selection, get_input_target
from . import logger
from .llm_clipboard import restore_clipboard, safe_paste, try_paste


# 按角色保留原有的相邻轮次去重规则；清除对话历史时也要清掉此记录。
_last_selection_by_role = {}


@dataclass
class SelectionContext:
    text: str = ''              # 本轮实际附加的文字
    preview: str = ''           # 成功读取的文字，包含被去重的选区
    note: str = ''


def format_selection_preview(selection: SelectionContext) -> str:
    """只用于终端说明；清理换行、控制字符，原文仍按角色长度限制发送。"""
    preview = ' '.join(''.join(
        c if c.isprintable() or c.isspace() else ' ' for c in selection.preview
    ).split())
    if not preview:
        return ''
    if len(preview) > 50:
        preview = preview[:50] + '...'
    suffix = f'（{selection.note}）' if selection.note else ''
    return f'    选中文本：{preview}{suffix}'


def _read_by_copy(target) -> str:
    """供回退与直接复制模式共用，保留旧的剪贴板保护与还原行为。"""
    # 保存目标后前台可能已经改变。不能把原窗口的复制操作发给另一个窗口。
    if not target or not target[0] or get_input_target() != target:
        return ''
    original_clipboard = try_paste()
    if get_input_target() != target:
        return ''
    keyboard.press_and_release('ctrl+c')
    try:
        time.sleep(0.1)
        selected_text = safe_paste()
    finally:
        # 非文本内容读不到时不拿空串还原，避免清掉图片。还原失败也不能丢掉
        # 已经读到的选区；restore_clipboard 自己处理占用重试并只记 INFO。
        if original_clipboard is not None:
            restore_clipboard(original_clipboard)
    # 旧方法只能通过剪贴板变化判断是否复制成功；相同文本的局限保留在此路径。
    if selected_text == (original_clipboard or ''):
        return ''
    return selected_text


def capture_selection(role_config, state) -> SelectionContext:
    """按全局模式获取选区，再统一处理长度限制、历史去重与预览。"""
    if not role_config.enable_read_selection:
        return SelectionContext()
    try:
        max_length = max(0, int(getattr(role_config, 'selection_max_length', 1000)))
        if not max_length:
            return SelectionContext()
        mode = getattr(Config, 'selection_mode', 'uia')
        if mode == 'ctrl_c':
            logger.info('选区读取：直接使用 Ctrl+C')
            selected_text = _read_by_copy(get_input_target())
        else:
            result = read_selection(max_length)
            selected_text = result.text
            logger.info(f'选区读取结果：{result.status}，{len(selected_text)} 字'
                        f'（{result.detail or "UIA"}）')
            # 部分控件未暴露真实选区时也会返回 empty，不能据此禁止用户选择的回退。
            # 密码与焦点变化是独立的跳过条件，不参与复制尝试。
            if (mode == 'uia_then_ctrl_c' and not selected_text.strip()
                    and result.status not in ('blocked', 'changed')):
                logger.info('选区读取：只读接口未取到文字，尝试 Ctrl+C')
                selected_text = _read_by_copy(result.target)
        if not selected_text.strip():
            return SelectionContext()

        selection = SelectionContext(preview=selected_text[:max_length])
        if selected_text == state.last_output_text:
            selection.note = '与上次输出相同，本轮未附加'
        elif (role_config.enable_history
              and selection.preview == _last_selection_by_role.get(role_config.name, '')):
            selection.note = '已在历史，本轮未重复附加'
        else:
            selection.text = selection.preview
        return selection
    except Exception as e:
        # 选区是可选上下文，读取失败不影响语音提问，也不在终端刷诊断。
        logger.info(f'获取选中文字失败: {e}')
        return SelectionContext()


def get_selected_text(role_config, state) -> str:
    """兼容直接调用的入口，仅返回本轮需要附加的选区。"""
    return capture_selection(role_config, state).text


def record_selection_usage(role_config, selection_text: str):
    """记录本轮使用的选区，维持相邻轮次的历史去重行为。"""
    _last_selection_by_role[role_config.name] = selection_text


def clear_selection_usage():
    """历史清空后，同一段文字必须能够重新附加。"""
    _last_selection_by_role.clear()

