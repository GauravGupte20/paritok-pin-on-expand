"""Deterministic stand-in for the Paritok 4B compressor.

The pipeline logic we care about — cache keying, [REF:id] tagging, shadow
storage, expand/re-collapse behaviour — is entirely independent of *what* the
compressor writes. Swapping the model for a deterministic function makes replay
runs reproducible, free, and fast enough to run in a loop, which is what lets
this harness be used as a test rather than an experiment.

Real-model runs use the same harness with the stub swapped out; see replay.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Roughly the compression the 4B model achieves on file_read segments (25.7%),
# so stub runs land in the same ballpark as real ones and the token accounting
# in a replay is representative rather than arbitrary.
_KEEP_FRACTION = 0.25


@dataclass
class StubCompressor:
    """Deterministic compressor with the LocalModelStrategy.compress signature.

    Keeps the first `_KEEP_FRACTION` of lines verbatim and replaces the rest with
    a marker. Crude, but it preserves the property that matters for pipeline
    tests: same input -> same output, every time.
    """

    calls: list[dict] = field(default_factory=list)

    def compress(
        self,
        text: str,
        *,
        query: str | None = None,
        level: str | None = None,
        kind: str | None = None,
        target_ratio: str | None = None,
        upstream_model: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        self.calls.append({"level": level, "kind": kind, "query": query,
                           "chars": len(text)})
        lines = text.splitlines()
        keep = max(1, int(len(lines) * _KEEP_FRACTION))
        head = "\n".join(lines[:keep])
        return f"{head}\n[... {len(lines) - keep} lines compressed by stub ...]"

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.calls.clear()
