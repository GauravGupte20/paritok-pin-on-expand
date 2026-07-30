"""Simulated multi-turn agent session.

Reproduces how a real agent conversation accumulates across turns when it runs
behind the Paritok proxy, without needing an upstream LLM. That matters because
the behaviour we are investigating is *cross-turn*: the proxy's own
already-expanded guard (`served_refs` in proxy/server.py) is scoped to a single
turn's agentic sub-loop, so anything that only replays one request cannot see it.

The model's side of the conversation is scripted: each turn declares which
tools it "calls". That keeps runs deterministic while still exercising the real
ParitokEngine code path for compression, virtual-tool resolution and shadow
storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paritok.middleware.wrapper import ParitokEngine

_REF_TAG = re.compile(r"\[REF:([a-f0-9]+)(?:\s+src=([^\]]*))?\]")


def refs_in(text: str) -> list[str]:
    """Every shadow id referenced by a [REF:...] tag in `text`."""
    return [m.group(1) for m in _REF_TAG.finditer(text)]


@dataclass
class TurnRecord:
    """What the engine did to one request."""

    index: int
    original_tokens: int
    compressed_tokens: int
    items_compressed: int
    items_skipped: int
    cache_hits: int
    refs_present: list[str] = field(default_factory=list)
    expanded_this_turn: list[str] = field(default_factory=list)
    # Ids the model expanded on an earlier turn that came back as a [REF:] tag
    # again on this one — i.e. work the agent already paid to undo.
    recollapsed: list[str] = field(default_factory=list)

    @property
    def saved_tokens(self) -> int:
        return self.original_tokens - self.compressed_tokens


@dataclass
class SessionResult:
    turns: list[TurnRecord] = field(default_factory=list)
    compressor_calls: int = 0

    @property
    def total_original(self) -> int:
        return sum(t.original_tokens for t in self.turns)

    @property
    def total_compressed(self) -> int:
        return sum(t.compressed_tokens for t in self.turns)

    @property
    def total_saved(self) -> int:
        return self.total_original - self.total_compressed

    @property
    def ratio(self) -> float:
        """Fraction of tokens saved across the session (higher is better)."""
        if not self.total_original:
            return 0.0
        return round(self.total_saved / self.total_original, 4)

    @property
    def recollapse_events(self) -> list[tuple[int, str]]:
        """(turn_index, shadow_id) for every re-collapse of an expanded ref."""
        return [(t.index, sid) for t in self.turns for sid in t.recollapsed]


def _text_of(messages: list[dict]) -> str:
    """Flatten a message list to text, for ref scanning."""
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
                    out.extend(b.get("text", "") for b in inner
                               if isinstance(b, dict))
    return "\n".join(out)


class SimulatedSession:
    """Drives a scripted conversation through a real ParitokEngine.

    Usage:
        session = SimulatedSession(engine)
        session.user("fix the auth bug")
        session.tool_result("Read", {"file_path": "auth.py"}, file_text)
        rec = session.send()                 # runs one request through the engine
        session.expand(rec.refs_present[0])  # model calls expand_context
        rec2 = session.send()
    """

    def __init__(self, engine: ParitokEngine, *, upstream_model: str = "gpt-4.1"):
        self.engine = engine
        self.upstream_model = upstream_model
        self.messages: list[dict] = []
        self.tools: list[dict] = [
            {"name": "Read", "description": "Read a file",
             "input_schema": {"type": "object", "properties": {}}},
        ]
        self.result = SessionResult()
        self._expanded: set[str] = set()
        self._tool_seq = 0

    # -- scripting the conversation ------------------------------------

    def user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def tool_result(self, tool_name: str, tool_input: dict, output: str) -> None:
        """Append an assistant tool_use + the matching user tool_result.

        Mirrors the shape a real agent sends, so `_build_tool_use_index` can
        associate the result with its file_path and the path short-circuit in
        the pipeline behaves as it would in production.
        """
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

    def expand(self, shadow_id: str) -> str:
        """Model calls expand_context; proxy resolves it and feeds the original
        back as a tool_result — matching proxy/server.py's server-side handling.
        """
        self._tool_seq += 1
        tool_id = f"toolu_{self._tool_seq:04d}"
        resolved = self.engine.resolve_virtual_call(
            "expand_context", {"shadow_id": shadow_id}
        )
        original = (resolved or {}).get("content", "")
        self.messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id,
                         "name": "expand_context",
                         "input": {"shadow_id": shadow_id}}],
        })
        self.messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id,
                         "content": original}],
        })
        self._expanded.add(shadow_id)
        self._pending_expand = shadow_id
        return original

    # -- running a request ---------------------------------------------

    def send(self) -> TurnRecord:
        """Run the accumulated conversation through the engine, once."""
        expanded_before = set(self._expanded)
        just_expanded = getattr(self, "_pending_expand", None)
        self._pending_expand = None

        processed, _tools, stats, _stubbed = self.engine.process_request(
            [dict(m) for m in self.messages],
            list(self.tools),
            upstream_model=self.upstream_model,
        )

        refs = refs_in(_text_of(processed))
        # A ref counts as re-collapsed when the model previously expanded it and
        # the engine has handed back that same [REF:id] tag again.
        recollapsed = sorted({r for r in refs if r in expanded_before})

        rec = TurnRecord(
            index=len(self.result.turns),
            original_tokens=stats.original_tokens,
            compressed_tokens=stats.compressed_tokens,
            items_compressed=stats.items_compressed,
            items_skipped=stats.items_skipped,
            cache_hits=stats.cache_hits,
            refs_present=refs,
            expanded_this_turn=[just_expanded] if just_expanded else [],
            recollapsed=recollapsed,
        )
        self.result.turns.append(rec)
        return rec
