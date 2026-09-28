"""
LLM OpenMate 输出模式

将 toast 角色的 LLM 流式输出转交 OpenMate 桌面看板展示（HTTP API，
文档见 OpenMate/docs/api/external-http-api.md）。

工作方式：
1. 请求开始时探测 /health（无状态按需探测、不维护长连接：OpenMate 可随时启停），
   OpenMate 以 external 模式运行且接受写入才走此路径；
   未配置目标角色时通过 GET /v1/presentation 查询当前生效角色（每次都查，
   角色可随时在 OpenMate 界面切换），再通过 GET /v1/characters/{id}/states
   获取该角色的情绪状态列表（带缓存，状态来自 OpenMate 启动快照，改立绘需
   重启 OpenMate），注入 system prompt 末尾要求 LLM 输出情绪标签，
   标签用稳定状态 ID、别名仅作语义注解
2. LLM 输出的 <state:情绪ID> 内联标签由 OpenMate 解析立绘状态并从
   显示/历史中剥离；分段时将纯标签段并入下一段正文，避免产生空消息
3. 分片经后台线程聚合后再 PATCH，避免逐 chunk 请求触发 OpenMate 的
   速率限制（600 次/60 秒），也避免 HTTP 阻塞 LLM 流读取
4. 角色开启 enable_result_splitter 时改走 open 分段队列：回复按分隔符
   切成多条消息逐条展示（OpenMateSegmentSender），第一段就绪即开始，
   边生成边追加；否则走流式正文（OpenMateStreamSender）
5. ESC 中断映射为 DELETE（取消流/队列）；中途发送失败时最终结果
   剥离标签后回退本地 Toast 展示，不丢内容
6. 接管确认后本地「正在处理结果」悬浮提示移交为 OpenMate 的 typing 指示
   （PUT /v1/presentation/typing）：首条消息/分片到达自动熄灭，失败路径
   显式关闭；typing 开启失败则保留本地提示（兼容旧版 OpenMate）
"""
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from core.tools.asyncio_to_thread import to_thread
from . import logger

# 状态标签的正则与剥离统一放在 llm_state_tags：本地 Toast / 打字两条路径也要用，
# 让它们反向 import 这个 OpenMate 专用模块说不通（见该模块开头的说明）。
# 这里再导出一次，原先从本模块 import 的调用方不必改。
from .llm_state_tags import STATE_TAG_RE, strip_state_tags  # noqa: F401


