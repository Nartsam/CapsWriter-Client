"""
LLM 上下文管理器

功能：
1. 管理对话历史。给了 store 时每次写入后整体落盘（logs/llm_memory/，格式见 llm_memory），
   重启时由 restore() 读回；不给则纯内存（测试与探针）
2. 基于 token 估算修剪历史长度（超过 max_length 的 80% 时从最旧的消息开始丢弃）

注意：历史没有按时间过期的清理逻辑，有意如此——记忆一直保留到用户在托盘里「清除记忆」。
last_interaction 仅作记录、从未被检查。
"""

import time
from typing import Dict, List, Optional, Tuple
from threading import Lock
from .llm_constants import ContextConstants, estimate_tokens, IMAGE_PLACEHOLDER
from .llm_memory import MemoryFileError
from . import logger


def _flatten_content(content: list, placeholder: str) -> str:
    """把带图消息的条目列表压成纯文本，图片换成占位符"""
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get('type') in ('image_url', 'input_image'):
            parts.append(placeholder)
        elif isinstance(part, dict):
            parts.append(part.get('text') or '')
        elif isinstance(part, str):
            parts.append(part)
    return '\n'.join(p for p in parts if p)


class ContextManager:
    """对话上下文管理器"""

    def __init__(self, max_length: int = 2000, store=None, key: str = '', display_name: str = ''):
        """
        Args:
            max_length: 历史容量（估算 token）
            store: MemoryStore；None 表示不落盘
            key: 记忆文件名，即角色 .py 文件名（不含扩展名）
            display_name: 角色显示名，只用于日志和写给人看的文件内容
        """
        self.max_length = max_length
        self.history = []
        self.last_interaction = None
        self._lock = Lock()
        self._store = store
        self.key = key
        self.display_name = display_name

    def add_message(self, role: str, content: str):
        """添加消息到历史"""
        with self._lock:
            timestamp = time.time()
            self.history.append({
                'timestamp': timestamp,
                'role': role,
                'content': content,
            })
            self.last_interaction = timestamp
            self._trim_history()
            self._save_locked()

    def add_turn(self, user_content, assistant_content: str):
        """写入一整轮对话。两条一起追加、只落盘一次，磁盘上不会出现只有提问的半轮"""
        with self._lock:
            timestamp = time.time()
            self.history.append({'timestamp': timestamp, 'role': 'user', 'content': user_content})
            self.history.append({'timestamp': timestamp, 'role': 'assistant', 'content': assistant_content})
            self.last_interaction = timestamp
            self._trim_history()
            self._save_locked()

    def demote_images(self, placeholder: str) -> int:
        """把历史里所有图片条目降级成占位文本，只应在写入新的带图消息前调用

        为什么只留最近一张：截图提问天然是多轮的（「这什么报错」→「那第三行呢」），
        模型必须还能看见最近那张；但把每一轮的图都留着，每次请求都要重传全部图片，
        而历史没有按时间过期的清理，图会一直占着内存直到被 token 压力挤出去。

        调用方（llm_processor._record_turn）在响应结束、写入本轮之前才调用，而本轮
        请求早已用降级前的历史组装好，所以换图那一轮的请求载荷里有两张真图（历史里
        一张、本句一张，分属两条 user 消息）。这是有意保留的：提前降级会让「连续发
        两张图时第二轮还能看见第一张」的能力消失，代价只是多传一张图。

        Returns:
            被降级的图片数量
        """
        demoted = 0
        with self._lock:
            for msg in self.history:
                content = msg.get('content')
                if not isinstance(content, list):
                    continue
                demoted += sum(
                    1 for part in content
                    if isinstance(part, dict) and part.get('type') in ('image_url', 'input_image')
                )
                # 降级后整条消息退回纯字符串，历史里因此最多只有一条列表形态的消息
                msg['content'] = _flatten_content(content, placeholder)
        return demoted

    def restore(self) -> Tuple[int, Optional[str]]:
        """从磁盘读回记忆，只在新建管理器后调用一次

        Returns:
            (恢复的轮数, 失败原因)。没有记忆文件时为 (0, None)
        """
        if self._store is None:
            return 0, None
        try:
            history = self._store.load(self.key)
        except MemoryFileError as e:
            logger.info(f"角色「{self.display_name}」的记忆文件无法使用，从空记忆开始：{e}")
            self._quarantine()
            return 0, e.reason
        if not history:
            return 0, None
        with self._lock:
            self.history = history
            self.last_interaction = history[-1]['timestamp']
            # max_context_length 可能在上次运行之后被调小，读回来先按新上限裁剪
            self._trim_history()
            rounds = sum(1 for msg in self.history if msg['role'] == 'user')
        logger.info(f"角色「{self.display_name}」已恢复 {rounds} 轮对话（{len(self.history)} 条消息）")
        return rounds, None

    def _quarantine(self):
        """把读不了的记忆文件改名保留。改名也失败时本次运行不再保存这个角色，
        否则下一轮保存会把那份文件覆盖掉"""
        try:
            target = self._store.quarantine(self.key)
            logger.info(f"原记忆文件已改名保留为 {target.name}")
        except OSError as e:
            self._store = None
            logger.warning(f"角色「{self.display_name}」的记忆文件无法改名保留，本次运行不再保存它的记忆：{e}")

    def _save_locked(self):
        """整体落盘。调用方须持有 _lock：写盘顺序因此与内存里的修改顺序一致"""
        if self._store is None:
            return
        # 图片在磁盘上只存占位符：真图以 base64 存放，最大约 4MB，换图前每轮都要重写一遍；
        # 重启恰好发生在追问同一张图的那几分钟里的情况很少，不值得
        snapshot = [
            {
                'timestamp': msg['timestamp'],
                'role': msg['role'],
                'content': (_flatten_content(msg['content'], IMAGE_PLACEHOLDER)
                            if isinstance(msg['content'], list) else msg['content']),
            }
            for msg in self.history
        ]
        try:
            self._store.save(self.key, self.display_name, snapshot)
        except (OSError, TypeError, ValueError) as e:
            # 回复已经展示过了，保存失败不能让这一轮报错；内存里的记忆照常使用
            logger.warning(f"角色「{self.display_name}」的记忆保存失败：{e}")

    def clear(self):
        """清除内存里的对话历史。磁盘上的记忆文件由 LLMHandler.clear_history 统一删除"""
        with self._lock:
            self.history = []
            self.last_interaction = None
            logger.info("对话历史已清除")

    def get_history(self) -> List[Dict]:
        """获取对话历史（只含 role/content 的副本）

        组装请求时只能用它、不要直接读 history：原条目还带 timestamp 字段，会随
        Responses 请求原样上行；而且 demote_images() 是原地修改的，交出活引用会互相影响。
        """
        with self._lock:
            return [
                {'role': msg['role'], 'content': msg['content']}
                for msg in self.history
            ]

    def _estimate_tokens(self, text: str) -> int:
        """估算文本的 token 数量（使用统一的常量）"""
        return estimate_tokens(text)

    def _trim_history(self):
        """基于 token 数量智能修剪历史

        策略：
        1. 如果超过限制的 80%，立即清理
        2. 保留 20% 空间给模型思考输出
        3. 从最早的消息开始删除，优先保留最近的对话
        """
        if not self.history:
            return

        # 计算当前历史的总 token 数
        total_tokens = sum(
            self._estimate_tokens(msg['content'])
            for msg in self.history
        )

        # 使用配置的阈值，保留空间给模型输出
        target_tokens = int(self.max_length * ContextConstants.TRIM_THRESHOLD_RATIO)

        # 如果总 token 数在阈值范围内，不需要修剪
        if total_tokens <= target_tokens:
            return

        threshold_percent = int(ContextConstants.TRIM_THRESHOLD_RATIO * 100)
        logger.info(f"[上下文裁剪] 当前 {total_tokens} tokens ({total_tokens/self.max_length*100:.1f}%)，触发清理（阈值：{target_tokens} tokens, {threshold_percent}%）")

        # 从头部（最旧的消息）开始删除，直到满足阈值限制
        removed_count = 0
        while self.history and total_tokens > target_tokens:
            # 删除最旧的消息
            removed_msg = self.history.pop(0)
            removed_tokens = self._estimate_tokens(removed_msg['content'])
            total_tokens -= removed_tokens
            removed_count += 1

        # 调试信息：打印裁剪结果
        if len(self.history) > 0:
            final_tokens = sum(self._estimate_tokens(msg['content']) for msg in self.history)
            logger.info(f"[上下文裁剪] 已删除 {removed_count} 条旧消息，保留 {len(self.history)} 条消息")
            logger.info(f"[上下文裁剪] 当前约 {final_tokens} tokens ({final_tokens/self.max_length*100:.1f}%)，剩余空间 {self.max_length - final_tokens} tokens")
