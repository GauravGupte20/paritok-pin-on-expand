"""End-to-end validation against the real Paritok proxy.

Runs a client turn through an actual `paritok proxy` process (mock provider and
mock compressor behind it, so no GPU or API key is needed) and compares:

  - what the provider was actually POSTed, from the mock's own records
  - what Paritok reports it saved, from the proxy's /stats

Prereqs (see run_validation.sh):
    python harness/mock_upstream.py &
    paritok proxy --port 8080 --anthropic-url http://127.0.0.1:9100 \
        --config-file paritok.yaml &
"""

from __future__ import annotations

import json
import sys

import httpx

from harness.build import real_source

PROXY = "http://127.0.0.1:8080"
MOCK = "http://127.0.0.1:9100"


def build_request() -> dict:
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "fix the session validation bug in auth.py"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_0001", "name": "Read",
                 "input": {"file_path": "/repo/auth.py"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_0001",
                 "content": real_source()}]},
        ],
        "tools": [
            {"name": "Read", "description": "Read a file",
             "input_schema": {"type": "object", "properties": {}}},
        ],
    }


def main() -> int:
    httpx.post(f"{MOCK}/_reset", timeout=10)

    payload = build_request()
    raw_chars = len(json.dumps(payload["messages"]))

    resp = httpx.post(f"{PROXY}/v1/messages", json=payload,
                      headers={"x-api-key": "test", "anthropic-version": "2023-06-01"},
                      timeout=120)
    if resp.status_code != 200:
        print(f"proxy returned {resp.status_code}: {resp.text[:400]}")
        return 2

    stats = httpx.get(f"{PROXY}/stats", timeout=10).json()
    recorded = httpx.get(f"{MOCK}/_recorded", timeout=10).json()

    print("ONE client turn through the real proxy")
    print("=" * 58)
    print(f"raw request chars: {raw_chars}")

    print(f"\nUpstream POSTs the provider actually received: {len(recorded['posts'])}")
    for post in recorded["posts"]:
        flag = "  <- carries expanded original" if post["carries_expand_result"] else ""
        print(f"  post {post['index']}: {post['input_tokens']:>6} input tokens, "
              f"{post['message_count']} messages{flag}")
    print(f"  total billed input tokens: {recorded['total_input_tokens']}")

    print("\nWhat Paritok reports (/stats):")
    for key in ("total_requests", "input_tokens_original", "input_tokens_compressed",
                "compression_ratio", "tokens_saved", "estimated_cost_saved_usd"):
        if key in stats:
            print(f"  {key:<26}: {stats[key]}")

    billed = recorded["total_input_tokens"]
    reported = stats.get("input_tokens_compressed", 0)
    original = stats.get("input_tokens_original", 0)

    print("\n" + "=" * 58)
    print(f"reported compressed tokens : {reported}")
    print(f"actually billed upstream   : {billed}")
    print(f"difference (uncounted)     : {billed - reported}")
    if original:
        print(f"saving as reported         : {1 - reported / original:>7.1%}")
        print(f"saving vs actual billing   : {1 - billed / original:>7.1%}")

    if len(recorded["posts"]) > 1:
        print("\nCONFIRMED: one client turn produced multiple upstream POSTs; the")
        print("expand round-trip re-sent content that /stats does not account for.")
        return 1
    print("\nSingle upstream POST — no resolve loop ran this turn.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
