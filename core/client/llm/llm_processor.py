"""
LLM 处理引擎

功能：
1. 执行 LLM API 调用
2. 处理流式输出
3. 更新上下文历史
4. 统一的错误处理和包装
5. 精确的生成时间统计（从第一个 token 开始）
"""
import time
import traceback
from typing import Callable, Optional, Dict, Any, List, Tuple
from .llm_role_config import RoleConfig
from .llm_interfaces import IContextManager
from .llm_client_pool import ClientPool
from .llm_constants import estimate_tokens, ToolNames, IMAGE_PLACEHOLDER
from .llm_anthropic import to_anthropic_messages, DEFAULT_MAX_TOKENS, WEB_SEARCH_TOOL
from .llm_exceptions import (
    APIException,
    wrap_openai_error, OpenAIErrorWrapper,
    TimeoutErrorWrapper, APIResponseErrorWrapper
)
from . import logger


def _dig(obj: Any, *names: str) -> Any:
    """按名称链逐层取值，兼容 SDK 的 pydantic 对象与 dict，任一层缺失返回 None

    各家 API 的 usage / 错误详情嵌套结构不同，且同一字段在不同版本里时有时无，
    直接 getattr 链会因为一个缺失字段就抛异常，把诊断日志本身变成新的故障点
    """
    for name in names:
        if obj is None:
            return None
        obj = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return obj


