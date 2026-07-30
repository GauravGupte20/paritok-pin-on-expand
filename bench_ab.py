"""A/B: stock Paritok vs pin-on-expand, both as real proxy processes.

Measures what the provider is actually POSTed across a multi-turn session,
alongside what each proxy's /stats claims. Run the servers first:

    python harness/mock_upstream.py &
    paritok proxy --port 8080 --anthropic-url http://127.0.0.1:9100 \
        --config-file paritok.yaml &
    python run_pinned_proxy.py --port 8081 --anthropic-url http://127.0.0.1:9100 \
        --config-file paritok.yaml &
    python bench_ab.py

The client's conversation grows the way a real agent's does: it keeps the full
original file content in its own history every turn, because the proxy only
rewrites requests in flight — the agent never sees the compressed form.
"""

from __future__ import annotations

import httpx

from harness.build import real_source

MOCK = "http://127.0.0.1:9100"
TURNS = 3


def build_messages(source: str, turn: int) -> list[dict]:
    messages: list[dict] = [
        {"role": "user", "content": "fix the argument parsing bug"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_0001", "name": "Read",
             "input": {"file_path": "/repo/argparse.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_0001",
             "content": source}]},
    ]
    for i in range(turn):
        messages.append({"role": "assistant", "content": "Working on it."})
        messages.append({"role": "user", "content": f"continue (step {i + 1})"})
    return messages


def run(proxy_url: str, label: str, source: str, *, expands: bool = True) -> dict:
    httpx.post(f"{MOCK}/_reset", timeout=10)
    httpx.post(f"{MOCK}/_set_expand", json={"enabled": expands}, timeout=10)
    before = httpx.get(f"{proxy_url}/stats", timeout=10).json()
    for turn in range(TURNS):
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": build_messages(source, turn),
            "tools": [{"name": "Read", "description": "Read a file",
                       "input_schema": {"type": "object", "properties": {}}}],
        }
        resp = httpx.post(f"{proxy_url}/v1/messages", json=payload,
                          headers={"x-api-key": "test",
                                   "anthropic-version": "2023-06-01"},
                          timeout=300)
        resp.raise_for_status()

    recorded = httpx.get(f"{MOCK}/_recorded", timeout=10).json()
    after = httpx.get(f"{proxy_url}/stats", timeout=10).json()
    # /stats is cumulative for the life of the proxy process, so scenarios run
    # back-to-back must be differenced rather than read absolutely.
    return {
        "label": label,
        "posts": len(recorded["posts"]),
        "billed": recorded["total_input_tokens"],
        "reported_original": (after.get("input_tokens_original", 0)
                              - before.get("input_tokens_original", 0)),
        "reported_compressed": (after.get("input_tokens_compressed", 0)
                                - before.get("input_tokens_compressed", 0)),
    }


def report(title: str, results: list[dict], baseline: int) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    print(f"{'variant':<16} {'POSTs':>6} {'billed':>9} {'reported':>9} {'vs no proxy':>12}")
    for r in results:
        delta = 1 - r["billed"] / baseline
        print(f"{r['label']:<16} {r['posts']:>6} {r['billed']:>9} "
              f"{r['reported_compressed']:>9} {delta:>11.1%}")
    stock, pinned = results
    if stock["billed"]:
        print(f"  pin-on-expand vs stock: "
              f"{1 - pinned['billed'] / stock['billed']:.1%} fewer billed tokens, "
              f"{stock['posts'] - pinned['posts']} fewer round-trips")


def main() -> int:
    from paritok.token_counter import count_tokens

    source = real_source()
    per_turn = count_tokens(source, "claude-sonnet-4-20250514")
    baseline = per_turn * TURNS

    print(f"{TURNS} client turns, one {per_turn}-token file in context")
    print(f"no proxy at all would bill roughly: {baseline}")

    # Control first: the pinning proxy accumulates pins for the life of the
    # process, so running the expand scenario first would leave pins in place
    # and make the control look better than a cold start.
    report("Scenario A — model never expands (control)",
           [run("http://127.0.0.1:8080", "stock Paritok", source, expands=False),
            run("http://127.0.0.1:8081", "pin-on-expand", source, expands=False)],
           baseline)

    report("Scenario B — model expands the file each turn",
           [run("http://127.0.0.1:8080", "stock Paritok", source, expands=True),
            run("http://127.0.0.1:8081", "pin-on-expand", source, expands=True)],
           baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
