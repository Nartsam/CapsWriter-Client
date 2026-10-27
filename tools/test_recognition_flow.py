# coding: utf-8
"""角色请求整段识别的真实录音、快捷键、协议和有序 writer；外部音频/网络/文件服务用桩。"""
from __future__ import annotations

import array
import asyncio
import base64
import json
import logging
from pathlib import Path
from subprocess import Popen
import tempfile
from threading import Event
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_stream_output as support

ROOT = support.ROOT


class PCM:
    def __init__(self, samples):
        self.samples = list(samples)
        self.shape = (len(self.samples), 1)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        return PCM(self.samples[index])

    def copy(self):
        return PCM(self.samples)

    def tobytes(self):
        return array.array('f', self.samples).tobytes()


class Socket:
    def __init__(self):
        self.frames = []
        self.closed = False
        self.gate = None
        self.error = None
        self.active = 0
        self.max_active = 0

    async def send(self, payload):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            self.frames.append(json.loads(payload))
            await asyncio.sleep(0)
        finally:
            self.active -= 1

    async def close(self):
        self.closed = True


class IOHarness(support.ProcessorHarness):
    async def make_io(self, save=False):
        self.make()
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tools')
        self.addCleanup(self.temp.cleanup)
        self.config.save_audio = save
        self.config.threshold = 0.3
        self.config.mic_seg_duration = 60
        self.config.mic_seg_overlap = 4
        self.config.context = ''
        self.config.language = 'auto'
        directory = Path(self.temp.name)
        self.files = []

        class FileManager:
            file_handle = None
            file_path = None

            def create(manager, channels, start):
                manager.file_path = directory / ('audio-%d.wav' % len(self.files))
                manager.file_handle = manager.file_path.open('wb')
                self.files.append(manager.file_path)
                return manager.file_path, manager.file_handle

            def write(manager, data):
                manager.file_handle.write(data.tobytes())

            def finish(manager):
                if manager.file_handle is not None:
                    manager.file_handle.close()
                    manager.file_handle = None
                return manager.file_path

        async def in_thread(function):
            return function()

        ns = dict(asyncio=asyncio, base64=base64, Config=self.config, Path=Path, Popen=Popen,
            logger=logging.getLogger('recognition-io'), AudioFileManager=FileManager,
            AudioMessage=support.protocol.AudioMessage,
            RecognitionControlMessage=support.protocol.RecognitionControlMessage, to_thread=in_thread,
            np=types.SimpleNamespace(concatenate=lambda blocks: PCM(v for b in blocks for v in b.samples),
                                     mean=lambda data, axis: data))
        self.recorder_class = support.load_class('core/client/audio/recorder.py', 'AudioRecorder', ns)
        ws_ns = dict(asyncio=asyncio, json=json, logger=logging.getLogger('recognition-writer'),
                     RecognitionMessage=support.protocol.RecognitionMessage,
                     CommunicationError=RuntimeError)
        self.ws_class = support.load_class('core/client/connection/websocket_manager.py', 'WebSocketManager', ws_ns)
        self.socket = Socket()
        self.state.websocket = self.socket
        self.app.ws = self.ws_class(self.app)
        self.app.ws._start_writer(self.socket)
        self.app.companion = types.SimpleNamespace(has_connections=False)
        task_ns = dict(asyncio=asyncio, time=time, Event=Event, logger=logging.getLogger('recognition-shortcut'),
            Status=lambda *a, **k: types.SimpleNamespace(start=Mock(), stop=Mock()),
            new_task_id=support.load('core.tools.task_id', 'core/tools/task_id.py').new_task_id,
            StreamSession=support.stream_mod.StreamSession)
        self.task_class = support.load_class('core/client/shortcut/task.py', 'ShortcutTask', task_ns)
        ui = support.sys.modules['core.client.ui']
        for name in ('show_recording_indicator', 'show_waiting_indicator', 'hide_recording_indicator',
                     'show_cancelled_indicator'):
            setattr(ui, name, Mock())
        self.config_patch = patch.dict(support.sys.modules, {
            'config_client': types.SimpleNamespace(ClientConfig=self.config)})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def shortcut(self):
        shortcut = types.SimpleNamespace(key='ctrl_r', streaming=True, hold_mode=True,
            suppress=True, is_toggle_key=lambda: False)
        task = self.task_class(self.app, shortcut, self.recorder_class)
        task.threshold = 0.3
        return task

    def audio(self, task_id, marker, stamp=2.0):
        self.app.loop.call_soon_threadsafe(self.state.queue_in.put_nowait, dict(type='data', task_id=task_id,
            time=stamp, data=PCM([marker] * 2400)))

    async def settle(self):
        for _ in range(8):
            await asyncio.sleep(0)
        if self.p._failures:
            await asyncio.wait_for(asyncio.gather(*list(self.p._failures)), 2)

    async def asyncTearDown(self):
        if hasattr(self, 'app') and hasattr(self.app.ws, '_writer'):
            for session in list(self.state.recognition_sessions.values()):
                if session.owner is not None and session.owner.is_recording:
                    session.owner.abort()
            await self.settle()
            await self.app.ws.close()
            await self.settle()


