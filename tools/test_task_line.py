# coding: utf-8
"""任务标识与录音协程这一侧的控制台第一行。

- 任务标识：随机前缀 + 创建时间的后缀、前 8 位各不相同、能通过挂图归档的文件名校验
- 录音协程：松手和打断时只登记不打印；最终包发不出去时当场打一行，且每个任务只打一次

AudioRecorder 的相关方法按源码抽出来，绑到桩对象上运行；ClientState、task_line、
image_archive 用仓库里的真实代码。结果处理那一侧的验证在 test_stream_output.py。
"""
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

RECORDER_METHODS = {'record_and_send', '_send_message', '_report_no_result'}


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


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 在事件循环里建 ClientState：它的 asyncio.Queue 要绑在这个循环上
        self.state = state_mod.ClientState()
        self.console = Mock()
        self.ws = types.SimpleNamespace(is_connected=True, send=AsyncMock(return_value=True))
        config = types.SimpleNamespace(threshold=0.3, save_audio=False)
        ns = dict(asyncio=asyncio, Config=config, console=self.console, logger=LOG,
                  AudioMessage=object, AudioFileManager=None, np=None,
                  new_task_id=new_task_id, format_task_line=task_line.format_task_line,
                  no_result_note=task_line.no_result_note,
                  NOT_CONNECTED=task_line.NOT_CONNECTED, SEND_FAILED=task_line.SEND_FAILED)
        tree = ast.parse((ROOT / 'core/client/audio/recorder.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'AudioRecorder')
        nodes = [n for n in cls.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in RECORDER_METHODS]
        self.assertEqual({n.name for n in nodes}, RECORDER_METHODS)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'recorder.py', 'exec'), ns)

        r = types.SimpleNamespace(state=self.state, _ws_manager=self.ws, task_id=None,
                                  _file_manager=None, _discard_audio_file=Mock())
        r._audio_message = lambda data, is_final: types.SimpleNamespace(task_id=r.task_id, is_final=is_final)
        for name in RECORDER_METHODS:
            setattr(r, name, (lambda f: lambda *a, **k: f(r, *a, **k))(ns[name]))
        self.recorder = r

    async def run_recording(self, end, before_end=None):
        """录一段 1 秒的音，再以 end（'finish' / 'abort'）收尾，等发送协程跑完"""
        q = self.state.queue_in
        job = asyncio.ensure_future(self.recorder.record_and_send())
        await q.put({'type': 'begin', 'time': 0.0, 'data': None})
        await q.put({'type': 'data', 'time': 1.0, 'data': [0.0] * 48000})
        for _ in range(5):
            await asyncio.sleep(0)
        if before_end:
            before_end(self.recorder.task_id)
        await q.put({'type': end, 'time': 2.0, 'data': None})
        await asyncio.wait_for(job, 2)
        for _ in range(5):
            await asyncio.sleep(0)
        return self.recorder.task_id

    def lines_with(self, tid):
        return [str(c.args[0]) for c in self.console.print.call_args_list if c.args and tid in str(c.args[0])]

    async def test_finish_registers_without_printing(self):
        tid = await self.run_recording('finish')
        self.console.print.assert_not_called()
        self.assertAlmostEqual(self.state.awaiting_results[tid], 1.0)

    async def test_abort_registers_without_printing(self):
        tid = await self.run_recording('abort', before_end=self.state.mark_task_cancelled)
        self.console.print.assert_not_called()
        self.assertIn(tid, self.state.awaiting_results)

    async def test_not_connected_prints_one_line_once(self):
        self.ws.is_connected = False
        tid = await self.run_recording('finish')
        lines = self.lines_with(tid)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('1.00s', lines[0])
        # 已经打过的任务不再留给断线清理
        self.assertEqual(self.state.drain_awaiting_results(), [])

    async def test_send_failure_prints_one_line(self):
        self.ws.send = AsyncMock(return_value=False)
        tid = await self.run_recording('finish')
        self.assertEqual(len(self.lines_with(tid)), 1)
        self.assertNotIn(tid, self.state.awaiting_results)

    async def test_cleanup_first_means_no_second_line(self):
        # 发送最终包期间断线清理抢先取走了登记，之后的发送失败就不再打印
        async def send(message):
            if message.is_final:
                self.state.drain_awaiting_results()
            return False

        self.ws.send = send
        tid = await self.run_recording('finish')
        self.assertEqual(self.lines_with(tid), [])

    async def test_aborted_and_not_connected_says_aborted(self):
        self.ws.is_connected = False
        normal = await self.run_recording('finish')
        aborted = await self.run_recording('abort', before_end=self.state.mark_task_cancelled)
        normal_line, = self.lines_with(normal)
        aborted_line, = self.lines_with(aborted)
        # 两种原因的说明彼此不同（不比对措辞）
        self.assertNotEqual(normal_line.replace(normal, ''), aborted_line.replace(aborted, ''))


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
