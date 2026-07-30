"""Pin-on-expand: stop re-compressing content the model already asked for.

The problem this fixes
----------------------
When the model calls `expand_context`, the proxy resolves it inside its own
loop (proxy/server.py:696-748) and POSTs the grown thread upstream again. The
client agent never sees that exchange — `_conceal_virtual_calls` strips it — so
on the next turn the agent re-sends the same original file content, Paritok
re-compresses it to the same [REF:id], and the model expands it again.

Every turn therefore costs: one compressed POST *plus* one POST carrying the
full original. That is strictly more than sending the original once, and none
of the second POST is counted by /stats (which is computed in process_request,
before the resolve loop runs).

The fix
-------
Treat an `expand_context` call as a signal about that content: the model has
stated it needs the exact text. Pin it, and pass it through verbatim from then
on. The [REF:id] never reappears, so the model has nothing to expand, and the
turn costs one POST instead of two.

Pins are keyed by content hash (the same sha256 the pipeline already uses for
its cache) and by source path, so a re-read of the same file at a different
offset still hits the pin.

Content the model never expands is compressed exactly as before — this narrows
compression only where the model has demonstrated compression was wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paritok.config import ParitokConfig
from paritok.middleware.wrapper import ParitokEngine
from paritok.pipelines.compress import CompressionPipeline, CompressionResult
from paritok.storage import ShadowStorage, content_hash
from paritok.token_counter import _DEFAULT_ENCODING, count_tokens


@dataclass
class PinStats:
    """Counters for what the policy did, for reporting and tests."""

    pins: int = 0
    pin_hits: int = 0
    pinned_tokens_passed: int = 0
    pinned_ids: set[str] = field(default_factory=set)
    pinned_paths: set[str] = field(default_factory=set)


class PinningPipeline(CompressionPipeline):
    """CompressionPipeline that passes pinned content through untouched."""

    def __init__(self, config: ParitokConfig | None = None,
                 storage: ShadowStorage | None = None,
                 stats: PinStats | None = None):
        super().__init__(config, storage)
        self.pin_stats = stats or PinStats()

    def pin(self, shadow_id: str) -> None:
        """Mark a shadow id — and the path it came from, if known — as verbatim."""
        if not shadow_id or shadow_id in self.pin_stats.pinned_ids:
            return
        self.pin_stats.pinned_ids.add(shadow_id)
        self.pin_stats.pins += 1
        path = self._path_for_shadow(shadow_id)
        if path:
            self.pin_stats.pinned_paths.add(path)

    def _path_for_shadow(self, shadow_id: str) -> str | None:
        """Reverse the path->shadow map the pipeline maintains.

        ShadowStorage only exposes path->id, so this scans the in-process map
        when one is available. Absent that, pinning still works by content hash;
        only the different-offset re-read case is missed.
        """
        mapping = getattr(self.storage, "_path_to_shadow", None)
        if not isinstance(mapping, dict):
            return None
        for path, sid in mapping.items():
            if sid == shadow_id:
                return path
        return None

    def is_pinned(self, content: str, source: str | None) -> bool:
        if source and source in self.pin_stats.pinned_paths:
            return True
        return content_hash(content) in self.pin_stats.pinned_ids

    def compress(self, content: str, **kwargs) -> CompressionResult:
        source = kwargs.get("source")
        if self.is_pinned(content, source):
            enc = kwargs.get("upstream_model") or _DEFAULT_ENCODING
            tokens = count_tokens(content, enc)
            self.pin_stats.pin_hits += 1
            self.pin_stats.pinned_tokens_passed += tokens
            return CompressionResult(
                compressed=content,
                original_tokens=tokens,
                compressed_tokens=tokens,
                metadata={"skipped": True, "reason": "pinned_after_expand"},
            )
        return super().compress(content, **kwargs)


class PinningEngine(ParitokEngine):
    """ParitokEngine that records a pin whenever the model expands a ref."""

    def __init__(self, config: ParitokConfig | None = None,
                 storage: ShadowStorage | None = None):
        super().__init__(config, storage)
        self.pin_stats = PinStats()
        # Replace the stock pipeline, reusing the storage it already built so
        # existing [REF:id] entries stay resolvable.
        self.pipeline = PinningPipeline(self.config, self.storage, self.pin_stats)

    def resolve_virtual_call(self, tool_name: str, tool_input: dict,
                             stubbed_tools: list[dict] | None = None) -> dict | None:
        result = super().resolve_virtual_call(tool_name, tool_input, stubbed_tools)
        if tool_name == "expand_context" and result is not None:
            raw = (tool_input.get("shadow_id") or tool_input.get("id") or "").strip()
            if raw.startswith("[REF:"):
                raw = raw[5:]
            shadow_id = raw.split()[0].rstrip("]") if raw else ""
            # Only pin a ref that actually resolved; a miss returns the
            # "can no longer be expanded" notice, which is not a signal.
            if shadow_id and self.storage.retrieve(shadow_id) is not None:
                self.pipeline.pin(shadow_id)
        return result


def install() -> None:
    """Patch ParitokEngine at its construction site.

    proxy/server.py imports ParitokEngine inside run_proxy (server.py:275) and
    instantiates it at line 286, so rebinding the name on the wrapper module
    before run_proxy is called is enough — no fork of the proxy required.
    """
    from paritok.middleware import wrapper

    wrapper.ParitokEngine = PinningEngine
