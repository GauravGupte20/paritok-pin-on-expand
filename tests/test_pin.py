"""Tests for pin-on-expand, run against the real engine with a stub compressor."""

from __future__ import annotations

import pytest

from harness.build import build_config, build_engine, real_source
from harness.session import SimulatedSession, refs_in
from paritok_adaptive import PinningEngine


@pytest.fixture
def source() -> str:
    return real_source()


def test_content_compresses_and_gets_a_ref(source):
    engine, _ = build_engine()
    session = SimulatedSession(engine)
    session.user("fix the parser")
    session.tool_result("Read", {"file_path": "/repo/argparse.py"}, source)
    rec = session.send()
    assert rec.refs_present, "large file_read should produce a [REF:] tag"
    assert rec.compressed_tokens < rec.original_tokens


def test_expand_records_a_pin(source):
    engine, _ = build_engine(build_config(), engine_cls=PinningEngine)
    session = SimulatedSession(engine)
    session.user("fix the parser")
    session.tool_result("Read", {"file_path": "/repo/argparse.py"}, source)
    rec = session.send()
    ref = rec.refs_present[0]

    assert engine.pin_stats.pins == 0
    session.expand(ref)
    assert engine.pin_stats.pins == 1
    assert ref in engine.pin_stats.pinned_ids


def test_pinned_content_passes_through_verbatim(source):
    engine, _ = build_engine(build_config(), engine_cls=PinningEngine)
    session = SimulatedSession(engine)
    session.user("fix the parser")
    session.tool_result("Read", {"file_path": "/repo/argparse.py"}, source)
    first = session.send()
    session.expand(first.refs_present[0])

    # A fresh session against the same engine: the client re-sends the original,
    # which is what a real agent does every turn.
    again = SimulatedSession(engine)
    again.user("fix the parser")
    again.tool_result("Read", {"file_path": "/repo/argparse.py"}, source)
    rec = again.send()

    assert not refs_in(str(rec.refs_present)), "pinned content must not be re-tagged"
    assert rec.refs_present == []
    assert engine.pin_stats.pin_hits >= 1


def test_unexpanded_content_still_compresses(source):
    """Pinning must not disable compression for content nobody expanded."""
    engine, _ = build_engine(build_config(), engine_cls=PinningEngine)
    session = SimulatedSession(engine)
    session.user("fix the parser")
    session.tool_result("Read", {"file_path": "/repo/other.py"}, source)
    rec = session.send()
    assert rec.refs_present
    assert engine.pin_stats.pin_hits == 0


def test_failed_expand_does_not_pin():
    """A ref that cannot be resolved is not a signal about any content."""
    engine, _ = build_engine(build_config(), engine_cls=PinningEngine)
    result = engine.resolve_virtual_call("expand_context", {"shadow_id": "deadbeef"})
    assert result is not None  # returns the non-fatal notice
    assert engine.pin_stats.pins == 0
