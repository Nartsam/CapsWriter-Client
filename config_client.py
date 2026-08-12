import os
from collections.abc import Iterable
from pathlib import Path

# 版本信息
__version__ = '2.6'

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# 客户端配置
class ClientConfig:
    # Server 地址：可写单个字符串，也可写成列表按序探测。列表形态下客户端启动后
    # 从前往后依次尝试，首个连上的会被锁定，此后断线也只等它恢复、不会改投其他候选
    # （换机请重启客户端）。列表项可用 'host:port' 单独覆盖下面的 port
    #   例：addr = ['10.102.33.100', '192.168.1.50:6001', '127.0.0.1']
    addr = ['10.102.33.100', '192.168.5.8', '127.0.0.1']    # Server 地址
    port = '6000'               # Server 端口

    # 多候选探测时单个候选的握手超时（秒），仅在候选多于一个且尚未锁定时生效；
    # 锁定之后与单地址配置一律沿用 websockets 默认的 10s。
    # 调小会更快跳过离线主机，但握手慢于该值的候选会被每一轮都跳过，
    # 症状是「ping 得通却永远连不上」——出现时先把这两个值调大
    addr_probe_timeout = 3          # 公网 / 域名 / 主机名等无法判定归属的地址
    addr_probe_timeout_lan = 2      # 内网与回环：10.* / 172.16-31.* / 192.168.* / 127.*

    # 快捷键配置列表
    shortcuts = [
        {
            'key': 'ctrl_r',     # 监听键，默认：caps_lock (大写锁定)
            'type': 'keyboard',     # 是键盘快捷键
            'suppress': True,      # 阻塞按键（短按会补发）
            'hold_mode': True,      # 长按模式
            'enabled': True         # 启用此快捷键
        },
        {
            'key': 'x2',     # 监听 鼠标前进 键
            'type': 'mouse',
            'suppress': True,
            'hold_mode': True,
            'enabled': False
        },
    ]

    threshold    = 0.5          # 快捷键触发阈值（秒）

    # 录音打断键：录音过程中或等待识别结果时按下，本次输入彻底作废（不上屏、不触发角色、不留录音）
    # 录音期间该键会被拦截、不传给前台程序；等待结果期间放行（避免服务端不回包时该键全局失灵）
    # 留空则禁用此功能。与中断 LLM 输出的 llm_stop_key 相互独立
    cancel_recording_key = 'esc'

    # 连续对话切换键：录音期间按下，若本句触发了具名角色则进入与该角色的连续对话，
    # 此后每句话都直接发给该角色、不必再念角色名。同一次录音内按奇数次生效、偶数次抵消。
    # 连续对话中再次按下则退出（当前这句话起即恢复正常角色检测）。
    # 录音期间该键会被拦截、不传给前台程序；非录音期间一律放行。留空则禁用此功能。
    # 不支持鼠标键和修饰键（Ctrl/Alt/Shift/Win），也不能与录音键或 cancel_recording_key 同名
    continuous_chat_key = 'space'
    # 连续对话闲置超时（秒）：超过此时长没有再对话则自动退出，静默生效、无提示。
    # 判定发生在下一次按下录音键时，0 表示永不超时
    continuous_chat_timeout = 300

    paste        = False        # 是否以写入剪切板然后模拟 Ctrl-V 粘贴的方式输出结果
    restore_clip = True         # 模拟粘贴后是否恢复剪贴板
    paste_apps   = ['WeiXin.exe', 'Telegram.exe']  # 匹配时强制粘贴

    enter_apps   = [('happ.exe', 0.5), ('hexin.exe', 0.5)]  # (应用名, 延迟秒数) 输出完成后自动回车，如同花顺，输入股票名后，需要回车才能切换

    save_audio = True           # 是否保存录音文件
    audio_name_len = 20         # 将录音识别结果的前多少个字存储到录音文件名中，建议不要超过200

    context = ''                # 提示词上下文，用于辅助 Fun-ASR-Nano 模型识别（例如输入人名、地名、专业术语等）
    language = 'auto'           # 识别语言：'auto', 'chinese', 'english', 'japanese' 等（各引擎支持范围不同）

    trash_punc = '，。,.'       # 识别结果要消除的末尾标点
    trash_punc_thresh = 8       # 识别结果的单词数量低于阈值时，强制去除末尾标点
    trash_punc_apps = ['WeiXin.exe', ]   # 对于指定的应用，强制去除末尾标点

    traditional_convert = False     # 是否将识别结果转换为繁体中文
    traditional_locale = 'zh-hant'  # 繁体地区：'zh-hant'（标准繁体）, 'zh-tw'（台湾繁体）, 'zh-hk'（香港繁体）

    hot = True                 # 是否启用热词替换（统一 RAG 匹配）
    hot_thresh = 0.85           # RAG 替换热词阈值（高阈值，用于实际替换）
    hot_similar = 0.6           # RAG 相似热词阈值（低阈值，用于 LLM 上下文）
    hot_rule = True             # 是否启用自定义规则替换（基于正则表达式）

    llm_enabled = True          # 是否启用 LLM 润色功能，需要配置 LLM/ 目录下的角色文件
    llm_stop_key = 'esc'        # 中断 LLM 输出的快捷键

    enable_tray = True          # 客户端默认启用托盘图标功能

    # 日志配置
    log_level = 'INFO'          # 日志级别：'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'

    mic_seg_duration = 60       # 麦克风听写时分段长度：60秒
    mic_seg_overlap = 4         # 麦克风听写时分段重叠：4秒

    file_seg_duration = 60      # 转录文件时分段长度
    file_seg_overlap = 4        # 转录文件时分段重叠

    file_save_srt = True        # 转录文件时是否保存 srt 字幕
    file_save_txt = True        # 转录文件时是否保存 txt 文本（按标点切分后的）
    file_save_json = True       # 转录文件时是否保存 json 结果（含原始时间戳）
    file_save_merge = False      # 转录文件时是否保存 merge.txt（未切分的段落长文本）

    udp_broadcast = False               # 是否启用 UDP 广播输出结果
    udp_broadcast_targets = [           # UDP 广播目标地址列表，格式: (地址, 端口)
        ('127.255.255.255', 6017),      # 本地回环广播
        # ('192.168.1.255', 6017),      # 局域网广播（示例，按需启用）
    ]

    udp_control = False             # 是否启用 UDP 控制录音（外部程序发送 START/STOP 命令）
    udp_control_addr = '127.0.0.1'  # UDP 控制监听地址（'0.0.0.0' 允许外部访问）
    udp_control_port = 6018         # UDP 控制监听端口

    # Companion 桌面伴侣应用 WebSocket 配置
    companion_enabled = True                # 是否启用 Companion WebSocket 服务器
    companion_addr = '127.0.0.1'            # 监听地址
    companion_port = 6019                   # 监听端口（端口被占用则自动禁用）

    # OpenMate 桌面看板 HTTP 接入配置（API 文档见 OpenMate/docs/api/external-http-api.md）
    # 探测到 OpenMate 以 external 模式运行时，toast 角色的 LLM 输出改由 OpenMate 展示，
    # 不再弹出本地 Toast 窗口；OpenMate 隐藏在托盘时会自行以无激活方式恢复显示
    openmate_enabled = True                     # 是否启用 OpenMate 展示
    openmate_url = 'http://127.0.0.1:18760'     # OpenMate HTTP 服务地址
    openmate_token = ''                         # Bearer token（OpenMate 未配置 token 则留空）
    openmate_character = ''                     # 目标角色 ID（留空跟随 OpenMate 当前角色，每次请求
                                                # 通过 GET /v1/presentation 查询）。情绪状态列表按
                                                # 最终角色通过 HTTP 自动获取，无需手工维护
    openmate_health_timeout = 0.5               # 探测 /health 的超时（秒），超时视为 OpenMate 不可用


