"""提醒的纯数据与时间规则。模型只描述时间语义，本地代码决定最终时刻。"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import math
import re
import unicodedata
import uuid


class ReminderError(ValueError):
    """可以直接向用户展示的提醒拒绝原因。"""


def check_fields(data, expected, label):
    missing = sorted(expected - set(data))
    extra = sorted(set(data) - expected)
    if missing or extra:
        detail = ('缺少 ' + ', '.join(missing) if missing else '包含多余字段 ' + ', '.join(extra))
        raise ReminderError(f'提醒未设置：模型返回的{label}{detail[:160]}。')


PARSE_ERROR = '提醒未设置：无法可靠解析提醒请求，请重试。'
SUBJECT_ERROR = '提醒未设置：提醒内容不明确，请说明需要提醒的具体事项。'
DELIVERY_ERROR = '提醒未设置：角色提醒文案生成失败，请重试。'


@dataclass
class Reminder:
    id: str
    due_at: float
    subject: str
    delivery_text: str
    role_name: str
    openmate_character: str = ''
    openmate_state: str = ''

    def to_dict(self):
        return asdict(self)


def clean_text(value, limit, error=PARSE_ERROR, field=None):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ReminderError(f'提醒未设置：{field}必须是非空文本，且不超过 {limit} 字。' if field else error)
    if any(ord(c) < 32 for c in value) or '<' in value or '>' in value:
        raise ReminderError(f'提醒未设置：{field}包含换行、控制字符或标签。' if field else error)
    return ' '.join(value.split())


def integer(value, low, high, field='时间'):
    if type(value) is not int or not low <= value <= high:
        raise ReminderError(f'提醒未设置：{field} 必须是 {low} 到 {high} 之间的整数。')
    return value


def resolve_time(spec, anchor, now, implicit_hours=6, min_lead=15):
    if not isinstance(spec, dict) or not math.isfinite(anchor) or not math.isfinite(now):
        raise ReminderError('提醒未设置：时间对象或本地时间基准无效。')
    base = datetime.fromtimestamp(anchor)
    if spec.get('kind') == 'relative':
        check_fields(spec, {'kind', 'offset_seconds'}, '相对时间对象')
        offset = integer(spec['offset_seconds'], 1, 10 ** 9, 'offset_seconds（延迟秒数）')
        due = math.floor(anchor + offset)
    elif spec.get('kind') == 'clock':
        check_fields(spec, {'kind', 'day_offset', 'hour', 'minute', 'second'}, '时刻对象')
        hour = integer(spec['hour'], 0, 23, 'hour（小时）')
        minute = integer(spec['minute'], 0, 59, 'minute（分钟）')
        second = integer(spec['second'], 0, 59, 'second（秒）')
        day = spec['day_offset']
        target = base.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if day is None:
            if target.timestamp() <= anchor:
                target += timedelta(days=1)
            if target.timestamp() - anchor > implicit_hours * 3600:
                raise ReminderError(f'提醒未设置：未指定日期的时刻距离现在超过 {implicit_hours:g} 小时，请明确日期。')
        else:
            target += timedelta(days=integer(day, -36500, 36500, 'day_offset（日期偏移）'))
        due = target.timestamp()
    else:
        raise ReminderError('提醒未设置：时间类型 kind 缺失或不支持。')
    limit = (base.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=3)).timestamp()
    if due >= limit:
        raise ReminderError('提醒未设置：仅支持今天、明天和后天的提醒。')
    if due <= now:
        raise ReminderError('提醒未设置：指定时间已经过去，请指定未来时间。')
    if due - now < min_lead:
        raise ReminderError(f'提醒未设置：指定时间距离现在不足 {min_lead:g} 秒，请留出更多时间。')
    return due


def create_reminder(data, role_name, character, allowed_states, anchor, now, implicit_hours=6, min_lead=15):
    check_fields(data, {'operation', 'time', 'subject', 'delivery_text', 'openmate_state'}, '创建结果')
    subject = clean_text(data['subject'], 120, SUBJECT_ERROR, 'subject（提醒事项）')
    delivery = clean_text(data['delivery_text'], 300, DELIVERY_ERROR, 'delivery_text（角色文案）')
    state = data['openmate_state']
    if not isinstance(state, str) or (state and (not character or state not in allowed_states)):
        raise ReminderError('提醒未设置：openmate_state（情绪状态）类型错误或不在允许列表中。')
    return Reminder(str(uuid.uuid4()), resolve_time(data['time'], anchor, now, implicit_hours, min_lead),
                    subject, delivery, role_name, character, state)


def subject_key(subject):
    return re.sub(r'[\s，。！？、,.!?]+', '', unicodedata.normalize('NFKC', subject))


def time_label(due_at, now):
    target = datetime.fromtimestamp(due_at)
    days = (target.date() - datetime.fromtimestamp(now).date()).days
    day = {0: '今天', 1: '明天', 2: '后天'}.get(days, target.strftime('%Y-%m-%d'))
    return f'{day} {target:%H:%M:%S}'


def confirmation(reminder, now, prefix='提醒已设置'):
    return f'{prefix}：{time_label(reminder.due_at, now)}「{reminder.role_name}」会提醒你{reminder.subject}'


def elapsed_label(seconds):
    remaining = max(0, int(seconds))
    if remaining == 0:
        return '刚刚'
    parts = []
    for unit, size in [('天', 86400), ('小时', 3600), ('分钟', 60), ('秒', 1)]:
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f'{count}{unit}')
        if len(parts) == 2:
            break
    return ''.join(parts) + '前'
