"""
LLM Toast 输出模式

流式显示在浮动窗口中；生成完成后按智能时长自动关闭（时长随字数、悬停不顺延、
拖拽后常驻，实现见 core/ui/toast_base.py 的智能时长一节）
"""
import asyncio

from core.client.output.text_output import TextOutput
from core.tools.asyncio_to_thread import to_thread
from .llm_state_tags import StateTagFilter, strip_state_tags
# 本模块此前用 logging.getLogger(__name__)，那是 'core.client.llm.llm_output_toast'
# 层级，与项目日志器 'client' 不在同一棵树上，写出去的记录根本进不了 logs/ 下的
# 文件——「Toast 窗口创建失败」这类关键错误因此一直是隐形的。改为复用包内日志器
from . import logger

LLM_TOAST_FONT_FAMILY = "Microsoft YaHei UI"
LLM_TOAST_FONT_SIZE = 16  # 兜底常量
LLM_TOAST_BG = "#f7fbff"
LLM_TOAST_FG = "black"
LLM_TOAST_ALPHA = 0.92
LLM_TOAST_CORNER_RADIUS = 12


async def handle_toast_mode(handler, text: str, role_config=None, matched_hotwords=None, content=None,
                            used_tools=None, image_data=None, selection=None) -> tuple:
    """Toast 浮动窗口模式

    used_tools: 出参列表，本次请求触发的服务端工具 ID 会追加进来
    image_data: 随本句上传的图片（base64 data URL），需一路转发到 handler.process
    selection: 本次请求已读取的选区快照，需一路转发到 handler.process
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
    openmate_result = await try_openmate_mode(
        handler, role_config, content, matched_hotwords, used_tools, image_data, selection)
    if openmate_result is not None:
        # OpenMate 接管展示：同样顶掉旧的本地 LLM 白色对话框。若不同时顶，之前被拖拽过
        # （永不自动关闭）的本地窗口会在用户一直走 OpenMate 路径时永久残留——本地既不弹
        # 新窗口、又无提前关闭路径，两条规则叠加成漏洞
        ToastMessageManager().close_llm_windows()
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
            # 智能时长：时长随字数、悬停不顺延（到期后移开即关）、拖拽后永不自动关闭
            smart_duration=True,
            stop_callback=lambda: task_stop_event.set()
        )

        # 新 LLM 回复窗口生成：顶掉所有旧的 LLM 白色对话框（无论是否拖拽），
        # 确保新窗口一弹出（即使还是空文本）旧窗口就已消失
        toast_manager.close_llm_windows()
        msg_id = toast_manager.add_message(msg)
        toast_window = await toast_manager.wait_for_window(msg_id, timeout=1.0)

        if not toast_window:
            logger.error("Toast 窗口创建失败")
            if msg_id: toast_manager.close_toast(msg_id)
            return ("", 0, 0.0)

        # chunks 里存的是**已剥标签**的可见文本：它既喂 Toast，也是 ESC 中断时的返回值
        chunks = []
        tag_filter = StateTagFilter()

        def stream_toast_chunk(chunk: str):
            visible = tag_filter.feed(chunk)
            if not visible:
                return
            chunks.append(visible)
            toast_manager.update_toast(msg_id, ''.join(chunks))

        def show_toast_status(status: str):
            # 状态只在正文到达前占位；一旦有分片，后续 update 会用全文覆盖掉它
            if not chunks:
                toast_manager.update_toast(msg_id, status)

        # 流式调用 LLM
        polished_text, token_count, gen_time = await to_thread(
            handler.process, role_config, content, matched_hotwords, stream_toast_chunk,
            show_toast_status, used_tools, image_data, selection
        )

        tail = tag_filter.finish()
        if tail:
            chunks.append(tail)
            toast_manager.update_toast(msg_id, ''.join(chunks))
        polished_text = strip_state_tags(polished_text)

        # 空回复不降级成识别原文：Toast 模式下这段文字压根没有被展示给用户，
        # 拿它冒充模型输出会让控制台、md 记录、UDP 广播三处都误报成一次成功回复，
        # 把静默失败伪装得连事后都查不出来（typing 模式的降级是真打字出去了，不同）
        if handler.monitor.should_stop():
            # 中断分支同样要留痕：llm_processor 的空回复告警被中断抑制了
            logger.info(f"[{role_config.display_name or '默认'}] 输出被用户中断，"
                        f"弹窗已关闭（已展示 {len(''.join(chunks))} 字）")
            toast_manager.close_toast(msg_id)
            return (''.join(chunks), token_count, gen_time)
        else:
            toast_manager.finish_toast(msg_id)
            if not polished_text:
                logger.warning(f"角色 [{role_config.display_name or '默认'}] 返回空回复，"
                               f"弹窗无内容（原始输入：{content[:30]}…）")
            return (polished_text, token_count, gen_time)

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
