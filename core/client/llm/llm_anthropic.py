"""
Anthropic Messages 格式的请求转换与极简客户端（角色 api_format='anthropic'）

为什么有这条路径：DeepSeek 的 Responses API 对 web_search 等内置工具一律忽略（官方
文档 guides/responses_api 的工具支持表），flash 模型会把工具调用当正文吐出来
（DSML 标记、<tool_result>、整段 Python 代码）；服务端联网搜索只剩 Anthropic 兼容
接口（/anthropic/v1/messages + web_search_20250305）还能用，且该接口上
thinking: disabled 是生效的，短问答首字比 Responses 快约 0.5s（2026-10-07 实测）。

内部消息统一是 Chat Completions 口味（MessageBuilder 产出、历史里存的也是它），
到这里才转换：system 提成顶层字段，图片 image_url（data URL）转成 base64 image 块。

运行时里没有 anthropic SDK，用 openai SDK 依赖的 httpx 直接发、自己解析 SSE。
httpx 在 AnthropicClient 构造时才导入：系统 Python 没有它，而离线测试要加载本模块
里的转换函数（经 llm_processor）。
"""
import json
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import logger

ANTHROPIC_VERSION = '2023-06-01'
# Anthropic 协议要求必须给 max_tokens；角色写 0（「用模型默认值」）时用它
DEFAULT_MAX_TOKENS = 4096
WEB_SEARCH_TOOL = {'type': 'web_search_20250305', 'name': 'web_search'}

# 与 openai SDK 的默认值一致：连不上或 408/409/429/5xx 时重试两次。只在收到响应头
# 之前重试，正文开始流动后不再重发，不会出现重复输出
MAX_RETRIES = 2
_RETRY_STATUS = {408, 409, 429}


def _image_block(url: str) -> Optional[Dict[str, Any]]:
    if not url:
        return None
    if url.startswith('data:') and ';base64,' in url:
        header, data = url[len('data:'):].split(';base64,', 1)
        return {'type': 'image',
                'source': {'type': 'base64', 'media_type': header or 'image/png', 'data': data}}
    return {'type': 'image', 'source': {'type': 'url', 'url': url}}


def _to_content(content: Any) -> Any:
    """Chat 口味的 content → Anthropic 口味；纯字符串两边通用，原样返回"""
    if not isinstance(content, list):
        return content
    blocks = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get('type')
        if part_type in ('text', 'input_text'):
            blocks.append({'type': 'text', 'text': part.get('text', '')})
        elif part_type in ('image_url', 'input_image'):
            url = part.get('image_url')
            if isinstance(url, dict):
                url = url.get('url', '')
            block = _image_block(url)
            if block:
                blocks.append(block)
    return blocks


