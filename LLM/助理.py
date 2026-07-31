"""
助理角色，联网模型
"""
from LLM.secrets import DEEPSEEK_API_KEY

# ==================== 基本信息 ====================
name = '助理'                         # 角色名称（留空表示默认）
enabled = True                           # 是否启用此角色

# ==================== API 配置 ====================
provider = 'deepseek'                               # API 提供商：'ollama', 'openai', 'deepseek', 'moonshot', 'zhipu', 'claude', 'gemini'
api_url = ''                                        # 留空则自动使用 provider 对应的默认值
api_key = DEEPSEEK_API_KEY                          # API Key
model = 'deepseek-v4-flash'                         # 模型名称

# ==================== 上下文管理 ====================
max_context_length = 1024 * 1024               # 最大上下文长度（token 数）历史累积超过它的 80% 时，从最旧的消息开始丢弃

# ==================== 功能配置 ====================
enable_hotwords = False                 # 是否启用热词
enable_thinking = False                 # 是否启用思考（仅 Ollama）
enable_history = True                   # 是否保留对话历史
enable_read_selection = True            # 是否启用获取选中文字（通过 Ctrl+C）
selection_max_length = 1000             # 选中文字最大长度

# ==================== 输出配置 ====================
output_mode = 'toast'                   # 输出方式：'typing' 直接打字, 'toast' 浮动窗口

# ==================== Toast 弹窗配置（仅在 output_mode='toast' 时有效） ====================
toast_min_width_px = 0                # 最小宽度像素值；正数时优先于比例配置
toast_max_width_px = 0                # 最大宽度像素值；正数时优先于比例配置
toast_min_width_ratio = 0.3             # 最小宽度比例回退值（像素值无效时使用）
toast_max_width_ratio = 0.5             # 最大宽度比例回退值（像素值无效时使用）
toast_initial_height = 0                # 窗口初始高度（0 表示自动计算）
toast_font_family = '微软雅黑'
toast_font_size = 16                     # 字体大小
toast_font_color = 'black'              # 字体颜色
toast_bg_color = '#f7fbff'              # 背景颜色
toast_duration = 3000                   # 显示时长（毫秒）
toast_editable = True                   # 是否可编辑（Markdown 渲染后）
toast_window_alpha = 0.92               # 窗口透明度，营造半透明亚克力效果
toast_corner_radius = 12                # 窗口圆角半径

# ==================== 生成参数 ====================
temperature = 0.3                       # 日常问答降低随机性，优先稳定、直接的答案
top_p = 0.9                             # Top-p 采样（0-1）
max_tokens = 512                        # 限制冗长回答，简单知识问答无需大篇幅输出
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
'''

