"""Engine construction for replay runs, and the sample content fixtures."""

from __future__ import annotations

import os

from paritok.config import ParitokConfig
from paritok.middleware.wrapper import ParitokEngine
from paritok.storage import MemoryShadowStorage

from harness.stub_backend import StubCompressor


def build_config(**overrides) -> ParitokConfig:
    """Config suited to offline replay.

    tool_discovery is forced to "passthrough": the default "embedding" strategy
    downloads bge-small and is irrelevant to content compression, which is what
    we are measuring.
    """
    config = ParitokConfig()
    config.tool_discovery.strategy = "passthrough"
    config.shadow_storage = "memory"
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def build_engine(config: ParitokConfig | None = None,
                 engine_cls: type[ParitokEngine] = ParitokEngine
                 ) -> tuple[ParitokEngine, StubCompressor]:
    """An engine wired to the deterministic stub compressor.

    Returns (engine, stub) so callers can assert on how many times the
    compressor was actually invoked — the cheapest signal that a policy is
    doing something.
    """
    config = config or build_config()
    engine = engine_cls(config, MemoryShadowStorage())
    stub = StubCompressor()
    engine.pipeline._model = stub
    return engine, stub


# Python's own argparse: real, substantial, widely available, and — unlike any
# file from the Paritok tree — free of the vocabulary this harness searches for.
# A fixture containing the literal strings "expand_context" or "[REF:" makes
# detection flags fire on the fixture's own text rather than on proxy behaviour.
_DEFAULT_FIXTURE = "/usr/lib/python3.12/argparse.py"


def real_source(path: str = _DEFAULT_FIXTURE) -> str:
    """A real source file, used as the file_read fixture.

    Preferred over sample_source() for anything that produces a number. The
    synthetic generator emits near-identical functions, which
    strategies/chunking.py:deduplicate_definitions collapses almost entirely —
    yielding a compression rate no real file would ever hit. Real source has the
    duplication profile the model was actually trained on.
    """
    from pathlib import Path

    return Path(os.environ.get("PARITOK_FIXTURE", path)).read_text(encoding="utf-8")


# A synthetic file_read segment. Fine for structural tests (does a [REF:] tag
# appear, does the resolve loop run); NOT suitable for measuring compression —
# see real_source() above.
def sample_source(name: str = "auth", n_funcs: int = 26) -> str:
    header = (
        f'"""{name} module."""\n\n'
        "import hashlib\n"
        "import logging\n"
        "from dataclasses import dataclass\n\n"
        "logger = logging.getLogger(__name__)\n\n"
    )
    body = []
    for i in range(n_funcs):
        body.append(
            f"def {name}_handler_{i}(request, session, *, retries: int = 3):\n"
            f'    """Handle {name} request variant {i}."""\n'
            f"    token = hashlib.sha256(str(request).encode()).hexdigest()\n"
            f"    logger.debug('handling {name} variant {i} token=%s', token)\n"
            f"    if not session.is_valid():\n"
            f"        raise ValueError('invalid session in {name}_handler_{i}')\n"
            f"    return {{'variant': {i}, 'token': token, 'retries': retries}}\n"
        )
    return header + "\n".join(body)
