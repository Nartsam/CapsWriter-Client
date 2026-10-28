# coding: utf-8
"""任务标识的格式约束。任务行在录音、打断、出错与断线时各打一次，见 test_recognition_flow.py。"""
from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for name in ('core', 'core.client', 'core.client.vision', 'core.tools'):
    module = types.ModuleType(name)
    module.__path__ = []
    module.logger = logging.getLogger('task-id-test')
    sys.modules[name] = module

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


if __name__ == '__main__':
    unittest.main()
