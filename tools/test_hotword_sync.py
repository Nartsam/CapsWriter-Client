"""热词同步离线验证；只加载标准库模块，不初始化模型或读取用户凭证。"""

import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
(ROOT / 'logs').mkdir(exist_ok=True)


def package(name, path):
    value = types.ModuleType(name)
    value.__path__ = [str(path)]
    value.logger = logging.getLogger('hotword-test')
    sys.modules[name] = value


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package('core', ROOT / 'core')
common = load('core.hotword_sync', ROOT / 'core/hotword_sync.py')
SERVER = (ROOT / 'core/server/hotword_sync.py').exists()
if SERVER:
    package('core.server', ROOT / 'core/server')
    service = load('core.server.hotword_sync', ROOT / 'core/server/hotword_sync.py')
    git_module = load('core.server.hotword_git', ROOT / 'core/server/hotword_git.py')
else:
    import asyncio
    package('core.client', ROOT / 'core/client')
    package('core.client.hotword', ROOT / 'core/client/hotword')
    sys.modules['websockets'] = types.ModuleType('websockets')
    service = load('core.client.hotword.sync', ROOT / 'core/client/hotword/sync.py')


class MergeTests(unittest.TestCase):
    def check(self, name, base, local, remote, expected, conflict=False):
        self.assertEqual(common.merge(name, base, local, remote), (expected, conflict))

    def test_initial_alias_union(self):
        self.check('hot.txt', None, '# 注释\nClaude | 克劳德\n',
                   '# 注释\nClaude | 克劳得\n', '# 注释\nClaude | 克劳德 | 克劳得\n')

    def test_delete_propagates(self):
        self.check('hot.txt', 'A\nB\n', 'A\n', 'A\nB\n', 'A\n')

    def test_alias_delete_and_add(self):
        self.check('hot.txt', 'A | a | b\n', 'A | b\n', 'A | a | b | c\n', 'A | b | c\n')

    def test_delete_modify_conflict(self):
        text, conflict = common.merge('hot.txt', 'A | a\n', '', 'A | b\n')
        self.assertTrue(conflict)
        self.assertIn('<<<<<<< 本机修改\n=======\nA | b\n', text)

    def test_regex_conflict(self):
        text, conflict = common.merge('hot-rule.txt', 'A = a\n', 'A = x\n', 'A = y\n')
        self.assertTrue(conflict)
        self.assertIn('A = x', text)
        self.assertIn('A = y', text)

    def test_independent_changes(self):
        self.check('hot-rule.txt', 'A = a\nB = b\n', 'A = x\nB = b\n',
                   'A = a\nB = y\n', 'A = x\nB = y\n')

    def test_server_lower_concurrent_bonus(self):
        self.check('hot-server.txt', 'A | 4\n', 'A | 6\n', 'A | 5\n', 'A | 5\n')

    def test_server_one_sided_bonus(self):
        self.check('hot-server.txt', 'A | 4\n', 'A | 6\n', 'A | 4\n', 'A | 6\n')

    def test_server_delete(self):
        self.check('hot-server.txt', 'A\nB\n', 'B\n', 'A\nB\n', 'B\n')

    def test_comments_and_spacing(self):
        text = '# 标题\r\n\r\n  A  |  a\r\n# 底部\r\n'
        self.check('hot.txt', text, text, text, text)

    def test_dedup_keeps_comments(self):
        self.check('hot.txt', None, 'A\n# 第二处注释\nA\n\n', '', 'A\n# 第二处注释\n\n')

    def test_different_added_words(self):
        self.check('hot.txt', 'A\n', 'A\nB\n', 'A\nC\n', 'A\nB\nC\n')

    def test_missing_newline(self):
        self.check('hot.txt', None, 'A', 'B', 'A\nB')

    def test_empty_rule_replacement(self):
        common.validate('hot-rule.txt', 'a =\n')

    def test_invalid_regex_and_template(self):
        for text in ('[ = a\n', r'(a) = \2' + '\n'):
            with self.assertRaises(ValueError):
                common.validate('hot-rule.txt', text)

    def test_header_only_generated_block_removed(self):
        body = '# 我的注释\nA\n'
        self.assertEqual(common.resolved_text(common.CONFLICT_HEADER + body), body)
        self.assertEqual(common.resolved_text(common.CONFLICT_HEADER.replace('\n', '\r\n') + body), body)
        with self.assertRaises(ValueError):
            common.resolved_text('<<<<<<< 本机修改\nA\n')

    def test_reorder_conflict(self):
        base = 'A = a\nB = b\nC = c\n'
        text, conflict = common.merge('hot-rule.txt', base, 'B = b\nA = a\nC = c\n',
                                      'A = a\nC = c\nB = b\n')
        self.assertTrue(conflict)

    def test_atomic_utf8(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'logs') as folder:
            path = Path(folder) / 'test.txt'
            common.atomic_write(path, '热词\r\n')
            self.assertEqual(path.read_bytes(), '热词\r\n'.encode())


