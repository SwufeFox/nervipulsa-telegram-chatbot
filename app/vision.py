"""Optional vision adapter for an OpenAI-compatible ModelBackend.

Integration: construct VisionBackend(backend, resolver, is_trusted). The async
resolver(file_id, max_bytes) returns image bytes and must enforce max_bytes
while downloading (including when the remote size is unknown). A Telegram
integration can implement this using its own authenticated file API; no
Telegram client or network access is included here.

The runtime projects events into ordinary user-message strings, so their JSON
shape alone is not proof of origin. is_trusted(index, message, envelope) must
verify the message against trusted runtime event provenance, not merely inspect
its contents. It must reject user-supplied lookalikes. Only media.file_id inside
an observation's payload is resolved. The caller should ensure the wrapped
backend supports OpenAI Chat Completions image_url data URLs.
"""
from __future__ import annotations

import base64
import copy
import json
from dataclasses import replace
from typing import Any, Awaitable, Callable

from nervipulsa.ports import ModelBackend, ModelRequest, ModelResponse

Resolver = Callable[[str, int], Awaitable[bytes]]
TrustCheck = Callable[[int, dict[str, Any], dict[str, Any]], bool]


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class VisionBackend:
    def __init__(self, backend: ModelBackend, resolver: Resolver, is_trusted: TrustCheck,
                 *, max_images: int = 4, max_total_bytes: int = 10 * 1024 * 1024) -> None:
        if max_images < 0 or max_total_bytes < 0:
            raise ValueError("vision limits must be non-negative")
        self.backend = backend
        self.resolver = resolver
        self.is_trusted = is_trusted
        self.max_images = max_images
        self.max_total_bytes = max_total_bytes

    async def complete(self, request: ModelRequest) -> ModelResponse:
        # The backend receives an isolated request, including for text-only calls.
        messages = copy.deepcopy(request.messages)
        candidates: list[tuple[int, int, dict[str, Any]]] = []
        trusted_events: dict[int, tuple[str, dict[str, Any]]] = {}
        for index, message in enumerate(messages):
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, str):
                continue
            try:
                event = json.loads(content)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("kind") != "runtime_event" or event.get("type") != "runtime.observation":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("kind") != "group_observations" or not isinstance(payload.get("observations"), list):
                continue
            if not self.is_trusted(index, copy.deepcopy(message), copy.deepcopy(event)):
                continue
            trusted_events[index] = (content, payload)
            for position, observation in enumerate(payload["observations"], 1):
                if not isinstance(observation, dict) or not isinstance(observation.get("payload"), dict):
                    continue
                media = observation["payload"].get("media")
                if not isinstance(media, dict) or media.get("kind") not in {None, "photo", "sticker"}:
                    continue
                candidates.append((index, position, media))

        results: dict[tuple[int, int], list[dict[str, Any]]] = {}
        count = 0
        total = 0
        for index, position, media in reversed(candidates):
            label = f"Observation {position}"
            blocks: list[dict[str, Any]] = []
            if not isinstance(media.get("file_id"), str) or not media["file_id"]:
                blocks.append({"type": "text", "text": f"{label}: image unavailable: no static image provided; the image was not provided to the model."})
            else:
                if media.get("preview"):
                    blocks.append({"type": "text", "text": f"{label}: animated sticker static preview only."})
                if count >= self.max_images:
                    reason = "image omitted: image count limit reached"
                elif total >= self.max_total_bytes:
                    reason = "image omitted: total byte limit reached"
                else:
                    count += 1
                    remaining = self.max_total_bytes - total
                    try:
                        data = await self.resolver(media["file_id"], remaining)
                    except Exception:
                        reason = "image unavailable: download failed"
                    else:
                        if not isinstance(data, bytes):
                            reason = "image unavailable: resolver returned invalid data"
                        elif len(data) > remaining:
                            reason = "image omitted: total byte limit exceeded"
                        elif (mime := _image_type(data)) is None:
                            reason = "image unavailable: unsupported image format"
                        else:
                            total += len(data)
                            blocks.append({"type": "text", "text": f"{label}: image follows."})
                            encoded = base64.b64encode(data).decode("ascii")
                            blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
                            results[index, position] = blocks
                            continue
                blocks.append({"type": "text", "text": f"{label}: {reason}; the image was not provided to the model."})
            results[index, position] = blocks

        for index, (content, payload) in trusted_events.items():
            blocks = [{"type": "text", "text": content}]
            for position in range(1, len(payload["observations"]) + 1):
                blocks.extend(results.get((index, position), []))
            if len(blocks) > 1:
                messages[index]["content"] = blocks
        return await self.backend.complete(replace(request, messages=messages, tools=copy.deepcopy(request.tools)))
