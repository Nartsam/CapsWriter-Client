# coding: utf-8
"""
识别结果处理模块

提供 ResultProcessor 类用于处理服务端返回的识别结果。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Optional

from config_client import ClientConfig as Config
from core.client.state import console
from core.protocol import RecognitionMessage

from core.client.output.text_output import TextOutput
from core.tools.window_detector import get_active_window_info
import keyboard
from . import logger

from core.client.udp.udp_broadcaster import broadcast_output_udp
from core.tools.zhconv import convert as zhconv_convert
from core.client.audio.file_manager import AudioFileManager
from core.client.llm.llm_write_md import write_llm_md
from core.client.llm.llm_constants import ToolNames, IMAGE_PLACEHOLDER
from core.client.llm.llm_role_config import RoleConfig
# 悬浮录音提示：用于显示等待、进度、错误和处理完成后的隐藏。
from core.client.ui import (
    hide_recording_indicator,
    show_processing_indicator,
    show_recording_indicator_error,
    update_recording_indicator_progress,
)

if TYPE_CHECKING:
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient
    from core.client.hotword.manager import HotwordManager
    from core.client.diary.diary_writer import DiaryWriter



def _estimate_tokens(text: str) -> int:
    """估算文本的 token 数"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


async def _auto_enter(delay: float) -> None:
    """延迟发送回车键"""
    await asyncio.sleep(delay)
    keyboard.press_and_release('enter')
    logger.debug(f"自动回车已发送 (延迟 {delay}s)")


