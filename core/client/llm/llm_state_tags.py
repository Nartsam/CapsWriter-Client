# coding: utf-8
"""
OpenMate 内联状态标签的识别与剥离

标签形如 `<state:微笑>`，由 OpenMate 的情绪指令要求模型输出，用来驱动立绘。
它是**展示层指令**：只有 OpenMate 那条路径需要看见它，本地 Toast、打字输出、
md 记录、UDP 广播都必须把它去掉。

为什么单独成模块而不是留在 llm_output_openmate 里：剥离曾经只发生在两个地方
（OpenMate 输出、写对话历史），2026-09-04 查明「写历史时剥离」正是情绪标签只在
第一轮出现的根因——历史里的 assistant 消息永远不带标签，模型从第二轮起照抄自己
上一条的形态，few-shot 漂移压过了 system 指令。修法是把剥离从记忆层移到展示层，
于是本地两条输出路径也要用到它，再从 OpenMate 模块里反向导入就说不通了。
"""

import re

# 与 OpenMate 文档 §4.4 的约束一致（非空、≤64 字符、无控制字符和嵌套 <）
STATE_TAG_RE = re.compile(r'<(?:state|状态):[^<>\x00-\x1f]{1,64}>')

_TAG_HEADS = ('state:', '状态:')
_MAX_BODY = 64
# 暂存上限：'<' + 最长的头 + 正文上限。超过它就绝无可能再补成标签，必须放行，
# 否则模型输出一个孤立的 '<' 之后的正文会被无限缓冲
_MAX_PENDING = 1 + max(len(h) for h in _TAG_HEADS) + _MAX_BODY
_HEAD_PREFIXES = frozenset(h[:i] for h in _TAG_HEADS for i in range(len(h) + 1))
_TAG_BREAKERS = re.compile(r'[<>\x00-\x1f]')


def strip_state_tags(text: str) -> str:
    """剥离状态标签。只适用于完整文本，流式分片请用 StateTagFilter"""
    return STATE_TAG_RE.sub('', text) if text else text


class StateTagFilter:
    """流式安全的状态标签剥离器

    直接对每个分片调用 `strip_state_tags` 是不够的：`<state:微笑>` 会被切成
    `<sta` + `te:微笑>` 两片，两片各自都不匹配，标签就原样漏出去了。打字模式下
    这意味着一个 `<state:微笑>` 被真的敲进用户正在编辑的文档里。

    做法是只暂存**可能还会补成标签**的那截尾巴，其余立即放行。用法：

        f = StateTagFilter()
        for chunk in stream:
            visible = f.feed(chunk)   # 可能是空串
        visible = f.finish()          # 冲掉残留
    """

    def __init__(self):
        self._pending = ''

    def feed(self, chunk: str) -> str:
        """喂入一个分片，返回此刻可以安全放行的文本（可能为空串）"""
        if not chunk:
            return ''
        buf = STATE_TAG_RE.sub('', self._pending + chunk)
        cut = self._hold_from(buf)
        self._pending = buf[cut:]
        return buf[:cut]

    def finish(self) -> str:
        """流结束，交出暂存的尾巴

        残留只可能是没能补全的半截标签或普通的 `<` 开头文本。原样交出而不是丢弃：
        流被 ESC 中途掐断时，丢弃会让用户看到的文本比模型实际生成的少一截。
        """
        tail, self._pending = self._pending, ''
        return tail

    @staticmethod
    def _hold_from(buf: str) -> int:
        """返回需要暂存的起始下标；没有可疑尾巴时返回 len(buf)

        只需检查**最后一个** `<`：标签正文里不允许再出现 `<`，所以一旦某个 `<`
        后面又出现了 `<`，前面那个就已经死了，不可能再补成标签。
        """
        i = buf.rfind('<')
        if i == -1:
            return len(buf)
        tail = buf[i:]
        if len(tail) > _MAX_PENDING:
            return len(buf)
        body = tail[1:]
        if body in _HEAD_PREFIXES:
            return i
        for head in _TAG_HEADS:
            if body.startswith(head):
                rest = body[len(head):]
                if len(rest) <= _MAX_BODY and not _TAG_BREAKERS.search(rest):
                    return i
                break
        return len(buf)
