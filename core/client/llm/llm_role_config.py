"""
LLM 角色配置 Dataclass

使用 Dataclass 替代字典，提供类型安全和更好的 IDE 支持
"""
from dataclasses import dataclass, field
from typing import Dict, Any, Optional


@dataclass
class RoleConfig:
    """角色配置

    包含单个角色的所有配置信息
    """
    # 静态常量
    DEFAULT_ROLE_NAME = '默认'

    # 基本信息
    name: str = ''                               # 角色显示名称
    module_name: str = ""                         # 模块名称（如 "LLM.翻译"）
    enabled: bool = True                          # 是否启用此角色

    # API 配置
    provider: str = 'ollama'                      # API 提供商
    api_url: str = ''                             # API 地址
    api_key: str = ''                             # API Key
    model: str = 'gemma3:4b'                      # 模型名称
    enable_vision: bool = False                   # 该角色能否接收图片。为真时，待挂图片会随
                                                  # 这句话一并上传；**不校验 model 是否真支持
                                                  # 视觉**——写错了服务端会返 400，约定 18 的
                                                  # 诊断日志会把错误正文打到终端

    # 上下文管理
    max_context_length: int = 4096                # 最大上下文长度（token 数）

    # 功能配置
    enable_thinking: bool = False                 # 是否启用思考（仅 Ollama 支持）
    enable_history: bool = False                  # 是否保留对话历史
    enable_hotwords: bool = False                 # 是否读取潜在热词列表
    enable_read_selection: bool = False           # 是否读取鼠标所选文字（通过 Ctrl+C）
    selection_max_length: int = 1000              # 选中文字最大长度
    enable_web_search: bool = False               # 联网搜索：改走 Responses API 并挂载服务端内置
                                                  # web_search 工具，由模型自行决定是否检索
                                                  # （目前仅 DeepSeek 的 deepseek-v4-flash 支持）
    enable_datetime: bool = False                 # 在用户消息里注入当前日期时间，供模型判断时效性
    request_timeout: float = 0                    # 单次请求超时（秒），0 表示用 provider 默认值；
                                                  # 联网搜索在首个正文分片前会静默数秒，必须调大

    # 输出配置
    output_mode: str = 'typing'                   # 输出方式: 'typing' 或 'toast' (即打字输出或弹窗输出)

    # Toast 弹窗配置
    toast_min_width_px: Optional[int] = None      # Toast 最小宽度像素值（合法时优先）
    toast_max_width_px: Optional[int] = None      # Toast 最大宽度像素值（合法时优先）
    toast_min_width_ratio: Optional[float] = None  # 最小宽度比例回退值
    toast_max_width_ratio: Optional[float] = None  # 最大宽度比例回退值
    toast_initial_height: int = 0                 # Toast 窗口初始高度（0 表示自动计算）
    toast_font_family: str = '微软雅黑'           # Toast 字体
    toast_font_size: int = 16                     # Toast 字体大小（角色缺省值）
    toast_font_color: str = 'black'               # Toast 字体颜色
    toast_bg_color: str = '#f7fbff'               # Toast 背景颜色
    toast_duration: int = 3000                    # Toast 显示时长（毫秒）
    toast_editable: bool = False                  # Toast 是否可编辑（Markdown 渲染后）
    toast_window_alpha: float = 0.92              # Toast 窗口透明度，营造半透明亚克力效果
    toast_corner_radius: int = 12                 # Toast 窗口圆角半径，0 表示不启用圆角

    # OpenMate 桌面看板配置（仅 output_mode='toast' 时有效，需全局 openmate_enabled）
    openmate_enabled: bool = True                 # 允许该角色使用 OpenMate 展示
    openmate_character: str = ''                  # 覆盖全局的 OpenMate 角色 ID（留空用全局配置，
                                                  # 情绪状态列表按最终角色通过 HTTP 自动获取）
    enable_result_splitter: bool = False          # 回复分段：按分隔符把回复切成多条消息逐条展示，
                                                  # 模仿人类连发多条 IM 消息（仅 OpenMate 展示路径
                                                  # 生效；未连接 OpenMate 时回退普通完整显示）
    result_split_delimiter: str = '\n\n'          # 分段分隔符；段落 trim 后为空则丢弃

    # 生成参数
    temperature: float = 0.7                      # 温度（0-2）
    top_p: float = 0.9                            # Top-p 采样
    max_tokens: int = 1024                        # 最大输出 token 数（0 表示使用模型默认值）
    stop: str = ''                                # 停止序列

    # 高级选项
    extra_options: Dict[str, Any] = field(default_factory=dict)  # 额外的 API 参数

    # 提示词前缀
    prompt_prefix_hotwords: str = '热词列表：'      # 热词列表前缀
    prompt_prefix_selection: str = '选中文字：'     # 选中文字前缀
    prompt_prefix_datetime: str = '当前时间：'      # 当前时间前缀
    prompt_prefix_input: str = '用户输入：'         # 用户输入前缀

    # System Prompt
    system_prompt: str = ''                       # 系统提示词

    @property
    def display_name(self) -> str:
        """显示名称：name 按 | 分割的第一部分"""
        parts = [p.strip() for p in self.name.split('|')] if self.name else ['']
        return parts[0]

    @property
    def names(self) -> list:
        """所有名称列表：name 按 | 分割的所有非空部分"""
        parts = [p.strip() for p in self.name.split('|')] if self.name else ['']
        return [p for p in parts if p]

