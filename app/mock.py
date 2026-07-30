"""In-process mock provider and mock compressor.

Mounted inside the web app so the proxies under test have somewhere real to POST.
Every upstream request is recorded, which is what lets the UI show the billing
reconciliation rather than just the compression.

When PARITOK_API_KEY is set the proxies compress through Paritok's hosted GPU
instead of the mock compressor here; the mock provider is still used, because we
must be able to script when the model calls expand_context and to count exactly
what was POSTed.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field

from paritok.token_counter import count_tokens

_KEEP_FRACTION = 0.25
# Attributes required: the prompt's instruction text contains a bare literal
# "[SEG]...[/SEG]" example, and a laxer pattern matches that first.
_SEG_HEADER = re.compile(r"\[SEG\s+([^\]]+)\]")
_REF_IN_TEXT = re.compile(r"\[REF:([a-f0-9]+)")


@dataclass
class UpstreamPost:
    index: int
    variant: str
    turn: int
    input_tokens: int
    message_count: int
    carries_expanded: bool


@dataclass
class RunRecorder:
    """Per-run upstream state. Reset between variants so counts never bleed."""

    posts: list[UpstreamPost] = field(default_factory=list)
    expanded: set[str] = field(default_factory=set)
    expand_enabled: bool = True
    variant: str = "stock"
    turn: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self, *, variant: str, expand_enabled: bool) -> None:
        with self._lock:
            self.posts.clear()
            self.expanded.clear()
            self.variant = variant
            self.expand_enabled = expand_enabled
            self.turn = 0

    @property
    def total_tokens(self) -> int:
        return sum(p.input_tokens for p in self.posts)

    def snapshot(self) -> list[dict]:
        return [
            {
                "index": p.index,
                "turn": p.turn,
                "input_tokens": p.input_tokens,
                "message_count": p.message_count,
                "carries_expanded": p.carries_expanded,
            }
            for p in self.posts
        ]


def flatten(messages: list[dict]) -> str:
    out: list[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                inner = block.get("content", block.get("text", ""))
                if isinstance(inner, str):
                    out.append(inner)
                elif isinstance(inner, list):
                    out.extend(b.get("text", "") for b in inner if isinstance(b, dict))
    return "\n".join(out)


def compress_reply(body: dict) -> dict:
    """Deterministic stand-in for the 4B model, in its trained wire format."""
    user_msg = ""
    for msg in body.get("messages", []):
        if msg.get("role") == "user":
            user_msg = msg.get("content", "")

    match = _SEG_HEADER.search(user_msg)
    header = match.group(1).strip() if match else "id=0 kind=file_read level=L1"
    inner = user_msg
    if match:
        start = match.end()
        end = inner.find("[/SEG]", start)
        inner = inner[start:end if end != -1 else len(inner)]

    lines = [ln for ln in inner.splitlines() if ln.strip()]
    keep = max(1, int(len(lines) * _KEEP_FRACTION))
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": f"[SEG {header}]\n" + "\n".join(lines[:keep]) + "\n[/SEG]",
            }
        }]
    }


def messages_reply(body: dict, rec: RunRecorder) -> dict:
    """Scripted Anthropic Messages endpoint that records what it was sent."""
    msgs = body.get("messages", [])
    text = flatten(msgs)
    raw = json.dumps(msgs)
    carries = "expand_context" in raw

    with rec._lock:
        # A request with no expand result is the first POST of a new client turn.
        if not carries:
            rec.turn += 1
            # In proxy mode _conceal_virtual_calls keeps the expansion out of the
            # client's history, so nothing carries over between turns and the
            # model must ask again.
            rec.expanded.clear()
        rec.posts.append(UpstreamPost(
            index=len(rec.posts),
            variant=rec.variant,
            turn=rec.turn,
            input_tokens=count_tokens(text, body.get("model", "")),
            message_count=len(msgs),
            carries_expanded=carries,
        ))
        enabled = rec.expand_enabled
        seen = set(rec.expanded)

    tools = {t.get("name") for t in body.get("tools", [])}
    todo = [r for r in _REF_IN_TEXT.findall(text) if r not in seen]

    if enabled and "expand_context" in tools and todo:
        ref = todo[0]
        with rec._lock:
            rec.expanded.add(ref)
        return {
            "id": "msg_mock", "type": "message", "role": "assistant",
            "model": body.get("model", "mock"),
            "content": [{"type": "tool_use", "id": "toolu_virt_1",
                         "name": "expand_context", "input": {"shadow_id": ref}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    return {
        "id": "msg_mock", "type": "message", "role": "assistant",
        "model": body.get("model", "mock"),
        "content": [{"type": "text", "text": "Done."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
