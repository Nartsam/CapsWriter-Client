"""
LLM 获取选中文字功能

功能：
1. 获取当前剪贴板内容（用于还原）
2. 模拟 Ctrl+C 复制选中的文字
3. 读取新的剪贴板内容
4. 还原原来的剪贴板内容
5. 判断内容是否变化，返回选中的文字
"""
import time
import keyboard
from . import logger
from .llm_clipboard import restore_clipboard, safe_paste, try_paste


# 全局变量：记录每个角色最后一次使用的选中文字
_last_selection_by_role = {}


def get_selected_text(role_config, state) -> str:
    """
    获取用户当前选中的文字（通过模拟 Ctrl+C）

    Args:
        role_config: 角色配置 RoleConfig 对象

    Returns:
        选中的文字，如果不应该使用则返回空字符串
    """
    global _last_selection_by_role

    # 检查是否启用获取选中文字
    if not role_config.enable_read_selection:
        return ""

    role_name = role_config.name

    try:
        # 保存当前剪贴板内容。必须用 try_paste：safe_paste 把「读不到」也折叠成
        # 空字符串，而下面要把它写回剪贴板——剪贴板里是图片时（视觉输入功能一开，
        # 这就成了常态）读取必然失败，拿那个空串写回去会先 EmptyClipboard，
        # 把用户待用的截图直接清掉
        original_clipboard = try_paste()

        # 模拟 Ctrl+C 复制选中的文字
        keyboard.press_and_release('ctrl+c')

        # 等待复制操作完成
        time.sleep(0.1)

        # 读取新的剪贴板内容
        selected_text = safe_paste()

        # 还原原来的剪贴板内容。原内容读不到（非文本）时一律不动剪贴板：
        # 此时若用户没有选中任何文字，Ctrl+C 不会改变剪贴板，图片就能原样保住。
        # （若确实选中了文字，Ctrl+C 本身已经覆盖掉图片，那是模拟 Ctrl+C 取选区的
        # 固有代价，除非连图片格式一并备份还原，不值得）
        #
        # 必须走 restore_clipboard 而不是裸 pyclip.copy：后者在剪贴板被别的进程占着时
        # 抛 ERROR_ACCESS_DENIED，异常一路冒到外层 except，把**已经读到的选中文字**
        # 一并丢掉，还往终端刷一条 WARNING。还原是收尾动作，失败不该牵连主结果
        if original_clipboard is not None:
            restore_clipboard(original_clipboard)

        # 如果内容没有变化，说明没有选中文字，返回空字符串。
        # 原内容读不到时按空串比较：那种情况下没选中文字读到的同样是空串
        if selected_text == (original_clipboard or "") or selected_text == state.last_output_text:
            return ""

        # 检查长度限制
        max_length = getattr(role_config, 'selection_max_length', 1000)
        if len(selected_text) > max_length:
            selected_text = selected_text[:max_length]

        # 如果开启了历史记录，检查选中文字是否与上一次使用的相同
        if role_config.enable_history:
            last_selection = _last_selection_by_role.get(role_name, "")
            if selected_text == last_selection:
                # 选中文字没有变化，且上一次已经使用过，不再加入上下文
                return ""

        # 检查是否只包含空白字符（空格、制表符、换行等）
        if not selected_text.strip():
            return ""

        return selected_text

    except Exception as e:
        logger.warning(f"获取选中文字失败: {e}")
        return ""


def record_selection_usage(role_config, selection_text: str):
    """
    记录角色使用的选中文字（用于下一轮判断是否重复）

    Args:
        role_config: 角色配置 RoleConfig 对象
        selection_text: 使用的选中文字（空字符串表示没有使用）
    """
    global _last_selection_by_role
    role_name = role_config.name
    _last_selection_by_role[role_name] = selection_text