@unittest.skipUnless(SERVER, '服务端测试')
class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'logs')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = service.HotwordSyncServer(self.root, '', self.root / 'missing')

    def exchange(self, text, base=None, name='hot.txt', **kwargs):
        return self.server.exchange(dict(name=name, text=text, base=base, **kwargs))[0]

    def test_two_clients_and_restart(self):
        self.exchange('A\n')
        self.exchange('A\nB\n', 'A\n')
        answer = self.exchange('A\nC\n', 'A\n')
        self.assertEqual(answer['text'], 'A\nC\nB\n')
        again = service.HotwordSyncServer(self.root, '', self.root / 'missing')
        self.assertEqual(again.state['files']['hot.txt'], answer['text'])

    def test_conflict_does_not_change_active(self):
        self.exchange('A = a\n', name='hot-rule.txt')
        self.exchange('A = b\n', 'A = a\n', 'hot-rule.txt')
        result = self.exchange('A = c\n', 'A = a\n', 'hot-rule.txt')
        self.assertEqual(result['type'], 'conflict')
        self.assertEqual(self.server.state['files']['hot-rule.txt'], 'A = b\n')
        result = self.exchange('A = d\n', result['base'], 'hot-rule.txt')
        self.assertEqual(result['type'], 'ack')

    def test_failed_persistence_no_ack_or_memory_change(self):
        self.exchange('A\n')
        with patch.object(self.server, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.exchange('B\n', 'A\n')
        self.assertEqual(self.server.state['files']['hot.txt'], 'A\n')

    def test_closing_rejects_changes(self):
        self.server.close()
        with self.assertRaises(ValueError):
            self.exchange('B\n')

    def test_no_arbitrary_filename(self):
        with self.assertRaises(ValueError):
            self.exchange('x', name='../config_server.py')

    def test_remote_pending_resolved_by_client(self):
        self.exchange('A = local\n', name='hot-rule.txt')
        self.server.state['pending']['hot-rule.txt'] = {
            'base': 'A = old\n', 'remote': 'A = remote\n', 'token': 'token'}
        result = self.exchange('A = local\n', name='hot-rule.txt')
        self.assertEqual(result['type'], 'conflict')
        result = self.exchange('A = chosen\n', result['base'], 'hot-rule.txt', resolve=result['resolve'])
        self.assertEqual(result['type'], 'ack')
        self.assertFalse(self.server.state['pending'])

    def test_local_conflict_cannot_resolve_unseen_git_conflict(self):
        self.exchange('A = server\n', name='hot-rule.txt')
        self.server.state['pending']['hot-rule.txt'] = {
            'base': 'A = old\n', 'remote': 'A = git\n', 'token': 'git-token'}
        result = self.exchange('A = client\n', 'A = old\n', 'hot-rule.txt')
        self.assertEqual(result['resolve'], '')
        result = self.exchange('A = chosen\n', result['base'], 'hot-rule.txt', resolve='')
        self.assertEqual(result['type'], 'conflict')
        self.assertIn('A = git', result['text'])
        self.assertEqual(result['resolve'], 'git-token')

    def test_edit_server_words_during_fetch(self):
        server = self.server
        server.repository = 'test/repo'
        class Remote:
            parent = 'abc'

            def fetch(self):
                common.atomic_write(server.root / 'hot-server.txt', 'new-word\n')
                return {name: '' for name in common.FILES}

            def push(self, files):
                return True

        with patch.object(git_module, 'HotwordGit', return_value=Remote()):
            server.sync_remote()
        self.assertEqual(common.read_text(server.root / 'hot-server.txt'), 'new-word\n')

    def test_real_git_two_servers_offline_and_no_empty_commit(self):
        bare = self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(bare)],
                       check=True, capture_output=True)
        first = service.HotwordSyncServer(self.root / 'first', str(bare), self.root / 'missing')
        second = service.HotwordSyncServer(self.root / 'second', str(bare), self.root / 'missing')
        first.exchange({'name': 'hot.txt', 'text': 'A\n', 'base': None})
        first.sync_remote()
        second.sync_remote()
        first.exchange({'name': 'hot.txt', 'text': 'A\nB\n', 'base': 'A\n'})
        second.exchange({'name': 'hot.txt', 'text': 'A\nC\n', 'base': 'A\n'})
        second.sync_remote()
        first.sync_remote()
        second.sync_remote()
        self.assertEqual(set(first.state['files']['hot.txt'].split()), {'A', 'B', 'C'})
        self.assertEqual(second.state['files']['hot.txt'], first.state['files']['hot.txt'])
        def head():
            return subprocess.run(['git', '-C', str(bare), 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True).stdout
        before = head()
        first.sync_remote()
        self.assertEqual(head(), before)


@unittest.skipIf(SERVER, '客户端测试')
class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'logs')
        self.addCleanup(self.temp.cleanup)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.addCleanup(self.loop.close)
        self.app = types.SimpleNamespace(base_dir=Path(self.temp.name), loop=self.loop)
        self.client = service.HotwordSyncClient(self.app)
        self.client._notify = lambda text: None

    def write(self, text, conflict=False):
        path = self.client.conflict_path('hot.txt') if conflict else self.app.base_dir / 'hot.txt'
        common.atomic_write(path, text)

    def reply(self, text, response_type='ack', **kwargs):
        request, snapshot = self.client._request('hot.txt')
        self.client._response(request, snapshot, dict(type=response_type, name='hot.txt', text=text, **kwargs))

    def conflict(self):
        self.write('A | local\n')
        self.reply(common.conflict_text('A | local\n', ''), 'conflict', base='', resolve='')

    def test_ack_updates_file_and_baseline(self):
        self.write('A\n')
        self.reply('A\nB\n')
        self.assertEqual(self.client._formal('hot.txt'), 'A\nB\n')
        self.assertEqual(self.client.state['base']['hot.txt'], 'A\nB\n')

    def test_save_while_request_in_flight(self):
        self.write('A\n')
        request, snapshot = self.client._request('hot.txt')
        self.write('A\nC\n')
        self.client._response(request, snapshot, dict(type='ack', name='hot.txt', text='A\nB\n'))
        self.assertEqual(self.client._formal('hot.txt'), 'A\nC\n')
        self.assertEqual(self.client._request('hot.txt')[0]['base'], 'A\n')

    def test_conflict_persists_and_delete_only_after_ack(self):
        self.conflict()
        path = self.client.conflict_path('hot.txt')
        self.assertTrue(path.exists())
        self.assertIsNone(self.client._request('hot.txt'))
        restarted = service.HotwordSyncClient(self.app)
        self.assertIn('hot.txt', restarted.state['pending'])
        self.write(common.CONFLICT_HEADER + '# 保留注释\nA\n', conflict=True)
        request, snapshot = self.client._request('hot.txt')
        self.assertEqual(request['text'], '# 保留注释\nA\n')
        self.assertTrue(path.exists())
        self.client._response(request, snapshot, dict(type='ack', name='hot.txt', text=request['text']))
        self.assertFalse(path.exists())
        self.assertEqual(self.client._formal('hot.txt'), request['text'])

    def test_response_error_keeps_resolution_file(self):
        self.conflict()
        self.write('A\n', conflict=True)
        self.reply('', 'error', message='离线')
        self.assertTrue(self.client.conflict_path('hot.txt').exists())

    def test_resolution_edited_before_ack(self):
        self.conflict()
        self.write('A\n', conflict=True)
        request, snapshot = self.client._request('hot.txt')
        self.write('B\n', conflict=True)
        self.client._response(request, snapshot, dict(type='ack', name='hot.txt', text='A\n'))
        self.assertEqual(self.client._request('hot.txt')[0]['text'], 'B\n')
        self.assertEqual(self.client._request('hot.txt')[0]['base'], 'A\n')

    def test_formal_edit_during_conflict_is_submitted_after_resolution(self):
        self.conflict()
        self.write('A | local\nB\n')
        self.write('A | resolved\n', conflict=True)
        self.reply('A | resolved\n')
        self.assertEqual(self.client._formal('hot.txt'), 'A | local\nB\n')
        request, snapshot = self.client._request('hot.txt')
        self.assertEqual(request['base'], 'A | local\n')
        combined, conflict = common.merge('hot.txt', request['base'], request['text'], 'A | resolved\n')
        self.assertFalse(conflict)
        self.client._response(request, snapshot, dict(type='ack', name='hot.txt', text=combined))
        self.assertEqual(self.client._formal('hot.txt'), 'A | resolved\nB\n')
        self.assertFalse(self.client.conflict_path('hot.txt').exists())

    def test_previous_server_baseline_survives_switch(self):
        self.write('X\n')
        anchor_a = dict(server='a', repository='repo', commit='', text=None)
        anchor_b = dict(server='b', repository='repo', commit='', text=None)
        self.client.peer = 'repo:a'
        self.reply('X\n', anchor=anchor_a)
        self.client.peer = 'repo:b'
        self.reply('X\n', anchor=anchor_b)
        self.write('')
        self.reply('', anchor=anchor_b)
        self.client.peer = 'repo:a'
        request, _ = self.client._request('hot.txt')
        self.assertEqual(request['base'], 'X\n')
        self.assertEqual(request['anchor']['server'], 'a')

    def test_resolution_reedited_during_formal_followup(self):
        self.conflict()
        self.write('A | local\nB\n')
        self.write('A | resolved\n', conflict=True)
        self.reply('A | resolved\n')
        self.write('A | final\n', conflict=True)
        request, snapshot = self.client._request('hot.txt')
        self.assertEqual(request['text'], 'A | final\n')
        self.assertEqual(request['base'], 'A | resolved\n')

    def test_recover_durable_ack(self):
        self.write('A\n')
        self.client.state['apply'] = dict(name='hot.txt', before='A\n', after='B\n', draft=None, candidate='A\n')
        self.client._save()
        recovered = service.HotwordSyncClient(self.app)
        self.assertEqual(recovered._formal('hot.txt'), 'B\n')
        self.assertEqual(recovered.state['base']['hot.txt'], 'B\n')

    def test_watcher_created_replaced_and_two_files(self):
        source = (ROOT / 'core/client/hotword/manager.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == '_HotwordFileHandler')
        import threading
        namespace = dict(FileSystemEventHandler=object, Path=Path, threading=threading,
                         CLIENT_FILES=common.CLIENT_FILES, logger=logging.getLogger('test'))
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<watcher>', 'exec'), namespace)
        manager = types.SimpleNamespace(files={'hot': self.app.base_dir / 'hot.txt',
                                              'rule': self.app.base_dir / 'hot-rule.txt'},
                                        _load_hot=lambda: None, _load_rule=lambda: None, synchronizer=None)
        handler = namespace['_HotwordFileHandler'](manager)
        handler._debounce_delay = 60
        handler.on_created(types.SimpleNamespace(src_path=str(manager.files['hot']), is_directory=False))
        handler.on_moved(types.SimpleNamespace(dest_path=str(manager.files['rule']), is_directory=False))
        self.assertEqual(set(handler._timers), set(manager.files.values()))
        handler.stop()

    def test_restart_waits_for_sync_shutdown(self):
        source = (ROOT / 'core/client/app.py').read_text(encoding='utf-8')
        cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'CapsWriterClient')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_finish_stop')
        events = []

        async def shutdown():
            events.append('sync start')
            await asyncio.sleep(0)
            events.append('sync done')

        async def close():
            events.append('socket closed')

        namespace = dict(asyncio=asyncio, logger=logging.getLogger('test'),
                         subprocess=types.SimpleNamespace(Popen=lambda command: events.append('spawn')),
                         console=types.SimpleNamespace(print=lambda *args: None))
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<shutdown>', 'exec'), namespace)
        app = types.SimpleNamespace(hotword_sync=types.SimpleNamespace(shutdown=shutdown),
                                    ws=types.SimpleNamespace(close=close),
                                    state=types.SimpleNamespace(reset=lambda: events.append('reset')),
                                    loop=types.SimpleNamespace(stop=lambda: events.append('stop')),
                                    _restart_command=['client'])
        self.loop.run_until_complete(namespace['_finish_stop'](app))
        self.assertEqual(events, ['sync start', 'sync done', 'socket closed', 'reset', 'spawn', 'stop'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