class ResultProcessor:
    """
    识别结果处理器
    
    负责处理服务端返回的识别结果：
    - 接收 WebSocket 消息
    - 执行热词替换
    - 可选地调用 LLM 进行润色
    - 输出最终文本
    - 保存录音和日记
    """
    
    def __init__(self, app: CapsWriterClient):
        """
        初始化结果处理器

        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self._exit_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()  # 保存事件循环引用

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    @property
    def ws(self) -> WebSocketManager:
        """快捷访问连接管理器"""
        return self.app.ws

    @property
    def hotword(self) -> HotwordManager:
        """快捷访问热词管理器"""
        return self.app.hotword

    @property
    def output(self) -> TextOutput:
        """快捷访问文本输出器"""
        return self.app.output

    @property
    def diary(self) -> DiaryWriter:
        """快捷访问日记写入器"""
        return self.app.diary

    def request_exit(self):
        """请求退出处理循环（线程安全）"""
        logger.info("收到退出请求，设置退出事件")

        # 线程安全地设置事件
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._exit_event.set)
            logger.debug("已通过 call_soon_threadsafe 设置退出事件")
        else:
            self._exit_event.set()
            logger.debug("已直接设置退出事件")
    
    def _format_llm_result(self, llm_result) -> str:
        """格式化 LLM 结果输出"""
        polished_text = llm_result.result
        role_name = llm_result.role_name
        processed = llm_result.processed
        token_count = llm_result.token_count
        generation_time = llm_result.generation_time  # 使用生成时间（从第一个 token 开始）

        # 空回复必须如实显示：此前空结果会被上游降级成识别原文，这行就会把用户
        # 自己的话原样打出来，看上去与一次正常回复毫无区别
        is_empty = not polished_text.strip()

        polished_text = polished_text.replace('\n', ' ').replace('\r', ' ')
        max_display_length = 50
        if len(polished_text) > max_display_length:
            polished_text = polished_text[:max_display_length] + '...'

        # 角色名后缀出本次触发的服务端工具，如「模型结果[助理(搜索)]」。
        # 工具执行是这条路径上唯一会让耗时突然变长的原因，不标出来就无从分辨
        # 「这次慢」是搜索导致的还是网络抖动
        tool_names = '、'.join(ToolNames.display(t) for t in (llm_result.used_tools or ()))
        if role_name and tool_names:
            role_label = f'[{role_name}({tool_names})]'
        elif role_name:
            role_label = f'[{role_name}]'
        else:
            role_label = ''
        if is_empty:
            # 措辞不写死成"模型没输出"：ESC 中断且尚无分片时同样走这里，
            # 具体原因由紧邻的 WARNING 日志给出（控制台会打印 WARNING 及以上）
            result_text = '[yellow]（空回复，详见日志）[/yellow]'
        else:
            result_text = f'[green]{polished_text}[/green]' if processed else polished_text

        if token_count == 0 and polished_text:
            token_count = _estimate_tokens(polished_text)

        # 使用生成时间计算速度（更准确）
        if processed and generation_time > 0:
            speed = token_count / generation_time if token_count > 0 else 0
            speed_label = f'    {speed:.1f} tokens/s' if speed > 0 else ''
        else:
            speed_label = ''

        return f'    模型结果{role_label}：{result_text}{speed_label}'
    
    def _resolve_forced_role(self, snapshot) -> Optional[RoleConfig]:
        """
        解析连续对话要强制路由到的角色

        角色文件可以被热重载、改名或禁用，所以粘滞的只是名字，每次都重新解析；
        解析不到就自动退出连续对话，避免后续每句话都撞在同一个失效角色上。
        """
        if snapshot is None or not snapshot.forced_role or not Config.llm_enabled:
            return None

        role_config = self.app.llm.resolve_role(snapshot.forced_role)
        if role_config is None:
            # 期间若已进入别的连续对话就不要误伤
            if self.state.continuous_role == snapshot.forced_role:
                self.state.exit_continuous()
            logger.warning(f"连续对话角色「{snapshot.forced_role}」已不存在或被禁用，自动退出连续对话")
            console.print(f'    连续对话：[yellow]角色「{snapshot.forced_role}」已失效，已退出[/yellow]')
        return role_config

    def _report_image_usage(self, pending_image, llm_result) -> None:
        """报告本次录音绑定图片的去向

        「下一条」是严格的：这次录音一旦有效落地，图必然消失——用掉或丢掉，没有
        第三种结果，也不会留到下一句（见约定 21）。丢弃只打控制台一行、不弹窗：
        它发生在事后、已无从补救，而弹窗会打断正在上屏的输出。
        """
        if pending_image is None:
            return
        if llm_result and llm_result.image_used:
            logger.info(f"图片已随本句上传: {pending_image.width}×{pending_image.height}")
            return

        if not Config.llm_enabled or not llm_result or not llm_result.processed:
            reason = '本句未触发 LLM 角色'
        else:
            reason = f'角色「{llm_result.role_name}」未开启视觉'
        console.print(f'    [yellow]已挂载的图片未使用，已丢弃[/yellow]（{reason}）')
        logger.info(f"图片未使用已丢弃：{reason}")

    def _update_continuous_state(self, snapshot, llm_result) -> None:
        """
        按本次录音的路由快照更新连续对话状态

        进入只能在这里判定：说这句话的时候还不知道会命中哪个角色。退出早在松手时
        就落地了（见 ShortcutTask.finish），这里只负责如实告知。
        """
        if snapshot is None:
            return

        if snapshot.is_arming:
            role_name = llm_result.role_name if llm_result else None
            is_named = bool(role_name) and role_name != RoleConfig.DEFAULT_ROLE_NAME
            if llm_result and llm_result.processed and is_named:
                self.state.enter_continuous(role_name)
                console.print(f'    连续对话：[green]已进入「{role_name}」[/green]')
            else:
                logger.info("本次录音按过连续对话切换键，但未命中具名角色，切换作废")
                console.print('    连续对话：[yellow]未触发角色，本次切换作废[/yellow]')
            return

        if snapshot.is_exiting:
            console.print(f'    连续对话：[yellow]已退出「{snapshot.sticky_role}」[/yellow]')
            return

        # 连续对话中的普通一句：刷新闲置计时。起点取回复处理完毕而不是录音开始，
        # 否则一个生成半分钟的长回复会白白吃掉超时预算
        if snapshot.sticky_role:
            self.state.touch_continuous()

    def _log_modifier_key_state(self) -> None:
        """
        检测并记录当前按下的所有键
        
        用于调试按键卡住问题。
        """
        try:
            import keyboard
            
            # 获取所有当前按下的键
            pressed_keys = keyboard._pressed_events
            
            # if pressed_keys:
            key_names = list(pressed_keys.keys())
            logger.debug(f"当前按下的键: {key_names}")
                
        except Exception as e:
            logger.debug(f"检测按键状态失败: {e}")
    
    async def start(self) -> None:
        """开启工作循环（含自动重联）"""
        while not self._exit_event.is_set():
            # 1. 尝试连接，失败则重试
            if not await self.ws.connect():
                await asyncio.sleep(2)
                continue

            # 2. 消息接收循环
            while not self._exit_event.is_set():
                try:
                    message = await self.ws.receive()
                    if message is None:
                        # 连接断开时给用户一个可见提示，避免等待状态一直挂着。
                        show_recording_indicator_error("服务端连接已断开")
                        break
                    await self._handle_message(message)
                except Exception as e:
                    # WebSocket 异常同样通过悬浮提示反馈给用户。
                    show_recording_indicator_error("服务端连接异常")
                    logger.debug(f"连接异常中断: {e}")
                    break

            console.print(f'[bold red]已断开服务端连接[/bold red]\n')
            self._cleanup()
            

    async def _handle_message(self, message: Optional[RecognitionMessage]) -> None:
        """处理接收到的消息"""
        if message is None:
            return

        # 已被手动打断的任务：整条流水线都不执行（热词、角色识别、LLM、上屏、
        # 日记、录音重命名一个都不做）。非最终结果一并丢弃，否则悬浮提示会在
        # 「已打断」之后又被分段进度顶回去
        if self.state.is_task_cancelled(message.task_id):
            if message.is_final:
                self.state.release_cancelled_task(message.task_id)
                # 打断路径已经做过回滚，这里只是把可能残留的快照清掉（pop 幂等）
                self.state.pop_continuous_snapshot(message.task_id)
                self.state.pop_image_snapshot(message.task_id)
                logger.info(f"丢弃已打断任务的识别结果，任务ID: {message.task_id}")
            return

        # 使用 text 字段（简单拼接结果，用于语音输入）
        text = message.text
        original_text = text  # 保存原始识别结果
        delay = message.time_complete - message.time_submit

        if message.is_final:
            logger.info(f"收到最终识别结果: {text}, 时延: {delay:.2f}s")
        else:
            logger.debug(
                f"接收到识别结果，文本: {text[:50]}{'...' if len(text) > 50 else ''}, "
                f"时延: {delay:.2f}s"
            )

        # 如果非最终结果，继续等待
        if not message.is_final:
            # 非最终结果用于刷新悬浮提示上的处理进度和预览文本。
            update_recording_indicator_progress(message.duration, text)
            return

        # 最终结果已返回，后续还要做热词、LLM 和输出处理
        # 同时关闭打断窗口：从这里开始文本随时可能上屏，已经无法"作废"
        if self.state.pending_task_id == message.task_id:
            self.state.pending_task_id = None
        show_processing_indicator()

        # 繁体转换
        if Config.traditional_convert:
            try:
                text = zhconv_convert(text, Config.traditional_locale)
            except Exception as e:
                logger.warning(f"繁体转换失败: {e}")

        # 1. 音素检索，热词替换
        hotword_start = time.monotonic()
        correction_result = self.hotword.get_phoneme_corrector().correct(text, k=10)
        if Config.hot:
            text = correction_result.text

        # 2. 去掉末尾符号
        text = TextOutput.strip_punc(text)

        # 3. 正则替换
        text = self.hotword.get_rule_corrector().substitute(text)
        hotword_elapsed = time.monotonic() - hotword_start

        # 保存最近一次识别结果
        self.state.last_recognition_text = text

        # 控制台输出：时延 + 热词时延合并到一行
        hotword_label = f'  热词时延: {hotword_elapsed:.2f}s' if Config.hot else ''
        console.print(f'    转录时延：{delay:.2f}s{hotword_label}')

        # 先显示原始识别结果
        original_text_stripped = TextOutput.strip_punc(original_text)
        console.print(f'    识别结果：[green]{original_text_stripped}')

        # 如果发生了热词替换，显示替换后的结果
        if original_text_stripped != text:
            console.print(f'    热词替换：[cyan]{text}')
            logger.debug(f"热词替换后: {text[:50]}{'...' if len(text) > 50 else ''}")

        # 热词匹配情况
        matched_hotwords = correction_result.matchs
        potential_hotwords = correction_result.similars

        # 1. 显示完全匹配/已替换的热词
        if matched_hotwords and Config.hot:
            # 提取热词文本 (现为 (原词, 热词, 分数))
            replaced_info = [f"{origin}->[green4]{hw}[/]" for origin, hw, score in matched_hotwords]
            console.print(f'    完全匹配：{", ".join(replaced_info)}')

        # 2. 潜在热词记录到 log
        if potential_hotwords and Config.hot:
            replaced_set = {hw for origin, hw, score in matched_hotwords}
            potential_matches = [(origin, hw, score) for origin, hw, score in potential_hotwords if hw not in replaced_set]
            if potential_matches:
                log_str = "; ".join([f"{origin}->{hw}({score:.2f})" for origin, hw, score in potential_matches])
                logger.debug(f"潜在热词: {log_str}")

        # 窗口兼容性检测
        paste = Config.paste
        process_name = get_active_window_info().get('process_name', '').lower()
        logger.debug(f"当前活动窗口: {process_name}")
        if any(app.lower() == process_name for app in Config.paste_apps):
            paste = True
            logger.debug(f"检测到兼容性应用: {process_name}，使用粘贴模式")

        # 自动回车检测
        for app, delay in Config.enter_apps:
            if app.lower() == process_name:
                asyncio.create_task(_auto_enter(delay))

        # 连续对话路由：由说这句话时的快照决定，而不是此刻的全局状态。两者之间隔着
        # 一整个服务端往返，期间用户完全可能已经开始说下一句或切换了状态
        snapshot = self.state.pop_continuous_snapshot(message.task_id)
        forced_role = self._resolve_forced_role(snapshot)

        # 本次录音绑定的图片。同样只看快照：按下录音键那一刻挂着什么就是什么，
        # 与此刻的全局待挂槽位无关（期间用户可能已经又截了一张）
        pending_image = self.state.pop_image_snapshot(message.task_id)

        # LLM 处理和输出
        llm_result = None
        if Config.llm_enabled:
            llm_result = await self.app.llm.process_and_output(
                text,
                paste=paste,
                matched_hotwords=potential_hotwords,  # 传递上下文热词给 LLM
                forced_role=forced_role,
                image_data=pending_image.data_url if pending_image else None
            )
        else:
            await self.output.output(text, paste=paste)
            self.state.set_output_text(text)
            broadcast_output_udp(text)

        # 文本已经完成输出，此时关闭悬浮提示。
        hide_recording_indicator()

        # 保存录音与写入 md 文件
        file_audio = None
        if Config.save_audio:
            # 重命名音频文件
            file_path = self.state.pop_audio_file(message.task_id)
            if file_path:
                file_manager = AudioFileManager()
                file_manager.file_path = file_path
                file_audio = file_manager.rename(text, message.time_start)

            # 写入日记
            self.diary.write(text, message.time_start, file_audio)

        # LLM 结果显示和保存
        if Config.llm_enabled and llm_result and llm_result.processed:
            console.print(self._format_llm_result(llm_result))
            # md 是这段对话事后唯一可读的记录，图片本体不落盘，但「当时带了图」
            # 必须留痕，否则半年后回看会觉得模型答非所问
            md_input = llm_result.input_text
            if llm_result.image_used:
                md_input = f'{IMAGE_PLACEHOLDER}{md_input}'
            write_llm_md(
                md_input,
                llm_result.result,
                llm_result.role_name,
                message.time_start,
                file_audio
            )

        # 图片去向如实告知：不论用掉还是丢掉，这次录音之后它都不复存在
        self._report_image_usage(pending_image, llm_result)

        # 连续对话状态迁移（进入判定放在最后：要等 role_name 确定）
        self._update_continuous_state(snapshot, llm_result)

        # 检测修饰键状态（调试用）
        self._log_modifier_key_state()

        console.line()
    
    def _cleanup(self) -> None:
        """清理资源"""
        if self.state.websocket is not None:
            try:
                if self.state.websocket.closed:
                    self.state.websocket = None
                    logger.debug("WebSocket 连接已清理")
            except Exception:
                self.state.websocket = None
