"""
LLM 常量配置

集中管理所有魔法数字、默认值和配置常量
"""

# ==================== 上下文管理常量 ====================
class ContextConstants:
    """上下文管理相关常量"""

    # Token 修剪阈值（使用 80% 阈值，保留 20% 给模型输出）
    TRIM_THRESHOLD_RATIO = 0.8

    # Token 估算常量
    CHARS_PER_TOKEN_CN = 1.5      # 中文字符：约 1.5 字符 = 1 token
    CHARS_PER_TOKEN_EN = 4.0      # 英文字符：约 4 字符 = 1 token

    # Unicode 中文字符范围
    CN_CHAR_START = '\u4e00'
    CN_CHAR_END = '\u9fff'

    # 单张图片按 DeepSeek 声明的每图上限计。图片在消息里是一个几十万字符的
    # base64 串，按字符估算会得出荒谬的 token 数，把历史裁剪逻辑整个冲垮
    IMAGE_TOKENS = 384


# ==================== RAG 常量 ====================
class RAGConstants:
    """热词 RAG 相关常量"""

    # 搜索参数
    DEFAULT_TOP_K = 5              # 默认返回前 5 个热词
    DEFAULT_THRESHOLD = 0.4        # 默认相似度阈值


# ==================== 文件监控常量 ====================
class WatcherConstants:
    """文件监控相关常量"""

    # 防抖延迟（秒）
    DEBOUNCE_DELAY = 3

    # 文件过滤
    PY_EXTENSION = '.py'
    INIT_FILE = '__init__.py'
    CACHE_DIR = '__pycache__'

    # 重载标记
    RELOAD_ALL_MARKER = '__reload_all__'


# ==================== 角色配置默认值 ====================


# ==================== API 配置 ====================
class APIConfig:
    """API 提供商配置"""

    # 默认 API URL
    DEFAULT_API_URLS = {
        'ollama': 'http://127.0.0.1:11434',
        'lmstudio': 'http://127.0.0.1:1234/v1',
        'openai': 'https://api.openai.com/v1',
        'deepseek': 'https://api.deepseek.com/v1',
        'moonshot': 'https://api.moonshot.cn/v1',
        'zhipu': 'https://open.bigmodel.cn/api/paas/v4',
        'volcengine': 'https://ark.cn-beijing.volces.com/api/v3',
        'cerebras': 'https://api.cerebras.ai/v1',
    }

    # 默认 API Keys
    DEFAULT_API_KEYS = {
        'ollama': 'ollama',
        'lmstudio': 'lmstudio',
        'openai': '',
        'deepseek': '',
        'moonshot': '',
        'zhipu': '',
        'cerebras': '',
    }

    # 请求超时配置（秒）
    # 本地模型第一次可能需要载入，时间稍长
    # 超过10秒可以认为网络有问题
    DEFAULT_TIMEOUTS = {
        'ollama': 2.0,       # 本地模型
        'lmstudio': 2.0,     # LM Studio 本地模型
        'openai': 2.0,       # OpenAI API
        'deepseek': 2.0,     # DeepSeek API
        'moonshot': 2.0,     # Moonshot API
        'zhipu': 2.0,        # 智谱 API
        'cerebras': 2.0,     # Cerebras API
        'claude': 2.0,       # Claude API
        'gemini': 2.0,       # Gemini API
    }

    # 默认超时（用于未列出的 provider）
    DEFAULT_TIMEOUT = 2.0


# ==================== 服务端内置工具 ====================
class ToolNames:
    """服务端内置工具的展示文案

    工具 ID 取自 Responses API 的事件名 `response.<工具ID>_call.<阶段>`，
    未登记的新工具一律回退到原始 ID，宁可显示得难看也不要静默丢失
    """

    # 控制台「模型结果[助理(搜索)]」里的短标签
    DISPLAY = {
        'web_search': '搜索',
    }

    # 工具执行期间的占位文案（此时还没有任何正文分片）
    STATUS = {
        'web_search': '正在联网搜索…',
    }

    @classmethod
    def display(cls, tool_id: str) -> str:
        return cls.DISPLAY.get(tool_id, tool_id)

    @classmethod
    def status(cls, tool_id: str) -> str:
        return cls.STATUS.get(tool_id, f'正在调用 {tool_id}…')


# 图片在纯文本场合（对话历史里的旧图、日记 md、UDP 广播）的占位表示。
# 历史里只保留最近一张真图，更早的一律降级成它——多轮追问「那第三行呢」需要
# 模型还能看见最近这张，而每轮重传全部历史图片是纯浪费（见约定 21）
IMAGE_PLACEHOLDER = '［图片］'


# ==================== Token 估算工具 ====================
def _estimate_part_tokens(part) -> int:
    """估算多模态 content 列表里单个条目的 token 数"""
    if isinstance(part, str):
        return estimate_tokens(part)
    if isinstance(part, dict):
        # Chat Completions 用 image_url，Responses API 用 input_image
        if part.get('type') in ('image_url', 'input_image'):
            return ContextConstants.IMAGE_TOKENS
        return estimate_tokens(part.get('text') or '')
    return 0


def estimate_tokens(text) -> int:
    """
    估算文本的 token 数量

    Args:
        text: 待估算的文本；带图消息的 content 是条目列表而非字符串

    Returns:
        估算的 token 数量
    """
    if not text:
        return 0

    # 带图消息的 content 形如 [{'type':'text',...}, {'type':'image_url',...}]，
    # 落到下面的逐字符迭代会拿 dict 去和字符比大小，直接 TypeError。历史里一旦
    # 存进一条带图消息，此后每次裁剪都会炸（见 docs/DEV_NOTES.md）
    if isinstance(text, (list, tuple)):
        return max(sum(_estimate_part_tokens(part) for part in text), 1)
    if not isinstance(text, str):
        return 1

    # 统计中文字符数量
    chinese_chars = sum(
        1 for c in text
        if ContextConstants.CN_CHAR_START <= c <= ContextConstants.CN_CHAR_END
    )

    # 统计非中文字符数量
    other_chars = len(text) - chinese_chars

    # 中文字符：约 1.5 字符 = 1 token
    # 英文和其他字符：约 4 字符 = 1 token
    tokens = int(
        chinese_chars / ContextConstants.CHARS_PER_TOKEN_CN +
        other_chars / ContextConstants.CHARS_PER_TOKEN_EN
    )

    return max(tokens, 1)  # 至少 1 个 token
