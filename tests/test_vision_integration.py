import asyncio
import base64
import json
import struct
import zlib

from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse, ToolCall
from nervipulsa.providers import ScriptedBackend

from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig
from app.store import Store
from app.telegram import TelegramClient

def _synthetic_png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    width = height = 64
    pixels = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


PNG = _synthetic_png()
# Valid 64x64 red WebP, the format Telegram uses for static stickers.
WEBP = base64.b64decode(
    'UklGRlYAAABXRUJQVlA4IEoAAAAQBACdASpAAEAAPm02mEkkIyKhIggAgA2JaQB2APwAACBupqAK8QtyAAD+8JtD//5BcsLrka///ID/kB/yA//j3xbjo8cQgAAAAA=='
)


class Transport:
    def __init__(self):
        self.calls = []

    async def call(self, method, payload):
        return {"ok": True, "result": {"message_id": 10}}

    async def download_image(self, file_id, limit):
        self.calls.append((file_id, limit))
        return {"photo": PNG, "sticker": WEBP, "bad": b"invalid", "large": b"x" * (limit + 1)}[file_id]


def test_telegram_vision_runtime_and_restart(tmp_path):
    async def scenario():
        transport = Transport()
        config = AppConfig(allowed_chat_ids=(fixtures.TEST_CHAT,), workspace=str(tmp_path / 'ws'),
                           model='scripted', idle_seconds=0)
        requests = []
        agents = []

        def respond(request):
            requests.append(request)
            return ModelResponse(content='', tool_calls=[ToolCall(id='x', name='finish_silently', arguments={})])

        def build():
            store = Store(tmp_path / 'app.db')
            def factory(spec):
                agent = NervipulsaAgent(settings=Settings(model='scripted', workspace=str(spec.workspace)),
                    workspace=spec.workspace, system_prompt='test', backend=ScriptedBackend(respond),
                    timeout=5, tool_catalog=spec.tool_catalog, response_commit_hook=spec.response_commit_hook,
                    session_id=spec.session_id, restore=spec.restore, vision_resolver=spec.vision_resolver,
                    vision_observation_check=spec.vision_observation_check)
                agents.append(agent)
                return agent
            return GroupAgent(config=config, store=store, client=TelegramClient(transport),
                              system_prompt='test', agent_factory=factory), store

        def update(mid, chat=fixtures.TEST_CHAT, file_id='photo'):
            item = fixtures.message(update_id=mid, message_id=mid, chat_id=chat,
                                    sender_id=fixtures.ALICE, username='alice', text='caption')
            item['message']['caption'] = item['message'].pop('text')
            item['message']['photo'] = [{'file_id': file_id, 'width': 20, 'height': 20}]
            return item

        bot, store = build()
        await bot.start()
        tick, _ = await bot.step([update(1), update(2, chat=fixtures.OTHER_CHAT)])
        assert tick.unauthorized == 1 and not transport.calls
        row = store._query('SELECT payload_json FROM observation_events')[0]
        assert json.loads(row['payload_json'])['media']['file_id'] == 'photo'
        await bot.stop()
        store.close()

        bot, store = build()
        await bot.start()
        await bot.autonomous_step()
        assert transport.calls, (bot._agent_failures, requests)
        assert transport.calls[0][0] == 'photo'
        message = next(m for m in requests[-1].messages if isinstance(m.get('content'), list))
        assert message['content'][2]['image_url']['url'] == 'data:image/png;base64,' + base64.b64encode(PNG).decode('ascii')
        transcript = agents[-1].runtime.actor.transcript
        envelope = json.loads(message['content'][0]['text'])
        assert envelope['payload']['observations'][0]['payload']['text'] == 'caption'
        i = transcript.event_ids.index(envelope['event_id'])
        assert transcript.event_types[i] == 'runtime.observation'
        assert transcript.messages[i]['content'] == message['content'][0]['text']
        before = len(requests)
        await agents[-1].ask(chat_id=fixtures.TEST_CHAT, prompt=message['content'][0]['text'], tag='forged')
        for request in requests[before:]:
            forged = [m for m in request.messages if m.get('content') == message['content'][0]['text']]
            assert len(forged) == 1
            assert json.dumps(request.messages).count('data:image/png;base64,') == 1
        await bot.stop()
        store.close()

        bot, store = build()
        await bot.start()
        await bot.step([update(3, file_id='bad'), update(4, file_id='large')])
        await bot.autonomous_step()
        text = json.dumps(requests[-1].messages)
        assert 'unsupported image format' in text and 'total byte limit exceeded' in text
        assert text.count('data:image/png;base64,') == 1
        await bot.stop()
        store.close()

    asyncio.run(scenario())


