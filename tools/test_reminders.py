"""一次性提醒离线回归：真实规则/存储/调度，隔离网络、桌面与角色凭证。"""
import ast
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import importlib
import inspect
import json
import logging
from pathlib import Path
import sys
import tempfile
import types
from typing import Any, Dict, List, Optional
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
        with self.assertRaises(model.ReminderError):
            model.resolve_time(spec, stamp(), stamp())
        self.assertEqual(model.resolve_time(spec, stamp(), stamp(), implicit_hours=12), stamp(8, 2, 30))

    def test_six_hours_inclusive_and_explicit_tomorrow(self):
        spec = {'kind': 'clock', 'day_offset': None, 'hour': 21, 'minute': 0, 'second': 0}
        self.assertEqual(model.resolve_time(spec, stamp(), stamp()), stamp(7, 21))
        spec.update(day_offset=1, hour=2, minute=30)
        self.assertEqual(model.resolve_time(spec, stamp(), stamp()), stamp(8, 2, 30))

    def test_explicit_past_not_rescheduled(self):
        spec = {'kind': 'clock', 'day_offset': 0, 'hour': 14, 'minute': 30, 'second': 0}
        with self.assertRaises(model.ReminderError):
            model.resolve_time(spec, stamp(), stamp())

    def test_lead_checked_at_completion(self):
        with self.assertRaises(model.ReminderError):
            model.resolve_time(payload(20)['time'], stamp(), stamp() + 10)
        self.assertEqual(model.resolve_time(payload(20)['time'], stamp(), stamp() + 5), stamp() + 20)

    def test_calendar_limit(self):
        spec = {'kind': 'clock', 'day_offset': 2, 'hour': 23, 'minute': 59, 'second': 59}
        self.assertEqual(model.resolve_time(spec, stamp(), stamp()), stamp(9, 23, 59, 59))
        spec.update(day_offset=3, hour=0, minute=0, second=0)
        with self.assertRaises(model.ReminderError):
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

    def test_confirmation_names_day_time_role_and_subject(self):
        item = model.create_reminder(payload(), '助理', '', [], stamp(), stamp())
        text = model.confirmation(item, stamp())
        for part in ('今天', '15:10:00', '助理', '站起来活动一下'):
            self.assertIn(part, text)

    def test_fullscreen_topmost_independent_and_other_monitor(self):
        self.assertTrue(fullscreen.is_fullscreen((1920, 0, 3840, 1080), (1920, 0, 3840, 1080)))
        self.assertFalse(fullscreen.is_fullscreen((0, 0, 1920, 1040), (0, 0, 1920, 1080)))
        self.assertFalse(fullscreen.is_fullscreen((0, 0, 1920, 1080), (0, 0, 1920, 1080), caption=True))
        self.assertFalse(fullscreen.is_fullscreen((1920, 0, 2800, 600), (1920, 0, 3840, 1080)))


