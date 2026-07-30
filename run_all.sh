#!/usr/bin/env bash
# Start servers and run every measurement in this repo, from cold.
set -euo pipefail
PY=./.venv/bin/python
pkill -f harness/mock_upstream.py 2>/dev/null || true
pkill -f "paritok proxy" 2>/dev/null || true
pkill -f run_pinned_proxy.py 2>/dev/null || true
sleep 2
PYTHONPATH=. $PY harness/mock_upstream.py & sleep 2
./.venv/bin/paritok proxy --port 8080 --anthropic-url http://127.0.0.1:9100 --config-file paritok.yaml & sleep 3
PYTHONPATH=. $PY run_pinned_proxy.py --port 8081 --anthropic-url http://127.0.0.1:9100 --config-file paritok.yaml & sleep 3
$PY validate_real_proxy.py || true
$PY bench_ab.py
$PY -m pytest tests/ -q
