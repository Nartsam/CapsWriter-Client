"""小规模 JSON 快照；创建/取消只有原子替换成功后才对调用方生效。"""
import json
import math
import os
from datetime import datetime
from pathlib import Path

from .model import Reminder, clean_text


class ReminderStore:
    def __init__(self, path):
        self.path = Path(path)
        self.items = []
        self.failed = False

    def load(self):
        self.failed = False
        if not self.path.exists():
            try:
                self.replace([])
            except Exception:
                self.failed = True
                raise
            return 'created'
        try:
            if self.path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('提醒文件超过大小限制')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('items'), list):
                raise ValueError('提醒文件结构无效')
            items = []
            ids = set()
            for raw in data['items']:
                item = Reminder(**raw)
                if (type(item.due_at) not in (int, float) or not math.isfinite(item.due_at)
                        or item.id in ids):
                    raise ValueError('提醒时间或 ID 无效')
                clean_text(item.id, 128)
                datetime.fromtimestamp(item.due_at)  # 有限数也可能超出平台可表示的日期范围
                clean_text(item.subject, 120)
                clean_text(item.delivery_text, 300)
                clean_text(item.role_name, 128)
                for value in (item.openmate_character, item.openmate_state):
                    if value:
                        clean_text(value, 64)
                    elif value != '':
                        raise ValueError('提醒角色状态无效')
                ids.add(item.id)
                items.append(item)
            self.items = items
            return 'loaded'
        except Exception:
            # 启动时必须落到一个可继续使用的完整快照；无效内容按空队列重建。
            self.items = []
            try:
                self.replace([])
            except Exception:
                self.failed = True
                raise
            return 'reset'

    def replace(self, items):
        if self.failed:
            raise OSError('提醒存储不可用')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        try:
            with temporary.open('w', encoding='utf-8', newline='\n') as stream:
                json.dump({'version': 1, 'items': [item.to_dict() for item in items]},
                          stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(self.path))
        finally:
            if temporary.exists():
                temporary.unlink()
        self.items = list(items)
