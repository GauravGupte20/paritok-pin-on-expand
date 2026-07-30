"""Does an expanded [REF:id] get re-collapsed on the following turn?

Reading the source suggests it does:

  1. proxy/server.py:740 resolves expand_context server-side and injects the
     original back into the conversation as a tool_result.
  2. On the next request that tool_result flows through _compress_messages
     (wrapper.py:374) like any other.
  3. It does not match _REF_PATTERN (compress.py:171), so it is not skipped.
  4. sid = content_hash(content) (compress.py:210) is the SAME id as the first
     time round, because it is a hash of the content itself — so the cache
     lookup at compress.py:213 hits and returns the very [REF:id] tag the model
     just spent a tool call expanding.

The guard that exists — served_refs at proxy/server.py:728 — is scoped to one
turn's agentic sub-loop, so it cannot prevent this across turns.

This script checks that end-to-end against the real engine.

    python repro_expand_loop.py
"""

from __future__ import annotations

from harness.build import build_engine, sample_source
from harness.session import SimulatedSession


def main() -> int:
    engine, stub = build_engine()
    session = SimulatedSession(engine)

    source = sample_source("auth")

    # Turn 1 — agent reads a file; the pipeline compresses it to a [REF:id].
    session.user("fix the session validation bug in auth.py")
    session.tool_result("Read", {"file_path": "/repo/auth.py"}, source)
    turn1 = session.send()

    print("Turn 1 — initial read")
    print(f"  original tokens   : {turn1.original_tokens}")
    print(f"  compressed tokens : {turn1.compressed_tokens}")
    print(f"  refs in context   : {turn1.refs_present}")
    print(f"  compressor calls  : {stub.call_count}")

    if not turn1.refs_present:
        print("\nNo [REF:] tag produced — content did not compress. "
              "Nothing to expand; adjust the fixture size.")
        return 2

    ref = turn1.refs_present[0]

    # The model decides it needs the exact source and calls expand_context.
    original = session.expand(ref)
    print(f"\nModel expands {ref} -> {len(original)} chars returned verbatim")

    # Turn 2 — the expanded original is now part of the conversation.
    session.user("now apply the fix")
    turn2 = session.send()

    print("\nTurn 2 — after expansion")
    print(f"  original tokens   : {turn2.original_tokens}")
    print(f"  compressed tokens : {turn2.compressed_tokens}")
    print(f"  refs in context   : {turn2.refs_present}")
    print(f"  cache hits        : {turn2.cache_hits}")
    print(f"  compressor calls  : {stub.call_count}")

    print("\n" + "=" * 62)
    if ref in turn2.refs_present:
        print(f"REPRODUCED: ref {ref} was expanded by the model, then handed")
        print("back as the same [REF:id] on the next turn. The expand_context")
        print("call bought nothing — the agent paid a tool round-trip to")
        print("retrieve content the gateway immediately re-collapsed.")
        print(f"\nre-collapse events: {session.result.recollapse_events}")
        return 1
    print(f"NOT REPRODUCED: ref {ref} did not reappear in turn 2.")
    print(f"turn 2 refs: {turn2.refs_present}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
