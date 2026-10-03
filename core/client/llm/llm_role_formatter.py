"""
LLM 角色信息格式化器

功能：
1. 格式化角色状态显示
2. 提供统一的 Rich 渲染支持
"""
import unicodedata
from rich.text import Text
from rich.console import Console
from .llm_role_config import RoleConfig


class RoleFormatter:
    """角色信息格式化器 - 负责角色显示的格式化"""

    @staticmethod
    def _get_display_width(text: str) -> int:
        """计算字符串的显示宽度（考虑中文字符占2个单位）"""
        width = 0
        for char in text:
            if unicodedata.east_asian_width(char) in ('W', 'F', 'A'):
                width += 2
            else:
                width += 1
        return width

    @staticmethod
    def format_status(role_name: str, role_config: RoleConfig, memory_note=None) -> Text:
        """
        格式化角色状态显示

        Args:
            role_name: 角色名称
            role_config: 角色配置（RoleConfig 对象）
            memory_note: 本次加载恢复记忆的结果 (文字, 样式)；None 时这一栏不占任何位置

        Returns:
            Text 对象（用于 Rich 渲染）
        """
        text = Text()

        # 角色名称：统一对齐到至少 8 个半角字符宽度
        display_width = RoleFormatter._get_display_width(role_name)
        padding = " " * max(0, 8 - display_width)
        text.append(f"{role_name}{padding}：", style="bold cyan")

        # 启用状态
        enabled = role_config.enabled
        text.append("启用 " if enabled else "启用 ", style="green" if enabled else "dim")

        # 输出方式
        output_mode = role_config.output_mode
        if output_mode == 'typing':
            text.append("打字 ", style="green")
        elif output_mode == 'toast':
            text.append("弹窗 ", style="blue")
        else:
            text.append("打字 ", style="dim")

        # 思考
        thinking = role_config.enable_thinking
        text.append("思考 " if thinking else "思考 ", style="green" if thinking else "dim")

        # 记忆
        history = role_config.enable_history
        text.append("记忆 " if history else "记忆 ", style="green" if history else "dim")

        # 热词
        hotwords = role_config.enable_hotwords
        text.append("热词 " if hotwords else "热词 ", style="green" if hotwords else "dim")

        # 读取选中文字
        read_selection = role_config.enable_read_selection
        text.append("读选区 " if read_selection else "读选区 ", style="green" if read_selection else "dim")

        # 记忆恢复结果：紧跟「读选区 」，没有时一个字符也不加，行与以前完全相同
        if memory_note:
            note_text, note_style = memory_note
            text.append(note_text, style=note_style)

        # 模型信息
        text.append(f"  ({role_config.model} from {role_config.provider})", style="dim")

        return text

    @staticmethod
    def print_status(role_name: str, role_config: RoleConfig, prefix: str = "  ", memory_note=None):
        """
        打印角色状态（带前缀）

        Args:
            role_name: 角色名称
            role_config: 角色配置
            prefix: 前缀文本（默认两个空格）
            memory_note: 见 format_status
        """
        console = Console()
        status_line = RoleFormatter.format_status(role_name, role_config, memory_note)

        text = Text(prefix)
        text.append(status_line)
        console.print(text)

    @staticmethod
    def print_update(role_name: str, role_config: RoleConfig):
        """
        打印角色更新信息

        Args:
            role_name: 角色名称
            role_config: 角色配置
        """
        console = Console()
        status_line = RoleFormatter.format_status(role_name, role_config)

        # 构建 "角色更新  " 前缀 + 状态行
        prefix = Text("\n角色更新  ")
        prefix.append(status_line)
        console.print(prefix + "\n")
