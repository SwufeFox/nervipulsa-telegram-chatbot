"""Shared bounds and literal search terms for sourced conversation memory."""
from __future__ import annotations

import json
import re
from typing import Sequence

MEMORY_KINDS = ("experience", "person", "interest", "preference")
MEMORY_CERTAINTIES = ("observed", "inferred")
MAX_MEMORY_ID_CHARS = 128
MAX_MEMORY_SUMMARY_CHARS = 1500
MAX_MEMORY_SOURCES = 12
MAX_MEMORY_PEOPLE = 8
MAX_MEMORY_TAGS = 8
MAX_MEMORY_TAG_CHARS = 64
MAX_ACTIVE_MEMORIES = 1024
MAX_RECALL_RESULTS = 10
MAX_RECALL_QUERY_CHARS = 256
MAX_SOURCE_QUOTE_CHARS = 240
MAX_MEMORY_TOOL_RESULT_CHARS = 16000
MAX_MEMORY_READ_ROUNDS = 3
MAX_MEMORY_READS_PER_WORKFLOW = 6

_WORDS = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]+\Z")


def search_terms(query: str) -> tuple[str, ...]:
    """Literal phrases/words and CJK bigrams, without semantic classification."""
    normalized = " ".join(query.casefold().split())
    if not normalized:
        return ()
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        if term and term not in seen and len(terms) < 12:
            terms.append(term)
            seen.add(term)

    if len(normalized) <= 64:
        add(normalized)
    words = _WORDS.findall(normalized)
    for word in words:
        if not _CJK.fullmatch(word):
            add(word)
    for word in words:
        if _CJK.fullmatch(word):
            if len(word) <= 2:
                add(word)
            else:
                for index in range(len(word) - 1):
                    add(word[index:index + 2])
                    if len(terms) >= 12:
                        return tuple(terms)
    return tuple(terms)


def like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def bounded_memories(
    memories: Sequence[dict[str, Any]], *, max_chars: int = MAX_MEMORY_TOOL_RESULT_CHARS,
    max_items: int = MAX_RECALL_RESULTS,
) -> tuple[list[dict[str, Any]], bool]:
    """Project evidence honestly while keeping the model's read bounded."""
    projected: list[dict[str, Any]] = []
    used = 2
    truncated = False
    for memory in memories:
        if len(projected) >= max_items:
            truncated = True
            break
        item = dict(memory)
        sources = list(memory.get("sources", ()))
        item["sources"] = sources[:3]
        if len(sources) > 3:
            item["sources_truncated"] = True
        cost = len(json.dumps(item, ensure_ascii=False)) + 2
        if not projected and cost + used > max_chars:
            while item["sources"] and cost + used > max_chars:
                item["sources"] = item["sources"][:-1]
                item["sources_truncated"] = True
                cost = len(json.dumps(item, ensure_ascii=False)) + 2
            while item["summary"] and cost + used > max_chars:
                excess = cost + used - max_chars
                item["summary"] = item["summary"][:-max(1, excess)]
                item["summary_truncated"] = True
                cost = len(json.dumps(item, ensure_ascii=False)) + 2
        if cost + used > max_chars:
            truncated = True
            break
        projected.append(item)
        used += cost
    return projected, truncated


def bounded_history(
    events: Sequence[dict[str, Any]], *, max_chars: int = MAX_MEMORY_TOOL_RESULT_CHARS,
) -> tuple[list[dict[str, Any]], bool]:
    """Keep recent matching source events with explicit truncation markers."""
    projected: list[dict[str, Any]] = []
    used = 2
    truncated = False
    for event in reversed(events):
        item = dict(event)
        payload = dict(event["payload"])
        item["payload"] = payload
        cost = len(json.dumps(item, ensure_ascii=False)) + 2
        if not projected and cost + used > max_chars:
            for key in ("text", "reply_text"):
                text = payload.get(key)
                while isinstance(text, str) and text and cost + used > max_chars:
                    excess = cost + used - max_chars
                    text = text[:-max(1, excess)]
                    payload[key] = text
                    item["payload_truncated"] = True
                    cost = len(json.dumps(item, ensure_ascii=False)) + 2
        if cost + used > max_chars:
            truncated = True
            break
        projected.append(item)
        used += cost
    projected.reverse()
    return projected, truncated
