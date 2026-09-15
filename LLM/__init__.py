"""
LLM 角色模块 - 仅用于角色配置

此文件夹主要存放角色配置文件。另有两个非角色模块：secrets.py（API 密钥，未被 git
跟踪）与 backends.py（角色的后端切换）。RoleLoader 会尝试导入本目录下每个 .py，
缺少 provider/model 的就记一条错误后跳过，所以放非角色模块是安全的
"""

# 此文件为空，仅用于标识 LLM 为 Python 包，请勿删除本文件



"""
# 角色配置模板

# 基本信息
name: str = ""                               # 角色显示名称
enabled: bool = True                         # 是否启用此角色

# API 配置
provider: str = 'ollama'                     # API 提供商
api_url: str = ''                            # API 地址
api_key: str = ''                            # API Key
model: str = 'gemma3:4b'                     # 模型名称

# 上下文管理
max_context_length: int = 4096               # 最大上下文长度（token 数）

# 功能配置
enable_thinking: bool = False                # 是否启用思考（仅 Ollama 支持）
enable_history: bool = False                 # 是否保留对话历史
enable_hotwords: bool = False                # 是否读取潜在热词列表
enable_read_selection: bool = False          # 是否读取鼠标所选文字（默认 UI Automation）
selection_max_length: int = 1000             # 选中文字最大长度

# 输出配置
output_mode: str = 'typing'                  # 输出方式: 'typing' 或 'toast' (即打字输出或弹窗输出)

# Toast 弹窗配置
toast_min_width_px: int = 320                # 最小宽度像素值；正数时优先于比例配置
toast_max_width_px: int = 800                # 最大宽度像素值；正数时优先于比例配置
toast_min_width_ratio: float = 0.2           # 最小宽度比例回退值（像素值无效时使用）
toast_max_width_ratio: float = 0.5           # 最大宽度比例回退值（像素值无效时使用）
toast_initial_height: int = 0                # Toast 窗口初始高度（0 表示自动计算）
toast_font_family: str = '微软雅黑'          # Toast 字体
toast_font_size: int = 30                     # Toast 字体大小
toast_font_color: str = 'black'              # Toast 字体颜色
toast_bg_color: str = '#f7fbff'              # Toast 背景颜色
toast_duration: int = 3000                   # Toast 显示时长（毫秒）
toast_editable = True                        # 是否可编辑（Markdown 渲染后）
toast_window_alpha: float = 0.92             # Toast 窗口透明度，营造半透明亚克力效果
toast_corner_radius: int = 12                # Toast 窗口圆角半径

# 生成参数
temperature: float = 0.7                     # 温度（0-2）
top_p: float = 0.9                           # Top-p 采样
max_tokens: int = 1024                       # 最大输出 token 数（0 表示使用模型默认值）
stop: str = ''                               # 停止序列

# 高级选项
extra_options: Dict[str, Any] = field(default_factory=dict)  # 额外的 API 参数

# 提示词前缀
prompt_prefix_hotwords: str = '热词列表：'      # 热词列表前缀
prompt_prefix_selection: str = '选中文字：'     # 选中文字前缀
prompt_prefix_input: str = '用户输入：'         # 用户输入前缀

# System Prompt
system_prompt: str = ''                      # 系统提示词

# OpenMate
openmate_enabled: bool = True  # 默认 True
openmate_character: str = ''   # 默认空
# 新增的 OpenMate 接入项，没写就用默认值：允许 OpenMate 接管、角色 ID 用全局配置。
# 想让助理永远只用本地弹窗，加一行 openmate_enabled = False 即可。

"""

