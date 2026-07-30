"""Replay that mirrors proxy semantics, including the virtual-tool resolve loop.

Why this exists separately from session.py: in proxy mode the expand_context
exchange never reaches the client agent. `_conceal_virtual_calls`
(proxy/server.py:678) strips virtual tool_use blocks from the reply, so Claude
Code's own history keeps only the compressed [REF:id] form. The expansion lives
purely inside `_anthropic_resolve`'s local `thread` (server.py:700).

That has a consequence worth measuring: `thread` accumulates the full original
and is POSTed upstream again (server.py:705-706), but `stats` was computed once
in process_request *before* that loop ran. So the tokens the provider actually
bills for a turn can exceed the tokens Paritok reports compressing.

This module counts what is actually sent upstream, POST by POST, so that gap can
be quantified rather than asserted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paritok.middleware.wrapper import ParitokEngine
from paritok.token_counter import count_tokens

from harness.session import _text_of, refs_in

# server.py caps the resolve loop; mirrored here so a scripted session cannot
# describe behaviour the real proxy would refuse to perform.
MAX_RESOLVE_ROUNDS = 8


@dataclass
class UpstreamPost:
    """One HTTP POST the proxy makes to the provider."""

    turn: int
    round_index: int
    tokens: int
    carries_expanded: bool


@dataclass
class ProxyTurnRecord:
    index: int
    stats_original: int
    stats_compressed: int
    posts: list[UpstreamPost] = field(default_factory=list)
    expanded_refs: list[str] = field(default_factory=list)

    @property
    def billed_tokens(self) -> int:
        """Input tokens the provider actually sees across every POST this turn."""
        return sum(p.tokens for p in self.posts)

    @property
    def reported_tokens(self) -> int:
        """What Paritok's stats say the compressed request cost."""
        return self.stats_compressed

    @property
    def uncounted_tokens(self) -> int:
        return self.billed_tokens - self.reported_tokens


@dataclass
class ProxyResult:
    turns: list[ProxyTurnRecord] = field(default_factory=list)

    @property
    def total_billed(self) -> int:
        return sum(t.billed_tokens for t in self.turns)

    @property
    def total_reported(self) -> int:
        return sum(t.reported_tokens for t in self.turns)

    @property
    def total_original(self) -> int:
        return sum(t.stats_original for t in self.turns)

    @property
    def total_uncounted(self) -> int:
        return self.total_billed - self.total_reported

    @property
    def reported_saving(self) -> float:
        """Saving as Paritok reports it."""
        if not self.total_original:
            return 0.0
        return round(1 - self.total_reported / self.total_original, 4)

    @property
    def actual_saving(self) -> float:
        """Saving against what the provider actually billed."""
        if not self.total_original:
            return 0.0
        return round(1 - self.total_billed / self.total_original, 4)

    @property
    def expand_call_count(self) -> int:
        return sum(len(t.expanded_refs) for t in self.turns)


class ProxySession:
    """A scripted conversation driven through the engine with proxy semantics.

    The model's behaviour is scripted per turn via `expand_plan`: a list of ref
    indices the model decides to expand on that turn. Everything else — the
    compression, the virtual-tool resolution, the shadow store — runs through
    the real engine.
    """

    def __init__(self, engine: ParitokEngine, *, upstream_model: str = "gpt-4.1"):
        self.engine = engine
        self.upstream_model = upstream_model
        # Client-visible history. Virtual exchanges never enter this.
        self.messages: list[dict] = []
        self.tools: list[dict] = [
            {"name": "Read", "description": "Read a file",
             "input_schema": {"type": "object", "properties": {}}},
        ]
        self.result = ProxyResult()
        self._tool_seq = 0

    def user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def tool_result(self, tool_name: str, tool_input: dict, output: str) -> None:
        self._tool_seq += 1
        tool_id = f"toolu_{self._tool_seq:04d}"
        self.messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id,
                         "name": tool_name, "input": tool_input}],
        })
        self.messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id,
                         "content": output}],
        })

    def _count(self, messages: list[dict]) -> int:
        return count_tokens(_text_of(messages), self.upstream_model)

    def send(self, *, expands: int = 0) -> ProxyTurnRecord:
        """Run one client turn.

        Args:
            expands: how many distinct refs the model expands this turn. Mirrors
                a model that needs exact source it has already seen compressed.
        """
        processed, _tools, stats, stubbed = self.engine.process_request(
            [dict(m) for m in self.messages],
            list(self.tools),
            upstream_model=self.upstream_model,
        )

        rec = ProxyTurnRecord(index=len(self.result.turns),
                              stats_original=stats.original_tokens,
                              stats_compressed=stats.compressed_tokens)

        # thread is proxy-local, exactly as in _anthropic_resolve.
        thread = list(processed)
        available = refs_in(_text_of(processed))
        served: set[str] = set()

        # Round 0: the compressed request as Paritok accounts for it.
        rec.posts.append(UpstreamPost(rec.index, 0, self._count(thread), False))

        for round_index in range(1, MAX_RESOLVE_ROUNDS + 1):
            todo = [r for r in available if r not in served][:1] if expands > 0 else []
            if not todo:
                break
            ref = todo[0]
            served.add(ref)
            expands -= 1

            self._tool_seq += 1
            tool_id = f"toolu_{self._tool_seq:04d}"
            resolved = self.engine.resolve_virtual_call(
                "expand_context", {"shadow_id": ref}, stubbed_tools=stubbed
            )
            original = (resolved or {}).get("content", "")

            # server.py:722 / 742 — assistant virtual call, then its result.
            thread = [
                *thread,
                {"role": "assistant",
                 "content": [{"type": "tool_use", "id": tool_id,
                              "name": "expand_context",
                              "input": {"shadow_id": ref}}]},
                {"role": "user",
                 "content": [{"type": "tool_result", "tool_use_id": tool_id,
                              "content": original}]},
            ]
            rec.expanded_refs.append(ref)
            # server.py:705-706 — the grown thread is POSTed upstream again.
            rec.posts.append(
                UpstreamPost(rec.index, round_index, self._count(thread), True)
            )

        # Turn ends: virtual calls are concealed, so client history is untouched
        # by the expansion. self.messages deliberately keeps its original form.
        self.result.turns.append(rec)
        return rec
