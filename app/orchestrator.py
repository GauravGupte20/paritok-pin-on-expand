"""Manages the real `paritok proxy` processes the app measures.

Both variants are genuine proxy processes, not reimplementations:

  stock  — `paritok proxy`, unmodified
  pinned — the same proxy with paritok_adaptive.install() applied first

Each run restarts both. That is deliberate: the compression cache, the shadow
store and the pin set all live for the life of the process, and /stats is
cumulative — a warm proxy silently returns a previous run's result. Restarting
is the only way a run means what it says.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
STARTUP_TIMEOUT = 45.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_config(*, mock_base: str, use_gpu: bool, api_key: str) -> Path:
    """A paritok.yaml pointing compression at the hosted GPU or the mock."""
    lines = [
        f"use_gpu_server: {'true' if use_gpu else 'false'}",
        "shadow_storage: memory",
        "tool_discovery:",
        "  strategy: passthrough",
        "compression:",
        "  min_tokens: 512",
        "  max_tokens: 50000",
    ]
    if use_gpu:
        lines += ["gpu_server:", f'  api_key: "{api_key}"']
    else:
        lines += [
            "local_model:",
            f"  base_url: {mock_base}/v1",
            "  model: mock-compressor",
        ]
    path = Path(tempfile.mkdtemp(prefix="paritok-cfg-")) / "paritok.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@dataclass
class ProxyHandle:
    variant: str
    port: int
    process: subprocess.Popen

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()


class ProxyFleet:
    """Starts a stock and a pinned proxy, both pointed at the in-process mock."""

    def __init__(self, mock_base: str):
        self.mock_base = mock_base
        self.api_key = os.environ.get("PARITOK_API_KEY", "").strip()
        self.use_gpu = bool(self.api_key)
        self.handles: dict[str, ProxyHandle] = {}

    @property
    def compressor(self) -> str:
        return "paritok-hosted-gpu" if self.use_gpu else "mock-compressor"

    def _spawn(self, variant: str) -> ProxyHandle:
        port = _free_port()
        config = _write_config(mock_base=self.mock_base,
                               use_gpu=self.use_gpu, api_key=self.api_key)
        if variant == "pinned":
            cmd = [sys.executable, str(REPO_ROOT / "run_pinned_proxy.py")]
        else:
            cmd = [sys.executable, "-m", "paritok.cli", "proxy"]
        cmd += ["--port", str(port),
                "--anthropic-url", self.mock_base,
                "--config-file", str(config)]

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ProxyHandle(variant=variant, port=port, process=proc)

    def _wait_healthy(self, handle: ProxyHandle) -> None:
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if handle.process.poll() is not None:
                raise RuntimeError(
                    f"{handle.variant} proxy exited during startup "
                    f"(code {handle.process.returncode})"
                )
            try:
                if httpx.get(f"{handle.url}/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        handle.stop()
        raise RuntimeError(f"{handle.variant} proxy did not become healthy in time")

    def restart(self) -> None:
        """Cold-start both proxies. Every run begins here."""
        self.stop()
        for variant in ("stock", "pinned"):
            handle = self._spawn(variant)
            try:
                self._wait_healthy(handle)
            except Exception:
                self.stop()
                raise
            self.handles[variant] = handle

    def stop(self) -> None:
        for handle in self.handles.values():
            handle.stop()
        self.handles.clear()

    def url(self, variant: str) -> str:
        return self.handles[variant].url
