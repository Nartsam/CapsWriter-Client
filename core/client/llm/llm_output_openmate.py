"""
LLM OpenMate 输出模式

将 toast 角色的 LLM 流式输出转交 OpenMate 桌面看板展示（HTTP API，
文档见 OpenMate/docs/api/external-http-api.md）。

工作方式：
1. 请求开始时探测 /health，OpenMate 以 external 模式运行且接受写入才走此路径；
   通过 GET /v1/characters/{id}/states 自动获取角色情绪状态列表（带缓存，
   状态来自 OpenMate 启动快照，改立绘需重启 OpenMate），注入 system prompt
   末尾要求 LLM 输出情绪标签，标签用稳定状态 ID、别名仅作语义注解
2. LLM 输出的 <state:情绪ID> 内联标签原样透传，由 OpenMate 解析立绘状态并从
   显示/历史中剥离，CapsWriter 无需自行解析
3. 分片经后台线程聚合后再 PATCH，避免逐 chunk 请求触发 OpenMate 的
   速率限制（600 次/60 秒），也避免 HTTP 阻塞 LLM 流读取
4. ESC 中断映射为 DELETE（取消流，不写历史）；中途发送失败时最终结果
   剥离标签后回退本地 Toast 展示，不丢内容
"""
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from core.tools.asyncio_to_thread import to_thread
from . import logger

# OpenMate 内联状态标签，与其文档 §4.4 的约束一致（非空、≤64 字符、无控制字符和嵌套 <）
STATE_TAG_RE = re.compile(r'<(?:state|状态):[^<>\x00-\x1f]{1,64}>')


def strip_state_tags(text: str) -> str:
    """剥离 OpenMate 内联状态标签，用于本地展示、历史存储和 UDP 广播"""
    return STATE_TAG_RE.sub('', text) if text else text


def build_emotion_prompt(states: list, default_state: str = '') -> str:
    """构建注入 system prompt 的情绪标签指令

    Args:
        states: OpenMate 返回的状态列表，元素形如 {'id': 'smile', 'aliases': ['微笑', '开心']}
        default_state: 角色默认状态 ID

    标签中要求 LLM 使用稳定的状态 ID（OpenMate 文档 §4.3 的建议），
    别名只作为帮助模型理解语义的注解；零配置扫描的状态别名可能为空。
    """
    lines = []
    for st in states:
        aliases = '、'.join(st.get('aliases') or [])
        lines.append(f'- {st["id"]}' + (f'（含义：{aliases}）' if aliases else ''))
    default_line = (f'平静陈述等没有明显情绪起伏时，使用默认情绪 {default_state}。\n'
                    if default_state else '')
    return (
        '\n\n# 情绪表达\n'
        '你的回复会显示在一个支持情绪立绘的桌面看板上。'
        '请在回复的最开头输出一个情绪标签，格式为 <state:情绪ID>；'
        '若回复中情绪发生明显变化，可在变化处再插入新标签。\n'
        '可用的情绪ID如下：\n'
        + '\n'.join(lines) + '\n'
        '标签中只能使用上面列出的情绪ID，从中挑选最贴切的一个，不要自创或使用含义文字。\n'
        + default_line +
        '不要在代码块内输出情绪标签，除标签本身外不要输出任何关于情绪的说明。'
    )


# ======================================================================
# --- HTTP 客户端 ---

class OpenMateClient:
    """OpenMate 外部 HTTP API 的极简客户端（标准库 urllib，不引入新依赖）"""

    def __init__(self, base_url: str, token: str = '', timeout: float = 5.0):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict = None, timeout: float = None) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        req = urllib.request.Request(f'{self.base_url}{path}', data=data, method=method)
        if body is not None:
            req.add_header('Content-Type', 'application/json; charset=utf-8')
        if self.token:
            req.add_header('Authorization', f'Bearer {self.token}')
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def is_available(self, timeout: float = 0.5) -> bool:
        """探测 OpenMate 是否以 external 模式运行且当前接受外部写入"""
        try:
            info = self._request('GET', '/health', timeout=timeout)
        except Exception:
            return False
        return (info.get('mode') == 'external'
                and bool(info.get('ready'))
                and bool(info.get('acceptsExternalMessages')))

    def list_characters(self) -> list:
        return self._request('GET', '/v1/characters').get('characters', [])

    def get_states(self, character_id: str) -> dict:
        return self._request(
            'GET', f'/v1/characters/{urllib.parse.quote(character_id, safe="")}/states')

    def create_stream(self, character: str = '', preempt: bool = True) -> str:
        # preempt=True：新对话抢占残留的旧活动流，与本地新 Toast 顶掉旧窗口的行为一致
        body = {'protocolVersion': 1, 'preempt': preempt}
        if character:
            body['character'] = character
        return self._request('POST', '/v1/streams', body)['id']

    def patch_delta(self, stream_id: str, delta: str):
        self._request('PATCH', f'/v1/streams/{urllib.parse.quote(stream_id, safe="")}',
                      {'delta': delta})

    def complete_stream(self, stream_id: str) -> str:
        return self._request(
            'POST', f'/v1/streams/{urllib.parse.quote(stream_id, safe="")}/complete', {})['id']

    def cancel_stream(self, stream_id: str):
        self._request('DELETE', f'/v1/streams/{urllib.parse.quote(stream_id, safe="")}')


