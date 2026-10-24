# coding: utf-8
"""
LLM 角色 Toast 的状态占位（handle_toast_mode，桩模块加载，不联网、不建窗口）

1. 窗口以「正在思考…」占位创建，正文与工具状态之后都整段取代它
2. 一个可见字都没收到时（空回复、只有情绪标签），收尾前先清掉占位，
   否则 Markdown 最终视图会把占位当成回复定格
3. 点击窗口触发取消后立即返回，迟到正文与错误不更新窗口；生成完成后关窗保留结果

窗口里实际显示什么（整段替换还是续写）由 tools/test_toast_padding.py 第 8 节用真 Tk 验证。
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import logging
from pathlib import Path
import sys
import threading
import types
from typing import Optional, Tuple
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for name in ('core', 'core.tools', 'core.client', 'core.client.llm', 'core.client.output', 'core.ui'):
    module = types.ModuleType(name)
    module.__path__ = []
    module.logger = logging.getLogger('toast-status-test')
    module.logger.addHandler(logging.NullHandler())
    module.logger.propagate = False
    sys.modules[name] = module

text_output = types.ModuleType('core.client.output.text_output')
text_output.TextOutput = types.SimpleNamespace(strip_punc=lambda text: text)
sys.modules[text_output.__name__] = text_output
openmate = types.ModuleType('core.client.llm.llm_output_openmate')
openmate.try_openmate_mode = AsyncMock(return_value=None)       # OpenMate 没开，走本地 Toast
sys.modules[openmate.__name__] = openmate
toast_ui = types.ModuleType('core.ui.toast')
toast_ui.ToastMessage = lambda **kwargs: types.SimpleNamespace(**kwargs)
sys.modules[toast_ui.__name__] = toast_ui

load('core.tools.asyncio_to_thread', 'core/tools/asyncio_to_thread.py')
load('core.client.llm.llm_state_tags', 'core/client/llm/llm_state_tags.py')
consts = load('core.client.llm.llm_constants', 'core/client/llm/llm_constants.py')
roles = load('core.client.llm.llm_role_config', 'core/client/llm/llm_role_config.py')
config_stub = types.ModuleType('config_client')
config_stub.ClientConfig = types.SimpleNamespace(llm_stop_key='esc')
with patch.dict(sys.modules, {'config_client': config_stub}):
    stop = load('core.client.llm.llm_stop_monitor', 'core/client/llm/llm_stop_monitor.py')
toast = load('core.client.llm.llm_output_toast', 'core/client/llm/llm_output_toast.py')


def process_signature():
    """真实 LLMHandler.process 的签名：桩按参数名取回调，参数增减或换位都不影响"""
    source = ast.parse((ROOT / 'core/client/llm/llm_handler.py').read_text(encoding='utf-8'))
    cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'LLMHandler')
    func = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'process')
    namespace = dict(RoleConfig=roles.RoleConfig, Optional=Optional, Tuple=Tuple)
    exec(compile(ast.Module(body=[func], type_ignores=[]), '<process>', 'exec'), namespace)
    return inspect.signature(namespace['process'])


SIGNATURE = process_signature()


class ToastStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = Mock()
        self.manager.add_message.return_value = 'msg'
        self.manager.wait_for_window = AsyncMock(return_value=object())
        toast_ui.ToastMessageManager = lambda: self.manager
        self.monitor = stop.StopMonitor()
        self.monitor.begin_request()
        self.role = roles.RoleConfig(name='助理', output_mode='toast')

    async def run_with(self, script, result):
        """script(arguments) 在「请求线程」里按顺序调用回调，模拟流式输出"""
        def process(*args, **kwargs):
            arguments = SIGNATURE.bind(None, *args, **kwargs).arguments
            script(arguments)
            arguments['toast_request'].finish()
            return result
        handler = types.SimpleNamespace(process=process, monitor=self.monitor)
        return await toast.handle_toast_mode(handler, '问题', self.role, [], '问题', [], None, None)

    def events(self):
        """管理器上与本窗口有关的调用，按发生顺序"""
        return [(c[0], c[1][1] if c[0] == 'update_toast' else None)
                for c in self.manager.method_calls
                if c[0] in ('update_toast', 'finish_toast', 'close_toast')]

    def created_text(self):
        return self.manager.add_message.call_args.args[0].text

    async def test_created_with_placeholder_then_replaced_by_text(self):
        def script(a):
            a['callback']('<state:微笑>今天')
            a['callback']('晴。')
        await self.run_with(script, ('<state:微笑>今天晴。', 5, 0.1))
        self.assertEqual(self.created_text(), toast.LLM_TOAST_THINKING_TEXT)
        self.assertTrue(self.created_text().strip(), '占位不能是空文本')
        self.assertEqual(self.events(), [('update_toast', '今天'), ('update_toast', '今天晴。'),
                                         ('finish_toast', None)])

    async def test_tool_status_replaces_placeholder_before_text(self):
        status = consts.ToolNames.status('web_search')

        def script(a):
            a['status_callback'](status)
            a['callback']('晴。')
        await self.run_with(script, ('晴。', 2, 0.1))
        self.assertEqual(self.events(), [('update_toast', status), ('update_toast', '晴。'),
                                         ('finish_toast', None)])

    async def test_empty_reply_clears_placeholder_before_finish(self):
        for name, script in (('空回复', lambda a: None),
                             ('只有情绪标签', lambda a: a['callback']('<state:微笑>')),
                             ('只有工具状态', lambda a: a['status_callback']('正在联网搜索…'))):
            with self.subTest(name):
                self.manager.reset_mock()
                result = await self.run_with(script, ('', 0, 0.0))
                self.assertEqual(result[0], '', '空回复不能降级成识别原文')
                self.assertEqual(self.events()[-2:], [('update_toast', ''), ('finish_toast', None)])

    async def test_interrupt_closes_without_clearing(self):
        result = await self.run_with(lambda a: a['toast_request'].cancel(), ('半截回答', 3, 0.1))
        self.assertEqual(result, ('', 0, 0.0))
        self.assertEqual(self.events(), [('close_toast', None)])
        self.assertTrue(self.monitor.toast_cancelled())

    async def test_closing_after_generation_preserves_answer(self):
        def script(a):
            a['callback']('完整回答')
            a['toast_request'].finish()
            self.manager.add_message.call_args.args[0].stop_callback()
        result = await self.run_with(script, ('完整回答', 4, 0.1))
        self.assertEqual(result[0], '完整回答')
        self.assertFalse(self.monitor.toast_cancelled())

    async def test_cancel_returns_before_network_and_ignores_late_work(self):
        for fail in (False, True):
            with self.subTest(late_error=fail):
                self.manager.reset_mock()
                self.monitor.end_request()
                self.monitor.begin_request()
                started, finished = asyncio.Event(), asyncio.Event()
                release = threading.Event()
                loop = asyncio.get_running_loop()

                def process(*args, **kwargs):
                    a = SIGNATURE.bind(None, *args, **kwargs).arguments
                    a['callback']('半截回答')
                    loop.call_soon_threadsafe(started.set)
                    try:
                        if not release.wait(5):
                            raise TimeoutError('test worker was not released')
                        a['callback']('迟到正文')
                        a['status_callback']('迟到状态')
                        if fail:
                            raise RuntimeError('late failure')
                        a['toast_request'].finish()
                        return '迟到回答', 3, 0.1
                    finally:
                        loop.call_soon_threadsafe(finished.set)

                handler = types.SimpleNamespace(process=process, monitor=self.monitor)
                task = asyncio.create_task(toast.handle_toast_mode(
                    handler, '问题', self.role, [], '问题', [], None, None))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    self.manager.add_message.call_args.args[0].stop_callback()
                    self.assertEqual(await asyncio.wait_for(task, 2), ('', 0, 0.0))
                    self.assertFalse(release.is_set(), '应在网络返回前就完成本地取消')
                    self.monitor.end_request()
                    self.monitor.begin_request()
                    self.assertEqual((await self.run_with(
                        lambda a: a['callback']('下一轮'), ('下一轮', 3, 0.1)))[0], '下一轮')
                    before = list(self.manager.method_calls)
                    release.set()
                    await asyncio.wait_for(finished.wait(), 2)
                    self.assertEqual(self.manager.method_calls, before, '旧请求不得更新或关闭新窗口')
                    self.assertFalse(self.monitor.toast_cancelled())
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    await asyncio.wait_for(finished.wait(), 2)

    async def test_cancelled_result_skips_reply_archive(self):
        source = ast.parse((ROOT / 'core/client/output/result_processor.py').read_text(encoding='utf-8'))
        cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'ResultProcessor')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_after_output')
        archive, console = Mock(), Mock()
        namespace = dict(RecognitionMessage=object, console=console,
                         Config=types.SimpleNamespace(save_audio=True, llm_enabled=True),
                         write_llm_md=archive)
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<after_output>', 'exec'), namespace)
        processor = types.SimpleNamespace(
            state=types.SimpleNamespace(pop_audio_file=Mock(return_value=None)), diary=Mock(),
            _report_image_usage=Mock(), _update_continuous_state=Mock(), _log_modifier_key_state=Mock())
        result = types.SimpleNamespace(cancelled=True, role_name='助理', processed=True, result='')
        message = types.SimpleNamespace(task_id='t', time_start=1)
        namespace['_after_output'](processor, message, '识别原文', result, None, None)
        archive.assert_not_called()
        processor.diary.write.assert_called_once_with('识别原文', 1, None)
        console.print.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=1)
