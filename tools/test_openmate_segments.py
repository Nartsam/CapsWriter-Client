# coding: utf-8
"""OpenMate 分段与状态发送回归：使用真实发送线程和请求构造，HTTP 仅在内存中收集。"""
import importlib
import logging
from pathlib import Path
import queue
import sys
import threading
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
for name, directory in [('core', 'core'), ('core.client', 'core/client'),
                        ('core.client.llm', 'core/client/llm'), ('core.tools', 'core/tools')]:
    module = types.ModuleType(name)
    module.__path__ = [str(ROOT / directory)]
    module.logger = logging.getLogger('openmate-segments-test')
    module.logger.addHandler(logging.NullHandler())
    module.logger.propagate = False
    sys.modules[name] = module

openmate = importlib.import_module('core.client.llm.llm_output_openmate')


class CaptureClient(openmate.OpenMateClient):
    def __init__(self, fail_method='', reject_status=False):
        super().__init__('http://offline.invalid')
        self.calls = []
        self.arrivals = queue.Queue()
        self.fail_method = fail_method
        self.reject_status = reject_status

    def _request(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        self.arrivals.put((method, path, body))
        if self.reject_status and path == '/v1/presentation/typing' and 'text' in body:
            raise openmate.urllib.error.HTTPError(self.base_url, 400, 'invalid_request', {}, None)
        if method == self.fail_method:
            raise OSError('模拟发送失败')
        return {'id': 'captured-message'}

    def segments(self):
        result = []
        for method, path, body in self.calls:
            if body and path.startswith('/v1/messages'):
                result.extend(body.get('segments', []))
                result.extend(body.get('appendSegments', []))
                if 'text' in body:
                    result.append(body['text'])
        return result


class SegmentTests(unittest.TestCase):
    def make_sender(self, delimiter='\n', client=None):
        client = client or CaptureClient()
        sender = openmate.OpenMateSegmentSender(client, '角色甲', delimiter)
        self.addCleanup(sender.abort)
        return sender, client

    def send(self, chunks, delimiter='\n'):
        sender, client = self.make_sender(delimiter)
        for chunk in chunks:
            sender.push(chunk)
        self.assertTrue(sender.finish())
        self.assertFalse(sender.failed)
        self.assertFalse(sender._thread.is_alive())
        return sender, client

    def assert_segments(self, client, expected):
        self.assertEqual(client.segments(), expected)
        self.assertTrue(all(openmate.strip_state_tags(text).strip()
                            for text in client.segments()))

    def test_own_line_tag_keeps_emotion_with_body(self):
        for tag in ('<state:普通>', '<状态:微笑>'):
            with self.subTest(tag=tag):
                sender, client = self.send([tag + '\n正文'])
                self.assert_segments(client, [tag + '正文'])
                self.assertTrue(sender.delivered)
                self.assertEqual(client.calls[0][0:2], ('POST', '/v1/messages'))
                self.assertEqual(client.calls[0][2]['character'], '角色甲')
                self.assertEqual(client.calls[0][2]['text'], tag + '正文')

    def test_first_body_is_sent_before_finish(self):
        sender, client = self.make_sender()
        sender.push('<state:普通>\n第一条正文\n')
        method, path, body = client.arrivals.get(timeout=2)
        self.assertEqual(body.get('segments'), ['<state:普通>第一条正文'])
        self.assertTrue(body['open'])
        sender.push('第二条正文')
        self.assertTrue(sender.finish())
        self.assert_segments(client, ['<state:普通>第一条正文', '第二条正文'])
        method, path, body = client.calls[-1]
        self.assertEqual((method, path), ('PATCH', '/v1/messages/captured-message'))
        self.assertEqual(body['appendSegments'], ['第二条正文'])
        self.assertIs(body.get('close'), True)

    def test_chunk_boundaries_do_not_change_segments(self):
        text = '<state:普通>\n\n第一条\n\n<状态:微笑>\n\n第二条'
        expected = ['<state:普通>第一条', '<状态:微笑>第二条']
        for cut in range(len(text) + 1):
            with self.subTest(cut=cut):
                sender, client = self.send([text[:cut], text[cut:]], '\n\n')
                self.assert_segments(client, expected)
        sender, client = self.send(list(text), '\n\n')
        self.assert_segments(client, expected)

    def test_consecutive_tags_keep_order_and_skip_blank_lines(self):
        sender, client = self.send(['<state:普通>\n \n<状态:微笑> \t <state:得意>\n',
                                    '\n<state:惊讶>正文'])
        self.assert_segments(client, ['<state:普通><状态:微笑><state:得意><state:惊讶>正文'])

    def test_tags_between_body_segments_attach_forward(self):
        sender, client = self.send(['第一条\n<state:微笑>\n第二条\n第三条'])
        self.assert_segments(client, ['第一条', '<state:微笑>第二条', '第三条'])

    def test_tag_only_reply_makes_no_request(self):
        for text in ('', '\n \t\n', '<state:普通>', '<state:普通>\n',
                     '<state:普通>\n\n<状态:微笑>\n'):
            with self.subTest(text=text):
                sender, client = self.send([text])
                self.assertEqual(client.calls, [])
                self.assertFalse(sender.delivered)

    def test_tag_only_tail_closes_without_appending_empty_segment(self):
        for tail in ('<state:普通>', '<state:普通>\n', '\n<状态:微笑>\n\n'):
            with self.subTest(tail=tail):
                sender, client = self.send(['正文\n' + tail])
                self.assert_segments(client, ['正文'])
                method, path, body = client.calls[-1]
                self.assertEqual((method, path), ('PATCH', '/v1/messages/captured-message'))
                self.assertIs(body.get('close'), True)
                self.assertFalse(body.get('appendSegments'))

    def test_inline_tags_and_unrecognized_text_are_unchanged(self):
        for text in ('<state:普通>你好，<state:微笑>世界', '公式 a<b 且 c>d',
                     '<state:未知状态>正文', '<state:未闭合', '<states:普通>',
                     '<state:' + '长' * 65 + '>'):
            with self.subTest(text=text):
                sender, client = self.send(list(text))
                self.assert_segments(client, [text])

    def test_close_tags_are_dropped_but_open_tags_kept(self):
        text = '<state:normal>第一条\n第二条</state:normal>\n</state>'
        for cut in range(len(text) + 1):
            with self.subTest(cut=cut):
                sender, client = self.send([text[:cut], text[cut:]])
                self.assert_segments(client, ['<state:normal>第一条', '第二条'])

    def test_stream_sender_drops_split_close_tag(self):
        text = '<state:normal>正文</state:normal>'
        for cut in range(len(text) + 1):
            with self.subTest(cut=cut):
                client = CaptureClient()
                sender = openmate.OpenMateStreamSender(client, '角色甲')
                sender.push(text[:cut])
                sender.push(text[cut:])
                self.assertTrue(sender.finish())
                self.assertEqual(''.join(body.get('delta', '') for _, _, body in client.calls if body),
                                 '<state:normal>正文')

    def test_plain_lines_and_custom_delimiter_keep_existing_split(self):
        sender, client = self.send(['\n \n第一条\n\n第二条\n'])
        self.assert_segments(client, ['第一条', '第二条'])
        sender, client = self.send(['<state:普通>|', '|第一条||第二条'], '||')
        self.assert_segments(client, ['<state:普通>第一条', '第二条'])

    def test_abort_does_not_send_pending_tags(self):
        sender, client = self.make_sender()
        sender.push('<state:普通>\n')
        sender.abort()
        self.assertEqual(client.calls, [])
        self.assertFalse(sender.delivered)
        self.assertFalse(sender._thread.is_alive())

    def test_abort_cancels_existing_queue_without_tag_segment(self):
        sender, client = self.make_sender()
        sender.push('正文\n')
        client.arrivals.get(timeout=2)
        sender.push('<state:微笑>\n')
        sender.abort()
        self.assert_segments(client, ['正文'])
        self.assertEqual(client.calls[-1], ('DELETE', '/v1/messages/captured-message', None))

    def test_send_failure_is_reported_for_fallback(self):
        client = CaptureClient(fail_method='PATCH')
        sender, client = self.make_sender(client=client)
        sender.push('<state:普通>\n正文\n')
        client.arrivals.get(timeout=2)
        sender.push('<state:微笑>\n第二条')
        self.assertFalse(sender.finish())
        self.assertTrue(sender.failed)
        self.assertEqual(client.calls[-1], ('DELETE', '/v1/messages/captured-message', None))

    def test_status_is_ordered_with_body_and_does_not_block_producer(self):
        for sender_type in (openmate.OpenMateStreamSender, openmate.OpenMateSegmentSender):
            with self.subTest(sender=sender_type.__name__):
                client = CaptureClient()
                client.set_typing(True)
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                request = client._request

                def blocked_request(method, path, body=None, timeout=None):
                    if body and body.get('text') == '检索提示':
                        entered.set()
                        if not release.wait(2):
                            raise TimeoutError('状态请求未被测试释放')
                    return request(method, path, body, timeout)

                with patch.object(client, '_request', side_effect=blocked_request):
                    sender = sender_type(client)
                    sender.push_status('检索提示')
                    self.assertTrue(entered.wait(2))
                    # 状态 HTTP 尚未返回时继续入队；积压正文中的状态也不能截断发送器。
                    sender.push('第一段\n\n')
                    sender.push_status('正文后的提示')
                    sender.push('第二段')
                    release.set()
                    self.assertTrue(sender.finish())
                client.set_typing(False)
                self.assertFalse(sender.failed)
                self.assertFalse(sender._thread.is_alive())
                self.assertEqual(client.calls[:2], [
                    ('PUT', '/v1/presentation/typing', {'active': True}),
                    ('PUT', '/v1/presentation/typing', {'active': True, 'text': '检索提示'}),
                ])
                self.assertEqual(client.calls[-1], ('PUT', '/v1/presentation/typing', {'active': False}))
                self.assertEqual(sum(method == 'PUT' for method, _, _ in client.calls), 3)
                if sender_type is openmate.OpenMateStreamSender:
                    self.assertEqual(''.join(body.get('delta', '') for _, _, body in client.calls if body),
                                     '第一段\n\n第二段')
                else:
                    self.assert_segments(client, ['第一段', '第二段'])

    def test_old_openmate_rejecting_status_still_receives_reply(self):
        for sender_type in (openmate.OpenMateStreamSender, openmate.OpenMateSegmentSender):
            with self.subTest(sender=sender_type.__name__):
                client = CaptureClient(reject_status=True)
                client.set_typing(True)
                sender = sender_type(client)
                sender.push_status('检索提示')
                sender.push('正文')
                self.assertTrue(sender.finish())
                self.assertFalse(sender.failed)
                self.assertTrue(sender.delivered)
                self.assertEqual(sum(method == 'PUT' for method, _, _ in client.calls), 2)
                if sender_type is openmate.OpenMateStreamSender:
                    self.assertIn(('PATCH', '/v1/streams/captured-message', {'delta': '正文'}), client.calls)
                else:
                    self.assert_segments(client, ['正文'])

    def test_status_only_finish_or_abort_does_not_create_a_message(self):
        for sender_type in (openmate.OpenMateStreamSender, openmate.OpenMateSegmentSender):
            for action in ('finish', 'abort'):
                with self.subTest(sender=sender_type.__name__, action=action):
                    client = CaptureClient()
                    sender = sender_type(client)
                    sender.push_status('检索提示')
                    getattr(sender, action)()
                    client.set_typing(False)
                    self.assertFalse(sender.delivered)
                    self.assertFalse(sender._thread.is_alive())
                    self.assertEqual(client.calls, [
                        ('PUT', '/v1/presentation/typing', {'active': True, 'text': '检索提示'}),
                        ('PUT', '/v1/presentation/typing', {'active': False}),
                    ])


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SegmentTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('运行 %d 项，失败 %d 项' % (result.testsRun, len(result.failures) + len(result.errors)))
    sys.exit(0 if result.wasSuccessful() else 1)
