"""Mock provider + mock compressor, for driving the real Paritok proxy offline.

Two roles on one port:

  /v1/chat/completions  — stands in for the Ollama-hosted 4B compressor. Returns
                          a deterministic [SEG]...[/SEG] body, so the proxy's
                          real LocalModelStrategy parsing path is exercised
                          without a GPU.
  /v1/messages          — stands in for api.anthropic.com. Scripted: the first
                          call of a turn asks for expand_context, the next
                          returns plain text. This is what makes the proxy run
                          its virtual-tool resolve loop for real.

Every /v1/messages request is recorded with its input-token count, so the
number of upstream POSTs per client turn — and what each one carried — can be
read back from /_recorded rather than inferred.
"""

from __future__ import annotations

import json
import re

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from paritok.token_counter import count_tokens

_KEEP_FRACTION = 0.25
# Attributes are required. The instruction text in the prompt contains a bare
# literal "[SEG]...[/SEG]" as an example, and a laxer pattern matches THAT first,
# yielding an empty header and a body of "..." for every segment.
_SEG_HEADER = re.compile(r"\[SEG\s+([^\]]+)\]")

# Every /v1/messages request the proxy made, in order.
RECORDED: list[dict] = []
# Refs the scripted model has already asked to expand, so it stops after one.
_EXPANDED: set[str] = set()
# When False the scripted model never calls expand_context — the control case,
# used to check a policy change does not regress sessions that never expand.
_EXPAND_ENABLED = {"on": True}

_REF_IN_TEXT = re.compile(r"\[REF:([a-f0-9]+)")


def _flatten(messages: list[dict]) -> str:
    out = []
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


async def compress(request: Request) -> JSONResponse:
    """Deterministic stand-in for the 4B model, in its trained wire format."""
    body = await request.json()
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
    body_text = "\n".join(lines[:keep])
    return JSONResponse({
        "choices": [{"message": {"role": "assistant",
                                 "content": f"[SEG {header}]\n{body_text}\n[/SEG]"}}],
    })


async def messages(request: Request) -> JSONResponse:
    """Scripted Anthropic Messages endpoint."""
    body = await request.json()
    msgs = body.get("messages", [])
    text = _flatten(msgs)
    raw = json.dumps(msgs)
    RECORDED.append({
        "index": len(RECORDED),
        # Flattened-text count: the human-readable content only.
        "input_tokens": count_tokens(text, body.get("model", "")),
        # Whole-payload count: what the provider actually tokenizes, including
        # block structure, ids and tool schemas. The honest billing figure.
        "wire_tokens": count_tokens(raw, body.get("model", "")),
        "message_count": len(msgs),
        "chars": len(text),
        "raw": raw,
        "carries_expand_result": "expand_context" in raw,
    })

    tools = {t.get("name") for t in body.get("tools", [])}
    refs = _REF_IN_TEXT.findall(text)

    # A request carrying no expand result is the first POST of a new client
    # turn. Clear the seen-set there, modelling a model that needs the exact
    # source again this turn. That is the honest assumption in proxy mode:
    # _conceal_virtual_calls keeps the expansion out of the client's history,
    # so nothing carries over and the model cannot "remember" the expansion.
    if not RECORDED[-1]["carries_expand_result"]:
        _EXPANDED.clear()

    todo = [r for r in refs if r not in _EXPANDED]

    if _EXPAND_ENABLED["on"] and "expand_context" in tools and todo:
        ref = todo[0]
        _EXPANDED.add(ref)
        return JSONResponse({
            "id": "msg_mock", "type": "message", "role": "assistant",
            "model": body.get("model", "mock"),
            "content": [{"type": "tool_use", "id": "toolu_virt_1",
                         "name": "expand_context", "input": {"shadow_id": ref}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

    return JSONResponse({
        "id": "msg_mock", "type": "message", "role": "assistant",
        "model": body.get("model", "mock"),
        "content": [{"type": "text", "text": "Done."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    })


async def set_expand(request: Request) -> JSONResponse:
    body = await request.json()
    _EXPAND_ENABLED["on"] = bool(body.get("enabled", True))
    return JSONResponse({"expand_enabled": _EXPAND_ENABLED["on"]})


async def recorded(_request: Request) -> JSONResponse:
    return JSONResponse({
        "posts": RECORDED,
        "total_input_tokens": sum(r["input_tokens"] for r in RECORDED),
    })


async def reset(_request: Request) -> JSONResponse:
    RECORDED.clear()
    _EXPANDED.clear()
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/v1/chat/completions", compress, methods=["POST"]),
    Route("/v1/messages", messages, methods=["POST"]),
    Route("/_recorded", recorded, methods=["GET"]),
    Route("/_reset", reset, methods=["POST"]),
    Route("/_set_expand", set_expand, methods=["POST"]),
])


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9100, log_level="warning")
