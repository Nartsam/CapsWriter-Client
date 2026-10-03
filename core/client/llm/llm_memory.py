"""
LLM 角色记忆的落盘存储

每个开启了历史的角色一个 JSON 文件，放在 logs/llm_memory/ 下，按角色 .py 文件名命名：
显示名会随改称呼、调整别名顺序而变，文件名不会，且由文件系统保证唯一。

何时写、何时读由 ContextManager 决定（每轮写入记忆后整体重写一次，启动时读回），
本模块只管文件格式与读写安全：先写临时文件再整体替换，不会留下半个文件；读不了的
文件改名保留，免得下一轮保存把它覆盖掉。
"""
import json
import math
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

FORMAT_VERSION = 1
_MESSAGE_ROLES = ('user', 'assistant')


class MemoryFileError(Exception):
    """记忆文件存在但无法使用

    Attributes:
        reason: 给角色表看的简短原因（不超过 5 个字），详细信息在异常文本里
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}：{detail}")
        self.reason = reason


def _validate(data) -> List[Dict]:
    """校验文件结构，返回历史条目；结构不对抛 ValueError"""
    if not isinstance(data, dict) or data.get('version') != FORMAT_VERSION:
        raise ValueError('结构或格式版本不符')
    history = data.get('history')
    if not isinstance(history, list):
        raise ValueError('缺少 history 列表')
    result = []
    for item in history:
        if (not isinstance(item, dict) or item.get('role') not in _MESSAGE_ROLES
                or not isinstance(item.get('content'), str)):
            raise ValueError('消息条目无效')
        timestamp = item.get('timestamp')
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp)):
            raise ValueError('时间戳无效')
        result.append({'timestamp': timestamp, 'role': item['role'], 'content': item['content']})
    return result


class MemoryStore:
    """logs/llm_memory/ 目录下的角色记忆文件"""

    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, key: str) -> Path:
        """key 是角色 .py 文件名（不含扩展名）"""
        return self.directory / f"{key}.json"

    def load(self, key: str) -> Optional[List[Dict]]:
        """读取历史

        Returns:
            历史条目；文件不存在时为 None

        Raises:
            MemoryFileError: 文件存在但读不出来或内容无效
        """
        path = self.path(key)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise MemoryFileError('无法读取', repr(e))
        try:
            return _validate(json.loads(raw.decode('utf-8')))
        except ValueError as e:     # 含 UnicodeDecodeError 与 JSONDecodeError
            raise MemoryFileError('文件已损坏', str(e))

    def save(self, key: str, display_name: str, history: List[Dict]) -> None:
        """整体重写一个角色的记忆。history 的 content 须已是纯文本（图片换成了占位符）

        display_name 只是写给人看的，读取时不用它认角色。
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(key)
        temporary = path.with_name(path.name + '.tmp')
        try:
            with temporary.open('w', encoding='utf-8', newline='\n') as stream:
                json.dump({'version': FORMAT_VERSION, 'display_name': display_name,
                           'history': history},
                          stream, ensure_ascii=False, indent=1)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()

    def quarantine(self, key: str) -> Path:
        """把无法使用的记忆文件改名保留，返回新路径；改名失败抛 OSError"""
        path = self.path(key)
        target = path.with_name(f"{key}.bad-{time.strftime('%Y%m%d-%H%M%S')}.json")
        os.replace(str(path), str(target))
        return target

    def clear(self) -> List[str]:
        """删除目录下的所有文件，包括暂时没有角色在用的、改名保留的损坏文件

        Returns:
            删除失败的文件名
        """
        try:
            entries = list(self.directory.iterdir())
        except FileNotFoundError:
            return []
        failed = []
        for entry in entries:
            if not entry.is_file():
                continue
            try:
                entry.unlink()
            except OSError:
                failed.append(entry.name)
        return failed
