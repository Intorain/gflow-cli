"""Live proof that GFLOW_CLI_BROWSER_PROXY routes the browser (#1036).

An APPROXIMATION of the reporter's network: a local logging CONNECT proxy on
127.0.0.1, not a network where Google is reachable only through a proxy. It proves
the browser's traffic goes through the configured proxy and that a dead proxy fails
fast with a hint naming the setting; it cannot prove the direct route was blocked.

Opt-in: ``-m e2e_image`` with ``GFLOW_CLI_E2E_PROFILE``. One image (quota, $0).
"""

from __future__ import annotations

import contextlib
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_image]


class _ConnectProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.hosts: list[str] = []


class _Handler(socketserver.StreamRequestHandler):
    server: _ConnectProxy

    def handle(self) -> None:
        line = self.rfile.readline(65536).decode("latin-1")
        while self.rfile.readline(65536) not in (b"\r\n", b"\n", b""):
            pass  # drop the request headers
        method, _, rest = line.partition(" ")
        target = rest.split(" ", 1)[0]
        self.server.hosts.append(f"{method} {target}")
        if method != "CONNECT":
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        host, _, port = target.rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)), timeout=15)
        except OSError:
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        client = self.connection
        with upstream, contextlib.suppress(OSError):  # either side may hang up
            while True:
                ready, _, _ = select.select([client, upstream], [], [], 60)
                if not ready:
                    return
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        return
                    (upstream if src is client else client).sendall(data)


@pytest.fixture
def proxy() -> Iterator[_ConnectProxy]:
    server = _ConnectProxy()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _dead_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _gflow(env: dict[str, str], *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "gflow_cli", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _google_hosts(server: _ConnectProxy) -> set[str]:
    return {h for h in server.hosts if "google" in h}


def test_t2i_generates_through_the_proxy(
    e2e_env: dict[str, str], proxy: _ConnectProxy, tmp_path: Path
) -> None:
    env = {**e2e_env, "GFLOW_CLI_BROWSER_PROXY": f"http://127.0.0.1:{proxy.server_address[1]}"}
    proc = _gflow(
        env, "image", "t2i", "a calm forest at dawn", "--out", str(tmp_path / "out"), "--json"
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "CONNECT flow.google.com:443" in proxy.hosts or "CONNECT labs.google:443" in proxy.hosts
    assert _google_hosts(proxy), proxy.hosts


def test_auth_status_verifies_through_the_proxy(
    e2e_env: dict[str, str], proxy: _ConnectProxy
) -> None:
    env = {**e2e_env, "GFLOW_CLI_BROWSER_PROXY": f"http://127.0.0.1:{proxy.server_address[1]}"}
    proc = _gflow(env, "auth", "status", "--profile", env["GFLOW_CLI_PROFILE"], timeout=180)
    assert proc.returncode == 0, proc.stdout[-800:] + proc.stderr[-800:]
    assert "verified" in proc.stdout
    # The browser-side probe (flow.google.com's server-attested myaccount check).
    assert "CONNECT myaccount.google.com:443" in proxy.hosts, proxy.hosts


def test_auth_renew_goes_through_the_proxy(e2e_env: dict[str, str], proxy: _ConnectProxy) -> None:
    env = {**e2e_env, "GFLOW_CLI_BROWSER_PROXY": f"http://127.0.0.1:{proxy.server_address[1]}"}
    proc = _gflow(env, "auth", "renew", "--profile", env["GFLOW_CLI_PROFILE"], "--json")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert '"authenticated"' in proc.stdout
    assert _google_hosts(proxy), proxy.hosts


def test_dead_proxy_fails_fast_naming_the_setting(e2e_env: dict[str, str], tmp_path: Path) -> None:
    env = {**e2e_env, "GFLOW_CLI_BROWSER_PROXY": f"http://127.0.0.1:{_dead_port()}"}
    started = time.monotonic()
    proc = _gflow(env, "image", "t2i", "a calm forest", "--out", str(tmp_path / "out"))
    elapsed = time.monotonic() - started
    assert proc.returncode == 11, proc.stderr[-800:]
    assert "GFLOW_CLI_BROWSER_PROXY" in proc.stdout + proc.stderr
    assert elapsed < 90, f"took {elapsed:.0f}s"
