"""Web app: run a real agent session through two real proxies and reconcile.

    uvicorn app.main:app --port 8420

Set PARITOK_API_KEY to compress through Paritok's hosted GPU instead of the
in-process mock compressor. The provider is always mocked — we have to script
when the model calls expand_context and count exactly what was POSTed, which a
real provider would neither do nor report.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.mock import RunRecorder
from app.mockserver import MockUpstream
from app.orchestrator import ProxyFleet
from paritok.token_counter import count_tokens

STATIC = Path(__file__).resolve().parent / "static"
MAX_SOURCE_CHARS = 200_000

app = FastAPI(title="Pin-on-Expand", docs_url=None, redoc_url=None)

recorder = RunRecorder()
mock_upstream = MockUpstream(recorder)
fleet: ProxyFleet | None = None


@app.on_event("startup")
def _startup() -> None:
    global fleet
    mock_upstream.start()
    fleet = ProxyFleet(mock_base=mock_upstream.base_url)


@app.on_event("shutdown")
def _shutdown() -> None:
    if fleet:
        fleet.stop()
    mock_upstream.stop()


# ── the run ──────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    source: str = Field(min_length=1)
    turns: int = Field(default=3, ge=1, le=8)
    expands: bool = True
    filename: str = "pasted.py"


def _messages(source: str, filename: str, turn: int) -> list[dict]:
    """A conversation shaped the way a real agent's is.

    The agent keeps the full original in its own history every turn: the proxy
    only rewrites requests in flight, so the compressed form never reaches it.
    """
    msgs: list[dict] = [
        {"role": "user", "content": "find and fix the bug in this file"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_0001", "name": "Read",
             "input": {"file_path": f"/repo/{filename}"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_0001", "content": source}]},
    ]
    for i in range(turn):
        msgs.append({"role": "assistant", "content": "Working on it."})
        msgs.append({"role": "user", "content": f"continue (step {i + 1})"})
    return msgs


def _run_variant(variant: str, req: RunRequest) -> dict:
    assert fleet is not None
    # Cold-start this variant's proxy only. Caches, shadow store and pins are
    # all per-process, so a fresh process is what makes the run mean anything.
    url = fleet.start_one(variant)
    recorder.reset(variant=variant, expand_enabled=req.expands)

    before = httpx.get(f"{url}/stats", timeout=10).json()
    compressed_sample = None

    for turn in range(req.turns):
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": _messages(req.source, req.filename, turn),
            "tools": [{"name": "Read", "description": "Read a file",
                       "input_schema": {"type": "object", "properties": {}}}],
        }
        resp = httpx.post(f"{url}/v1/messages", json=payload,
                          headers={"x-api-key": "demo",
                                   "anthropic-version": "2023-06-01"},
                          timeout=300)
        if resp.status_code != 200:
            raise HTTPException(502, f"{variant} proxy returned {resp.status_code}")

    after = httpx.get(f"{url}/stats", timeout=10).json()

    # The compressed form the proxy actually produced, pulled from the first
    # POST of the first turn so the UI can show it beside the original.
    first = next((p for p in recorder.posts if not p.carries_expanded), None)

    reported_original = (after.get("input_tokens_original", 0)
                         - before.get("input_tokens_original", 0))
    reported_compressed = (after.get("input_tokens_compressed", 0)
                           - before.get("input_tokens_compressed", 0))

    return {
        "variant": variant,
        "posts": recorder.snapshot(),
        "post_count": len(recorder.posts),
        "billed": recorder.total_tokens,
        "reported_original": reported_original,
        "reported_compressed": reported_compressed,
        "first_post_tokens": first.input_tokens if first else 0,
        "compressed_sample": compressed_sample,
    }


@app.post("/api/run")
def api_run(req: RunRequest) -> dict:
    if fleet is None:
        raise HTTPException(503, "not ready")
    if len(req.source) > MAX_SOURCE_CHARS:
        raise HTTPException(413, f"source exceeds {MAX_SOURCE_CHARS} characters")

    source_tokens = count_tokens(req.source, "claude-sonnet-4-20250514")
    if source_tokens < 512:
        raise HTTPException(
            422,
            f"This file is {source_tokens} tokens. Paritok's pipeline skips anything "
            f"under its 512-token floor, so there would be nothing to compress — "
            f"paste a longer file.",
        )

    try:
        results = {v: _run_variant(v, req) for v in ("stock", "pinned")}
    finally:
        # Never leave a proxy process behind holding memory between runs.
        fleet.stop()

    no_proxy = source_tokens * req.turns

    for r in results.values():
        r["vs_no_proxy"] = (1 - r["billed"] / no_proxy) if no_proxy else 0.0
        r["reported_saving"] = (
            1 - r["reported_compressed"] / r["reported_original"]
            if r["reported_original"] else 0.0
        )
        r["uncounted"] = r["billed"] - r["reported_compressed"]

    stock, pinned = results["stock"], results["pinned"]
    return {
        "compressor": fleet.compressor,
        "source_tokens": source_tokens,
        "turns": req.turns,
        "expands": req.expands,
        "no_proxy_tokens": no_proxy,
        "stock": stock,
        "pinned": pinned,
        "delta": {
            "billed_saved": stock["billed"] - pinned["billed"],
            "billed_saved_pct": (1 - pinned["billed"] / stock["billed"]
                                 if stock["billed"] else 0.0),
            "posts_saved": stock["post_count"] - pinned["post_count"],
        },
    }


# Samples come from the running interpreter's own stdlib: real, substantial code
# that is guaranteed present, and free of the "[REF:" / "expand_context"
# vocabulary that a file from this repo would carry into the mock's ref scanner.
_SAMPLES = {"argparse": "argparse", "server": "http.server"}


@app.get("/api/sample/{name}")
def api_sample(name: str) -> PlainTextResponse:
    module = _SAMPLES.get(name)
    if module is None:
        raise HTTPException(404, "unknown sample")
    try:
        import importlib.util

        spec = importlib.util.find_spec(module)
        if spec is None or not spec.origin:
            raise HTTPException(404, "sample not available on this host")
        return PlainTextResponse(Path(spec.origin).read_text(encoding="utf-8"))
    except (OSError, ImportError, ValueError) as exc:
        raise HTTPException(404, "sample not available on this host") from exc


@app.get("/api/health")
def api_health() -> dict:
    return {
        "ok": True,
        "compressor": fleet.compressor if fleet else "starting",
        "hosted_gpu": bool(os.environ.get("PARITOK_API_KEY", "").strip()),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
