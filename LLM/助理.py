"""
助理角色，联网模型
"""
from LLM.backends import resolve

# ==================== 基本信息 ====================
name = '助理'                         # 角色名称（留空表示默认）
enabled = True                           # 是否启用此角色

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
max_context_length = 256 * 1024                # 最大上下文长度（token 数）历史累积超过它的 80% 时，从最旧的消息开始丢弃

# ==================== 功能配置 ====================
enable_hotwords = False                 # 是否启用热词
enable_thinking = False                 # 是否启用思考（关闭时直接回答、更快；Ollama 与 DeepSeek 生效）
enable_history = True                   # 是否保留对话历史
enable_read_selection = True            # 是否启用获取选中文字（默认 UI Automation）
selection_max_length = 1000             # 选中文字最大长度
enable_web_search = True                # 联网搜索：挂载 DeepSeek 服务端内置 web_search 工具，是否检索由模型判断，普通问题不触发、速度不受影响
enable_datetime = True                  # 注入当前时间，模型据此判断知识是否过期
request_timeout = backend_config['request_timeout'] # 单次请求超时。两套后端都远超 provider 默认的 2s：DeepSeek 检索期数秒无输出，CPA 中位 3~7s、带图约 20s

# ==================== 输出配置 ====================
output_mode = 'toast'                   # 输出方式：'typing' 直接打字, 'toast' 浮动窗口

# ==================== Toast 弹窗配置（仅在 output_mode='toast' 时有效） ====================
toast_min_width_px = 0                # 最小宽度像素值；正数时优先于比例配置
toast_max_width_px = 0                # 最大宽度像素值；正数时优先于比例配置
toast_min_width_ratio = 0.3             # 最小宽度比例回退值（像素值无效时使用）
toast_max_width_ratio = 0.5             # 最大宽度比例回退值（像素值无效时使用）
toast_initial_height = 0                # 窗口初始高度（0 表示自动计算）
toast_font_family = '微软雅黑'
toast_font_size = 18                     # 字体大小
toast_font_color = 'black'              # 字体颜色
toast_bg_color = '#f7fbff'              # 背景颜色
toast_duration = 3000                   # 显示时长（毫秒）
toast_editable = True                   # 是否可编辑（Markdown 渲染后）
toast_window_alpha = 0.92               # 窗口透明度，营造半透明亚克力效果
toast_corner_radius = 12                # 窗口圆角半径

# ==================== 生成参数 ====================
temperature = 0.3                       # 日常问答降低随机性，优先稳定、直接的答案
top_p = 0.9                             # Top-p 采样（0-1）
max_tokens = 4096                        # 限制冗长回答，简单知识问答无需大篇幅输出
stop = ''                               # 停止序列

# ==================== 高级选项 ====================
extra_options = {}                      # 额外的 API 参数（JSON 格式）

# ==================== 提示词前缀 ====================
prompt_prefix_hotwords = '热词列表：'    # 热词列表前缀
prompt_prefix_selection = '选中文字：'   # 选中文字前缀
prompt_prefix_input = '用户输入：'       # 用户输入前缀

# ==================== System Prompt ====================
system_prompt = '''
你是一个面向知识性问题的简洁问答助手，优先保证答案正确、清楚、直接。

回答规则：
- 直接回答“用户输入”中的当前问题；“选中文字”和历史消息仅作为理解当前问题的上下文，不要偏离当前问题
- 第一处有效内容就给出答案或结论，不要先复述问题、分析需求或介绍回答步骤
- 默认用一至三句完整的短句回答；只有缺少必要说明会导致误解时，才补充最少量的解释
- 不要寒暄、致谢、道歉、夸赞用户，也不要添加“希望这能帮到你”“如果需要可以继续问”等客套话或收尾总结
- 只使用纯文本。不要使用 Markdown 标题、加粗、项目符号、表格、代码块、XML 标签、Emoji 或其他装饰格式；必须表达多个要点时，用简短普通句子自然衔接
- 不展示思考过程，不讨论与答案无关的背景，不主动扩展用户没有询问的话题
- 不确定时不要编造，用一句话简短说明不确定之处；问题存在多个合理解释且会显著改变答案时，只提出一个必要的澄清问题
- 只输出答案本身

联网搜索规则：
- 你可以使用联网搜索工具，但它会明显拖慢回答速度，只在确有必要时使用
- 以下情况必须先搜索再回答：答案会随时间变化（汇率、股价、油价、商品价格、天气、赛事比分、排行榜、新闻事件、政策法规、人事任免、软件最新版本、产品是否已发布等）；问题中出现“现在、目前、最新、今天、最近、还在不在”这类时效性措辞；问题涉及的事物可能出现在你训练数据截止之后
- 以下情况直接回答，不要搜索：定义、原理、概念解释、语言翻译与用法、数学计算、单位换算公式、编程语法、历史事件、稳定不变的事实，以及针对“选中文字”或上文内容本身的加工
- 参考“当前时间”判断你已有的知识是否可能过期；一旦怀疑过期就搜索，不要凭记忆给出可能陈旧的数字
- 搜索结果与你的记忆冲突时，以搜索结果为准
- 给出会随时间变化的数据时，在同一句里带上数据对应的时间点，例如“当前”“截至目前”“X 月 X 日约为……”；不要输出网址、来源列表或引用编号
'''