class ProtocolTests(unittest.TestCase):
    def test_fields_round_trip_and_local_defaults(self):
        audio = support.protocol.AudioMessage('t', 'mic', '', False, 1.0, streaming=True, revisable=True)
        self.assertTrue(support.protocol.AudioMessage.from_dict(json.loads(audio.to_json())).revisable)
        for action in ('request_full', 'commit_stream', 'cancel'):
            control = support.protocol.RecognitionControlMessage('t', action)
            data = json.loads(control.to_json())
            self.assertEqual(data, dict(type='recognition_control', task_id='t', action=action))
            self.assertEqual(support.protocol.RecognitionControlMessage.from_dict(data), control)
        msg = support.message(text='全文', phase='final', mode='full')
        self.assertEqual(support.protocol.RecognitionMessage.from_dict(msg.to_dict()), msg)
        old = msg.to_dict()
        del old['phase']
        self.assertEqual(support.protocol.RecognitionMessage.from_dict(old).phase, 'partial')


class WriterTests(IOHarness):
    async def test_online_results_require_explicit_phase_and_mode(self):
        for missing in ('phase', 'recognition_mode'):
            await self.make_io()
            data = support.message(phase='final', mode='full').to_dict()
            del data[missing]
            self.socket.recv = AsyncMock(return_value=json.dumps(data))
            closed = type('Closed', (Exception,), {})
            self.app.ws.receive.__func__.__globals__['websockets'] = types.SimpleNamespace(
                exceptions=types.SimpleNamespace(ConnectionClosedError=closed, ConnectionClosedOK=closed))
            with self.assertRaisesRegex(RuntimeError, 'phase.*recognition_mode'):
                await self.app.ws.receive()
            await self.app.ws.close()

    async def test_close_before_writer_starts(self):
        await self.make_io()
        await self.app.ws.close()
        self.assertTrue(self.socket.closed)

    async def test_old_close_never_waits_for_or_cancels_new_writer(self):
        await self.make_io()
        closing_started, allow_close = asyncio.Event(), asyncio.Event()
        async def close_old():
            closing_started.set()
            await allow_close.wait()
            self.socket.closed = True
        self.socket.close = close_old
        closing = asyncio.create_task(self.app.ws.close())
        await closing_started.wait()
        new = Socket()
        self.state.websocket = new
        self.app.ws._start_writer(new)
        new_writer = self.app.ws._writer
        allow_close.set()
        await asyncio.wait_for(closing, 2)
        self.assertFalse(new_writer.done())
        self.assertTrue(await self.app.ws.send(support.protocol.RecognitionControlMessage('new', 'cancel')))

    async def test_writer_orders_audio_control_and_end_and_close_waits(self):
        await self.make_io()
        self.socket.gate = asyncio.Event()
        audio = support.protocol.AudioMessage('t', 'mic', '', False, 1.0)
        first = self.app.ws.enqueue(audio)
        control = self.app.ws.enqueue(support.protocol.RecognitionControlMessage('t', 'request_full'))
        last = self.app.ws.enqueue(support.protocol.AudioMessage('t', 'mic', '', True, 1.0))
        await asyncio.sleep(0)
        closing = asyncio.create_task(self.app.ws.close())
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(closing.done())
        self.socket.gate.set()
        self.assertEqual(await asyncio.gather(first, control, last), [True, True, True])
        await closing
        self.assertEqual([x.get('action', x.get('is_final')) for x in self.socket.frames],
                         [False, 'request_full', True])
        self.assertEqual(self.socket.max_active, 1)

    async def test_send_error_resolves_all_futures_and_never_replays_after_reconnect(self):
        await self.make_io()
        self.socket.error = RuntimeError('socket failed')
        msg = support.protocol.RecognitionControlMessage('t', 'cancel')
        futures = [self.app.ws.enqueue(msg) for _ in range(3)]
        self.assertEqual(await asyncio.gather(*futures), [False] * 3)
        new = Socket()
        self.state.websocket = new
        self.app.ws._start_writer(new)
        self.assertFalse(await self.app.ws.send(msg, self.socket))
        self.assertEqual(new.frames, [])

    async def test_disconnect_during_send_settles_inflight_and_queued_futures(self):
        await self.make_io()
        self.socket.gate = asyncio.Event()
        msg = support.protocol.RecognitionControlMessage('t', 'cancel')
        futures = [self.app.ws.enqueue(msg) for _ in range(3)]
        await asyncio.sleep(0)
        self.app.ws._disconnect(self.socket, 'lost')
        self.assertEqual(await asyncio.gather(*futures), [False] * 3)