class PrefilterTests(unittest.TestCase):
    def test_plain_numbers_do_not_trigger_analysis(self):
        for text in ['请简单介绍一下你自己。', '雾切妹妹，请简单介绍一下你自己。',
                     '解释一下这段代码', '随便输出一段200字左右的文本', '给我三个例子',
                     '1加2等于几', '数字5是什么意思', '一', '半', '你好']:
            with self.subTest(text=text):
                self.assertFalse(analyzer.may_be_reminder(text))

    def test_reminder_words_and_time_expressions_are_independent(self):
        for text in ['5分钟后提醒我一下', '五分钟后提醒我一下', '10分钟后记得让我站起来',
                     '5分钟后起来活动', '五分钟后起来活动', '半小时后喝水', '半个小时后喝水',
                     '一个半小时后喝水', '5 分钟后喝水', '30秒后喝水', '一刻钟后喝水',
                     '两点半喝水', '9:30喝水', '９：３０喝水', '9月14号喝水', '9/14喝水',
                     '2026-09-14喝水', '待会儿叫我', '下班后喝水', '明早喝水',
                     '提醒我喝水', '记得让我起来', '查看待执行提醒', '取消喝水提醒',
                     '每天叫我起来', '把提醒改到晚点', '两三个小时后喝水']:
            with self.subTest(text=text):
                self.assertTrue(analyzer.may_be_reminder(text))


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

    # 下面几项看的是存储里的结果，回复给用户的措辞不钉死
    async def test_create_duplicate_limit_and_cancel(self):
        self.assertTrue(self.create())
        self.assertEqual(len(self.service.store.items), 1)
        self.assertTrue(self.create())                       # 同一事项同一时刻：不重复创建
        self.assertEqual(len(self.service.store.items), 1)
        with patch.object(Config, 'reminder_max_pending', 1):
            self.assertTrue(self.create('喝水'))              # 已到上限：不再创建
        self.assertEqual(len(self.service.store.items), 1)
        item = self.service.store.items[0]
        message = self.service._apply({'operation': 'cancel', 'id': item.id}, '', '', [], stamp(), stamp())
        self.assertTrue(message)
        self.assertEqual(self.service.store.items, [])

    async def test_list_cancel_does_not_guess(self):
        self.create()
        message = self.service._apply({'operation': 'list'}, '', '', [], stamp(), stamp())
        self.assertIn('站起来活动一下', message)
        message = self.service._apply({'operation': 'cancel', 'id': 'invented'}, '', '', [], stamp(), stamp())
        self.assertTrue(message)
        self.assertEqual(len(self.service.store.items), 1)

    async def test_vague_rejection_quotes_the_expression(self):
        message = self.service._apply({'operation': 'reject', 'reason': 'vague_time', 'expression': '待会儿'},
                                      '', '', [], stamp(), stamp())
        self.assertIn('待会儿', message)
        self.assertEqual(self.service.store.items, [])

    async def test_fullscreen_defers_then_merges(self):
        self.due(seconds_ago=4200)
        self.due('活动', 60)
        with patch.object(service_module, 'should_defer', return_value=True):
            await self.service.tick()
        out.show_role.assert_not_awaited()
        self.assertEqual(len(self.service.store.items), 2)
        await self.service.tick()
        out.show_role.assert_awaited_once()
        text = out.show_role.call_args.args[0]
        self.assertIn(model.elapsed_label(4200), text)
        self.assertIn('助理', text)
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

    async def test_delivery_prefix_tolerance_ignores_deferral_history(self):
        for remote in (False, True):
            for paused in (False, True):
                for seconds in (1, 9.9, 10, 10.1, 11):
                    with self.subTest(remote=remote, paused=paused, seconds=seconds):
                        item = self.due(seconds_ago=seconds)
                        if paused:
                            self.service.deferred.add(item.id)
                        client = Mock()
                        client.is_available.return_value = True
                        client.post_reminder_batch.return_value = {'id': 'batch', 'accepted': 1}
                        out.openmate_client.return_value = client
                        with patch.object(Config, 'openmate_enabled', remote):
                            await self.service.tick()
                        text = (client.post_reminder_batch.call_args.args[0][0]['text'] if remote
                                else out.show_role.call_args.args[0])
                        if seconds <= 10:
                            self.assertEqual(text, item.delivery_text)
                        else:
                            self.assertIn(model.elapsed_label(seconds), text)
                        self.assertEqual(self.service.store.items, [])

    async def test_local_merge_labels_roles_but_times_only_late_items(self):
        self.due(seconds_ago=10)
        self.due('活动', 11)
        await self.service.tick()
        text = out.show_role.call_args.args[0]
        self.assertEqual(text.count('助理'), 2)                   # 两条都标出角色
        self.assertIn(model.elapsed_label(11), text)               # 超出容差的才写迟到多久
        self.assertNotIn(model.elapsed_label(10), text)

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
        # <notts:…> 是 OpenMate 约定的「这段不朗读」标记；里面的措辞不钉死
        self.assertTrue(bodies[0]['text'].startswith('<notts:'))
        self.assertIn(model.elapsed_label(4200), bodies[0]['text'])
        self.assertTrue(bodies[0]['text'].endswith('>该喝水了。'))
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
        self.assertTrue(message)
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
            self.assertIn('ValueError', await self.service.handle(self.role, '十分钟后叫我', stamp()))
        self.assertEqual(self.service.store.items, [])
        with patch.object(analyzer, 'analyze', return_value={'operation': 'none'}):
            self.assertIsNone(await self.service.handle(self.role, '解释这句话里的十分钟', stamp()))

    async def test_five_minute_reminder_reaches_analysis_and_storage(self):
        with patch.object(analyzer, 'analyze', return_value=payload(300)) as analyze:
            message = await self.service.handle(self.role, '5分钟后提醒我一下', stamp())
        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.args[2], '5分钟后提醒我一下')
        self.assertIn('提醒已设置', message)
        self.assertEqual(len(self.service.store.items), 1)
        self.assertEqual(self.service.store.items[0].due_at, stamp() + 300)

    async def test_analysis_cancellation_no_commit(self):
        self.app.llm.monitor.should_stop = lambda: True
        with patch.object(analyzer, 'analyze', return_value=payload()):
            # 返回 None 会让这句话再去走普通角色请求，必须由提醒流程给出回复
            self.assertIsNotNone(await self.service.handle(self.role, '十分钟后叫我', stamp()))
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
            self.assertEqual((message.bg, message.fg), (namespace['REMINDER_BG'], namespace['REMINDER_FG']))
            self.assertEqual(message.duration, config.reminder_status_duration)
            self.assertTrue(message.reminder_status)


