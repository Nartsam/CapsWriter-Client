# coding: utf-8
"""本机 WebSocket 合同验证。测试端使用固定候选/全文，不运行 ASR 模型或真实 LLM。

需要仓库内虚拟环境中的 websockets 13.1；独立于零依赖离线流水线。
"""
from __future__ import annotations

import asyncio
import base64
import json
import types
import unittest

import websockets

import test_recognition_flow as io


class SocketContractTests(io.IOHarness):
    async def make_socket(self):
        await self.make_io()
        await self.app.ws.close()
        self.cases = {}
        self.sessions = {}
        self.server = await websockets.serve(self.serve, '127.0.0.1', 0, subprotocols=['binary'])
        port = self.server.sockets[0].getsockname()[1]
        namespace = self.app.ws.connect.__func__.__globals__
        namespace.update(websockets=websockets, Config=self.config, console=self.console,
                         ConnectionClosedError=websockets.exceptions.ConnectionClosedError,
                         ConnectionClosedOK=websockets.exceptions.ConnectionClosedOK)
        self.app.ws._locked = types.SimpleNamespace(url='ws://127.0.0.1:%d' % port,
                                                    display='local fixture', is_lan=True)
        self.assertTrue(await self.app.ws.connect())
        self.reader = asyncio.create_task(self.read_results())

    async def read_results(self):
        while True:
            result = await self.app.ws.receive()
            if result is None:
                return
            await self.p._handle_message(result)

    async def serve(self, socket):
        workers = []
        try:
            async for raw in socket:
                frame = json.loads(raw)
                task_id = frame['task_id']
                case = self.cases[task_id]
                session = self.sessions.setdefault(task_id, dict(audio=bytearray(), decision='',
                    ended=False, terminal=False, preview=False, controls=[]))
                if frame.get('type') == 'recognition_control':
                    action = frame['action']
                    session['controls'].append(action)
                    if action == 'cancel':
                        if not session['terminal']:
                            session['terminal'] = True
                            await socket.send(self.result(task_id, phase='final', status='cancelled').to_json())
                        continue
                    if session['terminal']:
                        continue
                    session['decision'] = 'full' if action == 'request_full' else 'stream'
                    case['chosen'].set()
                else:
                    session['audio'].extend(base64.b64decode(frame['data']))
                    session['ended'] = frame['is_final']
                    if not session['preview'] and frame['data'] and not case['short']:
                        session['preview'] = True
                        await socket.send(self.result(task_id, phase='partial',
                            text=case['preview'], delta=case['preview']).to_json())
                if session['terminal'] or not session['ended']:
                    continue
                if not session['decision']:
                    await socket.send(self.result(task_id, phase='decision', text=case['preview'],
                                                  delta=case['preview']).to_json())
                elif session['decision'] == 'full':
                    workers.append(asyncio.create_task(self.full_result(socket, task_id)))
                else:
                    session['terminal'] = True
                    await socket.send(self.result(task_id, phase='final', text=case['preview'],
                                                  delta=case['preview'] if case['short'] else '').to_json())
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    def result(self, task_id, phase, text='', delta='', status='ok', mode='stream'):
        result = io.support.message(task_id, text=text, delta=delta, phase=phase, mode=mode, status=status)
        result.time_start = self.cases[task_id]['start']
        result.duration = len(self.sessions[task_id]['audio']) / 4 / 16000
        return result

    async def full_result(self, socket, task_id):
        case, session = self.cases[task_id], self.sessions[task_id]
        await case['allow_full'].wait()
        if not session['terminal']:
            session['terminal'] = True
            await socket.send(self.result(task_id, phase='final', mode='full', text=case['full']).to_json())

    def launch_case(self, task, image=False, short=False, delayed=False, role=True):
        if image:
            self.state.set_pending_image(types.SimpleNamespace(data_url='image', width=1, height=1, archive_path=''))
        task.launch()
        recorder = task.recorder
        self.cases[recorder.task_id] = dict(preview='助理，预览版本' if role else '普通听写',
            full='助理，独立完整版本', chosen=asyncio.Event(), allow_full=asyncio.Event(),
            short=short, start=task.recording_start_time)
        if not delayed:
            self.cases[recorder.task_id]['allow_full'].set()
        for offset, marker in ((0.05, 1), (0.35, 2), (0.4, 3)):
            self.audio(recorder.task_id, marker, task.recording_start_time + offset)
        task.finish()
        return recorder.task_id

    async def wait_until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), 3)

    async def asyncTearDown(self):
        if hasattr(self, 'reader'):
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        await super().asyncTearDown()
        if hasattr(self, 'server'):
            self.server.close()
            await self.server.wait_closed()

    async def test_role_image_and_short_decision_use_full_audio_once(self):
        await self.make_socket()
        task = self.shortcut()
        for image, short in ((False, False), (True, False), (False, True)):
            tid = self.launch_case(task, image=image, short=short)
            await self.wait_until(lambda: tid not in self.state.recognition_sessions)
            call = self.llm.process_and_output.await_args
            self.assertEqual(call.kwargs['forced_content'], '独立完整版本')
            self.assertEqual(call.kwargs['image_data'], 'image' if image else None)
            self.assertEqual(self.sessions[tid]['controls'], ['request_full'])
            self.assertEqual(len(self.sessions[tid]['audio']), 2400 * 4)
            self.assertEqual(len(self.task_lines(tid)), 1)
        self.assertEqual(self.llm.process_and_output.await_count, 3)
        self.assertEqual(self.typed, [])

    async def test_cancel_during_full_and_next_recording_share_connection(self):
        await self.make_socket()
        task = self.shortcut()
        old = self.launch_case(task, image=True, delayed=True)
        await asyncio.wait_for(self.cases[old]['chosen'].wait(), 3)
        self.state.mark_task_cancelled(old)
        await self.p._fail_task(old, '')
        await self.wait_until(lambda: 'cancel' in self.sessions[old]['controls'])
        # 取消退回的图用于下一句；旧 full 的迟到完成事件不会再产生结果。
        new = self.launch_case(task)
        self.cases[old]['allow_full'].set()
        await self.wait_until(lambda: new not in self.state.recognition_sessions)
        self.assertEqual(self.llm.process_and_output.await_count, 1)
        self.assertEqual(self.llm.process_and_output.await_args.kwargs['image_data'], 'image')
        self.assertEqual(len(self.task_lines(old)), 1)

    async def test_pending_full_does_not_consume_next_dictation(self):
        await self.make_socket()
        task = self.shortcut()
        old = self.launch_case(task, delayed=True)
        await asyncio.wait_for(self.cases[old]['chosen'].wait(), 3)
        new = self.launch_case(task, short=True, role=False)
        await self.wait_until(lambda: new not in self.state.recognition_sessions)
        self.assertIn(old, self.state.recognition_sessions)
        self.assertEqual(self.typed, ['普通听写'])
        self.cases[old]['allow_full'].set()
        await self.wait_until(lambda: old not in self.state.recognition_sessions)
        self.assertEqual(self.llm.process_and_output.await_count, 1)
        self.assertEqual(self.sessions[new]['controls'], ['commit_stream'])


if __name__ == '__main__':
    unittest.main()
