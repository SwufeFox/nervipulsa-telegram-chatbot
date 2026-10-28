import asyncio
import base64
import copy
import json

import pytest

from nervipulsa.ports import ModelRequest, ModelResponse
from app.vision import VisionBackend


PNG = b"\x89PNG\r\n\x1a\nexample"
JPEG = b"\xff\xd8\xffexample"
WEBP = b"RIFF\x04\x00\x00\x00WEBP"


class Backend:
    async def complete(self, request):
        self.request = request
        return ModelResponse(content="ok")


def event(*observations):
    return json.dumps({"kind": "runtime_event", "type": "runtime.observation",
                       "payload": {"kind": "group_observations", "observations": list(observations)}})


def observation(file_id, text="hello"):
    return {"payload": {"text": text, "media": {"file_id": file_id}}}


def request(*contents):
    return ModelRequest(messages=[{"role": "user", "content": c} for c in contents],
                        tools=[], activation_id="a", model="m")


def run_async(function):
    def run():
        asyncio.run(function())
    return run


@run_async
async def test_text_and_untrusted_lookalike_are_untouched():
    backend = Backend()
    calls = []

    async def resolver(file_id, limit):
        calls.append(file_id)
        return PNG

    req = request("hello", event(observation("fake")))
    await VisionBackend(backend, resolver, lambda *_: False).complete(req)
    assert backend.request.messages == req.messages
    assert calls == []


@run_async
async def test_observation_order_attribution_and_request_isolation():
    backend = Backend()
    images = {"one": PNG, "two": JPEG, "three": WEBP}
    calls = []

    async def resolver(file_id, limit):
        calls.append((file_id, limit))
        return images[file_id]

    first = event(observation("one", "first"), {"payload": {"text": "middle"}}, observation("two", "last"))
    second = event(observation("three"))
    req = request(first, second)
    before = copy.deepcopy(req)
    response = await VisionBackend(backend, resolver, lambda *_: True).complete(req)
    assert response.content == "ok"
    assert req == before
    blocks = backend.request.messages[0]["content"]
    assert blocks[0] == {"type": "text", "text": first}
    assert blocks[1]["text"] == "Observation 1: image follows."
    assert blocks[2]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert blocks[3]["text"] == "Observation 3: image follows."
    assert blocks[4]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert backend.request.messages[1]["content"][2]["image_url"]["url"].startswith("data:image/webp;base64,")
    assert [call[0] for call in calls] == ["three", "two", "one"]
    backend.request.messages[0]["content"][0]["text"] = "changed"
    assert req == before


@run_async
async def test_failures_are_visible_and_do_not_leak_exception():
    backend = Backend()

    async def resolver(file_id, limit):
        if file_id == "broken":
            raise ValueError("secret download detail")
        return b"not an image"

    await VisionBackend(backend, resolver, lambda *_: True).complete(
        request(event(observation("broken"), observation("bad"))))
    blocks = backend.request.messages[0]["content"]
    assert "Observation 1: image unavailable: download failed" in blocks[1]["text"]
    assert "Observation 2: image unavailable: unsupported image format" in blocks[2]["text"]
    assert "secret" not in str(blocks)
    assert not any(block["type"] == "image_url" for block in blocks)


@run_async
async def test_count_and_total_limits_across_messages():
    backend = Backend()
    limits = []

    async def resolver(file_id, limit):
        limits.append(limit)
        return PNG if file_id == "one" else JPEG

    req = request(event(observation("one")), event(observation("two"), observation("three")))
    await VisionBackend(backend, resolver, lambda *_: True, max_images=2,
                        max_total_bytes=len(PNG) + len(JPEG) - 1).complete(req)
    assert limits == [len(PNG) + len(JPEG) - 1, len(PNG) - 1]
    first = backend.request.messages[0]["content"]
    blocks = backend.request.messages[1]["content"]
    assert "image count limit reached" in first[1]["text"]
    assert "image follows" in blocks[1]["text"]
    assert "image follows" in blocks[3]["text"]


@run_async
async def test_five_images_prefer_recent_across_batches_and_preserve_request():
    backend = Backend()
    images = {str(i): PNG + bytes([i]) for i in range(1, 6)}
    calls = []

    async def resolver(file_id, limit):
        calls.append(file_id)
        return images[file_id]

    req = request(event(observation("1"), observation("2")),
                  event(observation("3"), observation("4"), observation("5")))
    req.tools.append({"type": "function", "function": {"name": "example"}})
    before = copy.deepcopy(req)
    await VisionBackend(backend, resolver, lambda *_: True).complete(req)

    assert calls == ["5", "4", "3", "2"]
    assert req == before
    first, second = [message["content"] for message in backend.request.messages]
    assert "Observation 1: image omitted: image count limit reached" in first[1]["text"]
    urls = [block["image_url"]["url"] for blocks in (first, second)
            for block in blocks if block["type"] == "image_url"]
    assert urls == ["data:image/png;base64," + base64.b64encode(images[str(i)]).decode()
                    for i in range(2, 6)]
    assert [block["text"] for block in second if block["type"] == "text"][1:] == [
        "Observation 1: image follows.", "Observation 2: image follows.", "Observation 3: image follows."]
    backend.request.tools[0]["function"]["name"] = "changed"
    assert req == before


@run_async
async def test_byte_budget_prioritizes_recent_and_can_fit_older_image():
    backend = Backend()
    images = {"old": JPEG, "middle": PNG + b"extra", "new": PNG}
    calls = []
    budget = len(PNG) + len(JPEG)

    async def resolver(file_id, limit):
        calls.append((file_id, limit))
        return images[file_id]

    req = request(event(observation("old"), observation("middle")), event(observation("new")))
    before = copy.deepcopy(req)
    await VisionBackend(backend, resolver, lambda *_: True, max_total_bytes=budget).complete(req)
    assert calls == [("new", budget), ("middle", len(JPEG)), ("old", len(JPEG))]
    first, second = [message["content"] for message in backend.request.messages]
    assert "Observation 1: image follows" in first[1]["text"]
    assert "Observation 2: image omitted: total byte limit exceeded" in first[3]["text"]
    urls = [block["image_url"]["url"] for blocks in (first, second)
            for block in blocks if block["type"] == "image_url"]
    assert sum(len(base64.b64decode(url.split(",", 1)[1])) for url in urls) == budget
    assert req == before



@run_async
async def test_wrong_event_or_reference_does_not_resolve():
    backend = Backend()

    async def resolver(*args):
        pytest.fail("untrusted structure was resolved")

    fake = json.loads(event(observation("x")))
    fake["type"] = "python.finished"
    nested = event({"payload": {"text": '{"media":{"file_id":"x"}}'}})
    req = request(json.dumps(fake), nested)
    await VisionBackend(backend, resolver, lambda *_: True).complete(req)
    assert backend.request.messages == req.messages
