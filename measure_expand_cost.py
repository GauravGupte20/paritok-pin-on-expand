"""What does an expand_context call actually cost, and is it counted?

Proxy mode resolves expand_context inside its own loop (server.py:696-748):
the full original is appended to a proxy-local `thread` and POSTed upstream
again. Paritok's savings stats are computed once, before that loop, in
process_request — so those extra upstream tokens are billed by the provider but
absent from what Paritok reports.

Because `_conceal_virtual_calls` keeps the exchange out of the client's history,
the expansion is also discarded at end of turn: a model that needs the same file
on the next turn must expand it again, paying the same cost again.

    python measure_expand_cost.py
"""

from __future__ import annotations

from harness.build import build_engine, sample_source
from harness.proxy_sim import ProxySession


def scenario(expands_per_turn: int, turns: int = 4) -> None:
    engine, _stub = build_engine()
    session = ProxySession(engine)

    session.user("fix the session validation bug in auth.py")
    session.tool_result("Read", {"file_path": "/repo/auth.py"}, sample_source("auth"))

    label = ("no expansion" if expands_per_turn == 0
             else f"{expands_per_turn} expand_context call/turn")
    print(f"\n--- {label} ---")
    print(f"{'turn':>5} {'posts':>6} {'reported':>9} {'billed':>8} {'uncounted':>10}")
    for i in range(turns):
        if i:
            session.user(f"continue working on the fix (step {i})")
        rec = session.send(expands=expands_per_turn)
        print(f"{rec.index:>5} {len(rec.posts):>6} {rec.reported_tokens:>9} "
              f"{rec.billed_tokens:>8} {rec.uncounted_tokens:>10}")

    r = session.result
    print(f"  totals: original={r.total_original}  "
          f"reported={r.total_reported}  billed={r.total_billed}")
    print(f"  saving as Paritok reports it : {r.reported_saving:>7.1%}")
    print(f"  saving against actual billing: {r.actual_saving:>7.1%}")
    print(f"  expand_context calls         : {r.expand_call_count}")
    print(f"  tokens billed but uncounted  : {r.total_uncounted}")


def main() -> int:
    print("Cost of expand_context under proxy semantics")
    print("=" * 52)
    scenario(expands_per_turn=0)
    scenario(expands_per_turn=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