# ======================================================================
# --- 情绪状态获取（带缓存） ---

# 状态列表来自 OpenMate 启动时的角色目录快照，改动需重启 OpenMate 才生效，
# 短缓存即可避免每句话都多两次 HTTP 请求；失败结果缓存更短，兼顾配置修复后
# 尽快恢复和避免错误配置下每句话刷警告日志
_EMOTION_CACHE_TTL = 300.0
_EMOTION_FAIL_TTL = 60.0
_emotion_cache: dict = {}   # (base_url, character) -> (过期时刻, payload 或 None)


def fetch_emotion_info(client: OpenMateClient, character: str = ''):
    """获取角色情绪状态信息（带缓存）

    Returns:
        {'states': [{'id', 'aliases'}, ...], 'default_state': str}；
        获取失败或可选状态少于 2 个（只有默认立绘，注入情绪指令无意义）时返回 None
    """
    key = (client.base_url, character)
    now = time.monotonic()
    cached = _emotion_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    payload = None
    ttl = _EMOTION_CACHE_TTL
    try:
        char_id = character
        if not char_id:
            # API 无法查询"当前角色"，与 OpenMate 无有效当前角色时的行为
            # 保持一致：取角色列表第一个；多角色场景应显式配置 openmate_character
            chars = client.list_characters()
            char_id = chars[0]['id'] if chars else ''
        if char_id:
            info = client.get_states(char_id)
            states = info.get('states') or []
            if len(states) >= 2:
                payload = {
                    'states': states,
                    'default_state': (info.get('character') or {}).get('defaultState', ''),
                }
            else:
                logger.debug(f'OpenMate 角色 {char_id} 可选状态不足 2 个，不注入情绪指令')
    except Exception as e:
        ttl = _EMOTION_FAIL_TTL
        logger.warning(f'获取 OpenMate 情绪状态失败，本次不注入情绪指令: {e}')

    _emotion_cache[key] = (now + ttl, payload)
    return payload


# ======================================================================
# --- 流式发送器 ---

class OpenMateStreamSender:
    """后台线程发送器：聚合 LLM 分片为批量 delta 发往 OpenMate

    LLM 回调线程只做入队，HTTP 请求全部在独立线程执行；每轮发送前把队列中
    积压的分片合并成一个 PATCH，天然把请求频率压到 OpenMate 限流之下。
    首个分片到达时才创建流（惰性建流），避免 LLM 出错或首 token 过慢时
    留下空流占用 OpenMate 唯一的活动流通道。
    """

    def __init__(self, client: OpenMateClient, character: str = ''):
        self.client = client
        self.character = character
        self._queue: queue.Queue = queue.Queue()
        self._stream_id = None
        self._failed = False
        self._completed = False
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name='OpenMateSender')
        self._thread.start()

    @property
    def failed(self) -> bool:
        return self._failed

    def push(self, chunk: str):
        """LLM 流式回调入口（任意线程可调用）"""
        self._queue.put(('delta', chunk))

    def finish(self, timeout: float = 10.0) -> bool:
        """完成流（写入 OpenMate 历史）。返回内容是否已成功交给 OpenMate 展示"""
        self._queue.put(('complete', None))
        self._thread.join(timeout)
        return self._completed

    def abort(self, timeout: float = 5.0):
        """取消流（不写历史），用于 ESC 中断和 LLM 异常"""
        self._queue.put(('cancel', None))
        self._thread.join(timeout)

    def _worker(self):
        pending = []
        while True:
            kind, payload = self._queue.get()
            if kind != 'delta':
                self._finalize(kind)
                return
            pending.append(payload)
            # 聚合队列中积压的分片；若中途遇到控制命令，先送出文本再收尾
            control = None
            while control is None:
                try:
                    nxt_kind, nxt_payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt_kind == 'delta':
                    pending.append(nxt_payload)
                else:
                    control = nxt_kind
            self._send_delta(''.join(pending))
            pending.clear()
            if control is not None:
                self._finalize(control)
                return

    def _send_delta(self, text: str):
        if self._failed or not text:
            return
        try:
            if self._stream_id is None:
                self._stream_id = self.client.create_stream(self.character, preempt=True)
                logger.debug(f'OpenMate 流已创建: {self._stream_id[:8]}')
            self.client.patch_delta(self._stream_id, text)
        except Exception as e:
            logger.warning(f'OpenMate 流发送失败，将回退本地展示: {e}')
            self._failed = True

    def _finalize(self, kind: str):
        if self._failed:
            # 流已损坏：尽力取消残留的流，释放 OpenMate 唯一活动流通道
            if self._stream_id is not None:
                try:
                    self.client.cancel_stream(self._stream_id)
                except Exception:
                    pass
            return
        if self._stream_id is None:
            # LLM 无输出，从未建流；complete 视为成功（无内容需要展示）
            self._completed = (kind == 'complete')
            return
        try:
            if kind == 'complete':
                self.client.complete_stream(self._stream_id)
                self._completed = True
            else:
                self.client.cancel_stream(self._stream_id)
        except Exception as e:
            logger.warning(f'OpenMate 流收尾失败: {e}')
            self._failed = True


