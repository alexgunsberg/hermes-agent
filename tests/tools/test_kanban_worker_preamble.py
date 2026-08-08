"""Tests for the optional Kanban worker preamble (Buzz mem last-green SHA).

Run with:  python tests/tools/test_kanban_worker_preamble.py
(or: python -m unittest tests.tools.test_kanban_worker_preamble -v)

Scenarios:
  1. Disabled by default: no preamble section emitted.
  2. Graceful on missing `buzz` CLI: one-line skip note, no crash.
  3. Successful fetch: slug value rendered, with allow-list enforcement.
  4. Never crashes on any Buzz error (bad key, unreachable relay, etc.).
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_cli import kanban_db as kb
from hermes_cli import config as kb_config


def _open_board():
    """Create a fresh temp board DB and return its connection."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="kanban_preamble_test_")
    os.close(fd)
    os.remove(path)
    kb.init_db(Path(path))
    return kb.connect(Path(path)), path


def make_task(conn, task_id="t_test_preamble"):
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, workspace_kind, "
        "workspace_path, created_at, started_at) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, "Preamble test task", "running", "default", "scratch",
         "/tmp/x", 1700000000, 1700000000),
    )
    conn.commit()


class WorkerPreambleTests(unittest.TestCase):
    def _ctx(self, cfg, cli_path: "str | None" = "/fake/buzz", run_result=None):
        conn, path = _open_board()
        try:
            make_task(conn)
            patchers = [
                mock.patch.object(kb_config, "load_config", lambda: cfg),
                # Always override CLI resolution; cli_path=None forces the
                # "binary not found" graceful-skip branch.
                mock.patch.object(kb, "_resolve_buzz_cli", lambda configured="": cli_path or ""),
            ]
            if run_result is not None:
                patchers.append(
                    mock.patch.object(kb.subprocess, "run", lambda *a, **k: run_result)
                )
            for p in patchers:
                p.start()
            try:
                return kb.build_worker_context(conn, "t_test_preamble")
            finally:
                for p in patchers:
                    p.stop()
        finally:
            conn.close()
            os.remove(path)

    def test_disabled_by_default_emits_no_preamble(self):
        with mock.patch.object(kb_config, "load_config", lambda: {}):
            conn, path = _open_board()
            try:
                make_task(conn)
                ctx = kb.build_worker_context(conn, "t_test_preamble")
            finally:
                conn.close()
                os.remove(path)
        self.assertNotIn("## Worker preamble", ctx)
        self.assertNotIn("last-green-sha", ctx)

    def test_missing_cli_is_graceful(self):
        cfg = {"kanban": {"worker_preamble": {"buzz_mem_slugs": ["sl/last-green-sha"]}}}
        # cli_path=None => resolved to "" (binary not found)
        ctx = self._ctx(cfg, cli_path=None)
        self.assertIn("## Worker preamble", ctx)
        self.assertIn("`buzz` CLI not found", ctx)
        tail = ctx.split("## Worker preamble", 1)[1]
        self.assertNotIn("last-green-sha", tail)

    def test_successful_fetch_renders_value(self):
        cfg = {"kanban": {"worker_preamble": {"buzz_mem_slugs": ["sl/last-green-sha"]}}}

        class _Proc:
            returncode = 0
            stdout = b"abc1234deadbeef\n"
            stderr = b""

        ctx = self._ctx(cfg, run_result=_Proc())
        self.assertIn("sl/last-green-sha`: `abc1234deadbeef`", ctx)

    def test_denied_namespace_is_not_read(self):
        cfg = {"kanban": {"worker_preamble": {"buzz_mem_slugs": ["secret/x"]}}}
        calls = []

        def _fake_run(args, **k):
            calls.append(args)
            raise AssertionError("should never be called for denied slug")

        with mock.patch.object(kb_config, "load_config", lambda: cfg):
            with mock.patch.object(kb, "_resolve_buzz_cli", lambda configured="": "/fake/buzz"):
                with mock.patch.object(kb.subprocess, "run", _fake_run):
                    conn, path = _open_board()
                    try:
                        make_task(conn)
                        ctx = kb.build_worker_context(conn, "t_test_preamble")
                    finally:
                        conn.close()
                        os.remove(path)
        self.assertIn("denied (not an allow-listed namespace)", ctx)
        self.assertEqual(calls, [])

    def test_cli_error_is_reported_not_raised(self):
        cfg = {"kanban": {"worker_preamble": {"buzz_mem_slugs": ["sl/last-green-sha"]}}}

        class _Proc:
            returncode = 2
            stdout = b""
            stderr = b'{"error":"relay","message":"unreachable"}'

        ctx = self._ctx(cfg, run_result=_Proc())
        self.assertIn("unavailable", ctx)
        self.assertIn("exit 2", ctx)

    def test_load_config_failure_is_safe(self):
        def _boom():
            raise RuntimeError("boom")

        with mock.patch.object(kb_config, "load_config", _boom):
            conn, path = _open_board()
            try:
                make_task(conn)
                ctx = kb.build_worker_context(conn, "t_test_preamble")
            finally:
                conn.close()
                os.remove(path)
        self.assertNotIn("## Worker preamble", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
