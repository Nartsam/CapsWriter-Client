"""当前识别服务端的热词同步连接，不直接访问 Git 或外部存储。"""

import asyncio
import json
from pathlib import Path

import websockets

from core.hotword_sync import (CLIENT_FILES, CONFLICT_HEADER, SYNC_PATH, VERSION,
                              atomic_write, read_text, resolved_text, save_json, validate)
from .. import logger


class HotwordSyncClient:
    def __init__(self, app):
        self.app = app
        self.root = Path(app.base_dir)
        self.directory = self.root / 'logs' / 'hotword_sync'
        self.path = self.directory / 'client.json'
        self.state = json.loads(read_text(self.path)) if self.path.exists() else {'base': {}, 'pending': {}}
        self.state.setdefault('anchors', {})
        self.state.setdefault('peers', {})
        self.peer = None
        self.directory.mkdir(parents=True, exist_ok=True)
        self.task = None
        self.url = None
        self.dirty = set()
        self.wakeup = asyncio.Event()
        self.idle = asyncio.Event()
        self.websocket = None
        self._recover()

    def conflict_path(self, name):
        return self.directory / name.replace('.txt', '.conflict.txt')

    def _formal(self, name):
        path = self.root / name
        return read_text(path) if path.exists() else ''

    def _save(self):
        for name, anchor in self.state['anchors'].items():
            key = anchor['repository'] + ':' + anchor['server']
            if name in self.state['base']:
                self.state['peers'].setdefault(key, {})[name] = {
                    'base': self.state['base'][name], 'anchor': anchor}
        save_json(self.path, self.state)

    def _recover(self):
        # 应用 ack 的日志先于正式文件落盘。中途退出后可幂等完成，避免把旧文件当新删除上传。
        journal = self.state.get('apply')
        if journal:
            self._finish_apply(journal)
        for name, pending in self.state['pending'].items():
            path = self.conflict_path(name)
            if not path.exists():
                atomic_write(path, pending['draft'])

    def _finish_apply(self, journal):
        name, before, after = journal['name'], journal['before'], journal['after']
        current = self._formal(name)
        if current in (before, after):
            atomic_write(self.root / name, after)
            self.state['base'][name] = after
            pending = self.state['pending'].get(name)
            if pending:
                path = self.conflict_path(name)
                if not path.exists() or read_text(path) == journal.get('draft'):
                    if path.exists():
                        path.unlink()
                    self.state['pending'].pop(name, None)
                else:
                    pending.update(base=journal['candidate'], resolve='', accepted=False)
        else:
            self.state['base'][name] = before
            if name in self.state['pending']:
                self.state['pending'][name].update(
                    accepted=True, accepted_draft=journal.get('draft'), base=journal['candidate'], resolve='')
        self.state.pop('apply', None)
        if journal.get('anchor'):
            self.state['anchors'][name] = journal['anchor']
        self._save()

    def changed(self, path):
        path = Path(path).absolute()
        for name in CLIENT_FILES:
            if path in ((self.root / name).absolute(), self.conflict_path(name).absolute()):
                self.app.loop.call_soon_threadsafe(self._mark, name)

    def _mark(self, name):
        self.dirty.add(name)
        self.idle.clear()
        self.wakeup.set()

    def connect(self, url):
        if self.task and not self.task.done() and self.url == url:
            return
        if self.task:
            self.task.cancel()
        self.url = url
        self.task = self.app.loop.create_task(self._run(url))

    def stop(self):
        if self.task and self.app.loop.is_running():
            self.app.loop.call_soon_threadsafe(self.task.cancel)

    async def shutdown(self):
        if self.task and not self.task.done():
            try:
                if self.websocket is not None:
                    for name in CLIENT_FILES:
                        self._mark(name)
                    await asyncio.wait_for(self.idle.wait(), timeout=3)
            except asyncio.TimeoutError:
                logger.warning('退出前热词同步未完成，本地文件保留，下次连接补传')
            finally:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    def _request(self, name):
        formal = self._formal(name)
        observed_formal = formal
        pending = self.state['pending'].get(name)
        known = self.state['peers'].get(self.peer, {}).get(name, {})
        draft = None
        if pending:
            path = self.conflict_path(name)
            if not path.exists():
                return None
            draft = read_text(path)
            if pending.get('accepted') and draft != pending.get('accepted_draft'):
                pending['accepted'] = False
        if pending and not pending.get('accepted'):
            try:
                text = resolved_text(draft)
            except ValueError:
                return None
            base, token = pending['base'], pending.get('resolve', '')
            source = 'conflict'
        else:
            text, base, token, source = formal, known.get('base', self.state['base'].get(name)), '', 'formal'
        validate(name, text)
        request = {'type': 'sync', 'version': VERSION, 'name': name,
                   'base': base, 'text': text, 'resolve': token,
                   'anchor': pending.get('anchor') if pending and not pending.get('accepted') else
                             known.get('anchor', self.state['anchors'].get(name))}
        pending_anchor = pending.get('anchor') if pending else None
        if pending_anchor and known and any(pending_anchor.get(key) != known['anchor'].get(key)
                                             for key in ('repository', 'server')):
            request.update(base=known['base'], anchor=known['anchor'], resolve='')
        if source == 'conflict':
            # 冲突等待期间正式文件可能被继续编辑，要在 ack 后再合并这些修改。
            formal = pending.get('formal', formal)
        return request, {'formal': formal, 'observed_formal': observed_formal, 'draft': draft, 'source': source}

    def _response(self, request, snapshot, response):
        name = request['name']
        if response.get('type') == 'error':
            logger.warning('热词同步未完成：%s', response.get('message', '未知错误'))
            return
        if response.get('name') != name or response.get('type') not in ('ack', 'conflict'):
            raise ValueError('热词同步响应不匹配')
        path = self.conflict_path(name)
        formal_changed = self._formal(name) != snapshot['formal']
        draft_changed = (snapshot['draft'] is not None and
                         (not path.exists() or read_text(path) != snapshot['draft']))
        if response['type'] == 'conflict':
            if self._formal(name) != snapshot['observed_formal'] or draft_changed:
                self._mark(name)
                return
            draft = CONFLICT_HEADER + response['text']
            self.state['pending'][name] = {
                'base': response['base'], 'resolve': response.get('resolve', ''),
                'draft': draft, 'accepted': False, 'formal': snapshot['formal'],
                'anchor': response.get('anchor'),
            }
            self._save()
            atomic_write(path, draft)
            self._notify('热词同步有冲突，请编辑并保存：\n' + str(path))
            return
        validate(name, response['text'])
        if response.get('anchor'):
            self.state['anchors'][name] = response['anchor']
        if draft_changed:
            pending = self.state['pending'][name]
            if snapshot['source'] == 'conflict':
                pending['base'] = request['text']
            pending.update(resolve='', accepted=False)
            self._save()
            self._mark(name)
            return
        if formal_changed:
            # 请求发出后用户又保存了一次：以发出时的正式文件为基线再合并，禁止覆盖新编辑。
            self.state['base'][name] = snapshot['formal']
            if name in self.state['pending']:
                pending = self.state['pending'][name]
                if snapshot['source'] == 'conflict':
                    pending['base'] = request['text']
                pending.update(accepted=True, accepted_draft=snapshot['draft'], resolve='')
            self._save()
            self._mark(name)
            return
        journal = {'name': name, 'before': snapshot['formal'], 'after': response['text'],
                   'draft': snapshot['draft'], 'candidate': request['text'], 'anchor': response.get('anchor')}
        if snapshot['formal'] == response['text'] and name not in self.state['pending']:
            self.state['base'][name] = response['text']
            self._save()
            return
        self.state['apply'] = journal
        self._save()
        self._finish_apply(journal)

    @staticmethod
    def _notify(text):
        try:
            from core.ui.toast_manager import ToastMessage, ToastMessageManager
            ToastMessageManager().add_message(ToastMessage(
                text=text, duration=8000, font_size=16, font_family='Microsoft YaHei UI',
                reminder_status=True))
        except Exception:
            logger.warning(text)

    async def _receive(self, websocket, responses):
        try:
            async for raw in websocket:
                response = json.loads(raw)
                if response.get('type') == 'changed' and response.get('name') in CLIENT_FILES:
                    self._mark(response['name'])
                else:
                    await responses.put(response)
        finally:
            await responses.put(None)
            self.wakeup.set()

    async def _session(self, websocket):
        hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
        if hello.get('type') != 'hello' or hello.get('version') != VERSION:
            raise ValueError('服务端尚不支持本版热词同步')
        self.peer = hello['repository'] + ':' + hello['server']
        responses = asyncio.Queue()
        receiver = asyncio.create_task(self._receive(websocket, responses))
        self.websocket = websocket
        for name in CLIENT_FILES:
            self._mark(name)
        for name in self.state['pending']:
            self._notify('待处理热词冲突：\n' + str(self.conflict_path(name)))
        try:
            while not receiver.done():
                await self.wakeup.wait()
                self.wakeup.clear()
                while self.dirty:
                    name = next(name for name in CLIENT_FILES if name in self.dirty)
                    self.dirty.remove(name)
                    try:
                        item = self._request(name)
                    except (ValueError, OSError, UnicodeError) as error:
                        logger.warning('热词文件暂未同步，修正并保存后重试：%s', error)
                        continue
                    if item is None:
                        continue
                    request, snapshot = item
                    await websocket.send(json.dumps(request, ensure_ascii=False))
                    response = await asyncio.wait_for(responses.get(), timeout=10)
                    if response is None:
                        raise ConnectionError('热词同步连接已断开')
                    self._response(request, snapshot, response)
                self.idle.set()
        finally:
            self.websocket = None
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    async def _run(self, url):
        warned = False
        while True:
            try:
                kwargs = dict(uri=url.rstrip('/') + SYNC_PATH, subprotocols=['binary'],
                              open_timeout=5, close_timeout=1, max_size=None)
                if tuple(int(v) for v in websockets.__version__.split('.')) >= (14,):
                    kwargs['proxy'] = None
                async with websockets.connect(**kwargs) as websocket:
                    warned = False
                    await self._session(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not warned:
                    logger.warning('热词同步暂不可用，保留本机词库：%s', error)
                    warned = True
            # 只重连网络；文件内容由保存事件或重连触发读取，不定时轮询文件。
            await asyncio.sleep(3)