# 快捷键配置说明
r"""
快捷键配置字段说明：
  key        - 按键名称（见下方可用按键列表）
  type       - 输入类型：'keyboard'（键盘）或 'mouse'（鼠标）
  suppress   - 是否阻塞按键（True=阻塞，False=不阻塞）
  hold_mode  - 长按模式（True=按下录音松开停止，False=单击开始再次单击停止）
  enabled    - 是否启用此快捷键

阻塞模式说明：
  - 阻塞模式  ：长按录音识别，短按（<0.3秒）则自动补发按键，不影响单击功能
  - 非阻塞模式：对于 CapsLock/NumLock/ScrollLock 这类切换键，松开时会自动补发，以恢复按键状态

可用按键名称：

  字母数字：a - z, 0 - 9（大键盘）

  符号键：, . / \ ` ' - = [ ] ; '


  功能键：f1 - f24

  控制键:
      ctrl_l,   ctrl_r,
      shift,  shift_r,
      alt_l,    alt_gr,
      cmd,    cmd_r

  特殊键：
      space, enter, tab, backspace, delete, insert, home, end
      page_up, page_down, esc, caps_lock, num_lock, scroll_lock
      print_screen, pause, menu

  方向键：up, down, left, right

  鼠标键：x1, x2

示例配置：
  {'key': 'caps_lock', 'type': 'keyboard', 'suppress': False, 'hold_mode': True, 'enabled': True},
  {'key': 'f12', 'type': 'keyboard', 'suppress': True, 'hold_mode': True, 'enabled': True},
  {'key': 'x2', 'type': 'mouse', 'suppress': True, 'hold_mode': True, 'enabled': True},
"""

