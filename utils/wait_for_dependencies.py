"""
Boot-race mitigation: helpers that wait for required services before scripts proceed.

Problem: launchd plists with RunAtLoad=true fire when the Mac boots, but
Docker Desktop / FalkorDB / Tailscale need a few seconds (sometimes ~30s)
to come up. Scripts that connect immediately hit ConnectionError and fail
their first invocation. Watermark self-heals later but the failure pollutes
logs and burns a slot.

Solution: each script that needs a dependency calls wait_for_falkordb() or
wait_for_kg_hub_server() at the top. These poll the TCP port until it
accepts a connection or a deadline passes.

Idempotent + cheap: if the service is already up (typical steady-state), the
function returns within milliseconds.
"""

from __future__ import annotations

import socket
import sys
import time


def wait_for_port(
    host: str,
    port: int,
    timeout_seconds: float = 60.0,
    poll_interval: float = 1.0,
    label: str = "",
) -> bool:
    """
    Block until (host, port) accepts a TCP connection, or timeout.

    Returns True if connected, False if timed out.
    Prints progress every 5 seconds.
    """
    label = label or f"{host}:{port}"
    deadline = time.monotonic() + timeout_seconds
    last_status_print = 0.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return True
        except OSError:
            now = time.monotonic()
            if now - last_status_print >= 5.0:
                remaining = int(deadline - now)
                print(
                    f"[wait_for_port] {label} not ready, retrying… ({remaining}s left)",
                    file=sys.stderr,
                )
                last_status_print = now
            time.sleep(poll_interval)
    print(
        f"[wait_for_port] {label} did not become ready within {int(timeout_seconds)}s",
        file=sys.stderr,
    )
    return False


def wait_for_falkordb(timeout_seconds: float = 60.0) -> bool:
    """Wait for the FalkorDB container to accept connections on 127.0.0.1:6379."""
    return wait_for_port("127.0.0.1", 6379, timeout_seconds=timeout_seconds, label="FalkorDB")


def wait_for_kg_hub_server(timeout_seconds: float = 60.0) -> bool:
    """Wait for kg_hub_server's HTTP listener on 127.0.0.1:8080."""
    return wait_for_port("127.0.0.1", 8080, timeout_seconds=timeout_seconds, label="kg-hub-server")
