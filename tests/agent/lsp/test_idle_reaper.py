"""Regression tests for the conservative LSP idle reaper.

Covers:
- selection matrix (idle / in-flight / timeout gates)
- zero-timeout opt-out
- overlapping ops race (per-key in-flight refcount)
- shutdown failure retry (failed shutdown re-inserts for next pass)
"""
from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest

from agent.lsp.manager import DEFAULT_IDLE_TIMEOUT, LSPService


class FakeClient:
    """Minimal stand-in for :class:`agent.lsp.client.LSPClient`."""

    def __init__(
        self,
        server_id: str = "pyright",
        workspace_root: str = "/tmp/workspace",
        *,
        fail_shutdown: bool = False,
    ) -> None:
        self.server_id = server_id
        self.workspace_root = workspace_root
        self.is_running = True
        self.state = "ready"
        self.shutdown_calls = 0
        self.fail_shutdown = fail_shutdown

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.fail_shutdown:
            raise RuntimeError("shutdown boom")
        self.is_running = False


def _make_service(idle_timeout: float = 10.0, *, enabled: bool = True) -> LSPService:
    """Build a service and cancel the background reaper for deterministic tests."""
    svc = LSPService(
        enabled=enabled,
        wait_mode="document",
        wait_timeout=2.0,
        install_strategy="manual",
        idle_timeout=idle_timeout,
    )
    fut = svc._reaper_future
    svc._reaper_future = None
    if fut is not None:
        fut.cancel()
    return svc


def _plant(
    svc: LSPService,
    *,
    key=("pyright", "/tmp/workspace"),
    last_used: float = 0.0,
    in_flight: int = 0,
    client: Optional[FakeClient] = None,
) -> FakeClient:
    client = client or FakeClient(server_id=key[0], workspace_root=key[1])
    svc._clients[key] = client
    svc._last_used[key] = last_used
    if in_flight:
        svc._in_flight[key] = in_flight
    return client


# ---------------------------------------------------------------------------
# Selection matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "idle_timeout,age,in_flight,spawning,expect_selected",
    [
        # idle past timeout, idle refcount → selected
        (10.0, 10.0, 0, False, True),
        # exactly at timeout → selected (>=)
        (10.0, 10.0, 0, False, True),
        # still warm → kept
        (10.0, 9.9, 0, False, False),
        # in-flight protects even when stale
        (10.0, 100.0, 1, False, False),
        # overlapping refs still protect
        (10.0, 100.0, 3, False, False),
        # mid-spawn keys are skipped
        (10.0, 100.0, 0, True, False),
        # zero timeout opt-out (also covered dedicated below)
        (0.0, 100.0, 0, False, False),
        # negative treated as disabled
        (-1.0, 100.0, 0, False, False),
    ],
)
def test_idle_reap_selection_matrix(
    idle_timeout, age, in_flight, spawning, expect_selected
):
    svc = _make_service(idle_timeout=idle_timeout)
    key = ("typescript", "/tmp/ws-a")
    _plant(svc, key=key, last_used=0.0, in_flight=in_flight)
    if spawning:
        svc._spawning[key] = MagicMock()  # presence is what matters

    selected = svc._idle_reap_candidates(now=age)
    if expect_selected:
        assert selected == [key]
    else:
        assert selected == []
    # Read-only: registry untouched.
    assert key in svc._clients
    svc.shutdown()


def test_selection_matrix_mixed_clients():
    """Only the stale, non-busy, non-spawning client is selected."""
    svc = _make_service(idle_timeout=10.0)
    stale = ("pyright", "/tmp/a")
    warm = ("pyright", "/tmp/b")
    busy = ("typescript", "/tmp/c")
    _plant(svc, key=stale, last_used=0.0)
    _plant(svc, key=warm, last_used=5.0)
    _plant(svc, key=busy, last_used=0.0, in_flight=1)

    selected = svc._idle_reap_candidates(now=10.0)
    assert selected == [stale]
    svc.shutdown()


# ---------------------------------------------------------------------------
# Zero-timeout opt-out
# ---------------------------------------------------------------------------


def test_zero_timeout_disables_reaper_and_selection():
    svc = _make_service(idle_timeout=0)
    assert svc._reaper_future is None
    key = ("pyright", "/tmp/workspace")
    client = _plant(svc, key=key, last_used=0.0)

    assert svc._idle_reap_candidates(now=10_000.0) == []
    claimed = svc._claim_idle_clients(now=10_000.0)
    assert claimed == []
    assert key in svc._clients
    assert client.shutdown_calls == 0
    svc.shutdown()


def test_create_from_config_reads_idle_timeout(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"lsp": {"enabled": False, "idle_timeout": 42}},
    )
    svc = LSPService.create_from_config()
    assert svc is not None
    assert svc._idle_timeout == 42.0
    assert svc._idle_timeout != DEFAULT_IDLE_TIMEOUT


def test_create_from_config_defaults_idle_timeout(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"lsp": {"enabled": False}},
    )
    svc = LSPService.create_from_config()
    assert svc is not None
    assert svc._idle_timeout == float(DEFAULT_IDLE_TIMEOUT)