# ======================================================================
# --- 输出模式入口 ---

def _make_client() -> OpenMateClient:
    from config_client import ClientConfig as Config
    return OpenMateClient(Config.openmate_url, Config.openmate_token)


async def try_openmate_mode(handler, role_config, content, matched_hotwords) -> tuple:
    """尝试 OpenMate 展示路径

    Returns:
        (result, token_count, gen_time) 元组；OpenMate 未启用或不可用时返回 None，
        由调用方继续走本地 Toast 路径
    """
    from config_client import ClientConfig as Config
    if not getattr(Config, 'openmate_enabled', False) or not role_config.openmate_enabled:
        return None

    client = _make_client()
    available = await to_thread(client.is_available, Config.openmate_health_timeout)
    if not available:
        return None

    logger.info('OpenMate 可用，LLM 输出将转交 OpenMate 展示')
    return await _handle_openmate_mode(handler, client, role_config, content, matched_hotwords)


async def _handle_openmate_mode(handler, client, role_config, content, matched_hotwords) -> tuple:
    import dataclasses
    from config_client import ClientConfig as Config
    from .llm_role_config import RoleConfig

    character = role_config.openmate_character or Config.openmate_character
    emotion = await to_thread(fetch_emotion_info, client, character)

    # 情绪指令只在走 OpenMate 时注入，本地 Toast 路径不受污染；
    # 用副本追加，不改动角色原配置（角色对象会被后续请求复用）
    llm_config = role_config
    if emotion:
        llm_config = dataclasses.replace(
            role_config,
            system_prompt=(role_config.system_prompt or '')
                          + build_emotion_prompt(emotion['states'], emotion['default_state']),
        )

    sender = OpenMateStreamSender(client, character)
    try:
        polished_text, token_count, gen_time = await to_thread(
            handler.process, llm_config, content, matched_hotwords, sender.push
        )

        if handler.monitor.should_stop():
            # 用户中断：取消流（OpenMate 不写历史），与本地关闭 Toast 行为一致
            await to_thread(sender.abort)
            return (strip_state_tags(polished_text or content), token_count, gen_time)

        ok = await to_thread(sender.finish)
        result = strip_state_tags(polished_text or content)
        if not ok and result:
            # OpenMate 中途失效（退出/布局编辑/超时）：本地 Toast 兜底展示完整结果
            logger.warning('OpenMate 展示未完成，回退本地 Toast')
            _show_fallback_toast(role_config, result)
        return (result, token_count, gen_time)

    except Exception as e:
        await to_thread(sender.abort)

        from .llm_error_handler import handle_llm_error, should_fallback_to_original
        from core.client.output.text_output import TextOutput
        role_name = role_config.display_name or RoleConfig.DEFAULT_ROLE_NAME

        if should_fallback_to_original(e):
            result_text, _ = handle_llm_error(e, content, role_name)
            result_text = TextOutput.strip_punc(result_text)

            from .llm_output_typing import output_text
            await output_text(result_text, Config.paste)
            return (result_text, 0, 0.0)
        else:
            from .llm_error_handler import show_error_notification
            show_error_notification(e, role_name)
            return ("", 0, 0.0)


def _show_fallback_toast(role_config, text: str):
    """OpenMate 失效时的本地兜底：非流式 Toast 一次性展示最终文本"""
    from core.ui.toast import ToastMessageManager, ToastMessage
    from .llm_output_toast import (
        LLM_TOAST_FONT_FAMILY, LLM_TOAST_FONT_SIZE,
        LLM_TOAST_BG, LLM_TOAST_FG,
    )
    msg = ToastMessage(
        text=text,
        font_family=role_config.toast_font_family or LLM_TOAST_FONT_FAMILY,
        font_size=role_config.toast_font_size or LLM_TOAST_FONT_SIZE,
        bg=role_config.toast_bg_color or LLM_TOAST_BG,
        fg=role_config.toast_font_color or LLM_TOAST_FG,
        duration=role_config.toast_duration,
        initial_width=role_config.toast_initial_width,
        initial_height=role_config.toast_initial_height,
        streaming=False,
        window_type='text',
        markdown=True,
        editable=role_config.toast_editable,
        window_alpha=role_config.toast_window_alpha,
        corner_radius=role_config.toast_corner_radius,
    )
    ToastMessageManager().add_message(msg)
