#!/usr/bin/env python3
"""Coding-agent routing benchmark — Grok Build vs Cursor (vs siblings).

Runs an identical suite of small coding tasks through one or more external
coding-agent CLIs (headless mode), each in a fresh scratch git repo, then
verifies every result with the task's own check command and emits a JSON +
Markdown comparison report. The report backs the routing rules in
``optional-skills/autonomous-ai-agents/coding-agent-routing``.

Usage:
    python scripts/coding_agent_bench.py --agents grok,cursor --out /tmp/bench
    python scripts/coding_agent_bench.py --agents grok --tasks my_tasks.json
    # Offline harness validation with a stub agent:
    python scripts/coding_agent_bench.py --agents stub \
        --stub-cmd 'python fake_agent.py {prompt}' --out /tmp/bench

Agents whose CLI or auth is missing are reported as ``unavailable`` (with the
reason) instead of failing the whole run, so partial environments still
produce a useful report. Only stdlib is used.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Task suite
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """One benchmark task: files to seed, a prompt, and a verify command."""

    name: str
    prompt: str
    files: dict  # path -> contents seeded into the scratch repo
    verify: str  # shell command; exit 0 == success

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        missing = {"name", "prompt", "files", "verify"} - set(d)
        if missing:
            raise ValueError(f"task missing keys: {sorted(missing)}")
        return cls(name=d["name"], prompt=d["prompt"], files=dict(d["files"]),
                   verify=d["verify"])


DEFAULT_TASKS = [
    Task(
        name="bugfix-off-by-one",
        prompt=(
            "slice_page() in pager.py is broken: page 1 repeats the last item "
            "of page 0. Fix the bug. Do not change test_pager.py. Run "
            "`python test_pager.py` to confirm."
        ),
        files={
            "pager.py": (
                "def slice_page(items, page, size):\n"
                "    start = page * size - (1 if page else 0)\n"
                "    return items[start:start + size]\n"
            ),
            "test_pager.py": (
                "from pager import slice_page\n"
                "items = list(range(10))\n"
                "assert slice_page(items, 0, 3) == [0, 1, 2]\n"
                "assert slice_page(items, 1, 3) == [3, 4, 5]\n"
                "assert slice_page(items, 3, 3) == [9]\n"
                "print('ok')\n"
            ),
        },
        verify="python test_pager.py",
    ),
    Task(
        name="feature-small",
        prompt=(
            "Add a function titlecase_slug(s) to textutil.py that turns "
            "'hello-world_again' into 'Hello World Again' (split on '-' and "
            "'_', capitalize each word, join with spaces). Run "
            "`python test_textutil.py` to confirm."
        ),
        files={
            "textutil.py": "def slugify(s):\n    return s.lower().replace(' ', '-')\n",
            "test_textutil.py": (
                "from textutil import titlecase_slug\n"
                "assert titlecase_slug('hello-world_again') == 'Hello World Again'\n"
                "assert titlecase_slug('one') == 'One'\n"
                "print('ok')\n"
            ),
        },
        verify="python test_textutil.py",
    ),
    Task(
        name="refactor-dedupe",
        prompt=(
            "report.py has three nearly identical formatting functions. "
            "Refactor them into one parameterized helper while keeping the "
            "public functions and their behavior. Run `python test_report.py` "
            "to confirm."
        ),
        files={
            "report.py": (
                "def format_error(msg):\n"
                "    return '[ERROR] ' + msg.strip().capitalize()\n\n"
                "def format_warning(msg):\n"
                "    return '[WARNING] ' + msg.strip().capitalize()\n\n"
                "def format_info(msg):\n"
                "    return '[INFO] ' + msg.strip().capitalize()\n"
            ),
            "test_report.py": (
                "import inspect\n"
                "import report\n"
                "assert report.format_error(' bad ') == '[ERROR] Bad'\n"
                "assert report.format_warning('x') == '[WARNING] X'\n"
                "assert report.format_info('y') == '[INFO] Y'\n"
                "src = inspect.getsource(report)\n"
                "assert src.count('.strip().capitalize()') <= 1, 'not deduplicated'\n"
                "print('ok')\n"
            ),
        },
        verify="python test_report.py",
    ),
    Task(
        name="write-tests",
        prompt=(
            "Write test_stack.py covering Stack in stack.py: push/pop order, "
            "peek, len, and that pop on an empty stack raises IndexError. It "
            "must run with plain `python test_stack.py` (no pytest) and print "
            "'ok' at the end."
        ),
        files={
            "stack.py": (
                "class Stack:\n"
                "    def __init__(self):\n"
                "        self._items = []\n"
                "    def push(self, x):\n"
                "        self._items.append(x)\n"
                "    def pop(self):\n"
                "        return self._items.pop()\n"
                "    def peek(self):\n"
                "        return self._items[-1]\n"
                "    def __len__(self):\n"
                "        return len(self._items)\n"
            ),
        },
        verify="python test_stack.py && grep -q IndexError test_stack.py",
    ),
    Task(
        name="docs-readme",
        prompt=(
            "Write a README.md for this project (a tiny CLI in cli.py). It "
            "must contain a '# ' title, an '## Usage' section showing the "
            "greet command, and an '## License' section saying MIT."
        ),
        files={
            "cli.py": (
                "import sys\n"
                "def main():\n"
                "    if sys.argv[1:2] == ['greet']:\n"
                "        print('hello', *sys.argv[2:])\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            ),
        },
        verify=(
            "test -s README.md && grep -q '^# ' README.md && "
            "grep -qi '## Usage' README.md && grep -qi 'MIT' README.md"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Agent adapters
# ---------------------------------------------------------------------------


@dataclass
class Agent:
    """One delegate CLI: how to check availability and build a headless call."""

    name: str
    binary: str
    build_cmd: "callable"  # (prompt) -> argv list
    auth_check: "callable"  # () -> None | reason string
    timeout: int = 600
    model: str = "harness-default"
    version_argv: list = None

    def availability(self) -> str | None:
        if shutil.which(self.binary) is None:
            return f"binary `{self.binary}` not found on PATH"
        return self.auth_check()

    def resolved_version(self) -> str:
        if not self.version_argv:
            return "unversioned"
        try:
            out = subprocess.run(
                self.version_argv, capture_output=True, text=True, timeout=30)
            return (out.stdout or out.stderr).strip().splitlines()[0]
        except Exception as exc:  # pragma: no cover - defensive
            return f"version-check-failed: {exc}"


def _grok_auth() -> str | None:
    if os.environ.get("XAI_API_KEY"):
        return None
    if (Path.home() / ".grok" / "auth.json").exists():
        return None
    return "no ~/.grok/auth.json and XAI_API_KEY unset (run `grok login --device-code`)"


def _cursor_auth() -> str | None:
    if os.environ.get("CURSOR_API_KEY"):
        return None
    probe = subprocess.run(
        ["cursor-agent", "status"], capture_output=True, text=True, timeout=30,
    )
    if probe.returncode == 0 and "not" not in probe.stdout.lower():
        return None
    return "CURSOR_API_KEY unset and `cursor-agent status` reports no login"


def builtin_agents(stub_cmd: str | None = None,
                   grok_model: str = "grok-lean",
                   grok_effort: str = "high",
                   cursor_model: str = "grok-4.5[effort=high,fast=false]") -> dict:
    # Both adapters pin an explicit model/profile — never a plain-default
    # invocation — and every result records the resolved CLI version, so a
    # comparison is always attributable to an exact (version, model) pair.
    agents = {
        "grok": Agent(
            name="grok",
            binary="grok",
            build_cmd=lambda prompt: [
                "grok", "--no-auto-update", "--always-approve",
                "-m", grok_model, "--reasoning-effort", grok_effort,
                "--max-turns", "40", "--no-subagents", "--no-memory",
                "--disable-web-search",
                "--output-format", "json", "-p", prompt,
            ],
            auth_check=_grok_auth,
            model=f"{grok_model}[effort={grok_effort}]",
            version_argv=["grok", "--version"],
        ),
        "cursor": Agent(
            name="cursor",
            binary="cursor-agent",
            build_cmd=lambda prompt: [
                "cursor-agent", "-p", "--force", "-m", cursor_model,
                "--output-format", "json", prompt,
            ],
            auth_check=_cursor_auth,
            model=cursor_model,
            version_argv=["cursor-agent", "--version"],
        ),
    }
    if stub_cmd:
        agents["stub"] = Agent(
            name="stub",
            binary=shlex.split(stub_cmd)[0],
            build_cmd=lambda prompt: [
                (prompt if part == "{prompt}" else part)
                for part in shlex.split(stub_cmd)
            ],
            auth_check=lambda: None,
            timeout=120,
        )
    return agents


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class Result:
    agent: str
    task: str
    status: str  # passed | failed | agent-error | timeout | unavailable
    duration_s: float = 0.0
    files_changed: int = 0
    lines_changed: int = 0
    detail: str = ""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@local",
             "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@local"},
    )


def _seed_repo(task: Task, root: Path) -> Path:
    repo = root / task.name
    repo.mkdir(parents=True)
    for rel, contents in task.files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _diff_stats(repo: Path) -> tuple[int, int]:
    _git(repo, "add", "-A")
    out = _git(repo, "diff", "--cached", "--numstat", "HEAD").stdout
    files = lines = 0
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) >= 2:
            files += 1
            for cell in parts[:2]:
                if cell.isdigit():
                    lines += int(cell)
    return files, lines


def run_task(agent: Agent, task: Task, root: Path) -> Result:
    repo = _seed_repo(task, root / agent.name)
    cmd = agent.build_cmd(task.prompt)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=repo, capture_output=True, text=True, timeout=agent.timeout,
        )
    except subprocess.TimeoutExpired:
        return Result(agent.name, task.name, "timeout",
                      duration_s=round(time.monotonic() - start, 2),
                      detail=f"exceeded {agent.timeout}s")
    duration = round(time.monotonic() - start, 2)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        return Result(agent.name, task.name, "agent-error", duration_s=duration,
                      detail=f"exit {proc.returncode}: {tail}")
    files, lines = _diff_stats(repo)
    check = subprocess.run(
        ["bash", "-c", task.verify], cwd=repo, capture_output=True, text=True,
        timeout=120,
    )
    status = "passed" if check.returncode == 0 else "failed"
    detail = "" if status == "passed" else (
        (check.stderr or check.stdout or "").strip()[-400:])
    return Result(agent.name, task.name, status, duration_s=duration,
                  files_changed=files, lines_changed=lines, detail=detail)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_markdown(results: list, unavailable: dict, tasks: list) -> str:
    lines = ["# Coding-agent routing benchmark", ""]
    if unavailable:
        lines.append("## Unavailable agents")
        lines.append("")
        for name, reason in sorted(unavailable.items()):
            lines.append(f"- **{name}**: {reason}")
        lines.append("")
    agents = sorted({r.agent for r in results})
    if agents:
        lines += ["## Summary", "",
                  "| Agent | Passed | Failed | Errors/Timeouts | Total time (s) |",
                  "|---|---|---|---|---|"]
        for a in agents:
            rs = [r for r in results if r.agent == a]
            passed = sum(r.status == "passed" for r in rs)
            failed = sum(r.status == "failed" for r in rs)
            errors = len(rs) - passed - failed
            total = round(sum(r.duration_s for r in rs), 1)
            lines.append(f"| {a} | {passed}/{len(rs)} | {failed} | {errors} | {total} |")
        lines += ["", "## Per-task results", "",
                  "| Task | Agent | Status | Time (s) | Files Δ | Lines Δ | Detail |",
                  "|---|---|---|---|---|---|---|"]
        for t in tasks:
            for r in sorted((r for r in results if r.task == t.name),
                            key=lambda r: r.agent):
                detail = r.detail.replace("|", "\\|").replace("\n", " ")[:120]
                lines.append(
                    f"| {t.name} | {r.agent} | {r.status} | {r.duration_s} "
                    f"| {r.files_changed} | {r.lines_changed} | {detail} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_tasks(path: str | None) -> list:
    if not path:
        return list(DEFAULT_TASKS)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("tasks file must be a non-empty JSON array")
    return [Task.from_dict(d) for d in data]


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agents", default="grok,cursor",
                    help="comma-separated: grok,cursor,stub")
    ap.add_argument("--tasks", help="JSON file of tasks (default: built-in suite)")
    ap.add_argument("--out", default=None,
                    help="output directory for report.json / report.md")
    ap.add_argument("--stub-cmd", default=None,
                    help="command template for the `stub` agent; {prompt} is substituted")
    ap.add_argument("--timeout", type=int, default=600,
                    help="per-task agent timeout in seconds")
    ap.add_argument("--grok-model", default="grok-lean")
    ap.add_argument("--grok-effort", default="high")
    ap.add_argument("--cursor-model", default="grok-4.5[effort=high,fast=false]")
    args = ap.parse_args(argv)

    tasks = load_tasks(args.tasks)
    registry = builtin_agents(stub_cmd=args.stub_cmd,
                              grok_model=args.grok_model,
                              grok_effort=args.grok_effort,
                              cursor_model=args.cursor_model)
    requested = [a.strip() for a in args.agents.split(",") if a.strip()]
    unknown = [a for a in requested if a not in registry]
    if unknown:
        ap.error(f"unknown agents: {unknown} (known: {sorted(registry)})")

    results: list = []
    unavailable: dict = {}
    agent_meta: dict = {}
    workdir = Path(tempfile.mkdtemp(prefix="agent-bench-"))
    for name in requested:
        agent = registry[name]
        agent.timeout = args.timeout if name != "stub" else agent.timeout
        reason = agent.availability()
        if reason:
            unavailable[name] = reason
            print(f"[bench] {name}: UNAVAILABLE — {reason}", file=sys.stderr)
            continue
        agent_meta[name] = {"model": agent.model,
                            "resolved_version": agent.resolved_version()}
        for task in tasks:
            print(f"[bench] {name} ⇐ {task.name} ...", file=sys.stderr)
            res = run_task(agent, task, workdir)
            print(f"[bench]   {res.status} in {res.duration_s}s", file=sys.stderr)
            results.append(res)

    report = {
        "tasks": [t.name for t in tasks],
        "unavailable": unavailable,
        "agents": agent_meta,
        "results": [vars(r) for r in results],
        "workdir": str(workdir),
    }
    md = render_markdown(results, unavailable, tasks)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
        (out / "report.md").write_text(md, encoding="utf-8")
        print(f"[bench] wrote {out}/report.json and report.md", file=sys.stderr)
    print(md)
    # Exit 0 as long as the harness itself ran; agent failures are data.
    return 0


if __name__ == "__main__":
    sys.exit(main())
