"""一次性提醒离线回归：真实规则/存储/调度，隔离网络、桌面与角色凭证。"""
import ast
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import importlib
import json
import logging
from pathlib import Path
import sys
import tempfile
import types
from typing import Any, Dict, List, Optional, Tuple
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for name, directory in [('core', 'core'), ('core.client', 'core/client'),
                        ('core.client.llm', 'core/client/llm'), ('core.tools', 'core/tools')]:
    module = types.ModuleType(name)
    module.__path__ = [str(ROOT / directory)]
    module.logger = logging.getLogger('reminder-test')
    module.logger.addHandler(logging.NullHandler())
    module.logger.propagate = False
    sys.modules[name] = module

out = types.ModuleType('core.client.reminder.output')
out.show_status = AsyncMock(return_value=True)
out.show_role = AsyncMock(return_value=True)
out.fetch_states = Mock(return_value=[])
out.openmate_client = Mock()
sys.modules[out.__name__] = out
model = importlib.import_module('core.client.reminder.model')
store_module = importlib.import_module('core.client.reminder.store')
analyzer = importlib.import_module('core.client.reminder.analyzer')
service_module = importlib.import_module('core.client.reminder.service')
fullscreen = importlib.import_module('core.client.reminder.fullscreen')
RoleConfig = importlib.import_module('core.client.llm.llm_role_config').RoleConfig
Config = service_module.Config


def stamp(day=7, hour=15, minute=0, second=0):
    return datetime(2026, 9, day, hour, minute, second).timestamp()


def payload(seconds=600, subject='站起来活动一下'):
    return {'operation': 'create', 'time': {'kind': 'relative', 'offset_seconds': seconds},
            'subject': subject, 'delivery_text': '坐得够久了，起来活动一下。', 'openmate_state': ''}