class RecorderTests(IOHarness):
    async def test_threshold_flush_preserves_every_audio_sample_and_original_duration(self):
        await self.make_io()
        task = self.shortcut()
        task.launch()
        session = self.state.recognition_sessions[task.recorder.task_id]
        await asyncio.sleep(0)
        for offset, marker in ((0.05, 1), (0.35, 2), (0.4, 3)):
            self.audio(session.task_id, marker, session.start_time + offset)
        task.finish()
        await asyncio.wait_for(session.recorder._done, 2)
        self.console.print.assert_not_called()
        samples = array.array('f')
        for frame in self.socket.frames:
            self.assertEqual(frame['task_id'], session.task_id)
            self.assertTrue(frame['streaming'])
            self.assertTrue(frame['revisable'])
            samples.frombytes(base64.b64decode(frame['data']))
        self.assertEqual(list(samples), [1.0] * 800 + [2.0] * 800 + [3.0] * 800)
        self.assertAlmostEqual(self.state.awaiting_results[session.task_id], 0.15)
        self.assertTrue(self.socket.frames[-1]['is_final'])

    async def test_fast_recordings_bind_id_and_snapshots_before_coroutine_runs(self):
        await self.make_io()
        task = self.shortcut()
        self.state.set_pending_image('image A')
        self.state.continuous_role = '助理'
        task.launch()
        first = task.recorder
        self.assertIn(first.task_id, self.state.continuous_snapshots)
        self.assertEqual(self.state.image_snapshots[first.task_id], 'image A')
        self.audio(first.task_id, 1, task.recording_start_time + 1)
        task.finish()
        self.state.set_pending_image('image B')
        task.launch()
        second = task.recorder
        self.audio(second.task_id, 2, task.recording_start_time + 1)
        task.finish()
        await asyncio.wait_for(asyncio.gather(first._done, second._done), 2)
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(self.state.image_snapshots[second.task_id], 'image B')
        self.assertEqual([x['task_id'] for x in self.socket.frames],
                         [first.task_id, first.task_id, second.task_id, second.task_id])

    async def test_short_cancel_does_not_send_and_cannot_poison_next_recording(self):
        await self.make_io()
        task = self.shortcut()
        task.launch()
        first = task.recorder
        self.audio(first.task_id, 1, task.recording_start_time + 0.1)
        task.cancel()
        task.launch()
        second = task.recorder
        self.audio(second.task_id, 2, task.recording_start_time + 1)
        task.finish()
        await asyncio.wait_for(asyncio.gather(first._done, second._done), 2)
        await self.settle()
        self.assertTrue(all(x['task_id'] == second.task_id for x in self.socket.frames))
        self.assertNotIn(first.task_id, self.state.recognition_sessions)

    async def test_shortcut_threshold_applies_to_audio_cache(self):
        await self.make_io()
        task = self.shortcut()
        task.threshold = 1.0
        task.launch()
        recorder = task.recorder
        self.audio(recorder.task_id, 1, task.recording_start_time + 0.5)
        await self.settle()
        self.assertEqual(self.socket.frames, [])
        task.cancel()
        await self.settle()
        self.assertEqual(self.socket.frames, [])

    async def test_abort_after_audio_sends_end_before_cancel_and_deletes_file(self):
        await self.make_io(save=True)
        task = self.shortcut()
        task.launch()
        recorder = task.recorder
        self.audio(recorder.task_id, 1, task.recording_start_time + 1)
        await self.settle()
        task.abort()
        await self.settle()
        self.assertEqual([x.get('action', x.get('is_final')) for x in self.socket.frames],
                         [False, True, 'cancel'])
        self.assertTrue(task.wait_release)
        self.assertFalse(self.files[0].exists())
        self.assertFalse(self.state.audio_files)
        self.assertFalse(self.state.recognition_sessions)
        self.assertEqual(len(self.task_lines(recorder.task_id)), 1)

    async def test_error_during_recording_stops_only_that_recording_and_keeps_file(self):
        await self.make_io(save=True)
        task = self.shortcut()
        task.launch()
        recorder = task.recorder
        self.audio(recorder.task_id, 1, task.recording_start_time + 1)
        await self.settle()
        await self.feed(support.message(recorder.task_id, phase='final', mode='full', status='error'))
        self.assertFalse(task.is_recording)
        self.assertTrue(task.wait_release)
        self.assertTrue(self.files[0].exists())
        self.assertFalse(self.state.audio_files)
        self.assertFalse(self.state.recognition_sessions)
        self.app.diary.write.assert_not_called()

    async def test_waiting_cancel_deletes_saved_recording_and_returns_image(self):
        await self.make_io(save=True)
        self.state.set_pending_image('image')
        task = self.shortcut()
        task.launch()
        recorder = task.recorder
        self.audio(recorder.task_id, 1, task.recording_start_time + 1)
        task.finish()
        await recorder._done
        self.state.mark_task_cancelled(recorder.task_id)
        await self.p._fail_task(recorder.task_id, '')
        self.assertFalse(self.files[0].exists())
        self.assertEqual(self.state.pending_image, 'image')

    async def test_local_recording_error_cancels_remote_cache_but_keeps_local_file(self):
        await self.make_io(save=True)
        task = self.shortcut()
        task.launch()
        recorder = task.recorder
        self.audio(recorder.task_id, 1, task.recording_start_time + 1)
        await self.settle()
        recorder._file_manager.write = Mock(side_effect=BrokenPipeError('encoder failed'))
        self.audio(recorder.task_id, 2, task.recording_start_time + 2)
        await self.settle()
        self.assertEqual([x.get('action', x.get('is_final')) for x in self.socket.frames],
                         [False, True, 'cancel'])
        self.assertTrue(self.files[0].exists())
        self.assertFalse(self.state.recognition_sessions)

    async def test_failed_send_keeps_audio_and_finishes_task_once(self):
        await self.make_io(save=True)
        self.socket.error = RuntimeError('network down')
        task = self.shortcut()
        task.launch()
        recorder = task.recorder
        self.audio(recorder.task_id, 1, task.recording_start_time + 1)
        await self.settle()
        self.assertTrue(self.files[0].exists())
        self.assertFalse(self.state.recognition_sessions)
        self.assertFalse(self.state.audio_files)
        self.assertEqual(len(self.task_lines(recorder.task_id)), 1)

    async def test_zero_audio_finishes_locally(self):
        await self.make_io()
        task = self.shortcut()
        task.launch()
        task.finish()
        await self.settle()
        self.assertFalse(self.state.recognition_sessions)
        self.assertIsNone(self.state.pending_task_id)

    async def test_overlapping_shortcuts_do_not_start_second_consumer(self):
        await self.make_io()
        first, second = self.shortcut(), self.shortcut()
        first.launch()
        second.launch()
        self.assertFalse(second.is_recording)
        self.assertIsNone(second.recorder)
        first.cancel()
        await self.settle()

    async def test_audio_callback_is_ordered_before_finish(self):
        await self.make_io()
        cls = support.load_class('core/client/audio/stream.py', 'AudioStreamManager', dict(
            time=time, update_recording_indicator_level=Mock(), _block_dbfs=lambda data: -10))
        task = self.shortcut()
        task.launch()
        recorder = task.recorder
        cls(self.app)._audio_callback(PCM([1] * 2400), 2400, None, None)
        task.finish()
        await asyncio.wait_for(recorder._done, 2)
        self.assertEqual(len(self.socket.frames), 2)
        self.assertAlmostEqual(self.state.awaiting_results[recorder.task_id], 0.05)


class FileTranscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_and_disconnect_never_write_result_files(self):
        save = Mock()
        cls = support.load_class('core/client/transcribe/file_transcriber.py', 'FileTranscriber', dict(
            logger=logging.getLogger('file-test'), console=Mock(),
            ResultHandler=types.SimpleNamespace(save_results=save)))
        for response in (None, support.message(phase='final', mode='full', status='error')):
            app = types.SimpleNamespace(ws=types.SimpleNamespace(receive=AsyncMock(return_value=response)))
            transcriber = cls(app, Path('fixture.wav'))
            transcriber._apply_hotwords = Mock()
            await transcriber.receive()
            transcriber._apply_hotwords.assert_not_called()
            save.assert_not_called()

    async def test_success_keeps_normal_file_output(self):
        save = Mock(return_value='全文')
        cls = support.load_class('core/client/transcribe/file_transcriber.py', 'FileTranscriber', dict(
            logger=logging.getLogger('file-test'), console=Mock(),
            ResultHandler=types.SimpleNamespace(save_results=save)))
        result = support.message(text='全文', phase='final', mode='full')
        app = types.SimpleNamespace(ws=types.SimpleNamespace(receive=AsyncMock(return_value=result)))
        transcriber = cls(app, Path('fixture.wav'))
        transcriber._apply_hotwords = Mock()
        await transcriber.receive()
        transcriber._apply_hotwords.assert_called_once_with(result)
        save.assert_called_once_with(Path('fixture.wav'), result)


if __name__ == '__main__':
    unittest.main()