def build_emotion_prompt(states: list, default_state: str = '') -> str:
    """构建注入 system prompt 的情绪标签指令

    Args:
        states: OpenMate 返回的状态列表，元素形如 {'id': 'smile', 'aliases': ['微笑', '开心']}
        default_state: 角色默认状态 ID

    标签中要求 LLM 使用稳定的状态 ID（OpenMate 文档 §4.3 的建议），
    别名只作为帮助模型理解语义的注解；零配置扫描的状态别名可能为空。
    本地不校验模型输出的标签是否合法：OpenMate 对无效状态会回退默认立绘并剥离
    标签，本地再做一层跨分片校验只增加复杂度、没有用户可见收益。
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
        '情绪标签必须紧贴它所修饰的正文，不要单独占一行，也不要在回复末尾单独补标签。\n'
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

    def get_presentation(self) -> dict:
        """查询当前生效角色与活动流状态（含未传 character 时写请求的回退结果）"""
        return self._request('GET', '/v1/presentation')

    def get_states(self, character_id: str) -> dict:
        return self._request(
            'GET', f'/v1/characters/{urllib.parse.quote(character_id, safe="")}/states')

    def post_reminder_batch(self, items: list, request_id: str) -> dict:
        """成功仅表示 OpenMate 已将整批接收入内存，不等待实际展示。"""
        return self._request('POST', '/v1/message-batches', {
            'protocolVersion': 1, 'requestId': request_id, 'items': items})

    def create_open_queue(self, segments: list, character: str = '', request_id: str = '') -> str:
        """创建 open 分段消息队列（先创建、后追加、最终封口），返回消息 ID"""
        body = {'protocolVersion': 1, 'segments': segments, 'open': True}
        if character:
            body['character'] = character
        if request_id:
            body['requestId'] = request_id
        return self._request('POST', '/v1/messages', body)['id']

    def post_message(self, text: str, character: str = '', request_id: str = '') -> str:
        """提交一次性完整消息（回复未切出多段时的退化路径），返回消息 ID"""
        body = {'protocolVersion': 1, 'text': text}
        if character:
            body['character'] = character
        if request_id:
            body['requestId'] = request_id
        return self._request('POST', '/v1/messages', body)['id']

    def append_segments(self, message_id: str, segments: list = None, close: bool = False):
        """向 open 队列追加段落和/或封口。appendSegments 传空数组是 400，留空时必须省略"""
        body = {}
        if segments:
            body['appendSegments'] = segments
        if close:
            body['close'] = True
        self._request('PATCH',
                      f'/v1/messages/{urllib.parse.quote(message_id, safe="")}', body)

    def cancel_message(self, message_id: str):
        """取消分段队列：剩余段停止展示、按 interrupted 入库"""
        self._request('DELETE', f'/v1/messages/{urllib.parse.quote(message_id, safe="")}')

    def set_typing(self, active: bool):
        """开关「正在输入」指示（纯视觉：消息/流到达自动熄灭，30s 超时兜底）"""
        self._request('PUT', '/v1/presentation/typing', {'active': active})

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

    Args:
        character: 目标角色 ID；留空时通过 /v1/presentation 解析当前生效角色

    Returns:
        {'character_id': str, 'states': [{'id', 'aliases'}, ...], 'default_state': str}；
        获取失败或可选状态少于 2 个（只有默认立绘，注入情绪指令无意义）时返回 None
    """
    now = time.monotonic()
    char_id = character
    if not char_id:
        # 当前角色可随时在 OpenMate 界面切换，解析结果不缓存、每次请求都查；
        # /v1/presentation 返回的就是写请求不传 character 时消息实际会展示
        # 到的角色（API 文档 §5.4），避免把其他角色的状态 ID 注入给模型。
        # 仅失败结果短暂缓存在 (base_url, '') 键下，避免每句话都刷警告日志
        resolve_key = (client.base_url, '')
        cached = _emotion_cache.get(resolve_key)
        if cached and cached[0] > now:
            return cached[1]
        try:
            current = client.get_presentation().get('character') or {}
            char_id = current.get('id', '')
        except Exception as e:
            logger.warning(f'查询 OpenMate 当前角色失败，本次不注入情绪指令: {e}')
        if not char_id:
            _emotion_cache[resolve_key] = (now + _EMOTION_FAIL_TTL, None)
            return None

    key = (client.base_url, char_id)
    cached = _emotion_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    payload = None
    ttl = _EMOTION_CACHE_TTL
    try:
        info = client.get_states(char_id)
        states = info.get('states') or []
        if len(states) >= 2:
            payload = {
                'character_id': char_id,
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

    @property
    def delivered(self) -> bool:
        """是否已有内容实际发往 OpenMate 展示（用于判断 typing 指示会否自动熄灭）"""
        return self._stream_id is not None

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


class OpenMateSegmentSender:
    """分段消息发送器：把 LLM 流式输出按分隔符切成多条消息，经 open 队列逐条展示

    与 OpenMateStreamSender 接口一致（push/finish/abort/failed），按角色的
    enable_result_splitter 二选一。切分只是发送层钩子：本地对话历史与 md 日志
    仍保存未切分全文。第一段就绪即创建 open 队列开始展示（不等全文生成），
    后续段落边生成边追加，生成结束随最后一段封口；空白段丢弃，纯状态标签
    留给下一段正文，结束时丢弃未附着正文的标签；未发出完整段时退化为单条消息。

    发送器只按分隔符切，不按字数合并或截断：一条消息该多长、分几条由角色提示词
    约束。按字数强行合并会推迟首条投递，也容易把不同情绪或独立问题混进一条。
    """

    def __init__(self, client: OpenMateClient, character: str = '', delimiter: str = '\n\n'):
        self.client = client
        self.character = character
        self.delimiter = delimiter
        self._queue: queue.Queue = queue.Queue()
        self._buffer = ''
        self._pending_state_tags = ''
        self._message_id = None
        self._posted = False    # 退化单条消息已发出（此时 _message_id 保持 None）
        # 队列创建的幂等 ID：网络重试时避免同一回复被重复展示
        self._request_id = f'capswriter-{uuid.uuid4()}'
        self._failed = False
        self._completed = False
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name='OpenMateSegmentSender')
        self._thread.start()

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def delivered(self) -> bool:
        """是否已有内容实际发往 OpenMate 展示（用于判断 typing 指示会否自动熄灭）"""
        return self._message_id is not None or self._posted

    def push(self, chunk: str):
        """LLM 流式回调入口（任意线程可调用）"""
        self._queue.put(('delta', chunk))

    def finish(self, timeout: float = 10.0) -> bool:
        """封口队列。返回内容是否已成功交给 OpenMate 展示"""
        self._queue.put(('complete', None))
        self._thread.join(timeout)
        return self._completed

    def abort(self, timeout: float = 5.0):
        """取消队列（剩余段按 interrupted 入库），用于 ESC 中断和 LLM 异常"""
        self._queue.put(('cancel', None))
        self._thread.join(timeout)

    def _worker(self):
        # 与 OpenMateStreamSender 相同的聚合模式：合并积压分片后统一切分发送
        while True:
            kind, payload = self._queue.get()
            if kind != 'delta':
                self._finalize(kind)
                return
            parts = [payload]
            control = None
            while control is None:
                try:
                    nxt_kind, nxt_payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt_kind == 'delta':
                    parts.append(nxt_payload)
                else:
                    control = nxt_kind
            self._buffer += ''.join(parts)
            self._dispatch(self._cut_segments())
            if control is not None:
                self._finalize(control)
                return

    def _cut_segments(self) -> list:
        """从缓冲切出带正文的完整段落，独立状态标签留给后续正文"""
        segments = []
        while self.delimiter in self._buffer:
            head, self._buffer = self._buffer.split(self.delimiter, 1)
            head = self._prepare_segment(head)
            if head:
                segments.append(head)
        return segments

    def _prepare_segment(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ''
        if not strip_state_tags(text).strip():
            # OpenMate 会把标签变成状态事件；独立发送会留下正文为空的历史条目。
            # 保留标签顺序并附到下一段，避免在过滤空段时把立绘状态一并丢掉。
            self._pending_state_tags += ''.join(STATE_TAG_RE.findall(text))
            return ''
        text = self._pending_state_tags + text
        self._pending_state_tags = ''
        return text

    def _dispatch(self, segments: list):
        if self._failed or not segments:
            return
        try:
            if self._message_id is None:
                self._message_id = self.client.create_open_queue(
                    segments, self.character, self._request_id)
                logger.debug(f'OpenMate 分段队列已创建: {self._message_id[:8]}')
            else:
                self.client.append_segments(self._message_id, segments)
        except Exception as e:
            logger.warning(f'OpenMate 分段发送失败，将回退本地展示: {e}')
            self._failed = True

    def _finalize(self, kind: str):
        if kind != 'complete' or self._failed:
            self._buffer = ''
            self._pending_state_tags = ''
            self._cancel_residual()
            return
        last = self._prepare_segment(self._buffer)
        self._buffer = ''
        # 尾部只有标签时已无正文可以附着，不为它额外创建消息或追加空段。
        self._pending_state_tags = ''
        try:
            if self._message_id is None:
                # 全程未切出段落：退化为一次性完整消息；完全无内容则无需请求
                if last:
                    self.client.post_message(last, self.character, self._request_id)
                    self._posted = True
                self._completed = True
            else:
                self.client.append_segments(
                    self._message_id, [last] if last else None, close=True)
                self._completed = True
        except Exception as e:
            logger.warning(f'OpenMate 分段收尾失败: {e}')
            self._failed = True
            self._cancel_residual()

    def _cancel_residual(self):
        # 队列已损坏或被中断：尽力取消，停止剩余段展示（404 表示已被抢占/播完，忽略）
        if self._message_id is None:
            return
        try:
            self.client.cancel_message(self._message_id)
        except Exception:
            pass


# ======================================================================
# --- 输出模式入口 ---

def _make_client() -> OpenMateClient:
    from config_client import ClientConfig as Config
    return OpenMateClient(Config.openmate_url, Config.openmate_token)


def _set_typing_safe(client: OpenMateClient, active: bool) -> bool:
    """尽力开关 typing 指示：纯视觉锦上添花，失败不影响主流程

    返回是否成功，供调用方决定是否隐藏本地提示（旧版 OpenMate 无此端点时
    保留本地「正在处理结果」悬浮窗，行为向后兼容）
    """
    try:
        client.set_typing(active)
        return True
    except Exception as e:
        logger.debug(f'OpenMate typing 指示设置失败（忽略）: {e}')
        return False


async def try_openmate_mode(handler, role_config, content, matched_hotwords, used_tools=None,
                            image_data=None, selection=None) -> tuple:
    """尝试 OpenMate 展示路径

    Args:
        used_tools: 出参列表，本次请求触发的服务端工具 ID 会追加进来
        image_data: 随本句上传的图片（base64 data URL）
        selection: 本次请求已读取的选区快照，需一路转发到 handler.process

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
    return await _handle_openmate_mode(handler, client, role_config, content, matched_hotwords,
                                       used_tools, image_data, selection)


async def _handle_openmate_mode(handler, client, role_config, content, matched_hotwords,
                                used_tools=None, image_data=None, selection=None) -> tuple:
    import dataclasses
    from config_client import ClientConfig as Config
    from .llm_role_config import RoleConfig

    # 接管确认：本地「正在处理结果」悬浮提示移交为 OpenMate 的 typing 指示；
    # typing 开启失败（如旧版 OpenMate 无此端点）则保留本地提示不移交
    if await to_thread(_set_typing_safe, client, True):
        from core.client.ui import hide_recording_indicator
        hide_recording_indicator()

    character = role_config.openmate_character or Config.openmate_character
    emotion = await to_thread(fetch_emotion_info, client, character)

    # 情绪指令只在走 OpenMate 时注入，本地 Toast 路径不受污染；
    # 用副本追加，不改动角色原配置（角色对象会被后续请求复用）
    llm_config = role_config
    if emotion:
        logger.info(f"已注入 OpenMate 角色 '{emotion['character_id']}' 的情绪指令"
                    f"（{len(emotion['states'])} 个状态）")
        llm_config = dataclasses.replace(
            role_config,
            system_prompt=(role_config.system_prompt or '')
                          + build_emotion_prompt(emotion['states'], emotion['default_state']),
        )

    # 流/队列固定到已注入状态列表的角色：留空配置下若用户在生成期间于 OpenMate
    # 切换角色，标签仍与展示角色一致，不会失配回退默认立绘
    target_character = emotion['character_id'] if emotion else character
    if role_config.enable_result_splitter:
        sender = OpenMateSegmentSender(
            client, target_character, role_config.result_split_delimiter or '\n\n')
    else:
        sender = OpenMateStreamSender(client, target_character)
    try:
        polished_text, token_count, gen_time = await to_thread(
            handler.process, llm_config, content, matched_hotwords, sender.push, None, used_tools,
            image_data, selection
        )

        if handler.monitor.should_stop():
            # 用户中断：取消流（OpenMate 不写历史），与本地关闭 Toast 行为一致。
            # 这条分支此前一行日志都不打，而 llm_processor 的空回复告警又被中断
            # 抑制，于是「OpenMate 一片空白」在事后完全无从追查（2026-09-12 故障）
            role_label = role_config.display_name or '默认'
            if sender.delivered:
                logger.info(f"[{role_label}] 输出被用户中断，已发往 OpenMate 的内容被取消"
                            f"（正文 {len(polished_text)} 字）")
            else:
                logger.info(f"[{role_label}] 输出被用户中断，尚无任何内容发往 OpenMate"
                            f"（正文 {len(polished_text)} 字）")
            await to_thread(sender.abort)
            if not sender.delivered:
                # 未发出任何内容，typing 不会被消息自动熄灭，显式关闭
                await to_thread(_set_typing_safe, client, False)
            return (strip_state_tags(polished_text), token_count, gen_time)

        ok = await to_thread(sender.finish)
        # 空回复不降级成识别原文：这段文字没被 OpenMate 展示过，冒充模型输出会让
        # 控制台与 md 记录误报成一次成功回复（与 llm_output_toast 同一处理）
        result = strip_state_tags(polished_text)
        if not result:
            logger.warning(f"角色 [{role_config.display_name or '默认'}] 返回空回复，"
                           f"OpenMate 无内容可展示（原始输入：{content[:30]}…）")
        if not ok and result:
            # OpenMate 中途失效（退出/布局编辑/超时）：本地 Toast 兜底展示完整结果
            logger.warning('OpenMate 展示未完成，回退本地 Toast')
            _show_fallback_toast(role_config, result)
        if not ok or not sender.delivered:
            # 内容未成功交给 OpenMate（失败回退或 LLM 无输出）：显式熄灭 typing
            await to_thread(_set_typing_safe, client, False)
        return (result, token_count, gen_time)

    except Exception as e:
        await to_thread(sender.abort)
        await to_thread(_set_typing_safe, client, False)

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
        min_width_px=role_config.toast_min_width_px,
        max_width_px=role_config.toast_max_width_px,
        min_width_ratio=role_config.toast_min_width_ratio,
        max_width_ratio=role_config.toast_max_width_ratio,
        initial_height=role_config.toast_initial_height,
        streaming=False,
        window_type='text',
        markdown=True,
        editable=role_config.toast_editable,
        window_alpha=role_config.toast_window_alpha,
        corner_radius=role_config.toast_corner_radius,
        # 智能时长：非流式一次性展示，创建即按最终文本字数计时；
        # 悬停不顺延、拖拽后永不自动关闭（同 handle_toast_mode）
        smart_duration=True,
    )
    manager = ToastMessageManager()
    # 新 LLM 回复窗口生成：顶掉所有旧的 LLM 白色对话框（无论是否拖拽）
    manager.close_llm_windows()
    manager.add_message(msg)
