# coding: utf-8
"""
工具模块

提供各种通用工具函数和类。

模块架构：
- asyncio_to_thread: asyncio.to_thread 的兼容实现
- clean_assets: 独立脚本（不被客户端导入），递归清理没被 Markdown 引用的图片、音频附件
- empty_working_set: Windows 内存管理
- format_tools: 文本格式化（中英文空格调整）
- local_port: 本机 TCP 端口是否有程序监听（只读查询系统连接表）
- my_status: Rich Status 扩展
- signal_handler: Ctrl+C 退出处理
- srt_from_txt: SRT 字幕生成
- task_id: 任务标识（随机数 + 创建时间）
- token_sync: 把热词替换等文本改动同步回 token 序列（文件转录的时间戳）
- window_detector: 前台窗口与进程名检测
- windows_selection: 选区只读获取（UIA / Scintilla）
- zhconv: 中文简繁转换
"""

from core.tools.asyncio_to_thread import to_thread
from core.tools.empty_working_set import empty_working_set, empty_current_working_set
from core.tools.format_tools import adjust_space
from core.tools.my_status import Status

__all__ = [
    'to_thread',
    'empty_working_set',
    'empty_current_working_set',
    'adjust_space',
    'Status',
]
