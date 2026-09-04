"""
LLM 上下文管理器

功能：
1. 管理对话历史（纯内存，不落盘，客户端重启即清空）
2. 基于 token 估算修剪历史长度（超过 max_length 的 80% 时从最旧的消息开始丢弃）

注意：历史没有按时间过期的清理逻辑，last_interaction 仅作记录、从未被检查。
"""

import time
from typing import Dict, List
from threading import Lock
from .llm_constants import ContextConstants, estimate_tokens
from . import logger


class ContextManager:
    """对话上下文管理器"""

    def __init__(self, max_length: int = 2000):
        self.max_length = max_length
        self.history = []
        self.last_interaction = None
        self._lock = Lock()

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

    def demote_images(self, placeholder: str) -> int:
        """把历史里所有图片条目降级成占位文本，只应在写入新的带图消息前调用

        为什么只留最近一张：截图提问天然是多轮的（「这什么报错」→「那第三行呢」），
        模型必须还能看见最近那张；但把每一轮的图都留着，每次请求都要重传全部图片，
        而历史没有按时间过期的清理，图会一直占着内存直到被 token 压力挤出去。

        Returns:
            被降级的图片数量
        """
        demoted = 0
        with self._lock:
            for msg in self.history:
                content = msg.get('content')
                if not isinstance(content, list):
                    continue
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get('type') in ('image_url', 'input_image'):
                        parts.append(placeholder)
                        demoted += 1
                    elif isinstance(part, dict):
                        parts.append(part.get('text') or '')
                    elif isinstance(part, str):
                        parts.append(part)
                # 降级后整条消息退回纯字符串，历史里因此最多只有一条列表形态的消息
                msg['content'] = '\n'.join(p for p in parts if p)
        return demoted

    def clear(self):
        """清除对话历史"""
        with self._lock:
            self.history = []
            self.last_interaction = None
            logger.info("对话历史已清除")

    def get_history(self) -> List[Dict]:
        """获取对话历史"""
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
