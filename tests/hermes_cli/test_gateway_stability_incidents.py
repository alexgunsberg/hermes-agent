from __future__ import annotations

from pathlib import Path

from hermes_cli.gateway_stability_incidents import (
    analyze_profile_logs,
    detect_gateway_incidents,
    format_incidents,
    incidents_to_json,
)


def _write_log(home: Path, name: str, text: str) -> Path:
    path = home / "logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_repeated_unexpected_sigterm_detected_with_safe_default_owner(tmp_path: Path):
    home = tmp_path / "default"
    _write_log(
        home,
        "gateway.log",
        """
        2026-07-08 10:00:00,000 INFO Received SIGTERM as a planned gateway stop — exiting cleanly
        2026-07-08 10:01:00,000 INFO Received SIGTERM — initiating shutdown
        2026-07-08 10:01:00,010 WARNING Shutdown context: signal=SIGTERM pid=100 parent_cmdline=launchd
        2026-07-08 10:01:15,000 INFO Starting Hermes Gateway...
        2026-07-08 10:01:20,000 INFO Received SIGTERM — initiating shutdown
        """,
    )

    incidents = analyze_profile_logs("default", home, max_lines=100)

    sigterm = [incident for incident in incidents if incident.kind == "repeated_unexpected_sigterm"]
    assert len(sigterm) == 1
    assert sigterm[0].severity == "critical"
    assert sigterm[0].count == 2
    assert "another shell/profile" in sigterm[0].safe_next_command
    assert "do not restart default from inside itself" in sigterm[0].owner
    assert all("planned gateway stop" not in line.text for line in sigterm[0].evidence)


def test_restart_loop_breaker_tripped_is_significant_on_one_line(tmp_path: Path):
    home = tmp_path / "default"
    _write_log(
        home,
        "gateway.error.log",
        """
        2026-07-08 10:02:00,000 WARNING Restart-loop breaker TRIPPED: 3 restart-interrupted gateway boots within 60s (threshold 3). Skipping auto-resume to break a suspected SIGTERM-respawn loop (#30719).
        """,
    )

    incidents = analyze_profile_logs("default", home, max_lines=20)

    assert [incident.kind for incident in incidents] == ["restart_loop_breaker_tripped"]
    assert incidents[0].severity == "critical"
    assert "Restart-loop breaker TRIPPED" in incidents[0].evidence[0].text


def test_telegram_dns_fallback_cluster_requires_repetition_and_network_context(tmp_path: Path):
    home = tmp_path / "x"
    _write_log(
        home,
        "gateway.log",
        """
        2026-07-08 10:03:00,000 WARNING docs mention DNS but are unrelated
        2026-07-08 10:03:01,000 WARNING [telegram] NetworkError: httpx.ConnectError Temporary failure in name resolution for api.telegram.org
        2026-07-08 10:03:02,000 WARNING [telegram] DNS getaddrinfo failed; trying fallback IP
        2026-07-08 10:03:03,000 WARNING [telegram] fallback IP connect failed for api.telegram.org
        """,
    )

    incidents = analyze_profile_logs("x", home, max_lines=50)

    dns = [incident for incident in incidents if incident.kind == "telegram_dns_fallback_cluster"]
    assert len(dns) == 1
    assert dns[0].count == 3
    assert dns[0].safe_next_command == "hermes --profile x gateway status --deep --full"
    assert all("docs mention DNS" not in line.text for line in dns[0].evidence)


def test_stale_session_recovery_detects_single_significant_heal(tmp_path: Path):
    home = tmp_path / "websites"
    _write_log(
        home,
        "gateway.log",
        """
        2026-07-08 10:04:00,000 WARNING [telegram] Healing stale session lock for telegram:123:456 (owner task is done/absent)
        """,
    )

    incidents = analyze_profile_logs("websites", home, max_lines=20)

    stale = [incident for incident in incidents if incident.kind == "stale_session_recovery"]
    assert len(stale) == 1
    assert stale[0].severity == "warning"
    assert stale[0].owner == "websites gateway operator"


def test_detector_and_format_are_silent_when_no_incidents(tmp_path: Path):
    home = tmp_path / "default"
    _write_log(
        home,
        "gateway.log",
        """
        2026-07-08 10:05:00,000 INFO Starting Hermes Gateway...
        2026-07-08 10:05:01,000 INFO Connected to Telegram
        """,
    )

    incidents = detect_gateway_incidents(profile_homes=[("default", home)], max_lines=20)

    assert incidents == []
    assert format_incidents(incidents, show_empty=False) == ""
    assert incidents_to_json(incidents) == "[]"
