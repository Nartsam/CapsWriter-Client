"""
LLM 角色检测器

功能：
1. 检测文本是否匹配某个角色前缀
2. 返回对应的角色配置和去除前缀后的文本
"""
from typing import Tuple, Optional
from .llm_role_config import RoleConfig
from .llm_interfaces import IRoleLoader
from . import logger

# 角色名与正文之间的分隔符，去前缀时一并去掉
_SEPARATORS = '：，。,. '


class RoleDetector:
    """角色检测器 - 负责从输入文本中检测角色"""

    def __init__(self, role_loader: IRoleLoader):
        """
        Args:
            role_loader: 角色加载器实例
        """
        self.role_loader = role_loader

    def detect(self, text: str) -> Tuple[Optional[RoleConfig], str]:
        """
        检测文本是否匹配某个角色前缀

        Args:
            text: 输入文本

        Returns:
            (role_config, content) - role_config 是 RoleConfig 对象（具名角色优先，
            都不匹配时是启用中的默认角色，否则 None），content 是去除前缀后的文本
        """
        role, content, _ = self.detect_with_alias(text)
        return role, content

    def detect_with_alias(self, text: str) -> Tuple[Optional[RoleConfig], str, str]:
        """同 detect，另返回命中的触发名（显示名或别名；默认角色与未命中时为空串）"""

        for role_name, role_config in self.role_loader.get_roles().items():
            if role_name in ['默认', 'default', '']:
                continue

            # 检查角色是否启用
            if not role_config.enabled:
                continue

            # 遍历显示名和所有别名
            for name in role_config.names:
                if not name:
                    continue
                if text.startswith(name):
                    return role_config, text[len(name):].lstrip(_SEPARATORS), name

        # 未匹配，使用默认角色
        default_role = self.role_loader.get_default_role()
        if default_role.enabled:
            # logger.debug(f"未匹配到角色前缀，使用默认角色")
            return default_role, text, ''

        # logger.debug("未匹配到角色且默认角色未启用，返回原始文本")
        return None, text, ''

    @staticmethod
    def strip_locked_prefix(text: str, role: RoleConfig, matched_alias: str) -> str:
        """
        从整段重识别的全文里去掉已锁定角色的触发名

        先试流式预览时命中的那个，再试该角色的其他名字；都对不上（重识别改了触发词的写法）
        就保留全文交给该角色，不按预览时的长度截断，以免切掉正文。
        """
        for name in [matched_alias] + list(role.names):
            if name and text.startswith(name):
                return text[len(name):].lstrip(_SEPARATORS)
        return text

    def default_role_enabled(self) -> bool:
        """默认角色是否启用（启用时每句话都会交给它）"""
        return self.role_loader.get_default_role().enabled

    def is_name_prefix(self, text: str) -> bool:
        """
        text 是否只是某个启用中的具名角色名的开头（如「新岛」之于「新岛真」）

        流式上屏按停顿切句，第一句可能恰好停在角色名中间。此时还不能断定有没有喊角色，
        要等下一句接上再检测。
        """
        text = text.strip(_SEPARATORS)
        if not text:
            return False
        for role_name, role_config in self.role_loader.get_roles().items():
            if role_name in ['默认', 'default', ''] or not role_config.enabled:
                continue
            if any(name and len(name) > len(text) and name.startswith(text) for name in role_config.names):
                return True
        return False
