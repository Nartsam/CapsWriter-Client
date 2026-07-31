"""
LLM Toast 输出模式

流式显示在浮动窗口中，完成后停留 3 秒
"""
import asyncio
import logging

from core.client.output.text_output import TextOutput
from core.tools.asyncio_to_thread import to_thread

logger = logging.getLogger(__name__)

LLM_TOAST_FONT_FAMILY = "Microsoft YaHei UI"
LLM_TOAST_FONT_SIZE = 16  # 兜底常量
LLM_TOAST_BG = "#f7fbff"
LLM_TOAST_FG = "black"
LLM_TOAST_ALPHA = 0.92
LLM_TOAST_CORNER_RADIUS = 12


async def handle_toast_mode(handler, text: str, role_config=None, matched_hotwords=None, content=None,
                            used_tools=None) -> tuple:
    """Toast 浮动窗口模式

    used_tools: 出参列表，本次请求触发的服务端工具 ID 会追加进来
    """
    from core.ui.toast import ToastMessageManager, ToastMessage
    # 兼容性检测
    if not role_config or content is None:
        role_config, content_detected = handler.detect_role(text)
        content = content_detected if content is None else content

    if not role_config:
        return ("", 0, 0.0)

    # OpenMate 接入：探测到 OpenMate 以 external 模式运行时，改由其展示，不弹本地 Toast
    from .llm_output_openmate import try_openmate_mode
    openmate_result = await try_openmate_mode(handler, role_config, content, matched_hotwords, used_tools)
    if openmate_result is not None:
        return openmate_result

    handler.monitor.reset()  # 重置停止标志
    task_stop_event = handler.monitor.create_stop_callback()
    toast_manager = ToastMessageManager()
    msg_id = None

    try:
        # 创建初始 toast
        msg = ToastMessage(
            text="",
            # 优先使用角色配置的外观，空值回退到统一的白色亚克力默认值。
            # 此前这里硬编码常量是为了掩盖"字体永远显示为宋体"的问题，
            # 真正原因是命名字体跨 Tk 解释器失效，已在 toast_text/toast_base 中修复。
            font_family=role_config.toast_font_family or LLM_TOAST_FONT_FAMILY,
            font_size=role_config.toast_font_size or LLM_TOAST_FONT_SIZE,
            bg=role_config.toast_bg_color or LLM_TOAST_BG,
            fg=role_config.toast_font_color or LLM_TOAST_FG,
            duration=role_config.toast_duration,
            min_width_px=role_config.toast_min_width_px,
            max_width_px=role_config.toast_max_width_px,
            min_width_ratio=role_config.toast_min_width_ratio,
            max_width_ratio=role_config.toast_max_width_ratio,
            initial_height=role_config.toast_initial_height,
            streaming=True,
            window_type='text',
            markdown=True,
            editable=role_config.toast_editable,
            window_alpha=role_config.toast_window_alpha,
            corner_radius=role_config.toast_corner_radius,
            stop_callback=lambda: task_stop_event.set()
        )

        msg_id = toast_manager.add_message(msg)
        toast_window = await toast_manager.wait_for_window(msg_id, timeout=1.0)

        if not toast_window:
            logger.error("Toast 窗口创建失败")
            if msg_id: toast_manager.close_toast(msg_id)
            return ("", 0, 0.0)

        chunks = []
        def stream_toast_chunk(chunk: str):
            chunks.append(chunk)
            toast_manager.update_toast(msg_id, ''.join(chunks))

        def show_toast_status(status: str):
            # 状态只在正文到达前占位；一旦有分片，后续 update 会用全文覆盖掉它
            if not chunks:
                toast_manager.update_toast(msg_id, status)

        # 流式调用 LLM
        polished_text, token_count, gen_time = await to_thread(
            handler.process, role_config, content, matched_hotwords, stream_toast_chunk,
            show_toast_status, used_tools
        )

        if handler.monitor.should_stop():
            toast_manager.close_toast(msg_id)
            return (''.join(chunks) or content, token_count, gen_time)
        else:
            toast_manager.finish_toast(msg_id)
            return (polished_text or content, token_count, gen_time)

    except Exception as e:
        if msg_id: toast_manager.close_toast(msg_id)

        from .llm_error_handler import handle_llm_error, should_fallback_to_original
        from .llm_role_config import RoleConfig
        role_name = role_config.name or RoleConfig.DEFAULT_ROLE_NAME

        if should_fallback_to_original(e):
            result_text, _ = handle_llm_error(e, content, role_name)
            result_text = TextOutput.strip_punc(result_text)

            from .llm_output_typing import output_text
            from config_client import ClientConfig as Config
            await output_text(result_text, Config.paste)
            return (result_text, 0, 0.0)
        else:
            from .llm_error_handler import show_error_notification
            show_error_notification(e, role_name)
            return ("", 0, 0.0)
