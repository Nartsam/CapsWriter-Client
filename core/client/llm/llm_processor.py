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
from .llm_constants import estimate_tokens, ToolNames
from .llm_exceptions import (
    APIException,
    wrap_openai_error, OpenAIErrorWrapper,
    TimeoutErrorWrapper
)
from . import logger


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
        used_tools: Optional[List[str]] = None
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

        Returns:
            (处理后的文本, 输出token数, 生成时间秒)
        """
        logger.info(f"开始 LLM 处理，模型: {role_config.model}")

        # 获取客户端
        logger.debug(f"获取 LLM 客户端，提供商: {role_config.provider}, API: {role_config.api_url}")
        client = self.client_pool.get_client(
            provider=role_config.provider,
            api_url=role_config.api_url,
            api_key=role_config.api_key
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
                used_tools
            )

        except OpenAIErrorWrapper:
            raise
        except Exception as e:
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
                input_items.append(msg)

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
            # 是否检索由模型自行判断，普通问题不会触发，速度与不挂载工具时一致
            params['tools'] = [{'type': 'web_search'}]

        if role_config.stop:
            logger.debug("Responses API 不支持 stop 参数，已忽略")

        if role_config.extra_options:
            params.update(role_config.extra_options)

        return params

    def _stream_request(
        self,
        client: Any,
        callback: Optional[Callable[[str], None]],
        should_stop_check: Optional[Callable[[], bool]],
        role_config: RoleConfig,
        context_manager: Optional[IContextManager],
        messages: List[Dict[str, str]],
        status_callback: Optional[Callable[[str], None]] = None,
        used_tools: Optional[List[str]] = None
    ) -> Tuple[str, int, float]:
        """执行流式请求并协调不同处理引擎"""

        if role_config.provider == 'ollama':
            result = self._process_ollama_stream(
                client, role_config, messages, callback, should_stop_check
            )
        elif role_config.enable_web_search:
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

        # 更新历史
        if role_config.enable_history and context_manager:
            from .llm_output_openmate import strip_state_tags
            context_manager.add_message('user', messages[-1]['content'])
            # 状态标签是 OpenMate 展示层指令，进入历史会诱导后续回复在本地
            # Toast 展示时也输出标签，污染显示，故存历史前统一剥离
            context_manager.add_message('assistant', strip_state_tags(full_response))
            logger.debug(f"已更新历史记录")

        return result

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
        
        for chunk in stream:
            if should_stop_check and should_stop_check():
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

        for chunk in stream:
            if should_stop_check and should_stop_check():
                try: stream.close()
                except: pass
                break

            if chunk.choices and chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_response += content
                if generation_start_time is None:
                    generation_start_time = time.time()
                if callback:
                    callback(content)

            if hasattr(chunk, 'usage') and chunk.usage:
                total_tokens = getattr(chunk.usage, 'completion_tokens', 0)

        generation_time = time.time() - generation_start_time if generation_start_time else 0.0
        return full_response.strip(), total_tokens, generation_time

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

        for event in stream:
            if should_stop_check and should_stop_check():
                try: stream.close()
                except: pass
                break

            event_type = getattr(event, 'type', '')
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

            elif event_type == 'response.completed':
                usage = getattr(getattr(event, 'response', None), 'usage', None)
                if usage:
                    total_tokens = getattr(usage, 'output_tokens', 0) or 0

            elif event_type == 'error':
                # 流内错误事件：SDK 不会抛异常，不主动记录就只会表现为"没有输出"
                logger.error(f"Responses API 流内错误: {getattr(event, 'message', event)}")

        generation_time = time.time() - generation_start_time if generation_start_time else 0.0
        return full_response.strip(), total_tokens, generation_time
