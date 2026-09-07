"""本地状态/角色窗口与 OpenMate 批次投递，内部状态不经外部展示通道。"""
from core.ui.toast_manager import ToastMessage, ToastMessageManager
from core.ui.toast_reminder import REMINDER_FONT_SIZE, REMINDER_BG, REMINDER_FG
from core.client.llm.llm_role_config import RoleConfig
from core.client.llm.llm_output_openmate import OpenMateClient
from config_client import ClientConfig as Config
from core.client import logger


async def show_status(text):
    try:
        manager = ToastMessageManager()
        duration = getattr(Config, 'reminder_status_duration', 6000)
        if type(duration) is not int or not 1000 <= duration <= 60000:
            duration = 6000
        font_size = getattr(Config, 'reminder_status_font_size', REMINDER_FONT_SIZE)
        if type(font_size) is not int or not 8 <= font_size <= 48:
            font_size = REMINDER_FONT_SIZE
        msg_id = manager.add_message(ToastMessage(
            text=text, duration=duration, font_size=font_size, font_family='Microsoft YaHei UI',
            bg=REMINDER_BG, fg=REMINDER_FG, window_alpha=1.0, corner_radius=12,
            reminder_status=True))
        return await manager.wait_for_window(msg_id, timeout=3) is not None
    except Exception:
        # 通知失败不阻断原始听写归档、图片消费以及连续对话状态收尾。
        logger.warning('提醒状态窗口创建失败', exc_info=True)
        return False


async def show_role(text, role=None):
    role = role or RoleConfig()
    message = ToastMessage(
        text=text, font_family=role.toast_font_family, font_size=role.toast_font_size,
        bg=role.toast_bg_color, fg=role.toast_font_color, duration=role.toast_duration,
        min_width_px=role.toast_min_width_px, max_width_px=role.toast_max_width_px,
        min_width_ratio=role.toast_min_width_ratio, max_width_ratio=role.toast_max_width_ratio,
        initial_height=role.toast_initial_height, markdown=True, editable=role.toast_editable,
        window_alpha=role.toast_window_alpha, corner_radius=role.toast_corner_radius,
        smart_duration=True)
    manager = ToastMessageManager()
    manager.close_llm_windows()
    msg_id = manager.add_message(message)
    return await manager.wait_for_window(msg_id, timeout=3) is not None


def openmate_client():
    return OpenMateClient(Config.openmate_url, Config.openmate_token)


def fetch_states(character):
    if not character or not getattr(Config, 'openmate_enabled', False):
        return []
    client = openmate_client()
    if not client.is_available(Config.openmate_health_timeout):
        return []
    return client.get_states(character).get('states', [])