# ---------------------------------------------------------------------------
# Overlapping ops race (in-flight refcount)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlapping_ops_race_refcount_protects_then_releases():
    """A stale client with in-flight > 0 must not be claimed; after release it can."""
    svc = _make_service(idle_timeout=10.0)
    key = ("pyright", "/tmp/workspace")
    client = _plant(svc, key=key, last_used=0.0)

    # Simulate two overlapping diagnostics ops.
    svc._acquire_in_flight(key)
    svc._acquire_in_flight(key)
    assert svc._in_flight[key] == 2

    assert svc._idle_reap_candidates(now=100.0) == []
    assert svc._claim_idle_clients(now=100.0) == []
    assert key in svc._clients
    assert client.shutdown_calls == 0

    svc._release_in_flight(key)
    assert svc._in_flight[key] == 1
    # Still protected by the remaining ref.
    svc._last_used[key] = 0.0
    assert svc._claim_idle_clients(now=100.0) == []

    svc._release_in_flight(key)
    assert key not in svc._in_flight

    # Release refreshed last_used — force stale and reap.
    svc._last_used[key] = 0.0
    await svc._reap_idle(now=100.0)
    assert client.shutdown_calls == 1
    assert key not in svc._clients
    svc.shutdown()


@pytest.mark.asyncio
async def test_claim_loses_race_to_in_flight_acquire():
    """If in-flight flips between scan and claim, claim must spare the client."""
    svc = _make_service(idle_timeout=10.0)
    key = ("gopls", "/tmp/go")
    client = _plant(svc, key=key, last_used=0.0)

    # Patch candidates to return the key, then acquire before claim's lock work
    # by injecting in-flight inside a wrapper around pop path — simplest:
    # acquire after candidates would have selected, then call claim which re-checks.
    assert svc._idle_reap_candidates(now=50.0) == [key]
    svc._acquire_in_flight(key)
    claimed = svc._claim_idle_clients(now=50.0)
    assert claimed == []
    assert svc._clients[key] is client
    svc._release_in_flight(key)
    svc.shutdown()


# ---------------------------------------------------------------------------
# Shutdown failure retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_failure_retries_on_next_pass():
    svc = _make_service(idle_timeout=10.0)
    key = ("pyright", "/tmp/workspace")
    client = FakeClient(fail_shutdown=True)
    _plant(svc, key=key, last_used=0.0, client=client)

    await svc._reap_idle(now=100.0)

    assert client.shutdown_calls == 1
    # Failed shutdown re-inserts into the registry for retry.
    assert svc._clients[key] is client
    assert key in svc._last_used

    # Next pass succeeds.
    client.fail_shutdown = False
    await svc._reap_idle(now=100.0)
    assert client.shutdown_calls == 2
    assert key not in svc._clients
    assert key not in svc._last_used
    svc.shutdown()


@pytest.mark.asyncio
async def test_shutdown_failure_does_not_clobber_replacement_client():
    """If a fresh client spawned while shutdown failed, do not put the old one back."""
    svc = _make_service(idle_timeout=10.0)
    key = ("pyright", "/tmp/workspace")
    old = FakeClient(server_id="pyright", workspace_root="/tmp/workspace")
    _plant(svc, key=key, last_used=0.0, client=old)

    replacement = FakeClient(server_id="pyright", workspace_root="/tmp/workspace")

    async def boom_shutdown():
        old.shutdown_calls += 1
        # Replacement arrived while the old client's shutdown was failing.
        svc._clients[key] = replacement
        svc._last_used[key] = 99.0
        raise RuntimeError("shutdown boom")

    old.shutdown = boom_shutdown  # type: ignore[method-assign]

    await svc._reap_idle(now=100.0)

    assert old.shutdown_calls == 1
    assert svc._clients[key] is replacement
    assert svc._last_used[key] == 99.0
    svc.shutdown()


@pytest.mark.asyncio
async def test_reap_idle_shuts_down_only_registry_clients():
    """Happy path: stale registry client is removed and shutdown awaited."""
    svc = _make_service(idle_timeout=5.0)
    key = ("rust-analyzer", "/tmp/rs")
    client = _plant(svc, key=key, last_used=0.0)

    await svc._reap_idle(now=5.0)

    assert client.shutdown_calls == 1
    assert not client.is_running
    assert key not in svc._clients
    assert key not in svc._last_used
    svc.shutdown()


def test_status_includes_idle_timeout():
    svc = _make_service(idle_timeout=123.0)
    info = svc.get_status()
    assert info["idle_timeout"] == 123.0
    svc.shutdown()


def test_reaper_starts_when_timeout_positive():
    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=2.0,
        install_strategy="manual",
        idle_timeout=600.0,
    )
    try:
        assert svc._reaper_future is not None
    finally:
        svc.shutdown()


def test_reaper_not_started_when_timeout_zero():
    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=2.0,
        install_strategy="manual",
        idle_timeout=0,
    )
    try:
        assert svc._reaper_future is None
    finally:
        svc.shutdown()