class LLMProcessor:
    """LLM 处理引擎 - 负责 API 调用和流式输出"""

    def __init__(self, client_pool: ClientPool):
        """
        Args:
            client_pool: 客户端池
        """
        self.client_pool = client_pool

    def process(
        self,
        role_config: RoleConfig,
        messages: List[Dict[str, str]],
        callback: Optional[Callable[[str], None]] = None,
        should_stop_check: Optional[Callable[[], bool]] = None,
        context_manager: Optional[IContextManager] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        used_tools: Optional[List[str]] = None,
        toast_request=None
    ) -> Tuple[str, int, float]:
        """
        执行 LLM 处理

        Args:
            role_config: 角色配置
            messages: 消息列表
            callback: 流式输出回调函数
            should_stop_check: 检查是否应该停止的函数
            context_manager: 上下文管理器（用于更新历史）
            status_callback: 过程状态回调（如联网搜索中），仅用于展示，不计入正文
            used_tools: 出参列表，本次请求实际触发的服务端工具 ID 会按序追加进来
            toast_request: 本地 Toast 的取消/完成状态，决定本轮是否提交历史

        Returns:
            (处理后的文本, 输出token数, 生成时间秒)
        """
        logger.info(f"开始 LLM 处理，模型: {role_config.model}")

        # 获取客户端
        logger.debug(f"获取 LLM 客户端，提供商: {role_config.provider}, API: {role_config.api_url}")
        client = self.client_pool.get_client(
            provider=role_config.provider,
            api_url=role_config.api_url,
            api_key=role_config.api_key,
            api_format=role_config.api_format
        )

        try:
            return self._stream_request(
                client,
                callback,
                should_stop_check,
                role_config,
                context_manager,
                messages,
                status_callback,
                used_tools,
                toast_request
            )

        except OpenAIErrorWrapper:
            raise
        except Exception as e:
            if toast_request is not None and toast_request.cancelled.is_set():
                logger.debug(f"忽略已取消 Toast 请求的迟到异常: {type(e).__name__}")
                return '', 0, 0.0
            if isinstance(e, APIException):
                raise

            # 捕获 Ollama SDK 原生异常
            if role_config.provider == 'ollama':
                try:
                    import ollama
                    if isinstance(e, (ollama.ResponseError, ollama.RequestError)):
                        error_msg = f"Ollama API 调用失败: {e}"
                        logger.error(error_msg)
                        raise APIException(error_msg, role_config.provider) from e
                except ImportError:
                    pass

            # 捕获 OpenAI SDK 原生异常并包装
            import openai

            # 处理 httpx 超时异常（在流式读取时发生）
            if 'httpx' in str(type(e).__module__):
                error_type = type(e).__name__
                if 'Timeout' in error_type or 'timeout' in error_type:
                    # httpx.ReadTimeout 或 httpx.TimeoutException
                    wrapped_error = TimeoutErrorWrapper(e, role_config.provider)
                    logger.error(f"LLM API 请求超时: {wrapped_error}")
                    raise wrapped_error from e

            if isinstance(e, (
                openai.AuthenticationError,
                openai.RateLimitError,
                openai.APITimeoutError,
                openai.APIConnectionError,
                openai.APIError
            )):
                wrapped_error = wrap_openai_error(e, role_config.provider)
                logger.error(f"LLM API 调用失败: {wrapped_error}")
                raise wrapped_error from e

            # 其他未预期的异常，包装为 APIException 并抛出
            error_msg = f"LLM 处理失败: {e}"
            logger.error(f"{error_msg}\n{traceback.format_exc()}")
            raise APIException(error_msg, role_config.provider) from e

    def _build_request_params(
        self,
        role_config: RoleConfig,
        messages: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """统一构建请求参数"""
        params = {
            'model': role_config.model,
            'messages': messages,
            'temperature': role_config.temperature,
            'top_p': role_config.top_p,
        }

        if role_config.max_tokens > 0:
            params['max_tokens'] = role_config.max_tokens

        # 处理停止序列
        if role_config.stop:
            stop_list = role_config.stop
            if isinstance(stop_list, str):
                stop_list = [s.strip() for s in stop_list.split(',')]
            params['stop'] = stop_list

        # 合并额外选项
        if role_config.extra_options:
            params.update(role_config.extra_options)

        return params

    @staticmethod
    def _to_responses_content(content: Any) -> Any:
        """把 Chat Completions 口味的多模态 content 转成 Responses API 口味

        MessageBuilder 统一产出 Chat 口味作为内部表示（历史里存的也是它），
        两边的差异只在这一处收敛：文本条目 type 由 text 变 input_text；图片条目
        type 由 image_url 变 input_image，且 image_url 从 {'url': ...} 嵌套对象
        变成裸字符串。纯字符串 content 两边通用，原样返回（此前一直如此，
        所以不带图的请求行为逐字不变）。
        """
        if not isinstance(content, list):
            return content
        items = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get('type')
            if part_type in ('text', 'input_text'):
                items.append({'type': 'input_text', 'text': part.get('text', '')})
            elif part_type in ('image_url', 'input_image'):
                url = part.get('image_url')
                if isinstance(url, dict):
                    url = url.get('url', '')
                if url:
                    items.append({'type': 'input_image', 'image_url': url})
        return items

    def _build_responses_params(
        self,
        role_config: RoleConfig,
        messages: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """构建 Responses API 请求参数

        Responses API 与 Chat Completions 的差异：system 消息走 instructions，
        其余消息走 input，token 上限字段名为 max_output_tokens，且没有 stop 参数。
        """
        instructions = ''
        input_items = []
        for msg in messages:
            if msg.get('role') == 'system' and not instructions:
                instructions = msg.get('content', '')
            else:
                content = self._to_responses_content(msg.get('content'))
                if content is msg.get('content'):
                    input_items.append(msg)
                else:
                    input_items.append({'role': msg.get('role'), 'content': content})

        params = {
            'model': role_config.model,
            'input': input_items,
            'temperature': role_config.temperature,
            'top_p': role_config.top_p,
        }

        if instructions:
            params['instructions'] = instructions

        if role_config.max_tokens > 0:
            params['max_output_tokens'] = role_config.max_tokens

        if role_config.enable_web_search:
            # 服务端内置工具，检索在 DeepSeek 侧执行，本地无需搜索 API Key。
            # 是否检索由模型自行判断，普通问题不会触发，速度与不挂载工具时一致。
            # 没有接第三方搜索 API，也没有做本地关键词路由：后者既会漏判（「XX 发布了
            # 吗」不含时效词）又会误判（拖慢常识问答）
            params['tools'] = [{'type': 'web_search'}]

        if role_config.stop:
            logger.debug("Responses API 不支持 stop 参数，已忽略")

        if role_config.extra_options:
            params.update(role_config.extra_options)

        return params

    def _build_anthropic_params(
        self,
        role_config: RoleConfig,
        messages: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """构建 Anthropic Messages 请求参数（不含 stream，由客户端按调用方式补上）

        与另两套的差异：system 走顶层字段；max_tokens 是必填项；停止序列叫
        stop_sequences；搜索工具是 web_search_20250305
        """
        system, items = to_anthropic_messages(messages)
        params = {
            'model': role_config.model,
            'messages': items,
            'max_tokens': role_config.max_tokens if role_config.max_tokens > 0 else DEFAULT_MAX_TOKENS,
            'temperature': role_config.temperature,
            'top_p': role_config.top_p,
        }
        if system:
            params['system'] = system

        if role_config.stop:
            stop_list = role_config.stop
            if isinstance(stop_list, str):
                stop_list = [s.strip() for s in stop_list.split(',')]
            params['stop_sequences'] = stop_list

        # 与另两条 OpenAI 路径一致：只在关闭时显式声明。DeepSeek 的 Anthropic 接口上
        # 这个开关是生效的（Responses 上被忽略，见 DEV_NOTES）
        if not role_config.enable_thinking:
            params['thinking'] = {'type': 'disabled'}

        if role_config.enable_web_search:
            params['tools'] = [dict(WEB_SEARCH_TOOL)]

        if role_config.extra_options:
            params.update(role_config.extra_options)

        return params

    @staticmethod
    def _describe_messages(messages: List[Dict[str, str]]) -> str:
        """一行请求构成摘要（几条历史、带几张图）

        完整请求 JSON 只在 DEBUG 打，而控制台固定在 WARNING、日志平时也是 INFO，
        于是「这次到底把历史里的图带上了没有」在事后完全无从查证——2026-09-04 排查
        「历史里的图模型看不到」时只能靠写探针反推。这行是那次排查留下的常备诊断。
        """
        images = 0
        for msg in messages:
            content = msg.get('content')
            if isinstance(content, list):
                images += sum(
                    1 for part in content
                    if isinstance(part, dict)
                    and part.get('type') in ('image_url', 'input_image')
                )
        # 减去本句自己，剩下的才是历史
        history = max(0, sum(1 for m in messages if m.get('role') != 'system') - 1)
        return f"{history} 条历史 + 本句，{images} 张图"

    def _stream_request(
        self,
        client: Any,
        callback: Optional[Callable[[str], None]],
        should_stop_check: Optional[Callable[[], bool]],
        role_config: RoleConfig,
        context_manager: Optional[IContextManager],
        messages: List[Dict[str, str]],
        status_callback: Optional[Callable[[str], None]] = None,
        used_tools: Optional[List[str]] = None,
        toast_request=None
    ) -> Tuple[str, int, float]:
        """执行流式请求并协调不同处理引擎"""

        if toast_request is not None and toast_request.cancelled.is_set():
            return '', 0, 0.0
        logger.info(f"请求构成：{self._describe_messages(messages)}")

        if role_config.provider == 'ollama':
            result = self._process_ollama_stream(
                client, role_config, messages, callback, should_stop_check
            )
        elif role_config.api_format == 'anthropic':
            # 联网搜索（开启时）也在这条路径里挂工具，不再分 Responses / Chat 两条
            result = self._process_anthropic_stream(
                client, role_config, messages, callback, should_stop_check,
                status_callback, used_tools
            )
        elif role_config.enable_web_search:
            # 带图的请求同样走这里，不降级到 Chat Completions：图片与内置 web_search
            # 可以在同一个 Responses 请求里共存（tools/probe_llm_live.py 的 P1 复测）
            result = self._process_responses_stream(
                client, role_config, messages, callback, should_stop_check,
                status_callback, used_tools
            )
        else:
            result = self._process_openai_stream(
                client, role_config, messages, callback, should_stop_check
            )

        full_response, total_tokens, generation_time = result

        # Token 估算兜底（针对 Ollama 等不返回 usage 的提供商）
        if total_tokens == 0 and full_response:
            total_tokens = estimate_tokens(full_response)
            result = (full_response, total_tokens, generation_time)

        # 在工作线程里确定生成完成：之后再按 Esc 只关窗。取消先发生时整轮丢弃，
        # 即使用户已经看到了半截回答，也不把它写入后续上下文。
        if toast_request is None or toast_request.finish():
            self._record_turn(role_config, context_manager, messages, full_response)

        return result

    def _record_turn(
        self,
        role_config: RoleConfig,
        context_manager: Optional[IContextManager],
        messages: List[Dict[str, Any]],
        full_response: str
    ) -> None:
        """把本轮写进记忆（仅开启了历史的角色）

        回复为空（输出额度被推理耗尽、拒答、出字前就按了中断键）时整轮不写：用户没看到
        回答，记下来只会让之后每次请求都带着一条空消息，记忆落盘后更是一直带着；只写提问
        又会留下两条相连的 user 消息，模型容易把两个问题混在一起答。OpenMate / typing
        已输出的部分照常写入；本地 Toast 的取消轮次在调用本方法之前整轮拦截。
        """
        if not (role_config.enable_history and context_manager):
            return
        if not (full_response or '').strip():
            logger.info("回复为空，本轮不写入记忆")
            return
        user_content = messages[-1]['content']
        if isinstance(user_content, list):
            # 新一轮带图：先把历史里更早的图降成占位符，历史中因此最多只有一张真图
            demoted = context_manager.demote_images(IMAGE_PLACEHOLDER)
            if demoted:
                logger.debug(f"历史中 {demoted} 张旧图已降级为占位符")
        # 存**原文**，标签一并进历史。此前这里剥离过，结果是历史里的 assistant
        # 消息永远不带标签，模型从第二轮起照抄自己上一条的形态、不再输出标签
        # （few-shot 漂移压过 system 指令，2026-09-04 探针实测复现）。标签是展示
        # 层指令，剥离改由各输出路径在展示前做（见 llm_state_tags）。
        context_manager.add_turn(user_content, full_response)
        logger.debug(f"已更新历史记录")

    def _process_ollama_stream(
        self,
        client: Any,
        role_config: RoleConfig,
        messages: List[Dict[str, str]],
        callback: Optional[Callable[[str], None]],
        should_stop_check: Optional[Callable[[], bool]]
    ) -> Tuple[str, int, float]:
        """专门处理 Ollama 原生流响应"""
        
        # 1. 统一构建参数并提取 Ollama 选项
        params = self._build_request_params(role_config, messages)
        ollama_options = {
            k: v for k, v in params.items()
            if k in ['temperature', 'top_p', 'stop']
        }
        if role_config.max_tokens > 0:
            ollama_options['num_predict'] = role_config.max_tokens
        
        # 2. 发起请求
        stream = client.chat(
            model=role_config.model,
            messages=messages,
            stream=True,
            options=ollama_options,
            think=role_config.enable_thinking
        )

        # 3. 迭代处理
        full_response, total_tokens, generation_start_time = "", 0, None
        stopped = False

        for chunk in stream:
            if should_stop_check and should_stop_check():
                stopped = True
                break

            if hasattr(chunk, 'message') and chunk.message.content:
                content = chunk.message.content
                full_response += content
                if generation_start_time is None:
                    generation_start_time = time.time()
                if callback:
                    callback(content)

            if hasattr(chunk, 'done') and chunk.done:
                total_tokens = getattr(chunk, 'eval_count', 0)

        generation_time = time.time() - generation_start_time if generation_start_time else 0.0

        if not full_response.strip() and not stopped:
            logger.warning("Ollama 没有返回任何正文（开启 think 时内容可能全在思考字段里）")
        elif stopped:
            logger.info(f"Ollama 流被用户中断：正文 {len(full_response)} 字")

        return full_response.strip(), total_tokens, generation_time

    def _process_openai_stream(
        self,
        client: Any,
        role_config: RoleConfig,
        messages: List[Dict[str, str]],
        callback: Optional[Callable[[str], None]],
        should_stop_check: Optional[Callable[[], bool]]
    ) -> Tuple[str, int, float]:
        """专门处理 OpenAI 兼容 API 流响应"""

        params = self._build_request_params(role_config, messages)
        params['stream'] = True

        if not role_config.enable_thinking:
            params['extra_body'] = {"thinking": {"type": "disabled"}}

        if role_config.request_timeout > 0:
            params['timeout'] = role_config.request_timeout

        stream = client.chat.completions.create(**params)

        full_response, total_tokens, generation_start_time = "", 0, None
        finish_reason, usage_summary, stopped = '', 'usage 缺失', False

        for chunk in stream:
            if should_stop_check and should_stop_check():
                try: stream.close()
                except: pass
                stopped = True
                break

            if chunk.choices and chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_response += content
                if generation_start_time is None:
                    generation_start_time = time.time()
                if callback:
                    callback(content)

            if chunk.choices and chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

            if hasattr(chunk, 'usage') and chunk.usage:
                total_tokens = getattr(chunk.usage, 'completion_tokens', 0)
                usage_summary = self._format_usage(chunk.usage)

        generation_time = time.time() - generation_start_time if generation_start_time else 0.0

        # 空回复不会抛异常，不主动记录就只表现为「窗口一闪而过」，无从追查
        summary = f"结束原因 {finish_reason or '未知'}，{usage_summary}，正文 {len(full_response)} 字"
        if not full_response.strip() and not stopped:
            logger.warning(f"模型没有返回任何正文（{summary}）")
        elif stopped:
            # 同 Responses 路径：中断抑制了空回复告警，这里补终局快照
            logger.info(f"Chat Completions 流被用户中断：{summary}")
        elif finish_reason == 'length':
            logger.warning(f"回复达到 max_tokens 被截断（{summary}）")
        else:
            logger.debug(f"Chat Completions 流结束：{summary}")

        return full_response.strip(), total_tokens, generation_time

    @staticmethod
    def _format_usage(usage: Any) -> str:
        """把 usage 整理成一行日志，兼容 Responses 与 Chat Completions 两套字段名

        重点是 reasoning_tokens：它是判断「输出额度是否被思考内容吃光」的唯一直接
        证据——各家 API 对 max_tokens 是否计入思考 token 的口径并不一致
        """
        if usage is None:
            return 'usage 缺失'

        input_tokens = _dig(usage, 'input_tokens')
        if input_tokens is None:
            input_tokens = _dig(usage, 'prompt_tokens')
        output_tokens = _dig(usage, 'output_tokens')
        if output_tokens is None:
            output_tokens = _dig(usage, 'completion_tokens')
        reasoning_tokens = _dig(usage, 'output_tokens_details', 'reasoning_tokens')
        if reasoning_tokens is None:
            reasoning_tokens = _dig(usage, 'completion_tokens_details', 'reasoning_tokens')

        parts = [
            f"输入 {'?' if input_tokens is None else input_tokens} tokens",
            f"输出 {'?' if output_tokens is None else output_tokens} tokens",
        ]
        parts.append('未回报推理 token' if reasoning_tokens is None else f'其中推理 {reasoning_tokens}')
        return '，'.join(parts)

    @staticmethod
    def _format_event_counts(event_counts: Dict[str, int]) -> str:
        """事件类型计数，按出现次数降序

        未被解析分支认领的事件（例如思考分片）只会出现在这里，是排查
        「一个正文字符都没收到」时最先要看的东西
        """
        if not event_counts:
            return '无事件'
        items = sorted(event_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return ', '.join(f'{name}×{count}' for name, count in items)

    @staticmethod
    def _extract_tool_id(event_type: str) -> str:
        """从 Responses API 事件名中取出服务端工具 ID

        事件名形如 `response.web_search_call.in_progress`，工具 ID 即 `web_search`。
        这里按命名规律解析而不是硬编码 web_search，将来 DeepSeek 放开别的内置工具
        无需改动流解析逻辑。非工具事件返回空串
        """
        if not event_type.startswith('response.'):
            return ''
        segment = event_type[len('response.'):].split('.')[0]
        if not segment.endswith('_call'):
            return ''
        return segment[:-len('_call')]

    def _process_responses_stream(
        self,
        client: Any,
        role_config: RoleConfig,
        messages: List[Dict[str, str]],
        callback: Optional[Callable[[str], None]],
        should_stop_check: Optional[Callable[[], bool]],
        status_callback: Optional[Callable[[str], None]] = None,
        used_tools: Optional[List[str]] = None
    ) -> Tuple[str, int, float]:
        """处理 Responses API 流响应（联网搜索路径）

        服务端执行工具期间不会有正文分片产生，只能靠 <工具>_call 事件告知用户，
        否则窗口会空着好几秒像卡死
        """

        params = self._build_responses_params(role_config, messages)
        params['stream'] = True

        if not role_config.enable_thinking:
            params['extra_body'] = {"thinking": {"type": "disabled"}}

        if role_config.request_timeout > 0:
            params['timeout'] = role_config.request_timeout

        stream = client.responses.create(**params)

        full_response, total_tokens, generation_start_time = "", 0, None
        seen_tools = []
        # 诊断三件套：事件类型计数、终局状态、usage。空回复时它们是唯一能说明
        # 「模型这几秒到底在干什么」的线索——没有被下面任何分支认领的事件
        # （典型是思考分片）只会在计数里显形
        event_counts = {}
        final_status, usage_summary, refusal, stopped = '', 'usage 缺失', '', False

        for event in stream:
            if should_stop_check and should_stop_check():
                try: stream.close()
                except: pass
                stopped = True
                break

            event_type = getattr(event, 'type', '')
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            tool_id = self._extract_tool_id(event_type)

            if event_type == 'response.output_text.delta':
                content = getattr(event, 'delta', '') or ''
                if content:
                    full_response += content
                    if generation_start_time is None:
                        generation_start_time = time.time()
                    if callback:
                        callback(content)

            elif tool_id:
                # 同一个工具会连发 in_progress/searching/completed 多个事件，只认第一次
                if tool_id not in seen_tools:
                    seen_tools.append(tool_id)
                    if used_tools is not None:
                        used_tools.append(tool_id)
                    logger.info(f"模型触发了服务端工具: {tool_id}")
                    if status_callback:
                        status_callback(ToolNames.status(tool_id))

            elif event_type in ('response.completed', 'response.incomplete', 'response.failed'):
                # 三个终局事件共用同一个 response 对象结构。incomplete 表示生成被上限
                # 截断（额度可能全被思考或工具调用消耗，此时正文为空），failed 是
                # 服务端明确报错——两者以前都落进 else 被丢弃，是"查不出原因"的根源
                response_obj = getattr(event, 'response', None)
                usage = _dig(response_obj, 'usage')
                if usage:
                    total_tokens = _dig(usage, 'output_tokens') or 0
                    usage_summary = self._format_usage(usage)
                final_status = _dig(response_obj, 'status') or event_type

                if event_type == 'response.incomplete':
                    reason = _dig(response_obj, 'incomplete_details', 'reason') or '未说明'
                    logger.warning(f"Responses API 响应被截断（原因: {reason}），"
                                   f"{usage_summary}，已收到正文 {len(full_response)} 字")
                elif event_type == 'response.failed':
                    error_obj = _dig(response_obj, 'error')
                    # message 是人能读的那部分，缺失时退回整个对象，宁可难看也别丢信息
                    error_detail = _dig(error_obj, 'message') or error_obj
                    logger.error(f"Responses API 响应失败: {error_detail}，{usage_summary}")

            elif event_type in ('response.refusal.delta', 'response.refusal.done'):
                # 模型拒答：正文流为空但这不是故障，需要与"截断"区分开
                refusal += getattr(event, 'delta', '') or getattr(event, 'refusal', '') or ''

            elif event_type == 'error':
                # 流内错误事件：SDK 不会抛异常，不主动记录就只会表现为"没有输出"
                logger.error(f"Responses API 流内错误: {getattr(event, 'message', event)}")

        generation_time = time.time() - generation_start_time if generation_start_time else 0.0

        if refusal:
            logger.warning(f"模型拒绝回答: {refusal}")

        summary = (f"状态 {final_status or '未知'}，{usage_summary}，正文 {len(full_response)} 字，"
                   f"事件 [{self._format_event_counts(event_counts)}]")
        # 读空回复告警的判据：状态 incomplete 且推理 token 很多 → 输出额度在第一个可见
        # token 之前被推理吃光；状态是干净的 completed、事件里只有 reasoning 项、没有
        # message 也没有任何 <工具>_call → 工具调用被中间网关吞掉、回合直接结束
        # （CPA 网关补齐 web_search 之前的症状：闲聊正常，一问时效性问题就空）
        if not full_response.strip() and not stopped:
            logger.warning(f"模型没有返回任何正文（{summary}）")
        elif stopped:
            # 中断会把上面那条空回复告警一并抑制掉，这里必须自己留下终局快照，
            # 否则「（空回复，详见日志）」在日志里对不上任何一条记录
            logger.info(f"Responses 流被用户中断：{summary}")
        else:
            logger.debug(f"Responses 流结束：{summary}")

        return full_response.strip(), total_tokens, generation_time

    def _process_anthropic_stream(
        self,
        client: Any,
        role_config: RoleConfig,
        messages: List[Dict[str, Any]],
        callback: Optional[Callable[[str], None]],
        should_stop_check: Optional[Callable[[], bool]],
        status_callback: Optional[Callable[[str], None]] = None,
        used_tools: Optional[List[str]] = None
    ) -> Tuple[str, int, float]:
        """处理 Anthropic Messages 流（api_format='anthropic'）

        服务端工具以 server_tool_use 内容块出现，块的 name 就是工具 ID（web_search），
        与 Responses 路径的 <工具>_call 事件一样触发状态占位与 used_tools
        """
        params = self._build_anthropic_params(role_config, messages)
        timeout = role_config.request_timeout if role_config.request_timeout > 0 else None

        full_response, total_tokens, generation_start_time = "", 0, None
        seen_tools = []
        # 计数键带上块与增量的类型（block_start[server_tool_use]、delta[thinking_delta]），
        # 只数事件名的话空回复时看不出模型这几秒在思考、在搜索还是什么都没干
        event_counts = {}
        usage = {}
        stop_reason, refusal_note, stopped = '', '', False

        with client.stream(params, timeout=timeout) as events:
            for event in events:
                if should_stop_check and should_stop_check():
                    stopped = True
                    break

                event_type = event.get('type', '')
                block = event.get('content_block') or {}
                delta = event.get('delta') or {}
                if event_type == 'content_block_start':
                    count_key = f"block_start[{block.get('type', '?')}]"
                elif event_type == 'content_block_delta':
                    count_key = f"delta[{delta.get('type', '?')}]"
                else:
                    count_key = event_type
                event_counts[count_key] = event_counts.get(count_key, 0) + 1

                if event_type == 'content_block_delta' and delta.get('type') == 'text_delta':
                    content = delta.get('text') or ''
                    if content:
                        full_response += content
                        if generation_start_time is None:
                            generation_start_time = time.time()
                        if callback:
                            callback(content)

                elif event_type == 'content_block_start' and block.get('type') == 'server_tool_use':
                    tool_id = block.get('name') or 'server_tool'
                    # 一个回合里可能搜索多次，只认第一次
                    if tool_id not in seen_tools:
                        seen_tools.append(tool_id)
                        if used_tools is not None:
                            used_tools.append(tool_id)
                        logger.info(f"模型触发了服务端工具: {tool_id}")
                        if status_callback:
                            status_callback(ToolNames.status(tool_id))

                elif event_type == 'content_block_start' and block.get('type', '').endswith('_tool_result'):
                    # 工具执行失败时 content 是 {'type': ..._error, 'error_code': ...} 而不是结果列表，
                    # 模型会接着凭记忆回答，不记下来就看不出这次其实没搜到
                    result = block.get('content')
                    if isinstance(result, dict) and result.get('error_code'):
                        logger.warning(f"服务端工具执行失败: {block.get('type')} {result.get('error_code')}")

                elif event_type == 'message_start':
                    usage.update(_dig(event, 'message', 'usage') or {})

                elif event_type == 'message_delta':
                    # message_delta 里的 usage 是累计终值（输入含搜索结果、输出含工具调用参数）
                    usage.update(event.get('usage') or {})
                    stop_reason = delta.get('stop_reason') or stop_reason

                elif event_type == 'error':
                    # 不能按正常结束返回：半截正文会被写入记忆。响应错误走现有错误提示，
                    # 不触发把识别原文打进当前窗口的降级路径。
                    error = APIResponseErrorWrapper(
                        RuntimeError(f"Anthropic 接口流内错误: {_dig(event, 'error', 'message') or event}"),
                        role_config.provider)
                    logger.error(f"LLM API 调用失败: {error}")
                    raise error

        generation_time = time.time() - generation_start_time if generation_start_time else 0.0
        total_tokens = usage.get('output_tokens') or 0
        usage_summary = self._format_usage(usage) if usage else 'usage 缺失'
        searches = _dig(usage, 'server_tool_use', 'web_search_requests')
        if searches:
            usage_summary += f"，搜索 {searches} 次"

        if stop_reason == 'refusal':
            refusal_note = '（模型拒答）'
        summary = (f"结束原因 {stop_reason or '未知'}{refusal_note}，{usage_summary}，"
                   f"正文 {len(full_response)} 字，事件 [{self._format_event_counts(event_counts)}]")
        if not full_response.strip() and not stopped:
            logger.warning(f"模型没有返回任何正文（{summary}）")
        elif stopped:
            # 同 Responses 路径：中断抑制了空回复告警，这里补终局快照
            logger.info(f"Anthropic 流被用户中断：{summary}")
        elif stop_reason == 'max_tokens':
            logger.warning(f"回复达到 max_tokens 被截断（{summary}）")
        elif stop_reason == 'pause_turn':
            # 服务端工具循环过长时会暂停回合、要求客户端续发；本客户端不续发，回复可能不完整
            logger.warning(f"服务端暂停了本回合，回复可能不完整（{summary}）")
        else:
            logger.debug(f"Anthropic 流结束：{summary}")

        return full_response.strip(), total_tokens, generation_time
