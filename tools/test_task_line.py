# coding: utf-8
"""任务标识与任务行格式。录音和断线集成测试见 test_recognition_flow.py。"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger('task-line-test')
LOG.addHandler(logging.NullHandler())   # 未连接分支会记 WARNING，不往测试输出里刷
LOG.propagate = False


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for name in ('core', 'core.client', 'core.client.audio', 'core.client.vision', 'core.tools'):
    module = types.ModuleType(name)
    module.__path__ = []
    module.logger = LOG
    sys.modules[name] = module

# state.py 在模块级构造 rich 的 Console
for name in ('rich', 'rich.console', 'rich.theme'):
    sys.modules[name] = types.ModuleType(name)
sys.modules['rich.console'].Console = lambda **kw: None
sys.modules['rich.theme'].Theme = lambda *a, **k: None
# 发送失败时 _send_message 会就地导入悬浮提示
ui = types.ModuleType('core.client.ui')
ui.show_recording_indicator_error = Mock()
sys.modules['core.client.ui'] = ui

state_mod = load('core.client.state', 'core/client/state.py')
task_line = load('core.client.task_line', 'core/client/task_line.py')
task_id_mod = load('core.tools.task_id', 'core/tools/task_id.py')
archive = load('core.client.vision.image_archive', 'core/client/vision/image_archive.py')
new_task_id = task_id_mod.new_task_id

class TaskIdTests(unittest.TestCase):
    def test_suffix_is_creation_time(self):
        ts = time.mktime((2026, 10, 20, 17, 54, 20, 0, 0, -1))
        tid = new_task_id(ts)
        self.assertTrue(tid.endswith('_10-20_17-54-20'), tid)
        # 缺省取当前时间
        now = new_task_id()
        self.assertEqual(now.split('_', 1)[1], time.strftime('%m-%d_%H-%M-%S'))

    def test_prefix_is_random_hex_and_distinct(self):
        ts = time.time()
        ids = [new_task_id(ts) for _ in range(200)]
        # 服务端几条日志只打前 8 位：同一时刻创建的也要在前 8 位上分得开
        self.assertEqual(len({i[:8] for i in ids}), len(ids))
        for i in ids:
            self.assertRegex(i[:8], r'^[0-9a-f]{8}$')
            self.assertEqual(i[8], '_')

    def test_archive_still_prunes_files_named_with_it(self):
        # 归档文件名校验不认的字符会让改名后的图永远不被清理，且不报错
        with tempfile.TemporaryDirectory(dir=ROOT / 'tools') as tmp:
            directory = Path(tmp)
            old = directory / '261020175420_DV5_pend.png'
            old.write_bytes(b'x')
            os.utime(old, (1000, 1000))
            renamed = Path(archive.mark_outcome(str(old), new_task_id(), used=True))
            self.assertNotEqual(renamed, old)
            self.assertTrue(renamed.exists())
            newer = directory / '261020175421_DV5_pend.png'
            newer.write_bytes(b'x')
            archive._prune(directory, keep=1)
            self.assertFalse(renamed.exists())
            self.assertTrue(newer.exists())


# 录音、取消与断线的任务行集成验证见 test_recognition_flow.py。


class FormatTests(unittest.TestCase):
    def test_duration_optional(self):
        with_duration = task_line.format_task_line('tid', 1.5, 'x')
        without = task_line.format_task_line('tid', None, 'x')
        self.assertIn('1.50s', with_duration)
        # 没登记录音时长时只少这一项，其余照旧
        self.assertEqual(len(with_duration.split('  ')), len(without.split('  ')) + 1)
        self.assertTrue('tid' in without and without.endswith('x'))

    def test_partial_abort_note_differs(self):
        self.assertNotEqual(task_line.aborted_note(True), task_line.aborted_note(False))


if __name__ == '__main__':
    unittest.main()
