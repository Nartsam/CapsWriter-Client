# coding: utf-8
"""
任务标识：`8 位随机十六进制数_月-日_时-分-秒`，如 `a3f91c07_10-20_17-54-20`

麦克风录音、文件转录、字幕调整共用。格式上的几条约束：

- 随机数排在最前：服务端有几条日志只打印标识的前 8 位，以日期开头的话，同一天的任务
  会显示成同一个前缀。
- 服务端按标识在全局字典里保存识别状态，不按连接区分，几台电脑连同一个服务端时也不能
  重复。要同一秒创建、32 位随机数又相同才会重复，概率可以忽略。时间后缀只给人看，
  不承担去重。
- 只用字母、数字、下划线和连字符：挂图归档的文件名里带着它，归档的文件名校验
  （`image_archive._ARCHIVE_NAME_RE`）只认这些字符，不认的文件不会被清理，也不报错。

服务端只把它当普通字符串，改格式不必同步服务端。
"""

import secrets
import time
from typing import Optional


def new_task_id(timestamp: Optional[float] = None) -> str:
    """生成任务标识

    Args:
        timestamp: 创建时刻（`time.time()` 的值），缺省取当前时间；后缀按本地时间写
    """
    stamp = time.strftime('%m-%d_%H-%M-%S', time.localtime(timestamp))
    return f'{secrets.token_hex(4)}_{stamp}'
