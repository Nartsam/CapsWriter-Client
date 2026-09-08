"""提醒协调器。队列变更只在应用事件循环内执行，网络线程只返回数据。"""
import asyncio
import math
import time
import uuid

from config_client import ClientConfig as Config
from core.client import logger
from core.tools.asyncio_to_thread import to_thread
from . import analyzer, output
from .fullscreen import should_defer
from .model import (ReminderError, PARSE_ERROR, SUBJECT_ERROR, clean_text,
                    create_reminder, confirmation, elapsed_label, time_label, subject_key)
from .store import ReminderStore


def setting(name, default, low=0.01, high=1000000):
    value = getattr(Config, name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return value if low <= value <= high else default


class ReminderService:
    def __init__(self, app):
        self.app = app
        self.store = ReminderStore(app.base_dir / 'logs' / 'reminders.json')
        self.task = None
        self.wakeup = None
        self.stopped = True
        self.inflight = set()
        self.startup = set()
        self.deferred = set()
        self._probe_failed = False

    async def start(self):
        if self.task is not None and not self.task.done():
            return
        if not getattr(Config, 'reminder_enabled', True):
            return
        self.stopped = False
        self.wakeup = asyncio.Event()
        try:
            load_result = self.store.load()
            if load_result == 'created':
                logger.info('未找到提醒存储文件，已创建空队列：logs/reminders.json')
            elif load_result == 'reset':
                logger.warning('提醒存储文件无效，已重建为空队列：logs/reminders.json')
            now = time.time()
            grace = setting('reminder_overdue_max_hours', 6) * 3600
            retained = []
            for item in self.store.items:
                if item.due_at < now - grace:
                    logger.info(f'提醒已过期，不再展示：{item.id} {item.subject}')
                else:
                    retained.append(item)
                    if item.due_at <= now:
                        self.startup.add(item.id)
            if len(retained) != len(self.store.items):
                self.store.replace(retained)
        except Exception:
            logger.warning('提醒存储初始化失败，禁用本次提醒服务', exc_info=True)
            self.stopped = True
            await output.show_status('提醒不可用：无法初始化提醒存储文件，请检查 logs/reminders.json。')
            return
        self.task = asyncio.create_task(self._run())

    def stop(self):
        self.stopped = True
        if self.task:
            self.app.loop.call_soon_threadsafe(self.task.cancel)

    def pending(self):
        return sorted((item for item in self.store.items if item.id not in self.inflight),
                      key=lambda item: (item.due_at, item.id))

    async def handle(self, role, content, anchor):
        """None 表示恢复普通角色处理；字符串表示本次已由提醒系统完整处理。"""
        if not getattr(Config, 'reminder_enabled', True) or not analyzer.may_be_reminder(content):
            return None
        if self.stopped or self.store.failed:
            message = '提醒未设置：提醒服务不可用，请检查日志后重试。'
            await output.show_status(message)
            return message
        timeout = setting('reminder_request_timeout', 60, high=600)
        try:
            # 此快照只包含事项与时刻，既不提供角色回复，也不引用历史对话。
            snapshot = [{'id': item.id, 'subject': item.subject, 'role': item.role_name,
                         'time': time_label(item.due_at, anchor)} for item in self.pending()]
            character = role.openmate_character or ''  # 不继承全局 character，留空就跟随 OpenMate
            states = []
            if character and role.openmate_enabled:
                try:
                    states = await to_thread(output.fetch_states, character)
                except Exception:
                    logger.info('提醒创建时无法获取 OpenMate 状态，使用无状态提醒')
            allowed = [state['id'] for state in states if isinstance(state, dict) and isinstance(state.get('id'), str)]
            result = await asyncio.wait_for(to_thread(analyzer.analyze, self.app.llm, role, content,
                                                      snapshot, states, anchor, timeout), timeout=timeout)
            if self.stopped or self.app.llm.monitor.should_stop():
                raise ReminderError('提醒未设置：本次提醒任务已取消。')
            operation = result.get('operation')
            if operation == 'none' and set(result) == {'operation'}:
                return None
            message = self._apply(result, role.display_name, character, allowed, anchor, time.time())
        except ReminderError as error:
            message = str(error)
        except OSError:
            logger.warning('提醒存储写入失败', exc_info=True)
            message = '提醒操作失败：无法保存提醒数据，请检查磁盘和文件权限。'
        except Exception:
            logger.warning('提醒语义解析失败', exc_info=True)
            message = PARSE_ERROR
        logger.info(message)
        await output.show_status(message)
        return message

    def _apply(self, data, role_name, character, allowed, anchor, now):
        operation = data.get('operation')
        if operation == 'create':
            item = create_reminder(data, role_name, character, allowed, anchor, now,
                                   setting('reminder_implicit_date_max_hours', 6),
                                   setting('reminder_min_lead_seconds', 15))
            for existing in self.store.items:
                if (existing.role_name == item.role_name and existing.due_at == item.due_at
                        and subject_key(existing.subject) == subject_key(item.subject)):
                    return confirmation(existing, now, '提醒已存在')
            maximum = int(setting('reminder_max_pending', 50, low=1, high=1000))
            if len(self.store.items) >= maximum:
                return f'提醒未设置：待执行提醒已达到 {maximum} 条，请先取消部分提醒。'
            self.store.replace(self.store.items + [item])
            self.wakeup.set()
            logger.info(f'创建提醒：{item.id}，到期时间戳 {item.due_at}')
            return confirmation(item, now)
        if operation == 'list' and set(data) == {'operation'}:
            items = self.pending()
            return ('待执行提醒：\n' + '\n'.join(
                f'{index + 1}. {time_label(item.due_at, now)}「{item.role_name}」{item.subject}'
                for index, item in enumerate(items))) if items else '当前没有待执行提醒。'
        if operation == 'cancel' and set(data) == {'operation', 'id'}:
            selected = next((item for item in self.pending() if item.id == data['id']), None)
            if selected is None:
                return '提醒未取消：没有找到唯一的待执行提醒，请查看提醒列表后明确指定。'
            self.store.replace([item for item in self.store.items if item.id != selected.id])
            self.startup.discard(selected.id)
            self.deferred.discard(selected.id)
            self.wakeup.set()
            logger.info(f'取消提醒：{selected.id}')
            return f'提醒已取消：{time_label(selected.due_at, now)}「{selected.role_name}」{selected.subject}'
        if operation == 'reject':
            reason = data.get('reason')
            if reason == 'vague_time' and set(data) == {'operation', 'reason', 'expression'}:
                expression = clean_text(data['expression'], 80)
                return f'提醒未设置：“{expression}”未明确指定时间，请给出具体时刻。'
            messages = {
                'subject': SUBJECT_ERROR,
                'recurring': '提醒未设置：不支持周期提醒，请指定一次具体时间。',
                'multiple': '提醒未设置：每次只支持创建一条提醒，请分别指定。',
                'edit_unsupported': '提醒未修改：不支持修改提醒，请取消原提醒后重新创建。',
                'cancel_ambiguous': '提醒未取消：请明确指定唯一一条提醒，可先查看提醒列表。',
            }
            if set(data) == {'operation', 'reason'} and reason in messages:
                return messages[reason]
        raise ReminderError(PARSE_ERROR)

    def _complete(self, ids):
        remaining = [item for item in self.store.items if item.id not in ids]
        try:
            self.store.replace(remaining)
        except OSError:
            # 已展示不能在每秒轮询中重复弹出；落盘失败保留磁盘快照，重启可能再提示一次。
            self.store.items = remaining
            self.store.failed = True
            logger.warning('已交付提醒的完成状态保存失败；本次不重播，重启可能重复', exc_info=True)
        self.startup.difference_update(ids)
        self.deferred.difference_update(ids)

    async def tick(self):
        now = time.time()
        items = [item for item in self.pending() if item.due_at <= now]
        if not items:
            return
        overdue = [item for item in items if item.id in self.startup]
        if overdue:
            ids = {item.id for item in overdue}
            self.inflight.update(ids)
            try:
                text = '软件关闭期间以下提醒已到期：\n' + '\n'.join(
                    f'{time_label(item.due_at, now)}「{item.role_name}」{item.subject}' for item in overdue)
                if await output.show_status(text):
                    self._complete(ids)
            finally:
                self.inflight.difference_update(ids)
            items = [item for item in items if item.id not in ids]
        if not items:
            return
        try:
            deferred = await to_thread(should_defer)
            self._probe_failed = False
        except Exception:
            if not self._probe_failed:
                logger.warning('提醒全屏/锁屏检测失败，保留待执行队列', exc_info=True)
            self._probe_failed = True
            deferred = True
        if deferred:
            self.deferred.update(item.id for item in items)
            return
        # 检测期间用户可能取消了提醒；交付起点再确认，随后从可取消列表移走。
        valid_ids = {item.id for item in self.pending()}
        items = [item for item in items if item.id in valid_ids]
        ids = {item.id for item in items}
        self.inflight.update(ids)
        try:
            delivered = await self._deliver(items)
            if delivered:
                self._complete(delivered)
        finally:
            self.inflight.difference_update(ids)

    async def _deliver(self, items):
        if not items or self.stopped:
            return set()
        now = time.time()
        roles = {item.id: self.app.llm.resolve_role(item.role_name) for item in items}
        remote = [item for item in items if getattr(Config, 'openmate_enabled', False)
                  and (roles[item.id] is None or roles[item.id].openmate_enabled)]
        delivered = set()
        if remote:
            client = output.openmate_client()
            try:
                if await to_thread(client.is_available, Config.openmate_health_timeout):
                    bodies = []
                    for item in remote:
                        body = {'id': item.id, 'text': item.delivery_text}
                        if item.openmate_character:
                            body['character'] = item.openmate_character
                        if item.openmate_character and item.openmate_state:
                            try:
                                states = await to_thread(client.get_states, item.openmate_character)
                                if item.openmate_state in [state['id'] for state in states.get('states', [])]:
                                    body['state'] = item.openmate_state
                            except Exception:
                                logger.info('提醒触发时无法核验状态，本次省略情绪')
                        bodies.append(body)
                    # 网络准备期间也可能进入全屏；尚未交付时仍可安全留在本地队列。
                    if await to_thread(should_defer):
                        self.deferred.update(item.id for item in items)
                        return set()
                    now = time.time()
                    for item, body in zip(remote, bodies):
                        if item.id in self.deferred or now - item.due_at >= 1:
                            body['text'] = f'<notts:{elapsed_label(now - item.due_at)}，>' + item.delivery_text
                    if self.stopped:
                        return set()
                    # 配置可调大待执行上限，仍遵守 OpenMate 的项数/UTF-16 总长度限制。
                    chunks = []
                    chunk, length = [], 0
                    for body in bodies:
                        size = len(body['text'].encode('utf-16-le')) // 2
                        if chunk and (len(chunk) == 200 or length + size > 65536):
                            chunks.append(chunk)
                            chunk, length = [], 0
                        chunk.append(body)
                        length += size
                    if chunk:
                        chunks.append(chunk)
                    for chunk in chunks:
                        if self.stopped or await to_thread(should_defer):
                            self.deferred.update(item.id for item in items if item.id not in delivered)
                            return delivered
                        response = await to_thread(client.post_reminder_batch, chunk, str(uuid.uuid4()))
                        if not response.get('id') or response.get('accepted') != len(chunk):
                            raise ValueError('OpenMate 批次回执无效')
                        delivered.update(body['id'] for body in chunk)
            except Exception:
                logger.warning('OpenMate 提醒交付失败，回退本地窗口；超时可能造成重复展示', exc_info=True)
        local = [item for item in items if item.id not in delivered]
        if local and not self.stopped:
            try:
                deferred = await to_thread(should_defer)
            except Exception:
                deferred = True
                logger.warning('本地回退前无法确认全屏状态，保留未交付项', exc_info=True)
            if deferred:
                self.deferred.update(item.id for item in local)
                return delivered
            now = time.time()
            lines = []
            for item in local:
                delayed = item.id in self.deferred or now - item.due_at >= 1 or len(local) > 1
                prefix = f'{elapsed_label(now - item.due_at)}，「{item.role_name}」：' if delayed else ''
                lines.append(prefix + item.delivery_text)
            same_role = len({item.role_name for item in local}) == 1
            role = roles[local[0].id] if same_role else None
            try:
                if await output.show_role('\n\n'.join(lines), role):
                    delivered.update(item.id for item in local)
            except Exception:
                logger.warning('提醒窗口创建失败，保留未交付项', exc_info=True)
        for item in items:
            if item.id in delivered:
                logger.info(f'提醒已交付：{item.id} {item.subject}')
        return delivered

    async def _run(self):
        try:
            while not self.stopped:
                self.wakeup.clear()
                try:
                    await self.tick()
                except Exception:
                    logger.warning('提醒调度失败，保留未交付提醒', exc_info=True)
                pending = self.pending()
                delay = max(1, min(60, pending[0].due_at - time.time())) if pending else 60
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