class TimeTests(unittest.TestCase):
    def test_relative_anchor_and_second_precision(self):
        self.assertEqual(model.resolve_time(payload()['time'], stamp() + .8, stamp() + 5), stamp() + 600)

    def test_implicit_clock_cap(self):
        spec = {'kind': 'clock', 'day_offset': None, 'hour': 2, 'minute': 30, 'second': 0}
        with self.assertRaisesRegex(model.ReminderError, '超过 6 小时'):
            model.resolve_time(spec, stamp(), stamp())
        self.assertEqual(model.resolve_time(spec, stamp(), stamp(), implicit_hours=12), stamp(8, 2, 30))

    def test_six_hours_inclusive_and_explicit_tomorrow(self):
        spec = {'kind': 'clock', 'day_offset': None, 'hour': 21, 'minute': 0, 'second': 0}
        self.assertEqual(model.resolve_time(spec, stamp(), stamp()), stamp(7, 21))
        spec.update(day_offset=1, hour=2, minute=30)
        self.assertEqual(model.resolve_time(spec, stamp(), stamp()), stamp(8, 2, 30))

    def test_explicit_past_not_rescheduled(self):
        spec = {'kind': 'clock', 'day_offset': 0, 'hour': 14, 'minute': 30, 'second': 0}
        with self.assertRaisesRegex(model.ReminderError, '已经过去'):
            model.resolve_time(spec, stamp(), stamp())

    def test_lead_checked_at_completion(self):
        with self.assertRaisesRegex(model.ReminderError, '不足 15 秒'):
            model.resolve_time(payload(20)['time'], stamp(), stamp() + 10)
        self.assertEqual(model.resolve_time(payload(20)['time'], stamp(), stamp() + 5), stamp() + 20)

    def test_calendar_limit(self):
        spec = {'kind': 'clock', 'day_offset': 2, 'hour': 23, 'minute': 59, 'second': 59}
        self.assertEqual(model.resolve_time(spec, stamp(), stamp()), stamp(9, 23, 59, 59))
        spec.update(day_offset=3, hour=0, minute=0, second=0)
        with self.assertRaisesRegex(model.ReminderError, '今天、明天和后天'):
            model.resolve_time(spec, stamp(), stamp())

    def test_invalid_types_and_fields(self):
        for spec in [{'kind': 'relative', 'offset_seconds': True}, {'kind': 'relative', 'offset_seconds': 1.5},
                     {'kind': 'relative', 'offset_seconds': -1}, {'kind': 'relative', 'offset_seconds': 600, 'extra': 1}]:
            with self.subTest(spec=spec), self.assertRaises(model.ReminderError):
                model.resolve_time(spec, stamp(), stamp())

    def test_subject_delivery_and_state_are_validated(self):
        for field_name, value in [('subject', ''), ('delivery_text', ''), ('delivery_text', '字' * 301),
                                  ('delivery_text', '<state:smile>你好'), ('openmate_state', 'missing')]:
            data = payload()
            data[field_name] = value
            with self.subTest(field=field_name), self.assertRaises(model.ReminderError):
                model.create_reminder(data, '助理', '', [], stamp(), stamp())

    def test_elapsed_two_nonzero_units(self):
        for seconds, expected in [(0, '刚刚'), (35, '35秒前'), (192, '3分钟12秒前'),
                                  (4205, '1小时10分钟前'), (183665, '2天3小时前')]:
            self.assertEqual(model.elapsed_label(seconds), expected)

    def test_confirmation_exact(self):
        item = model.create_reminder(payload(), '助理', '', [], stamp(), stamp())
        self.assertEqual(model.confirmation(item, stamp()), '提醒已设置：今天 15:10:00「助理」会提醒你站起来活动一下')

    def test_high_recall_and_strict_json(self):
        for text in ['10分钟后记得让我站起来', '十分钟后叫我', '两点半提醒我', '待会儿提醒我', '取消喝水提醒']:
            self.assertTrue(analyzer.may_be_reminder(text))
        for text in ['[]', '{"operation":"none","operation":"list"}', '```json\n{}\n```', '{"operation":"edit"}']:
            with self.assertRaises(model.ReminderError):
                analyzer.decode_result(text)

    def test_fullscreen_topmost_independent_and_other_monitor(self):
        self.assertTrue(fullscreen.is_fullscreen((1920, 0, 3840, 1080), (1920, 0, 3840, 1080)))
        self.assertFalse(fullscreen.is_fullscreen((0, 0, 1920, 1040), (0, 0, 1920, 1080)))
        self.assertFalse(fullscreen.is_fullscreen((0, 0, 1920, 1080), (0, 0, 1920, 1080), caption=True))
        self.assertFalse(fullscreen.is_fullscreen((1920, 0, 2800, 600), (1920, 0, 3840, 1080)))


