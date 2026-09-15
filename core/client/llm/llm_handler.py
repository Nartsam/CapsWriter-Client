"""
LLM 处理器 - 协调器

功能：
1. 协调各个组件（角色加载、上下文管理、客户端池、消息构建）
2. 提供统一的处理接口
3. 流式输出
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Any
from pathlib import Path

from .llm_role_loader import RoleLoader
from .llm_context import ContextManager
from .llm_watcher import LLMFileWatcher
from .llm_role_config import RoleConfig
from .llm_client_pool import ClientPool
from .llm_message_builder import MessageBuilder
from .llm_role_detector import RoleDetector
from .llm_processor import LLMProcessor
from .llm_get_selection import capture_selection, record_selection_usage, clear_selection_usage
from core.tools.asyncio_to_thread import to_thread
from . import logger
from .llm_stop_monitor import StopMonitor
from core.client.udp.udp_broadcaster import broadcast_output_udp


@dataclass
class LLMResult:
    """LLM 处理结果"""
    result: str                    # 润色后的文本
    role_name: Optional[str]       # 角色名
    processed: bool                # 是否经过处理
    token_count: int               # token数
    polish_time: float             # 总耗时（秒）
    input_text: str                # 输入文本（已移除角色前缀）
    generation_time: float = 0.0   # 生成时间（秒，从第一个 token 开始）
    used_tools: List[str] = field(default_factory=list)  # 本次请求实际触发的服务端工具 ID
    image_used: bool = False       # 本次绑定的图片是否真的随请求上传了
    reminder_handled: bool = False  # 独立提醒任务，不写角色对话归档、不广播输出


# ======================================================================
# --- LLM 处理器（协调器）---

class LLMHandler:
    """LLM 润色处理器（协调器）"""

    def __init__(self, app):
        logger.info("初始化 LLM 处理器")
        self.app = app

        # 获取热词管理器
        self.hotword_manager = app.hotword

        # 角色管理
        self.role_loader = RoleLoader()
        self.roles = self.role_loader.get_roles()
        logger.info(f"已加载角色: {list(self.roles.keys())}")

        # 上下文管理器池
        self.context_managers: Dict[str, ContextManager] = {}
        self._init_context_managers()

        # 客户端池
        self.client_pool = ClientPool()

        # 消息构建器
        self.message_builder = MessageBuilder(app)

        # 角色检测器
        self.role_detector = RoleDetector(self.role_loader)

        # LLM 处理引擎
        self.processor = LLMProcessor(self.client_pool)

        # 5. 子服务监控
        # 配置文件监控
        self.watcher = LLMFileWatcher(
            on_roles_reload=lambda: self.reload_roles(),
            get_roles=lambda: self.roles,
        )
        # 中断按键监控
        self.monitor = StopMonitor()

    def start(self):
        """启动 LLM 系统的子服务监控"""
        self.watcher.start()
        self.monitor.start()
        logger.debug("LLM 系统子服务已启动")

    def stop(self):
        """停止 LLM 系统的子服务监控"""
        self.watcher.stop()
        self.monitor.stop()
        logger.debug("LLM 系统子服务已停止")

    def _init_context_managers(self):
        """为启用了历史的角色创建上下文管理器"""
        for role_name, role_config in self.roles.items():
            if role_config.enable_history:
                self.context_managers[role_name] = ContextManager(
                    max_length=role_config.max_context_length,
                )

    def reload_roles(self):
        """重新加载所有角色（保留历史记录）"""
        logger.info("重新加载角色配置")
        # 保存旧的历史记录
        old_contexts = {}
        for role_name, ctx in self.context_managers.items():
            old_contexts[role_name] = {
                'history': ctx.history.copy(),
                'last_interaction': ctx.last_interaction
            }

        # 清空并重新加载
        self.context_managers.clear()
        self.client_pool.clear()
        self.role_loader.load_all_roles()
        self.roles = self.role_loader.get_roles()
        logger.info(f"重新加载后角色: {list(self.roles.keys())}")
        self._init_context_managers()

        # 恢复历史记录
        for role_name, ctx in self.context_managers.items():
            if role_name in old_contexts:
                ctx.history = old_contexts[role_name]['history']
                ctx.last_interaction = old_contexts[role_name]['last_interaction']
                logger.debug(f"恢复角色 '{role_name}' 的历史记录: {len(ctx.history)} 条")

    def clear_history(self):
        """清除所有角色的对话历史记录"""
        logger.info("正在清除所有角色的对话历史记录...")
        count = 0
        for role_name, manager in self.context_managers.items():
            manager.clear()
            count += 1
        clear_selection_usage()
        logger.info(f"已清除 {count} 个角色的对话历史记录")

    def detect_role(self, text: str) -> Tuple[Optional[RoleConfig], str]:
        """检测文本是否匹配某个角色前缀

        Args:
            text: 输入文本

        Returns:
            (role_config, content) - role_config 是 RoleConfig 对象，content 是去除前缀后的文本
        """
        return self.role_detector.detect(text)

    def resolve_role(self, role_name: str) -> Optional[RoleConfig]:
        """按显示名取回一个启用中的具名角色

        供连续对话使用：粘滞角色只存名字，每次请求重新解析，这样角色文件被热重载、
        改名或禁用之后不会继续用着一份过期的配置。

        Args:
            role_name: 角色显示名（RoleConfig.display_name）

        Returns:
            角色配置；不存在、已禁用或指向默认角色时返回 None
        """
        if not role_name or role_name == RoleConfig.DEFAULT_ROLE_NAME:
            return None
        role_config = self.roles.get(role_name)
        if role_config is None or not role_config.enabled:
            return None
        return role_config

    def process(self, role_config: RoleConfig, content: str, matched_hotwords=None, callback=None,
                status_callback=None, used_tools=None, image_data=None,
                selection=None) -> Tuple[str, int, float]:
        """执行实际的 LLM 模型调用（内部方法）

        Args:
            role_config: 角色配置对象
            content: 去除前缀后的输入内容
            matched_hotwords: [(hotword, score), ...] 来自 hot_phoneme 的检索结果
            callback: 流式输出的回调函数
            status_callback: 过程状态回调（如联网搜索中），仅用于展示
            used_tools: 出参列表，本次请求触发的服务端工具 ID 会追加进来
            image_data: 随本句上传的图片（base64 data URL）。与 used_tools 一样，
                新增输出模式时必须记得转发，漏了不报错、只是图片静默丢失
            selection: 本次请求已读取的选区；None 仅供直接调用时现场读取

        Returns:
            (处理后的文本, 输出token数, 生成时间秒)
        """
        # 获取中断检查函数
        should_stop_check = lambda: self.monitor.should_stop()

        # 中断键的起效闸门（见 StopMonitor.on_stop_pressed）：在这里统一包一层，
        # 三条流式路径与粘贴模式就都覆盖到了，不必往 llm_processor 里塞监控对象。
        # callback 即便原本是 None 也要传包装后的，否则粘贴模式（不给回调）永远
        # 武装不起来，ESC 对它就成了摆设
        monitor = self.monitor

        def marking_callback(chunk: str):
            monitor.mark_output_started()
            if callback:
                callback(chunk)

        def marking_status(status: str):
            # 工具状态占位（如「正在联网搜索」）同样是用户看得见的东西
            monitor.mark_output_started()
            status_callback(status)

        # 获取处理后的角色名称（空字符串 -> '默认'）
        role_name = role_config.display_name or RoleConfig.DEFAULT_ROLE_NAME
        logger.debug(f"开始 LLM 核心处理 [角色: {role_name}] [内容长度: {len(content)}]")

        # 获取上下文管理器（如果启用历史）
        context_manager = self.context_managers.get(role_name) if role_config.enable_history else None
        if context_manager:
            logger.debug(f"角色 '{role_name}' 启用历史，当前历史条数: {len(context_manager.history)}")
        
        # 获取选中文字（如果启用）
        if selection is None:
            selection = capture_selection(role_config, self.app.state)
        selection_text = selection.text
        
        # 构建消息
        messages = self.message_builder.build_messages(
            role_config, content, context_manager,
            image_data=image_data,
            hotwords=matched_hotwords,
            selection_text=selection_text
        )
        
        # 使用 LLM 处理引擎执行请求
        result_text, token_count, gen_time = self.processor.process(
            role_config=role_config,
            messages=messages,
            callback=marking_callback,
            should_stop_check=should_stop_check,
            context_manager=context_manager,
            status_callback=marking_status if status_callback else None,
            used_tools=used_tools
        )

        # 记录选中文字的使用（用于下一轮判断是否重复）
        record_selection_usage(role_config, selection_text)

        return result_text, token_count, gen_time

    async def process_and_output(self, text: str, paste: bool = None, matched_hotwords=None,
                                 forced_role: Optional[RoleConfig] = None,
                                 image_data: Optional[str] = None,
                                 reminder_anchor: Optional[float] = None,
                                 on_selection=None) -> Optional[LLMResult]:
        """
        统一入口：处理输入文本并根据配置执行输出（打字或弹屏）

        Args:
            text: 待润色的完整原始文本（含可能的前缀）
            paste: 是否强制使用粘贴模式（None 则遵循配置）
            matched_hotwords: 潜在热词列表
            forced_role: 连续对话指定的粘滞角色。给出时跳过角色前缀检测，整句原样发给它
            image_data: 本次录音绑定的图片（base64 data URL）。只有角色
                enable_vision=True 时才真的带上，否则静默丢弃（见约定 21）
            reminder_anchor: 录音结束时间戳；仅实时录音入口提供，用作提醒时间基准
            on_selection: 选区就绪后通知控制台打印预览与识别结果；跳过读取时传 None
        """
        import time
        from .llm_output_typing import handle_typing_mode, output_text
        from .llm_output_toast import handle_toast_mode
        from core.client.output.text_output import TextOutput

        try:
            start_time = time.time()
            # 进入请求作用域：中断键的两层闸门由此开始计时，finally 保证成对关闭
            self.monitor.begin_request()

            # 1. 角色检测。连续对话中不做检测：此时「我在跟这个角色说话」已经明确，
            #    再让角色名保留劫持能力，说「翻译一下这句话」就会被劫走
            if forced_role is not None:
                role_config, content = forced_role, text
            else:
                role_config, content = self.detect_role(text)

            # 2. 如果不匹配任何需要处理的角色
            if not role_config:

                if on_selection:
                    on_selection(None)

                # 打字输出
                await output_text(text, paste)
            
                # 更新全局状态并 UDP 广播
                self.app.state.set_output_text(text)
                broadcast_output_udp(text)

                return LLMResult(result=text, role_name=None, processed=False, 
                                    token_count=0, polish_time=0, input_text=text)


            # 只有录音入口提供 anchor，文件转录和默认角色不会创建提醒。
            # 拦截在 Companion、选区读取和图片上传之前；none 才恢复原来的角色请求。
            reminders = getattr(self.app, 'reminders', None)
            if (reminders is not None and reminder_anchor is not None
                    and role_config.display_name
                    and role_config.display_name != RoleConfig.DEFAULT_ROLE_NAME):
                message = await reminders.handle(role_config, content, reminder_anchor)
                if message is not None:
                    if on_selection:
                        on_selection(None)
                    return LLMResult(
                        result=message, role_name=role_config.display_name, processed=True,
                        token_count=0, polish_time=time.time() - start_time, input_text=content,
                        reminder_handled=True)

            # 3. Companion 转发：连接 Electron 时，跳过本地 LLM，将清洗后的文本转发
            if self.app.companion.has_connections:
                if on_selection:
                    on_selection(None)
                role_name = role_config.display_name or RoleConfig.DEFAULT_ROLE_NAME
                await self.app.companion.emit_user_message(content, role_name)
                logger.info(f"已转发至 Companion: [{role_name}] {content[:50]}")
                return LLMResult(
                    result=content, role_name=role_name, processed=True,
                    token_count=0, polish_time=0, input_text=content)

            # 选区只采集一次，并且早于 Toast/OpenMate 展示；预览与模型使用同一份快照。
            # 提醒与 Companion 不消费选区，故必须在这两个入口之后读取。
            selection = await to_thread(capture_selection, role_config, self.app.state)
            if on_selection:
                on_selection(selection)

            # 4. 根据输出模式分发处理
            # used_tools 由各输出模式一路透传到流解析层，在那里追加实际触发的工具 ID。
            # 不用返回值携带：三个输出模式里有十来处提前 return 的降级分支，扩成四元组
            # 漏改一处就是运行时崩溃；也不用 handler 上的实例属性，避免跨请求残留
            used_tools = []
            # 角色不支持视觉就不带图。不校验 model 是否真是视觉模型——那是角色文件
            # 作者的责任，写错了服务端会返 400 并由约定 18 的诊断日志打到终端
            image_for_request = image_data if role_config.enable_vision else None
            if image_data and not image_for_request:
                logger.info(f"角色「{role_config.display_name or RoleConfig.DEFAULT_ROLE_NAME}」"
                            f"未开启 enable_vision，本次图片不上传")
            image_used = bool(image_for_request)
            if role_config.output_mode == 'toast':
                result, token_count, gen_time = await handle_toast_mode(
                    self, text, role_config, matched_hotwords, content, used_tools, image_for_request,
                    selection)
            else: # typing
                result, token_count, gen_time = await handle_typing_mode(
                    self, text, paste, matched_hotwords, role_config, content, used_tools,
                    image_for_request, selection)

            # 5. 后置处理
            # 更新全局状态（即便是中断了，也记录已经输出的部分）
            if result:
                self.app.state.set_output_text(result)
                broadcast_output_udp(result)

            return LLMResult(
                result=result,
                role_name=role_config.display_name or RoleConfig.DEFAULT_ROLE_NAME,
                processed=True,
                token_count=token_count,
                polish_time=time.time() - start_time,
                input_text=content,
                generation_time=gen_time,
                used_tools=used_tools,
                image_used=image_used
            )
        finally:
            self.monitor.end_request()


# ======================================================================
# --- 测试 ---

if __name__ == "__main__":
    print("=" * 60)
    print("LLM 处理器 - 测试模式")
    print("=" * 60)

    import asyncio
    from core.client.state import ClientState
    from types import SimpleNamespace

    async def run_test_cases():
        # 创建一个模拟的 app 实例
        mock_app = SimpleNamespace()
        mock_app.base_dir = Path(".").resolve()

        # 模拟 hotword manager
        mock_app.hotword = SimpleNamespace()

        # 模拟 state
        mock_app.state = ClientState(mock_app)
        
        # 初始化处理器
        handler = LLMHandler(mock_app)
        handler.start()
        
        print(f"\n已加载角色: {list(handler.roles.keys())}")

        test_cases = [
            "呃，我想查看一下当前目录的文件",
            "命令 查看当前目录的文件",
            "Python 读取文件",
        ]

        print("\n--- 测试案例 ---\n")
        try:
            for test_text in test_cases:
                print(f"输入: {test_text}")
                # 注意：这里直接调用 process 而不是 process_and_output，避免依赖复杂的 UI/打字逻辑
                role_config, content = handler.detect_role(test_text)
                if role_config:
                    print(f"检测到角色: {role_config.name}")
                    # 由于是模拟环境，可能无法真正发起请求，这里仅演示逻辑
                else:
                    print("未检测到特定角色")
                print("-" * 40)
        finally:
            handler.stop()

    asyncio.run(run_test_cases())