class AnalyzerRequestTests(unittest.TestCase):
    def test_parse_errors_are_specific_and_snippets_are_bounded(self):
        # 不同的失败原因要给出不同的说明；措辞本身不钉死
        reasons = []
        for text in ['', '[]', '{"operation":[]}', '{"operation":"none","operation":"list"}',
                     '```json\n{}\n```', '{"operation":"edit"}', 'x' * 1000]:
            with self.subTest(text=text[:20]), self.assertRaises(model.ReminderError) as caught:
                with patch.object(analyzer, 'logger') as log:
                    analyzer.decode_result(text)
            reasons.append(str(caught.exception))
            # 解析失败只记 INFO（不往终端刷字），日志里的原文片段有上限
            log.warning.assert_not_called()
            for call in log.info.call_args_list:
                self.assertNotIn('x' * 300, ''.join(str(a) for a in call.args))
        self.assertEqual(len(set(reasons[:4])), 4, reasons[:4])
        self.assertIn('operation', reasons[2])

    def test_field_errors_identify_the_field(self):
        for field, value in [('time', {'kind': 'relative', 'offset_seconds': '300'}),
                             ('openmate_state', 'unknown'), ('subject', ''), ('delivery_text', '<tag>')]:
            data = payload()
            data[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(model.ReminderError,
                    'offset_seconds' if field == 'time' else field):
                model.create_reminder(data, '助理', '', [], stamp(), stamp())
        data = payload()
        del data['delivery_text']
        with self.assertRaisesRegex(model.ReminderError, 'delivery_text'):
            model.create_reminder(data, '助理', '', [], stamp(), stamp())

    def test_delivery_text_is_never_mechanically_prefixed(self):
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
        cases = [('custom', 'responses', False), ('custom', 'responses', True),
                 ('openai', 'openai', False), ('openai', 'openai', True), ('ollama', 'openai', False)]
        for provider, api_format, web in cases:
            with self.subTest(provider=provider, api_format=api_format, web=web):
                role = RoleConfig(name='助理', provider=provider, api_format=api_format, enable_web_search=web,
                                  system_prompt='温柔地提醒', max_tokens=4096,
                                  thinking_level='none', extra_options={'tools': ['forbidden'], 'stream': True,
                                                                      'reasoning': {'effort': 'none'}})
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
                responses = api_format == 'responses' or web
                request = (client.chat if provider == 'ollama' else
                           client.responses.create if responses else client.chat.completions.create)
                kwargs = request.call_args.kwargs
                self.assertIs(kwargs['stream'], False)
                self.assertNotIn('tools', kwargs)
                self.assertNotIn('reasoning', kwargs)
                self.assertNotIn('reasoning_effort', kwargs)
                self.assertNotIn('thinking', kwargs)
                if provider != 'ollama':
                    client.with_options.assert_called_once_with(max_retries=0)
                    if responses:
                        client.chat.completions.create.assert_not_called()
                    else:
                        client.responses.create.assert_not_called()
                messages = kwargs.get('messages', kwargs.get('input'))
                body = json.loads(messages[-1]['content'])
                self.assertEqual(body['utterance'], '解释十分钟')
                self.assertEqual(body['role_style'], role.system_prompt)
                self.assertEqual(handler.client_pool.get_client.call_args.kwargs['request_timeout'], 60)

    def test_anthropic_path_is_isolated_and_nonstreaming(self):
        role = RoleConfig(name='助理', provider='deepseek', api_format='anthropic', enable_web_search=True,
                          system_prompt='温柔地提醒', max_tokens=4096,
                          thinking_level='none', extra_options={'tools': ['forbidden'], 'stream': True,
                                                               'thinking': {'type': 'disabled'},
                                                               'output_config': {'effort': 'low'}})
        client = Mock()
        client.create.return_value = {'stop_reason': 'end_turn',
                                      'content': [{'type': 'text', 'text': '{"operation":"none"}'}]}
        handler = types.SimpleNamespace(client_pool=Mock())
        handler.client_pool.get_client.return_value = client
        self.assertEqual(analyzer.analyze(handler, role, '解释十分钟', [], [], stamp(), 60),
                         {'operation': 'none'})
        pool_kwargs = handler.client_pool.get_client.call_args.kwargs
        self.assertEqual(pool_kwargs['api_format'], 'anthropic')
        self.assertEqual(pool_kwargs['request_timeout'], 60)
        create = importlib.import_module('core.client.llm.llm_anthropic').AnthropicClient.create
        call = inspect.signature(create).bind(client, *client.create.call_args.args,
                                              **client.create.call_args.kwargs)
        call.apply_defaults()
        body = call.arguments['body']
        self.assertNotIn('tools', body)
        self.assertNotIn('stream', body)       # 由客户端的 create() 补成 False
        self.assertNotIn('thinking', body)     # 关掉思考会把相对时间误判为模糊，见 analyzer 注释
        self.assertNotIn('output_config', body)
        self.assertEqual(body['system'], analyzer.SYSTEM)
        self.assertTrue(all(m['role'] in ('user', 'assistant') for m in body['messages']))
        self.assertEqual(json.loads(body['messages'][-1]['content'])['utterance'], '解释十分钟')
        self.assertEqual(call.arguments['max_retries'], 0)

        client.create.return_value = {'stop_reason': 'max_tokens', 'content': []}
        with self.assertRaises(model.ReminderError):
            analyzer.analyze(handler, role, '解释十分钟', [], [], stamp(), 60)


class ProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_recording_archive_image_and_continuous_contract(self):
        source = ast.parse((ROOT / 'core/client/output/result_processor.py').read_text(encoding='utf-8'))
        cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'ResultProcessor')
        methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in {'_handle_message', '_after_output', '_paste_for_foreground',
                                     '_report_image_usage', '_update_continuous_state',
                                     '_print_task_line'}]
        config = types.SimpleNamespace(llm_enabled=True, traditional_convert=False, hot=False, paste=False,
                                       paste_apps=[], save_audio=True)
        namespace = dict(Optional=Optional, RecognitionMessage=Any, Config=config, RoleConfig=RoleConfig,
                         console=Mock(), logger=logging.getLogger('reminder-test'),
                         time=importlib.import_module('time'), AudioFileManager=Mock(),
                         TextOutput=types.SimpleNamespace(strip_punc=lambda value: value),
                         get_active_window_info=lambda: {'process_name': 'editor'},
                         show_processing_indicator=Mock(), hide_recording_indicator=Mock(),
                         update_recording_indicator_progress=Mock(), write_llm_md=Mock(),
                         broadcast_output_udp=Mock(), IMAGE_PLACEHOLDER='[图片]',
                         # 归档改名：真实实现要落盘，这里桩掉并返回空串（等价于未归档）
                         mark_outcome=Mock(return_value=''),
                         format_task_line=importlib.import_module('core.client.task_line').format_task_line)
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<real-processor>', 'exec'), namespace)
        for result_text in ['提醒已设置', model.PARSE_ERROR]:
            with self.subTest(result=result_text):
                # 字段要跟 PendingImage 对齐：少一个 archive_path，_report_image_usage
                # 就会在真实调用里抛 AttributeError 而这里查不出来
                image = types.SimpleNamespace(data_url='image', width=100, height=100,
                                              archive_path='')
                snapshot = types.SimpleNamespace(is_arming=True, is_exiting=False, sticky_role=None)
                state = Mock()
                state.is_task_cancelled.return_value = False
                state.pop_continuous_snapshot.return_value = snapshot
                state.pop_image_snapshot.return_value = image
                state.pop_audio_file.return_value = None
                state.pop_awaiting_result.return_value = None
                result = types.SimpleNamespace(result=result_text, role_name='助理', processed=True,
                                               reminder_handled=True, image_used=False)
                llm = types.SimpleNamespace(process_and_output=AsyncMock(return_value=result))
                hotword = Mock()
                hotword.get_phoneme_corrector.return_value.correct.return_value = types.SimpleNamespace(
                    text='十分钟后叫我', matchs=[], similars=[])
                hotword.get_rule_corrector.return_value.substitute.side_effect = lambda value: value
                processor = types.SimpleNamespace(state=state, app=types.SimpleNamespace(llm=llm),
                    hotword=hotword, diary=Mock(), _resolve_forced_role=Mock(return_value=None),
                    _log_modifier_key_state=Mock(), _streams={},
                    _paste_for_foreground=namespace['_paste_for_foreground'])
                for name in ['_report_image_usage', '_update_continuous_state', '_after_output',
                             '_print_task_line']:
                    setattr(processor, name, types.MethodType(namespace[name], processor))
                message = types.SimpleNamespace(task_id='task', text='十分钟后叫我', is_final=True,
                                               time_complete=stamp()+20, time_submit=stamp(),
                                               time_start=stamp()-5, duration=5)
                await namespace['_handle_message'](processor, message)
                self.assertEqual(llm.process_and_output.call_args.kwargs['reminder_anchor'], stamp())
                state.pop_image_snapshot.assert_called_once_with('task')
                processor.diary.write.assert_called_once_with(message.text, message.time_start, None)
                state.enter_continuous.assert_called_once_with('助理')
                # 挂着的图被消耗了，要在终端说一声（不钉措辞）
                self.assertIn('图片', str(namespace['console'].print.call_args_list))
        namespace['write_llm_md'].assert_not_called()
        namespace['broadcast_output_udp'].assert_not_called()


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
                         capture_selection=Mock(return_value=types.SimpleNamespace(text='', preview='', note='')),
                         to_thread=AsyncMock(side_effect=lambda func, *args: func(*args)),
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
        self.assertIn('image', self.toast.handle_toast_mode.call_args.args)
        self.namespace['broadcast_output_udp'].assert_called_once_with('普通回答')

    async def test_plain_numbers_reach_role_without_reminder_analysis(self):
        # 使用真实提醒入口，防止仅测试正则却漏掉路由仍调用分析或吞掉普通回复。
        with tempfile.TemporaryDirectory(dir=ROOT / 'tools') as directory:
            self.app.base_dir = Path(directory)
            self.app.llm = self.handler
            self.app.reminders = service_module.ReminderService(self.app)
            self.app.reminders.stopped = False
            with patch.object(analyzer, 'analyze', side_effect=RuntimeError('backend unavailable')) as analyze, \
                    patch.object(out, 'show_status', new_callable=AsyncMock) as status:
                for content in ['请简单介绍一下你自己。', '随便输出一段200字左右的文本']:
                    with self.subTest(content=content):
                        self.handler.detect_role.return_value = (self.role, content)
                        self.toast.handle_toast_mode.reset_mock()
                        result = await self.process(self.handler, '助理，' + content,
                                                    reminder_anchor=stamp(), image_data='image')
                        self.assertFalse(result.reminder_handled)
                        self.assertEqual(result.result, '普通回答')
                        self.assertTrue(result.image_used)
                        self.toast.handle_toast_mode.assert_awaited_once()
                analyze.assert_not_called()
                status.assert_not_awaited()
            self.assertEqual(self.app.reminders.store.items, [])

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