class StoreTests(unittest.TestCase):
    def test_unrepresentable_timestamp_is_reset(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tools') as directory:
            store = store_module.ReminderStore(Path(directory) / 'reminders.json')
            item = model.Reminder('bad', 1e100, '喝水', '该喝水了。', '助理')
            store.replace([item])
            self.assertEqual(store.load(), 'reset')
            self.assertFalse(store.failed)
            self.assertEqual(store.items, [])
            self.assertEqual(json.loads(store.path.read_text(encoding='utf-8')),
                             {'version': 1, 'items': []})

    def test_missing_file_is_created_as_empty_snapshot(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tools') as directory:
            store = store_module.ReminderStore(Path(directory) / 'nested' / 'reminders.json')
            self.assertEqual(store.load(), 'created')
            self.assertEqual(store.items, [])
            self.assertEqual(json.loads(store.path.read_text(encoding='utf-8')),
                             {'version': 1, 'items': []})

    def test_roundtrip_and_failed_atomic_replace(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tools') as directory:
            store = store_module.ReminderStore(Path(directory) / 'reminders.json')
            item = model.create_reminder(payload(), '助理', '', [], stamp(), stamp())
            store.replace([item])
            saved = store.path.read_bytes()
            self.assertFalse(saved.startswith(b'\xef\xbb\xbf'))
            loaded = store_module.ReminderStore(store.path)
            loaded.load()
            self.assertEqual(loaded.items, [item])
            with patch.object(store_module.os, 'replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    store.replace([])
            self.assertEqual(store.items, [item])
            self.assertEqual(store.path.read_bytes(), saved)
            self.assertFalse(store.path.with_name('reminders.json.tmp').exists())

    def test_corrupt_file_is_rebuilt_as_empty_snapshot(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tools') as directory:
            store = store_module.ReminderStore(Path(directory) / 'reminders.json')
            with store.path.open('w', encoding='utf-8') as stream:
                stream.write('{broken')
            self.assertEqual(store.load(), 'reset')
            self.assertFalse(store.failed)
            self.assertEqual(json.loads(store.path.read_text(encoding='utf-8')),
                             {'version': 1, 'items': []})


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tools')
        self.role = RoleConfig(name='助理', openmate_character='character-a')
        self.app = types.SimpleNamespace(base_dir=Path(self.temp.name), loop=asyncio.get_running_loop(),
            llm=types.SimpleNamespace(resolve_role=Mock(return_value=self.role), monitor=types.SimpleNamespace(should_stop=lambda: False)))
        self.service = service_module.ReminderService(self.app)
        self.service.stopped = False
        self.service.wakeup = asyncio.Event()
        out.show_status.reset_mock()
        out.show_role.reset_mock()
        out.show_status.return_value = True
        out.show_role.return_value = True
        self.clock = patch.object(service_module.time, 'time', return_value=stamp())
        self.clock.start()
        self.probe = patch.object(service_module, 'should_defer', return_value=False)
        self.probe.start()
        self.remote = patch.object(Config, 'openmate_enabled', False)
        self.remote.start()

    async def test_store_is_located_under_logs(self):
        self.assertEqual(self.service.store.path, Path(self.temp.name) / 'logs' / 'reminders.json')

    async def asyncTearDown(self):
        if self.service.task:
            self.service.task.cancel()
            await self.service.task
        self.clock.stop()
        self.probe.stop()
        self.remote.stop()
        self.temp.cleanup()

    def create(self, subject='站起来活动一下', seconds=600):
        return self.service._apply(payload(seconds, subject), '助理', '', [], stamp(), stamp())

    def due(self, subject='喝水', seconds_ago=0):
        item = model.Reminder(str(len(self.service.store.items)), stamp() - seconds_ago,
                              subject, '该喝水了。', '助理')
        self.service.store.replace(self.service.store.items + [item])
        return item

    async def test_create_duplicate_limit_and_cancel(self):
        self.assertIn('提醒已设置', self.create())
        self.assertIn('提醒已存在', self.create())
        with patch.object(Config, 'reminder_max_pending', 1):
            self.assertIn('已达到 1 条', self.create('喝水'))
        item = self.service.store.items[0]
        message = self.service._apply({'operation': 'cancel', 'id': item.id}, '', '', [], stamp(), stamp())
        self.assertIn('提醒已取消', message)
        self.assertEqual(self.service.store.items, [])

    async def test_list_cancel_does_not_guess(self):
        self.create()
        message = self.service._apply({'operation': 'list'}, '', '', [], stamp(), stamp())
        self.assertIn('站起来活动一下', message)
        message = self.service._apply({'operation': 'cancel', 'id': 'invented'}, '', '', [], stamp(), stamp())
        self.assertIn('提醒未取消', message)
        self.assertEqual(len(self.service.store.items), 1)

    async def test_vague_rejection_exact(self):
        message = self.service._apply({'operation': 'reject', 'reason': 'vague_time', 'expression': '待会儿'},
                                      '', '', [], stamp(), stamp())
        self.assertEqual(message, '提醒未设置：“待会儿”未明确指定时间，请给出具体时刻。')

    async def test_fullscreen_defers_then_merges(self):
        self.due(seconds_ago=4200)
        self.due('活动', 60)
        with patch.object(service_module, 'should_defer', return_value=True):
            await self.service.tick()
        out.show_role.assert_not_awaited()
        self.assertEqual(len(self.service.store.items), 2)
        await self.service.tick()
        out.show_role.assert_awaited_once()
        self.assertIn('1小时10分钟前，「助理」：', out.show_role.call_args.args[0])
        self.assertEqual(self.service.store.items, [])

    async def test_startup_neutral_summary_never_remote(self):
        a = self.due(seconds_ago=60)
        b = self.due('活动', 70)
        self.service.startup = {a.id, b.id}
        with patch.object(Config, 'openmate_enabled', True), patch.object(service_module, 'should_defer', return_value=True):
            await self.service.tick()
        out.show_status.assert_awaited_once()
        self.assertIn('活动', out.show_status.call_args.args[0])
        self.assertNotIn('该喝水了。', out.show_status.call_args.args[0])
        out.show_role.assert_not_awaited()
        self.assertEqual(self.service.store.items, [])

    async def test_failed_local_window_retains_pending(self):
        self.due()
        out.show_role.return_value = False
        await self.service.tick()
        self.assertEqual(len(self.service.store.items), 1)
        self.assertEqual(self.service.inflight, set())

    async def test_openmate_notts_character_and_state_revalidation(self):
        item = self.due(seconds_ago=4200)
        item.openmate_character = 'character-a'
        item.openmate_state = 'removed'
        self.due('活动', 30)
        client = Mock()
        client.is_available.return_value = True
        client.get_states.return_value = {'states': [{'id': 'smile'}]}
        client.post_reminder_batch.return_value = {'id': 'batch', 'accepted': 2}
        out.openmate_client.return_value = client
        with patch.object(Config, 'openmate_enabled', True):
            await self.service.tick()
        bodies = client.post_reminder_batch.call_args.args[0]
        self.assertEqual(bodies[0]['text'], '<notts:1小时10分钟前，>该喝水了。')
        self.assertEqual(bodies[0]['character'], 'character-a')
        self.assertNotIn('state', bodies[0])
        self.assertNotIn('character', bodies[1])
        self.assertNotIn('助理', bodies[0]['text'])
        out.show_role.assert_not_awaited()
        self.assertEqual(self.service.store.items, [])

    async def test_openmate_error_falls_back_once(self):
        self.due()
        client = Mock()
        client.is_available.return_value = True
        client.post_reminder_batch.side_effect = TimeoutError()
        out.openmate_client.return_value = client
        with patch.object(Config, 'openmate_enabled', True):
            await self.service.tick()
        out.show_role.assert_awaited_once()
        self.assertEqual(self.service.store.items, [])

    async def test_inflight_cannot_cancel(self):
        item = self.due()
        self.service.inflight.add(item.id)
        message = self.service._apply({'operation': 'cancel', 'id': item.id}, '', '', [], stamp(), stamp())
        self.assertIn('提醒未取消', message)
        self.assertEqual(len(self.service.store.items), 1)

    async def test_restart_future_grace_and_expiration(self):
        self.due('recent', 30)
        self.due('old', 21601)
        self.create()
        await self.service.start()
        self.service.task.cancel()
        try:
            await self.service.task
        except asyncio.CancelledError:
            pass
        self.service.task = None
        self.assertEqual(len(self.service.store.items), 2)
        self.assertEqual(len(self.service.startup), 1)
        await self.service.tick()
        self.assertEqual(len(self.service.store.items), 1)
        self.assertGreater(self.service.store.items[0].due_at, stamp())

    async def test_analyzer_failure_and_none_paths(self):
        with patch.object(analyzer, 'analyze', side_effect=ValueError('bad response')):
            self.assertEqual(await self.service.handle(self.role, '十分钟后叫我', stamp()), model.PARSE_ERROR)
        self.assertEqual(self.service.store.items, [])
        with patch.object(analyzer, 'analyze', return_value={'operation': 'none'}):
            self.assertIsNone(await self.service.handle(self.role, '解释这句话里的十分钟', stamp()))

    async def test_analysis_cancellation_no_commit(self):
        self.app.llm.monitor.should_stop = lambda: True
        with patch.object(analyzer, 'analyze', return_value=payload()):
            self.assertIn('已取消', await self.service.handle(self.role, '十分钟后叫我', stamp()))
        self.assertEqual(self.service.store.items, [])


class StatusStyleTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_status_output_uses_dedicated_style_and_validates_size(self):
        source = ast.parse((ROOT / 'core/client/reminder/output.py').read_text(encoding='utf-8'))
        method = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'show_status')
        manager = Mock()
        manager.wait_for_window = AsyncMock(return_value=object())
        config = types.SimpleNamespace(reminder_status_font_size=18, reminder_status_duration=6000)
        namespace = dict(ToastMessageManager=lambda: manager, ToastMessage=types.SimpleNamespace, Config=config,
                         REMINDER_FONT_SIZE=18, REMINDER_BG='#FFF4D6', REMINDER_FG='#332B1A',
                         logger=logging.getLogger('reminder-test'))
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<real-status-output>', 'exec'), namespace)
        for requested, expected in [(18, 18), (20, 20), (True, 18), (0, 18), ('20', 18), (100, 18)]:
            config.reminder_status_font_size = requested
            self.assertTrue(await namespace['show_status']('提醒已设置'))
            message = manager.add_message.call_args.args[0]
            self.assertEqual(message.font_size, expected)
            self.assertEqual((message.bg, message.fg, message.window_alpha), ('#FFF4D6', '#332B1A', 1.0))
            self.assertEqual(message.duration, 6000)
            self.assertTrue(message.reminder_status)


class AnalyzerRequestTests(unittest.TestCase):
    def test_natural_delivery_is_requested_and_never_mechanically_prefixed(self):
        self.assertIn('避免仅照抄 subject', analyzer.SYSTEM)
        self.assertIn('不要固定套用前缀', analyzer.SYSTEM)
        for delivery in ['该站起来活动一下了。', '记得站起来活动一下', '该去喝水了']:
            data = payload()
            data['delivery_text'] = delivery
            item = model.create_reminder(data, '助理', '', [], stamp(), stamp())
            self.assertEqual(item.delivery_text, delivery)
            self.assertEqual(item.subject, data['subject'])

    def test_openmate_batch_http_contract(self):
        source = ast.parse((ROOT / 'core/client/llm/llm_output_openmate.py').read_text(encoding='utf-8'))
        cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'OpenMateClient')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'post_reminder_batch')
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<batch-contract>', 'exec'), namespace)
        client = types.SimpleNamespace(_request=Mock(return_value={'id': 'accepted', 'accepted': 1}))
        items = [{'id': 'reminder', 'text': '<notts:1分钟前，>该喝水了。'}]
        self.assertEqual(namespace['post_reminder_batch'](client, items, 'request'),
                         {'id': 'accepted', 'accepted': 1})
        client._request.assert_called_once_with('POST', '/v1/message-batches',
            {'protocolVersion': 1, 'requestId': 'request', 'items': items})

    def test_backend_paths_are_isolated_and_nonstreaming(self):
        for provider, web in [('cpa', False), ('cpa', True), ('ollama', False)]:
            with self.subTest(provider=provider, responses=web):
                role = RoleConfig(name='助理', provider=provider, enable_web_search=web,
                                  system_prompt='温柔地提醒', max_tokens=4096,
                                  extra_options={'tools': ['forbidden'], 'stream': True})
                client = Mock()
                client.with_options.return_value = client
                reply = '{"operation":"none"}'
                client.chat.completions.create.return_value = types.SimpleNamespace(choices=[
                    types.SimpleNamespace(finish_reason='stop', message=types.SimpleNamespace(content=reply))])
                client.responses.create.return_value = types.SimpleNamespace(status='completed', output_text=reply)
                client.chat.return_value = types.SimpleNamespace(message=types.SimpleNamespace(content=reply))
                handler = types.SimpleNamespace(client_pool=Mock())
                handler.client_pool.get_client.return_value = client
                self.assertEqual(analyzer.analyze(handler, role, '解释十分钟', [], [], stamp(), 60),
                                 {'operation': 'none'})
                request = (client.chat if provider == 'ollama' else
                           client.responses.create if web else client.chat.completions.create)
                kwargs = request.call_args.kwargs
                self.assertIs(kwargs['stream'], False)
                self.assertNotIn('tools', kwargs)
                messages = kwargs.get('messages', kwargs.get('input'))
                body = json.loads(messages[-1]['content'])
                self.assertEqual(set(body), {'anchor', 'role_style', 'pending', 'allowed_states', 'utterance'})
                self.assertEqual(body['role_style'], role.system_prompt)
                self.assertEqual(handler.client_pool.get_client.call_args.kwargs['request_timeout'], 60)


class ProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_recording_archive_image_and_continuous_contract(self):
        source = ast.parse((ROOT / 'core/client/output/result_processor.py').read_text(encoding='utf-8'))
        cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'ResultProcessor')
        methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in {'_handle_message', '_report_image_usage', '_update_continuous_state'}]
        config = types.SimpleNamespace(llm_enabled=True, traditional_convert=False, hot=False, paste=False,
                                       paste_apps=[], enter_apps=[('editor', 0)], save_audio=True)
        namespace = dict(Optional=Optional, RecognitionMessage=Any, Config=config, RoleConfig=RoleConfig,
                         console=Mock(), logger=logging.getLogger('reminder-test'),
                         time=importlib.import_module('time'), asyncio=asyncio, AudioFileManager=Mock(),
                         TextOutput=types.SimpleNamespace(strip_punc=lambda value: value),
                         get_active_window_info=lambda: {'process_name': 'editor'},
                         show_processing_indicator=Mock(), hide_recording_indicator=Mock(),
                         update_recording_indicator_progress=Mock(), write_llm_md=Mock(),
                         broadcast_output_udp=Mock(), _auto_enter=AsyncMock(), IMAGE_PLACEHOLDER='[图片]')
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<real-processor>', 'exec'), namespace)
        for result_text in ['提醒已设置', model.PARSE_ERROR]:
            with self.subTest(result=result_text):
                image = types.SimpleNamespace(data_url='image', width=100, height=100)
                snapshot = types.SimpleNamespace(is_arming=True, is_exiting=False, sticky_role=None)
                state = Mock()
                state.is_task_cancelled.return_value = False
                state.pop_continuous_snapshot.return_value = snapshot
                state.pop_image_snapshot.return_value = image
                state.pop_audio_file.return_value = None
                result = types.SimpleNamespace(result=result_text, role_name='助理', processed=True,
                                               reminder_handled=True, image_used=False)
                llm = types.SimpleNamespace(process_and_output=AsyncMock(return_value=result))
                hotword = Mock()
                hotword.get_phoneme_corrector.return_value.correct.return_value = types.SimpleNamespace(
                    text='十分钟后叫我', matchs=[], similars=[])
                hotword.get_rule_corrector.return_value.substitute.side_effect = lambda value: value
                processor = types.SimpleNamespace(state=state, app=types.SimpleNamespace(llm=llm),
                    hotword=hotword, diary=Mock(), _resolve_forced_role=Mock(return_value=None),
                    _log_modifier_key_state=Mock())
                for name in ['_report_image_usage', '_update_continuous_state']:
                    setattr(processor, name, types.MethodType(namespace[name], processor))
                message = types.SimpleNamespace(task_id='task', text='十分钟后叫我', is_final=True,
                                               time_complete=stamp()+20, time_submit=stamp(),
                                               time_start=stamp()-5, duration=5)
                await namespace['_handle_message'](processor, message)
                self.assertEqual(llm.process_and_output.call_args.kwargs['reminder_anchor'], stamp())
                state.pop_image_snapshot.assert_called_once_with('task')
                processor.diary.write.assert_called_once_with(message.text, message.time_start, None)
                state.enter_continuous.assert_called_once_with('助理')
                self.assertIn('提醒任务不使用图片', str(namespace['console'].print.call_args_list))
        namespace['write_llm_md'].assert_not_called()
        namespace['broadcast_output_udp'].assert_not_called()
        namespace['_auto_enter'].assert_not_called()


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 执行真实入口方法，隔离加载角色文件、剪贴板、网络和桌面；绝不导入角色凭证。
        source = ast.parse((ROOT / 'core/client/llm/llm_handler.py').read_text(encoding='utf-8'))
        result_class = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'LLMResult')
        handler_class = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'LLMHandler')
        method = next(node for node in handler_class.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == 'process_and_output')
        namespace = dict(dataclass=dataclass, field=field, Optional=Optional, List=List, RoleConfig=RoleConfig,
                         logger=logging.getLogger('reminder-test'), broadcast_output_udp=Mock(),
                         __name__='core.client.llm.llm_handler_test', __package__='core.client.llm')
        exec(compile(ast.Module(body=[result_class, method], type_ignores=[]), '<real-routing>', 'exec'), namespace)
        self.namespace = namespace
        self.process = namespace['process_and_output']
        self.role = RoleConfig(name='助理', output_mode='toast', enable_vision=True)
        self.app = types.SimpleNamespace(reminders=types.SimpleNamespace(handle=AsyncMock(return_value='提醒已设置')),
            companion=types.SimpleNamespace(has_connections=False, emit_user_message=AsyncMock()), state=Mock())
        self.handler = types.SimpleNamespace(app=self.app, monitor=Mock(),
                                            detect_role=Mock(return_value=(self.role, '十分钟后叫我')))
        typing = types.ModuleType('core.client.llm.llm_output_typing')
        typing.handle_typing_mode = AsyncMock(return_value=('普通回答', 1, 1))
        typing.output_text = AsyncMock()
        toast = types.ModuleType('core.client.llm.llm_output_toast')
        toast.handle_toast_mode = AsyncMock(return_value=('普通回答', 1, 1))
        text = types.ModuleType('core.client.output.text_output')
        text.TextOutput = Mock()
        self.toast = toast
        self.modules = patch.dict(sys.modules, {typing.__name__: typing, toast.__name__: toast, text.__name__: text})
        self.modules.start()

    async def asyncTearDown(self):
        self.modules.stop()

    async def test_named_reminder_precedes_companion_and_never_outputs_image_or_udp(self):
        self.app.companion.has_connections = True
        result = await self.process(self.handler, '助理，十分钟后叫我', reminder_anchor=stamp(), image_data='image')
        self.assertTrue(result.reminder_handled)
        self.assertTrue(result.processed)
        self.assertFalse(result.image_used)
        self.app.reminders.handle.assert_awaited_once_with(self.role, '十分钟后叫我', stamp())
        self.app.companion.emit_user_message.assert_not_awaited()
        self.toast.handle_toast_mode.assert_not_awaited()
        self.app.state.set_output_text.assert_not_called()
        self.namespace['broadcast_output_udp'].assert_not_called()

    async def test_none_restores_normal_request_with_image(self):
        self.app.reminders.handle.return_value = None
        result = await self.process(self.handler, '解释十分钟', reminder_anchor=stamp(), image_data='image')
        self.assertFalse(result.reminder_handled)
        self.assertTrue(result.image_used)
        self.assertEqual(self.toast.handle_toast_mode.call_args.args[-1], 'image')
        self.namespace['broadcast_output_udp'].assert_called_once_with('普通回答')

    async def test_forced_named_role_is_eligible(self):
        result = await self.process(self.handler, '十分钟后叫我', forced_role=self.role, reminder_anchor=stamp())
        self.assertTrue(result.reminder_handled)
        self.handler.detect_role.assert_not_called()

    async def test_default_dictation_and_nonrecording_skip_reminders(self):
        for role, anchor in [(RoleConfig(), stamp()), (None, stamp()), (self.role, None)]:
            with self.subTest(role=role, anchor=anchor):
                self.handler.detect_role.return_value = (role, '十分钟后叫我')
                result = await self.process(self.handler, '十分钟后叫我', reminder_anchor=anchor)
                self.assertFalse(result.reminder_handled)
        self.app.reminders.handle.assert_not_awaited()


if __name__ == '__main__':
    unittest.main(testRunner=unittest.TextTestRunner(stream=sys.stdout, verbosity=1))