def to_anthropic_messages(messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """拆出 system 文本，其余消息转成 Anthropic 口味

    Anthropic 的 messages 里只允许 user / assistant；system 消息（MessageBuilder 只产出
    开头一条）全部并进顶层 system 字段
    """
    system_parts, items = [], []
    for msg in messages:
        role = msg.get('role')
        if role == 'system':
            if msg.get('content'):
                system_parts.append(msg['content'])
        elif role in ('user', 'assistant'):
            items.append({'role': role, 'content': _to_content(msg.get('content'))})
    return '\n\n'.join(system_parts), items


def extract_text(response: Dict[str, Any]) -> str:
    """非流式响应里的正文：所有 text 块按序拼接（工具块、思考块不算）"""
    return ''.join(block.get('text', '') for block in response.get('content') or []
                   if isinstance(block, dict) and block.get('type') == 'text')


class AnthropicHTTPError(Exception):
    """非 2xx 响应。status_code 供提醒诊断等只记状态码的日志使用"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f'HTTP {status_code}: {detail}')


def _error_detail(body: bytes) -> str:
    """错误响应只取 error.message，避免把整份响应写进日志"""
    text = body.decode('utf-8', 'replace')
    try:
        data = json.loads(text)
        error = data.get('error') if isinstance(data, dict) else None
        if isinstance(error, dict) and error.get('message'):
            return str(error['message'])[:300]
    except ValueError:
        pass
    return text[:300]


class AnthropicClient:
    """POST {base_url}/v1/messages 的极简客户端，按 ClientPool 缓存复用（保持连接）"""

    def __init__(self, base_url: str, api_key: str, timeout: float, provider: str):
        import httpx
        self._httpx = httpx
        self.url = base_url.rstrip('/') + '/v1/messages'
        self.api_key = api_key
        self.provider = provider
        self._http = httpx.Client(timeout=timeout)

    def _headers(self, stream: bool) -> Dict[str, str]:
        headers = {'x-api-key': self.api_key, 'anthropic-version': ANTHROPIC_VERSION,
                   'content-type': 'application/json'}
        if stream:
            headers['accept'] = 'text/event-stream'
        return headers

    def _wrap(self, error: Exception, status_code: Optional[int] = None) -> Exception:
        """映射成与 openai SDK 路径相同的包装异常：llm_error_handler 按类型决定弹错误框
        还是降级为打出识别原文。openai 路径由 LLMProcessor.process 包装并记日志，这里
        抛出的已是包装类、process 会原样放行，所以日志在这里记"""
        from .llm_exceptions import (
            AuthenticationErrorWrapper, RateLimitErrorWrapper, TimeoutErrorWrapper,
            ConnectionErrorWrapper, APIResponseErrorWrapper)
        if status_code in (401, 403):
            cls = AuthenticationErrorWrapper
        elif status_code == 429:
            cls = RateLimitErrorWrapper
        elif status_code is not None:
            cls = APIResponseErrorWrapper
        elif isinstance(error, self._httpx.TimeoutException):
            cls = TimeoutErrorWrapper
        else:
            cls = ConnectionErrorWrapper
        wrapped = cls(error, self.provider)
        wrapped.status_code = status_code
        logger.error(f"LLM API 调用失败: {wrapped}")
        return wrapped

    def _send(self, body: Dict[str, Any], stream: bool, timeout: Optional[float],
              max_retries: int):
        """发出请求直到拿到 2xx 响应头；返回未读正文的 httpx 响应"""
        httpx = self._httpx
        kwargs = {'timeout': timeout} if timeout else {}
        for attempt in range(max_retries + 1):
            last = attempt == max_retries
            request = self._http.build_request('POST', self.url, json=body,
                                               headers=self._headers(stream), **kwargs)
            try:
                response = self._http.send(request, stream=stream)
            except httpx.TimeoutException as e:
                # 超时不重试：request_timeout 是 60s 量级，重试会让人干等几分钟
                raise self._wrap(e) from e
            except httpx.TransportError as e:
                if last:
                    raise self._wrap(e) from e
                logger.info(f"连接失败，{0.5 * 2 ** attempt:.1f}s 后重试：{type(e).__name__}")
                time.sleep(0.5 * 2 ** attempt)
                continue
            if response.status_code < 400:
                return response
            detail = _error_detail(response.read())
            response.close()
            retryable = response.status_code in _RETRY_STATUS or response.status_code >= 500
            if last or not retryable:
                error = AnthropicHTTPError(response.status_code, detail)
                raise self._wrap(error, response.status_code) from error
            logger.info(f"HTTP {response.status_code}，{0.5 * 2 ** attempt:.1f}s 后重试：{detail}")
            time.sleep(0.5 * 2 ** attempt)
        raise AssertionError('unreachable')

    @contextmanager
    def stream(self, body: Dict[str, Any], timeout: Optional[float] = None,
               max_retries: int = MAX_RETRIES) -> Iterator[Iterator[Dict[str, Any]]]:
        """流式请求：with 块内迭代 SSE 事件（已解析的 dict），退出 with 即断开连接

        正文流动后的网络错误（读超时、对端断开）原样抛出 httpx 异常，交给
        LLMProcessor.process 统一分类，与 openai SDK 路径的行为一致
        """
        response = self._send(dict(body, stream=True), True, timeout, max_retries)
        try:
            yield self._iter_events(response)
        finally:
            response.close()

    @staticmethod
    def _iter_events(response) -> Iterator[Dict[str, Any]]:
        # 每个事件是 `event: <类型>` + `data: <JSON>` 两行，data 里的 type 与 event 行相同，
        # 只解析 data 行即可
        for line in response.iter_lines():
            if not line.startswith('data:'):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event

    def create(self, body: Dict[str, Any], timeout: Optional[float] = None,
               max_retries: int = MAX_RETRIES) -> Dict[str, Any]:
        """非流式请求，返回解析后的响应 JSON"""
        response = self._send(dict(body, stream=False), False, timeout, max_retries)
        try:
            return response.json()
        finally:
            response.close()

    def close(self):
        self._http.close()