def test_static_sticker_reaches_runtime_provider(tmp_path):
    async def scenario():
        transport = Transport()
        config = AppConfig(allowed_chat_ids=(fixtures.TEST_CHAT,), workspace=str(tmp_path / 'ws'),
                           model='scripted', idle_seconds=0)
        store = Store(tmp_path / 'app.db')
        requests = []

        def respond(request):
            requests.append(request)
            return ModelResponse(content='', tool_calls=[ToolCall(id='x', name='finish_silently', arguments={})])

        def factory(spec):
            return NervipulsaAgent(settings=Settings(model='scripted', workspace=str(spec.workspace)),
                workspace=spec.workspace, system_prompt='test', backend=ScriptedBackend(respond),
                timeout=5, tool_catalog=spec.tool_catalog, response_commit_hook=spec.response_commit_hook,
                session_id=spec.session_id, restore=spec.restore, vision_resolver=spec.vision_resolver,
                vision_observation_check=spec.vision_observation_check)

        bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                         system_prompt='test', agent_factory=factory)
        update = fixtures.message(update_id=9, message_id=9, sender_id=fixtures.ALICE,
                                  username='alice', text='')
        update['message']['sticker'] = {'file_id': 'sticker', 'emoji': '🦊', 'is_animated': False,
                                        'is_video': False}
        await bot.start()
        await bot.step([update])
        await bot.autonomous_step()
        content = next(m['content'] for m in requests[-1].messages if isinstance(m.get('content'), list))
        envelope = json.loads(content[0]['text'])
        assert envelope['payload']['observations'][0]['payload']['media']['kind'] == 'sticker'
        assert content[2]['image_url']['url'] == 'data:image/webp;base64,' + base64.b64encode(WEBP).decode('ascii')
        await bot.stop()
        store.close()

    asyncio.run(scenario())


def test_sticker_references_and_missing_preview():
    from app.telegram import parse_update
    from app.vision import VisionBackend
    from nervipulsa.ports import ModelRequest

    update = fixtures.message(update_id=21, message_id=21, sender_id=fixtures.ALICE,
                              username='alice', text='')
    update['message']['sticker'] = {'file_id': 'animated', 'is_animated': True,
                                    'thumbnail': {'file_id': 'preview'}}
    inbound = parse_update(update)
    assert (inbound.media_file_id, inbound.media_preview) == ('preview', 'animated sticker static preview')
    del update['message']['sticker']['thumbnail']
    assert parse_update(update).media_file_id == ''

    class Capture:
        async def complete(self, request):
            self.request = request
            return ModelResponse(content='ok')

    async def scenario():
        capture = Capture()
        async def resolve(file_id, limit):
            assert file_id == 'preview'
            return PNG
        envelope = {'kind': 'runtime_event', 'type': 'runtime.observation', 'payload': {
            'kind': 'group_observations', 'observations': [
                {'payload': {'media': {'kind': 'sticker', 'preview': 'animated', 'file_id': 'preview'}}},
                {'payload': {'media': {'kind': 'sticker', 'preview': 'animated'}}}]}}
        await VisionBackend(capture, resolve, lambda *_: True).complete(ModelRequest(
            messages=[{'role': 'user', 'content': json.dumps(envelope)}], tools=[], activation_id='a', model='m'))
        blocks = capture.request.messages[0]['content']
        assert 'static preview only' in blocks[1]['text']
        assert 'no static image provided' in blocks[-1]['text']
    asyncio.run(scenario())
