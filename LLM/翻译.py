"""
翻译助手角色
"""
from LLM.backends import resolve

# ==================== 基本信息 ====================
name = '翻译'                           # 角色名称（留空表示默认）
enabled = False                           # 是否启用此角色

# ==================== API 配置 ====================
backend = 'deepseek'                                # 后端：'deepseek'（直连）或 'cpa'（局域网网关）。模型 / 地址 / 超时见 LLM/backends.py
backend_config = resolve(backend)
provider = backend_config['provider']               # API 提供商
api_url = backend_config['api_url']                 # API 地址
api_key = backend_config['api_key']                 # API Key
model = backend_config['model']                     # 模型名称
api_format = backend_config['api_format']           # 请求格式（OpenAI / Anthropic），随后端而定
enable_vision = True                                # 接收 vision_capture_key 挂载的图片。注意本项不会校验 model 是否真支持视觉，写错了服务端会返 400

# ==================== 上下文管理 ====================
max_context_length = 128*1024               # 最大上下文长度（token 数）

# ==================== 功能配置 ====================
enable_hotwords = False                 # 是否启用热词
enable_thinking = False                 # 是否启用思考（关闭时直接回答、更快；Ollama 与 DeepSeek 生效）
enable_history = False                  # 是否保留对话历史
enable_read_selection = True            # 是否启用获取选中文字（默认 UI Automation）
selection_max_length = 2048             # 选中文字最大长度
request_timeout = backend_config['request_timeout'] # 单次请求超时。两套后端都远超 provider 默认的 2s：DeepSeek 检索期数秒无输出，CPA 中位 3~7s、带图约 20s

# ==================== 输出配置 ====================
output_mode = 'toast'                   # 输出方式：'typing' 直接打字, 'toast' 浮动窗口

# ==================== Toast 弹窗配置（仅在 output_mode='toast' 时有效） ====================
toast_min_width_px = 0                # 最小宽度像素值；正数时优先于比例配置
toast_max_width_px = 0                # 最大宽度像素值；正数时优先于比例配置
toast_min_width_ratio = 0.3             # 最小宽度比例回退值（像素值无效时使用）
toast_max_width_ratio = 0.5             # 最大宽度比例回退值（像素值无效时使用）
toast_initial_height = 0                # 窗口初始高度（0 表示自动计算）
toast_font_family = 'Consolas'
toast_font_size = 18                     # 字体大小
toast_font_color = 'black'              # 字体颜色
toast_bg_color = '#f7fbff'              # 背景颜色
toast_duration = 3000                   # 显示时长（毫秒）
toast_editable = True                   # 是否可编辑（Markdown 渲染后）
toast_window_alpha = 0.92               # 窗口透明度，营造半透明亚克力效果
toast_corner_radius = 12                # 窗口圆角半径

# ==================== 生成参数 ====================
temperature = 0.2                       # 翻译任务降低随机性，减少措辞漂移
top_p = 0.9                             # Top-p 采样（0-1）
max_tokens = 4096                       # 最大输出 token 数
stop = ''                               # 停止序列

# ==================== 高级选项 ====================
extra_options = {}                      # 额外的 API 参数（JSON 格式）

# ==================== 提示词前缀 ====================
prompt_prefix_hotwords = '热词列表：'    # 热词列表前缀
prompt_prefix_selection = '选中文字：'   # 选中文字前缀
prompt_prefix_input = '用户输入：'       # 用户输入前缀

# ==================== System Prompt ====================
system_prompt = '''
你是一名专业译者。你的目标不是逐字替换，而是在完整保留原意、事实、语气和信息量的前提下，写出目标语言母语者自然会使用的表达。

输入规则：
- 如果提供了“选中文字”，它就是需要翻译的原文；“用户输入”只用于说明目标语言、语气、用途或其他要求，不要把这部分一起翻译
- 如果没有“选中文字”，“用户输入”就是需要翻译的原文
- 用户明确指定目标语言时，以用户要求为准
- 用户未指定目标语言时：中文原文翻译成自然英文；其他语言原文翻译成简体中文

翻译要求：
- 先理解整段含义，再用目标语言自然重写；不要机械保留原文语序、句式和搭配
- 优先采用目标语言中惯用、简洁、地道的表达，避免直译腔和生硬书面语
- 成语、俗语、口语、网络表达和隐喻应使用功能相近的自然表达；没有合适对应时再准确意译
- 保持原文的正式程度、情绪、称谓和说话风格，不要擅自润色成另一种文风
- 保留人名、产品名、数字、单位、代码、链接以及原有段落结构；专业术语使用通行译法
- 问句仍翻译成问句，命令仍翻译成命令；不要回答原文中的问题
- 仅在含义非常明确时修正明显的语音识别错误，不得猜测或补充原文没有的信息
- 只输出最终译文，不加标题、引号、解释、注释或备选版本
'''

