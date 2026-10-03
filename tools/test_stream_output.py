# coding: utf-8
"""流式上屏：逐句打字、开头角色名改为松手后处理、角色名被切开时的等待、打断与旧服务端回退。

ResultProcessor 的相关方法按源码抽出来，绑到桩对象上运行；打字、热词、LLM、指示器均为桩。
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
import time
import types
from typing import Dict, Optional, Tuple
import unittest
from unittest.mock import AsyncMock, Mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for name in ('core', 'core.client', 'core.client.llm', 'core.client.output'):
    module = types.ModuleType(name)
    module.__path__ = []
    module.logger = logging.getLogger('stream-test')
    sys.modules[name] = module

roles = load('core.client.llm.llm_role_config', 'core/client/llm/llm_role_config.py')
load('core.client.llm.llm_interfaces', 'core/client/llm/llm_interfaces.py')
detector_mod = load('core.client.llm.llm_role_detector', 'core/client/llm/llm_role_detector.py')
stream_mod = load('core.client.output.stream_output', 'core/client/output/stream_output.py')
RoleConfig = roles.RoleConfig

METHODS = {'_handle_message', '_after_output', '_paste_for_foreground', '_correct_text',
           '_on_stream_delta', '_decide_stream_mode', '_type_piece', '_finish_stream'}


def extract_methods(namespace):
    tree = ast.parse((ROOT / 'core/client/output/result_processor.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ResultProcessor')
    nodes = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in METHODS]
    assert {n.name for n in nodes} == METHODS, {n.name for n in nodes} ^ METHODS
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'result_processor.py', 'exec'), namespace)


class FakeLoader:
    """角色加载器桩：助理、新岛真|真学姐（具名），默认角色可开关"""

    def __init__(self, default_enabled=False):
        self.roles = {'助理': RoleConfig(name='助理'), '新岛真': RoleConfig(name='新岛真|真学姐'),
                      '翻译': RoleConfig(name='翻译', enabled=False)}
        self.default = RoleConfig(name=RoleConfig.DEFAULT_ROLE_NAME, enabled=default_enabled)

    def get_roles(self):
        return dict(self.roles)

    def get_default_role(self):
        return self.default


class RoleDetectorTests(unittest.TestCase):
    def test_name_prefix(self):
        d = detector_mod.RoleDetector(FakeLoader())
        self.assertTrue(d.is_name_prefix('新岛'))
        self.assertTrue(d.is_name_prefix('真学，'))      # 别名的开头，末尾标点忽略
        self.assertFalse(d.is_name_prefix('新岛真'))      # 完整角色名交给 detect
        self.assertFalse(d.is_name_prefix('翻'))          # 禁用的角色不算
        self.assertFalse(d.is_name_prefix('今天'))
        self.assertFalse(d.is_name_prefix('，'))

    def test_default_role_enabled(self):
        self.assertFalse(detector_mod.RoleDetector(FakeLoader()).default_role_enabled())
        self.assertTrue(detector_mod.RoleDetector(FakeLoader(True)).default_role_enabled())


class TrimTests(unittest.TestCase):
    def test_trim(self):
        trim = stream_mod.trim_for_strip_punc
        self.assertEqual(trim('你好', '，再见。', '你好，再见'), '，再见')
        self.assertEqual(trim('你好', '，再见。', '你好，再见。'), '，再见。')
        self.assertEqual(trim('你好。', '', '你好'), '')        # 已上屏的改不了


def message(task_id='t', delta='', text='', is_final=False):
    return types.SimpleNamespace(task_id=task_id, delta=delta, text=text, is_final=is_final,
                                 time_complete=10.0, time_submit=9.5, time_start=1.0, duration=3.0)


class ProcessorTests(unittest.IsolatedAsyncioTestCase):
    def make(self, llm_enabled=True, cancelled=False, default_role=False):
        self.config = types.SimpleNamespace(
            llm_enabled=llm_enabled, traditional_convert=False, hot=True, paste=False,
            paste_apps=[], enter_apps=[], save_audio=False)
        self.console = Mock()
        self.note = Mock()
        self.progress = Mock()
        self.typed = []
        ns = dict(Optional=Optional, Tuple=Tuple, Dict=Dict, Config=self.config, RecognitionMessage=object,
                  console=self.console, logger=logging.getLogger('stream-test'), time=time, asyncio=asyncio,
                  TextOutput=types.SimpleNamespace(strip_punc=lambda t: t[:-1] if t.endswith('。') and len(t) < 6 else t),
                  show_processing_indicator=Mock(), hide_recording_indicator=Mock(),
                  update_recording_indicator_progress=self.progress,
                  update_recording_indicator_stream_note=self.note,
                  get_active_window_info=lambda: {'process_name': 'Code.exe'},
                  broadcast_output_udp=Mock(), RoleConfig=RoleConfig, zhconv_convert=None,
                  format_selection_preview=lambda s: '',
                  StreamSession=stream_mod.StreamSession, trim_for_strip_punc=stream_mod.trim_for_strip_punc,
                  PENDING=stream_mod.PENDING, TYPING=stream_mod.TYPING, DEFERRED=stream_mod.DEFERRED)
        extract_methods(ns)

        detector = detector_mod.RoleDetector(FakeLoader(default_role))
        llm = types.SimpleNamespace(detect_role=detector.detect, is_role_name_prefix=detector.is_name_prefix,
                                    process_and_output=AsyncMock(return_value=None))
        state = Mock()
        state.is_task_cancelled.return_value = cancelled
        state.pending_task_id = 't'
        state.streamed_task_ids = []
        state.pop_image_snapshot.return_value = None
        state.pop_continuous_snapshot.return_value = None
        hotword = Mock()
        # 热词：把「鱿鱼」纠正成「由于」，并记一条完全匹配
        hotword.get_phoneme_corrector.return_value.correct.side_effect = lambda t, k=10: types.SimpleNamespace(
            text=t.replace('鱿鱼', '由于'), matchs=[('鱿鱼', '由于', 1.0)] if '鱿鱼' in t else [], similars=[])
        hotword.get_rule_corrector.return_value.substitute.side_effect = lambda t: t

        async def output(text, paste=None):
            if text:
                self.typed.append(text)

        p = types.SimpleNamespace(state=state, hotword=hotword, app=types.SimpleNamespace(llm=llm),
                                  output=types.SimpleNamespace(output=output), _streams={},
                                  _resolve_forced_role=Mock(return_value=None), _report_image_usage=Mock(),
                                  _update_continuous_state=Mock(), _log_modifier_key_state=Mock())
        for name in METHODS:
            fn = ns[name]
            if isinstance(fn, staticmethod):
                setattr(p, name, fn.__func__)
            else:
                setattr(p, name, (lambda f: lambda *a, **k: f(p, *a, **k))(fn))
        self.p, self.llm, self.state = p, llm, state
        return p

    async def feed(self, *msgs):
        for m in msgs:
            await self.p._handle_message(m)

    async def test_typing_sentence_by_sentence(self):
        self.make()
        await self.feed(message(delta='鱿鱼今天下雨'), message(delta='，我们改在室内'))
        self.assertEqual(self.typed, ['由于今天下雨', '，我们改在室内'])
        self.assertEqual(self.state.streamed_task_ids, ['t'])
        await self.feed(message(delta='开会。', text='鱿鱼今天下雨，我们改在室内开会。', is_final=True))
        self.assertEqual(self.typed, ['由于今天下雨', '，我们改在室内', '开会。'])
        self.state.set_output_text.assert_called_once_with('由于今天下雨，我们改在室内开会。')
        self.llm.process_and_output.assert_not_called()
        self.p._update_continuous_state.assert_called_once()
        self.assertEqual(self.p._streams, {})
        # 各句的热词替换要汇总到终端那一行（只看原词出现了，不管排版）
        printed = ' '.join(str(c.args[0]) for c in self.console.print.call_args_list)
        self.assertIn('鱿鱼', printed)

    async def test_role_at_start_defers_to_release(self):
        self.make()
        await self.feed(message(delta='助理，帮我查一下'), message(delta='明天的天气'))
        self.assertEqual(self.typed, [])
        self.note.assert_called()
        self.assertIn('助理', self.note.call_args.args[0])
        full = '助理，帮我查一下明天的天气。'
        await self.feed(message(delta='。', text=full, is_final=True))
        self.llm.process_and_output.assert_awaited_once()
        self.assertEqual(self.llm.process_and_output.call_args.args[0], full)

    async def test_role_name_split_waits_for_next_sentence(self):
        self.make()
        await self.feed(message(delta='新岛'))
        self.assertEqual(self.typed, [])
        await self.feed(message(delta='真，你好'))
        self.assertEqual(self.typed, [])
        await self.feed(message(delta='。', text='新岛真，你好。', is_final=True))
        self.assertEqual(self.typed, [])
        self.assertEqual(self.llm.process_and_output.call_args.args[0], '新岛真，你好。')

    async def test_name_prefix_that_turns_out_not_a_role(self):
        self.make()
        await self.feed(message(delta='新岛'), message(delta='这个地方很好'))
        self.assertEqual(self.typed, ['新岛这个地方很好'])

    async def test_pending_until_release_goes_whole(self):
        self.make()
        await self.feed(message(delta='新岛'), message(delta='', text='新岛。', is_final=True))
        self.assertEqual(self.typed, [])
        self.assertEqual(self.llm.process_and_output.call_args.args[0], '新岛')   # 整段流程照常去末尾标点

    async def test_llm_disabled_never_defers(self):
        self.make(llm_enabled=False)
        await self.feed(message(delta='助理，你好'))
        self.assertEqual(self.typed, ['助理，你好'])

    async def test_cancelled_task_types_nothing(self):
        self.make(cancelled=True)
        await self.feed(message(delta='你好'), message(delta='。', text='你好。', is_final=True))
        self.assertEqual(self.typed, [])
        self.state.release_cancelled_task.assert_called_once_with('t')

    async def test_old_server_without_delta_uses_whole_flow(self):
        self.make()
        await self.feed(message(text='第一段', is_final=False))
        self.progress.assert_called_once()
        await self.feed(message(text='第一段第二段。', is_final=True))
        self.assertEqual(self.llm.process_and_output.call_args.args[0], '第一段第二段。')

    async def test_strip_punc_applies_to_last_piece(self):
        self.make()
        await self.feed(message(delta='好的'), message(delta='。', text='好的。', is_final=True))
        self.assertEqual(self.typed, ['好的'])          # 全文短，末尾句号按配置去掉


class RoleLoaderErrorTests(unittest.TestCase):
    """角色文件导入失败要记日志；出自 secrets.py 的异常只记类型，不记信息"""

    def setUp(self):
        sys.modules['config_client'] = types.SimpleNamespace(BASE_DIR=str(ROOT))
        self.loader_mod = load('core.client.llm.llm_role_loader', 'core/client/llm/llm_role_loader.py')

    def raise_in(self, filename, exc_code):
        code = compile(exc_code, filename, 'exec')
        try:
            exec(code, {})
        except Exception as e:
            return e

    def test_secrets_error_is_redacted(self):
        e = self.raise_in(str(ROOT / 'LLM' / 'secrets.py'), "KEY = sk_live_abc123\n")
        text = self.loader_mod._describe_error(e)
        self.assertIn('NameError', text)
        self.assertNotIn('sk_live_abc123', text)

    def test_other_error_keeps_message(self):
        e = self.raise_in(str(ROOT / 'LLM' / 'backends.py'),
                          "raise ImportError(\"cannot import name 'CPA_API_KEY' from 'LLM.secrets'\")\n")
        self.assertIn("cannot import name 'CPA_API_KEY'", self.loader_mod._describe_error(e))


if __name__ == '__main__':
    unittest.main()
