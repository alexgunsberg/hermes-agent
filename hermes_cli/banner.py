"""Welcome banner, ASCII art, skills summary, and update check for the CLI.

Pure display functions with no HermesCLI state dependency.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from hermes_constants import get_hermes_home
from typing import TYPE_CHECKING, Dict, List, Optional

# rich and prompt_toolkit are imported lazily (inside the functions that use
# them) rather than at module level.  Importing this module is on the TUI
# gateway's critical startup path purely to reach the lightweight update-check
# helpers (``prefetch_update_check``); pulling rich.console + prompt_toolkit
# eagerly added ~50ms of wasted imports before ``gateway.ready`` could fire.
# Keep the type-only reference available to checkers without the runtime cost.
if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)


# =========================================================================
# ANSI building blocks for conversation display
# =========================================================================

_GOLD = "\033[1;38;2;255;215;0m"  # True-color #FFD700 bold
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RST = "\033[0m"


def cprint(text: str):
    """Print ANSI-colored text through prompt_toolkit's renderer."""
    from prompt_toolkit import print_formatted_text as _pt_print
    from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
    try:
        _pt_print(_PT_ANSI(text))
    except Exception:
        # prompt_toolkit needs a real console. On Windows, a redirected or
        # absent stdout (pythonw.exe, CI, `hermes ... > file`) raises
        # NoConsoleScreenBufferError from its Win32Output — display helpers
        # must never crash the caller over that, so degrade to plain print.
        print(text)


# =========================================================================
# Skin-aware color helpers
# =========================================================================

def _skin_color(key: str, fallback: str) -> str:
    """Get a color from the active skin, or return fallback."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin().get_color(key, fallback)
    except Exception:
        return fallback
# =========================================================================
# ASCII Art & Branding
# =========================================================================

from hermes_cli import __version__ as VERSION, __release_date__ as RELEASE_DATE

HERMES_AGENT_LOGO = """[bold #FFD700]██╗  ██╗███████╗██████╗ ███╗   ███╗███████╗███████╗       █████╗  ██████╗ ███████╗███╗   ██╗████████╗[/]
[bold #FFD700]██║  ██║██╔════╝██╔══██╗████╗ ████║██╔════╝██╔════╝      ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝[/]
[#FFBF00]███████║█████╗  ██████╔╝██╔████╔██║█████╗  ███████╗█████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║[/]
[#FFBF00]██╔══██║██╔══╝  ██╔══██╗██║╚██╔╝██║██╔══╝  ╚════██║╚════╝██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║[/]
[#CD7F32]██║  ██║███████╗██║  ██║██║ ╚═╝ ██║███████╗███████║      ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║[/]
[#CD7F32]╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚══════╝╚══════╝      ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝[/]"""

HERMES_CADUCEUS = """[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⡀⠀⣀⣀⠀⢀⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#CD7F32]⠀⠀⠀⠀⠀⠀⢀⣠⣴⣾⣿⣿⣇⠸⣿⣿⠇⣸⣿⣿⣷⣦⣄⡀⠀⠀⠀⠀⠀⠀[/]
[#FFBF00]⠀⢀⣠⣴⣶⠿⠋⣩⡿⣿⡿⠻⣿⡇⢠⡄⢸⣿⠟⢿⣿⢿⣍⠙⠿⣶⣦⣄⡀⠀[/]
[#FFBF00]⠀⠀⠉⠉⠁⠶⠟⠋⠀⠉⠀⢀⣈⣁⡈⢁⣈⣁⡀⠀⠉⠀⠙⠻⠶⠈⠉⠉⠀⠀[/]
[#FFD700]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣴⣿⡿⠛⢁⡈⠛⢿⣿⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#FFD700]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠿⣿⣦⣤⣈⠁⢠⣴⣿⠿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#FFBF00]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠉⠻⢿⣿⣦⡉⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#FFBF00]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢷⣦⣈⠛⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣴⠦⠈⠙⠿⣦⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⣿⣤⡈⠁⢤⣿⠇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠛⠷⠄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⠑⢶⣄⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⠁⢰⡆⠈⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠳⠈⣡⠞⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]"""



# =========================================================================
# Skills scanning
# =========================================================================

def get_available_skills() -> Dict[str, List[str]]:
    """Return skills grouped by category, filtered by platform and disabled state.

    Delegates to ``_find_all_skills()`` from ``tools/skills_tool`` which already
    handles platform gating (``platforms:`` frontmatter) and respects the
    user's ``skills.disabled`` config list.
    """
    try:
        from tools.skills_tool import _find_all_skills
        all_skills = _find_all_skills()  # already filtered
    except Exception:
        return {}

    skills_by_category: Dict[str, List[str]] = {}
    for skill in all_skills:
        category = skill.get("category") or "general"
        skills_by_category.setdefault(category, []).append(skill["name"])
    return skills_by_category


# =========================================================================
# Update check
# =========================================================================

# Cache update check results for 6 hours to avoid repeated git fetches
_UPDATE_CHECK_CACHE_SECONDS = 6 * 3600

# Sentinel returned when we know an update exists but can't count commits
# (e.g. nix-built hermes — no local git history to count against).
UPDATE_AVAILABLE_NO_COUNT = -1

_UPSTREAM_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"
_OFFICIAL_REPO_CANONICAL = "github.com/nousresearch/hermes-agent"

# Absolute shallow-history target for passive update checks. Used as
# ``git fetch --depth <N>`` (idempotent absolute depth), never relative
# ``--deepen`` on every cache expiry. Passive at or near N once and stays
# there; equal-tip checks avoid depth recovery entirely via ls-remote.
_SHALLOW_HISTORY_TARGET = 200


def _canonical_github_remote(url: str | None) -> str:
    """Return ``host/owner/repo`` for common GitHub remote URL forms."""
    if not url:
        return ""
    value = url.strip()
    if value.startswith("git@github.com:"):
        value = "github.com/" + value[len("git@github.com:"):]
    elif value.startswith("ssh://git@github.com/"):
        value = "github.com/" + value[len("ssh://git@github.com/"):]
    else:
        parsed = urlparse(value)
        if parsed.netloc and parsed.path:
            value = f"{parsed.netloc}{parsed.path}"
    value = value.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.lower()


def _is_ssh_remote(url: str | None) -> bool:
    if not url:
        return False
    value = url.strip().lower()
    return value.startswith("git@") or value.startswith("ssh://")


def _is_official_ssh_remote(url: str | None) -> bool:
    return _is_ssh_remote(url) and _canonical_github_remote(url) == _OFFICIAL_REPO_CANONICAL


def _classify_git_stderr(stderr: str | None) -> Optional[str]:
    """Map git stderr to a stable machine-readable error code."""
    text = (stderr or "").lower()
    if not text:
        return None
    if "dubious ownership" in text or "safe.directory" in text:
        return "git-ownership"
    if (
        "permission denied" in text
        or "read-only file system" in text
        or "operation not permitted" in text
    ):
        return "git-permission"
    # Lock contention is not offline — callers fail closed without trusting
    # stale FETCH_HEAD, and must never unlink locks from the passive path.
    if "could not lock" in text or ("unable to create" in text and "lock" in text):
        return "check-failed"
    if (
        "could not resolve host" in text
        or "unable to access" in text
        or "network is unreachable" in text
        or "connection refused" in text
        or "timed out" in text
        or "temporary failure in name resolution" in text
    ):
        return "offline"
    return None


def _git_cmd_prefix(cwd: Path, *, relax_ownership: bool) -> list[str]:
    """Build a ``git`` argv prefix.

    When ``relax_ownership`` is True, add a *process-local*
    ``-c safe.directory=<abs>`` so read-only probes work on root-owned
    checkouts (e.g. ``/opt/hermes-agent``). Never writes global git config.
    """
    cmd = ["git"]
    if relax_ownership:
        cmd.extend(["-c", f"safe.directory={cwd.resolve()}"])
    return cmd


def _git_run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 5,
    relax_ownership: bool = False,
) -> Optional[subprocess.CompletedProcess]:
    """Run a bounded argv-only git subprocess. Never shell=True."""
    try:
        return subprocess.run(
            [*_git_cmd_prefix(cwd, relax_ownership=relax_ownership), *args],
            capture_output=True,
            text=True,
            # git output is UTF-8; on Windows text=True defaults to the ANSI
            # code page and bytes like 0x90 (3rd byte of 🐛 in a commit
            # subject) crash the stdlib reader thread (#52649).
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
        )
    except Exception:
        return None


def _git_stdout(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 5,
    relax_ownership: bool = False,
) -> Optional[str]:
    result = _git_run(
        args, cwd=cwd, timeout=timeout, relax_ownership=relax_ownership
    )
    if result is None or result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _resolve_git_dirs(repo_dir: Path) -> list[Path]:
    """Return unique absolute git-dir paths for ``repo_dir``.

    Linked worktrees / candidate installs share objects via the *common*
    git dir (``git rev-parse --git-common-dir``). Passiveability and shallow
    state live there, not only in the per-worktree git dir.
    """
    dirs: list[Path] = []
    for flag in ("--git-common-dir", "--git-dir"):
        raw = _git_stdout(
            ["rev-parse", flag], cwd=repo_dir, relax_ownership=True
        )
        if not raw:
            continue
        root = Path(raw)
        if not root.is_absolute():
            root = (repo_dir / root).resolve()
        else:
            root = root.resolve()
        if root not in dirs:
            dirs.append(root)
    return dirs


def _count_commits_behind(repo_dir: Path, target_ref: str) -> Optional[int]:
    """Return ``rev-list --count HEAD..<target_ref>`` when countable."""
    result = _git_run(
        ["rev-list", "--count", f"HEAD..{target_ref}"],
        cwd=repo_dir,
        relax_ownership=True,
    )
    if result is None or result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    if not text.isdigit():
        return None
    return int(text)


def _ls_remote_main_sha(
    remote: str = _UPSTREAM_REPO_URL,
    *,
    timeout: int = 10,
) -> Optional[str]:
    """Return the tip SHA of ``refs/heads/main`` at ``remote``, or None."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", remote, "refs/heads/main"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    upstream_rev = result.stdout.split()[0]
    return upstream_rev or None


def _check_via_rev(local_rev: str) -> Optional[int]:
    """Compare an embedded git revision to upstream main via ls-remote.

    Returns 0 if up-to-date, ``UPDATE_AVAILABLE_NO_COUNT`` if behind,
    or ``None`` on failure.
    """
    upstream_rev = _ls_remote_main_sha(_UPSTREAM_REPO_URL)
    if not upstream_rev:
        return None
    return 0 if upstream_rev == local_rev else UPDATE_AVAILABLE_NO_COUNT


def repo_install_writable(repo_dir: Path) -> bool:
    """True when this process can write the install root and its git dirs.

    Linked worktrees point ``.git`` at a file; writability of the *common*
    git dir is what matters for fetch/update, so both common-dir and
    git-dir are checked when resolvable.
    """
    try:
        if not os.access(repo_dir, os.W_OK):
            return False
        git_paths = _resolve_git_dirs(repo_dir)
        if not git_paths:
            git_entry = repo_dir / ".git"
            if git_entry.exists() and not os.access(git_entry, os.W_OK):
                return False
            return True
        for path in git_paths:
            if path.exists() and not os.access(path, os.W_OK):
                return False
        return True
    except OSError:
        return False


def _check_via_local_git(
    repo_dir: Path,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Count commits behind origin/main in a local checkout.

    Returns ``(behind, error_code, current_revision)``.

    Safety contract for passive update checks:
      * never unlink ``shallow.lock`` / ``index.lock`` / ``HEAD.lock`` /
        ``packed-refs.lock`` (lock recovery is explicit maintenance only);
      * never progressively ``--deepen`` on every cache expiry — shallow
        recovery uses an absolute ``--depth`` target once tips differ;
      * trust ``FETCH_HEAD`` only after a successful fetch in *this* call;
        failed fetch + stale FETCH_HEAD fails closed (ls-remote / error);
      * argv-only bounded subprocesses; linked-worktree common-dir aware
        writable probes.

    Uses process-local ``safe.directory`` for **read-only** git ops so
    root-owned installs still report availability. Fetch writes ``.git``
    without ownership relax; failures fall back to ``HEAD`` + ls-remote.
    """
    head = _git_run(
        ["rev-parse", "HEAD"], cwd=repo_dir, relax_ownership=True
    )
    head_rev = (head.stdout or "").strip() if head and head.returncode == 0 else None
    head_err = _classify_git_stderr(head.stderr if head else None)

    if not head_rev:
        return None, head_err or "check-failed", None

    origin = _git_run(
        ["remote", "get-url", "origin"], cwd=repo_dir, relax_ownership=True
    )
    origin_url = (
        (origin.stdout or "").strip()
        if origin and origin.returncode == 0
        else None
    )

    if _is_official_ssh_remote(origin_url):
        checked = _check_via_rev(head_rev)
        if checked is None:
            return None, "offline", head_rev
        if checked == UPDATE_AVAILABLE_NO_COUNT:
            return 1, None, head_rev
        return checked, None, head_rev

    shallow_state = _git_stdout(
        ["rev-parse", "--is-shallow-repository"],
        cwd=repo_dir,
        relax_ownership=True,
    )
    if shallow_state == "true":
        is_shallow: Optional[bool] = True
    elif shallow_state == "false":
        is_shallow = False
    else:
        # Unknown is not equivalent to a full clone.  A plain fetch can
        # silently unshallow/download unbounded history, so fail closed to the
        # read-only tip comparison below.
        is_shallow = None

    # Tip probe that never writes local git state. Equal tips short-circuit
    # before any depth recovery so repeated checks cannot progressively
    # deepen a shallow clone that is already current.
    remote_for_ls = origin_url or _UPSTREAM_REPO_URL
    upstream_tip = _ls_remote_main_sha(remote_for_ls)
    if upstream_tip is None and origin_url and origin_url != _UPSTREAM_REPO_URL:
        upstream_tip = _ls_remote_main_sha(_UPSTREAM_REPO_URL)
    if upstream_tip is not None and upstream_tip == head_rev:
        return 0, None, head_rev

    if is_shallow is None:
        if upstream_tip is not None:
            return UPDATE_AVAILABLE_NO_COUNT, None, head_rev
        checked = _check_via_rev(head_rev)
        if checked is not None:
            return checked, None, head_rev
        return None, "check-failed", head_rev

    fetch_ok = False
    fetch_error: Optional[str] = None
    try:
        # Scope the fetch to the one branch the behind-count compares against.
        # An unscoped ``git fetch origin`` transfers every remote head (~1,400
        # on this repo — measured 3.0 s vs 0.55 s scoped) and can burn the full
        # 10 s timeout on slow links. ``cmd_update`` already scopes its fetch
        # for the same reason.
        #
        # Shallow path: absolute ``--depth TARGET`` (idempotent), never
        # relative ``--deepen`` and never passive lock unlinks. Full clones
        # keep a plain scoped fetch for exact counts.
        fetch_args = ["fetch", "origin", "main"]
        if is_shallow:
            fetch_args += ["--depth", str(_SHALLOW_HISTORY_TARGET)]
        fetch_args.append("--quiet")
        # Fetch writes into ``.git`` — never pass process-local safe.directory.
        fetch_result = _git_run(
            fetch_args, cwd=repo_dir, timeout=30 if is_shallow else 10,
            relax_ownership=False,
        )
        if fetch_result is not None and fetch_result.returncode == 0:
            fetch_ok = True
        elif fetch_result is not None:
            fetch_error = (
                _classify_git_stderr(fetch_result.stderr) or "check-failed"
            )
    except Exception:
        fetch_error = "check-failed"

    if not fetch_ok:
        # Failed fetch: NEVER trust stale FETCH_HEAD. Fall back to the
        # ls-remote tip probe (already computed) or a fresh official compare.
        if upstream_tip is not None:
            if upstream_tip == head_rev:
                return 0, None, head_rev
            return UPDATE_AVAILABLE_NO_COUNT, None, head_rev
        checked = _check_via_rev(head_rev)
        if checked is not None:
            return checked, None, head_rev
        return None, fetch_error or "offline", head_rev

    # Capture FETCH_HEAD once, validate it against the read-only main-tip probe,
    # then use the immutable object ID for every graph operation.  FETCH_HEAD
    # is shared mutable state: another concurrent fetch can overwrite it after
    # our successful fetch and before these subprocesses run.
    target_rev = _git_stdout(
        ["rev-parse", "FETCH_HEAD"], cwd=repo_dir, relax_ownership=True
    )
    if not target_rev or upstream_tip is None or target_rev != upstream_tip:
        # Missing or contradictory evidence must never report "latest".  The
        # earlier equal-tip path already returned 0, so unknown count is the
        # conservative truthful result here.
        return UPDATE_AVAILABLE_NO_COUNT, None, head_rev
    if head_rev == target_rev:
        return 0, None, head_rev

    if is_shallow:
        merge_base = _git_stdout(
            ["merge-base", "HEAD", target_rev],
            cwd=repo_dir,
            relax_ownership=True,
        )
        if merge_base:
            counted = _count_commits_behind(repo_dir, target_rev)
            if counted is not None:
                return counted, None, head_rev
        return UPDATE_AVAILABLE_NO_COUNT, None, head_rev

    counted = _count_commits_behind(repo_dir, target_rev)
    if counted is not None:
        return counted, None, head_rev

    # Count failed (missing origin/main ref, etc.) — last-resort SHA compare.
    if upstream_tip is not None:
        behind = 0 if head_rev == upstream_tip else UPDATE_AVAILABLE_NO_COUNT
        return behind, None, head_rev
    checked = _check_via_rev(head_rev)
    if checked is not None:
        return checked, None, head_rev
    return None, "check-failed", head_rev


def check_for_updates_details() -> Dict[str, Optional[object]]:
    """Rich update check for API consumers.

    Returns a dict with:
      - behind: int | None (null only on true failure / N/A)
      - error_code: str | None (set when behind is null due to a failed check)
      - current_revision: str | None
      - message: str | None
      - repo_writable: bool | None (git installs only)
    """
    hermes_home = get_hermes_home()
    cache_file = hermes_home / ".update_check"
    embedded_rev = os.environ.get("HERMES_REVISION") or None

    try:
        from hermes_cli.config import detect_install_method, get_project_root
        if detect_install_method(get_project_root()) == "docker":
            return {
                "behind": None,
                "error_code": None,
                "current_revision": None,
                "message": None,
                "repo_writable": None,
            }
    except Exception:
        pass

    now = time.time()
    try:
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if (
                now - cached.get("ts", 0) < _UPDATE_CHECK_CACHE_SECONDS
                and cached.get("rev") == embedded_rev
                and cached.get("ver") == VERSION
            ):
                return {
                    "behind": cached.get("behind"),
                    "error_code": cached.get("error_code"),
                    "current_revision": cached.get("current_revision"),
                    "message": cached.get("message"),
                    "repo_writable": cached.get("repo_writable"),
                }
    except Exception:
        pass

    behind: Optional[int] = None
    error_code: Optional[str] = None
    current_revision: Optional[str] = embedded_rev
    message: Optional[str] = None
    repo_writable: Optional[bool] = None

    if embedded_rev:
        behind = _check_via_rev(embedded_rev)
        if behind is None:
            error_code = "offline"
            message = "Couldn't reach the update source — try again later."
    else:
        # Prefer the running code's location over the profile-scoped path.
        # $HERMES_HOME/hermes-agent/ may be a stale copy from --clone-all;
        # Path(__file__) always resolves to the actual installed checkout.
        repo_dir = Path(__file__).parent.parent.resolve()
        if not (repo_dir / ".git").exists():
            repo_dir = hermes_home / "hermes-agent"
        if not (repo_dir / ".git").exists():
            behind = None
            # No checkout — not a failed check; caller (docker/unknown) decides.
            error_code = None
        else:
            repo_writable = repo_install_writable(repo_dir)
            behind, error_code, current_revision = _check_via_local_git(repo_dir)
            if behind is None and error_code:
                if error_code == "git-ownership":
                    message = (
                        "Git refused this checkout due to ownership mismatch. "
                        "Updates can't be applied from this process; reinstall "
                        "or fix directory ownership, then retry."
                    )
                elif error_code == "git-permission":
                    message = (
                        "The Hermes install directory isn't writable by this "
                        "process, so the update check couldn't refresh refs. "
                        "Update from an account that owns the install, or "
                        "reinstall into a user-writable location."
                    )
                elif error_code == "offline":
                    message = "Couldn't reach the update source — try again later."
                else:
                    message = "Couldn't check for updates — try again later."

    try:
        cache_file.write_text(
            json.dumps(
                {
                    "ts": now,
                    "behind": behind,
                    "rev": embedded_rev,
                    "ver": VERSION,
                    "error_code": error_code,
                    "current_revision": current_revision,
                    "message": message,
                    "repo_writable": repo_writable,
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    return {
        "behind": behind,
        "error_code": error_code,
        "current_revision": current_revision,
        "message": message,
        "repo_writable": repo_writable,
    }


def check_for_updates() -> Optional[int]:
    """Check whether a Hermes update is available.

    Two paths: if ``HERMES_REVISION`` is set (nix builds embed it), compare
    it to upstream main via ``git ls-remote``. Otherwise look for a local
    git checkout and count commits behind ``origin/main``.

    Returns the number of commits behind, ``UPDATE_AVAILABLE_NO_COUNT`` (-1)
    if behind but the count is unknown, ``0`` if up-to-date, or ``None`` if
    the check failed or doesn't apply. Cached for 6 hours.
    """
    return check_for_updates_details().get("behind")  # type: ignore[return-value]


def _resolve_repo_dir() -> Optional[Path]:
    """Return the active Hermes git checkout, or None if this isn't a git install.

    Prefers the running code's location over the profile-scoped path
    because ``$HERMES_HOME/hermes-agent/`` may be a stale copy carried
    over by ``--clone-all``.
    """
    repo_dir = Path(__file__).parent.parent.resolve()
    if not (repo_dir / ".git").exists():
        hermes_home = get_hermes_home()
        repo_dir = hermes_home / "hermes-agent"
    return repo_dir if (repo_dir / ".git").exists() else None


def _git_short_hash(repo_dir: Path, rev: str) -> Optional[str]:
    """Resolve a git revision to an 8-character short hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", rev],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(repo_dir),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def get_git_banner_state(repo_dir: Optional[Path] = None) -> Optional[dict]:
    """Return upstream/local git hashes for the startup banner.

    For source installs and dev images this runs ``git rev-parse`` against
    the active checkout.  When no checkout is available — the canonical case
    is the published Docker image, which excludes ``.git`` from the build
    context — we fall back to the baked-in build SHA (see
    ``hermes_cli/build_info.py``) and return it as a frozen
    ``upstream == local`` state with ``ahead=0``.  A built image is by
    definition pinned to one commit, so "ahead" is always zero and the
    banner correctly shows ``· upstream <sha>`` with no carried-commits
    annotation.
    """
    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        # No git checkout — try the baked build SHA (Docker image path).
        try:
            from hermes_cli.build_info import get_build_sha
            baked = get_build_sha(short=8)
            if baked:
                return {"upstream": baked, "local": baked, "ahead": 0}
        except Exception:
            pass
        return None

    upstream = _git_short_hash(repo_dir, "origin/main")
    local = _git_short_hash(repo_dir, "HEAD")
    if not upstream or not local:
        # Live-git lookup failed (e.g. shallow clone without origin/main).
        # Fall back to the baked build SHA if available.
        try:
            from hermes_cli.build_info import get_build_sha
            baked = get_build_sha(short=8)
            if baked:
                return {"upstream": baked, "local": baked, "ahead": 0}
        except Exception:
            pass
        return None

    ahead = 0
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            ahead = int((result.stdout or "0").strip() or "0")
    except Exception:
        ahead = 0

    return {"upstream": upstream, "local": local, "ahead": max(ahead, 0)}


_RELEASE_URL_BASE = "https://github.com/NousResearch/hermes-agent/releases/tag"
_latest_release_cache: Optional[tuple] = None  # (tag, url) once resolved


def get_latest_release_tag(repo_dir: Optional[Path] = None) -> Optional[tuple]:
    """Return ``(tag, release_url)`` for the latest git tag, or None.

    Local-only — runs ``git describe --tags --abbrev=0`` against the
    Hermes checkout. Cached per-process. Release URL always points at the
    canonical NousResearch/hermes-agent repo (forks don't get a link).
    """
    global _latest_release_cache
    if _latest_release_cache is not None:
        return _latest_release_cache or None

    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        _latest_release_cache = ()  # falsy sentinel — skip future lookups
        return None

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            cwd=str(repo_dir),
        )
    except Exception:
        _latest_release_cache = ()
        return None

    if result.returncode != 0:
        _latest_release_cache = ()
        return None

    tag = (result.stdout or "").strip()
    if not tag:
        _latest_release_cache = ()
        return None

    url = f"{_RELEASE_URL_BASE}/{tag}"
    _latest_release_cache = (tag, url)
    return _latest_release_cache


def format_banner_version_label() -> str:
    """Return the version label shown in the startup banner title."""
    base = f"Hermes Agent v{VERSION} ({RELEASE_DATE})"
    state = get_git_banner_state()
    if not state:
        return base

    upstream = state["upstream"]
    local = state["local"]
    ahead = int(state.get("ahead") or 0)

    if ahead <= 0 or upstream == local:
        return f"{base} · upstream {upstream}"

    carried_word = "commit" if ahead == 1 else "commits"
    return f"{base} · upstream {upstream} · local {local} (+{ahead} carried {carried_word})"


# =========================================================================
# Non-blocking update check
# =========================================================================

_update_result: Optional[int] = None
_update_check_done = threading.Event()


def prefetch_update_check():
    """Kick off update check in a background daemon thread."""
    def _run():
        global _update_result
        _update_result = check_for_updates()
        _update_check_done.set()
    t = threading.Thread(target=_run, daemon=True)
    t.start()


def get_update_result(timeout: float = 0.5) -> Optional[int]:
    """Get result of prefetched check. Returns None if not ready."""
    _update_check_done.wait(timeout=timeout)
    return _update_result


# =========================================================================
# Welcome banner
# =========================================================================

def _format_context_length(tokens: int) -> str:
    """Format a token count for display (e.g. 128000 → '128K', 1048576 → '1M')."""
    if tokens >= 1_000_000:
        val = tokens / 1_000_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}M"
        return f"{val:.1f}M"
    elif tokens >= 1_000:
        val = tokens / 1_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}K"
        return f"{val:.1f}K"
    return str(tokens)


def _display_toolset_name(toolset_name: str) -> str:
    """Normalize internal/legacy toolset identifiers for banner display."""
    if not toolset_name:
        return "unknown"
    return (
        toolset_name[:-6]
        if toolset_name.endswith("_tools")
        else toolset_name
    )


def build_welcome_banner(console: "Console", model: str, cwd: str,
                         tools: List[dict] = None,
                         enabled_toolsets: List[str] = None,
                         session_id: str = None,
                         get_toolset_for_tool=None,
                         context_length: int = None,
                         provider: str = None):
    """Build and print a welcome banner with caduceus on left and info on right.

    Args:
        console: Rich Console instance.
        model: Current model name.
        cwd: Current working directory.
        tools: List of tool definitions.
        enabled_toolsets: List of enabled toolset names.
        session_id: Session identifier.
        get_toolset_for_tool: Callable to map tool name -> toolset name.
        context_length: Model's context window size in tokens.
        provider: Active provider id. When ``"moa"``, ``model`` is a MoA
            preset name and the banner renders the aggregator instead of a
            bare model slug.
    """
    from model_tools import check_tool_availability, TOOLSET_REQUIREMENTS
    from rich.panel import Panel
    from rich.table import Table
    if get_toolset_for_tool is None:
        from model_tools import get_toolset_for_tool

    tools = tools or []
    enabled_toolsets = enabled_toolsets or []

    _, unavailable_toolsets = check_tool_availability(quiet=True)
    # The availability check walks the GLOBAL toolset registry, so it includes
    # toolsets that aren't part of this agent's platform set at all (e.g.
    # `discord`, `feishu_doc` on a CLI session). Those must never surface in the
    # banner's "Available Tools" — they aren't exposed to the agent. Restrict to
    # toolsets actually enabled for this agent; a toolset that's enabled but
    # currently has unmet deps legitimately shows as disabled/lazy below.
    _enabled_ts = {str(t) for t in enabled_toolsets}
    if _enabled_ts:
        unavailable_toolsets = [
            item for item in unavailable_toolsets
            if str(item.get("id", item.get("name", ""))) in _enabled_ts
        ]
    disabled_tools = set()
    # Tools whose toolset has a check_fn are lazy-initialized (e.g. honcho,
    # homeassistant) — they show as unavailable at banner time because the
    # check hasn't run yet, but they aren't misconfigured.
    lazy_tools = set()
    for item in unavailable_toolsets:
        toolset_name = item.get("name", "")
        ts_req = TOOLSET_REQUIREMENTS.get(toolset_name, {})
        tools_in_ts = item.get("tools", [])
        if ts_req.get("check_fn"):
            lazy_tools.update(tools_in_ts)
        else:
            disabled_tools.update(tools_in_ts)

    layout_table = Table.grid(padding=(0, 2))
    layout_table.add_column("left", justify="center")
    layout_table.add_column("right", justify="left")

    # Resolve skin colors once for the entire banner
    accent = _skin_color("banner_accent", "#FFBF00")
    dim = _skin_color("banner_dim", "#B8860B")
    text = _skin_color("banner_text", "#FFF8DC")
    session_color = _skin_color("session_border", "#8B8682")

    # Use skin's custom caduceus art if provided
    try:
        from hermes_cli.skin_engine import get_active_skin
        _bskin = get_active_skin()
        _hero = _bskin.banner_hero if hasattr(_bskin, 'banner_hero') and _bskin.banner_hero else HERMES_CADUCEUS
    except Exception:
        _bskin = None
        _hero = HERMES_CADUCEUS
    left_lines = ["", _hero, ""]
    if (provider or "").strip().lower() == "moa":
        # MoA virtual provider: ``model`` is a preset name. Show the preset and
        # its aggregator so the banner is meaningful instead of a bare slug.
        preset_name = model
        agg_label = ""
        try:
            from hermes_cli.config import load_config
            from hermes_cli.moa_config import normalize_moa_config

            _moa = normalize_moa_config(load_config().get("moa") or {})
            _preset = _moa.get("presets", {}).get(preset_name)
            if _preset:
                _agg = _preset.get("aggregator") or {}
                _am = str(_agg.get("model") or "")
                agg_label = _am.split("/")[-1] if "/" in _am else _am
        except Exception:
            agg_label = ""
        if len(preset_name) > 28:
            preset_name = preset_name[:25] + "..."
        agg_str = f" [dim {dim}]·[/] [dim {dim}]agg {agg_label}[/]" if agg_label else ""
        ctx_str = f" [dim {dim}]·[/] [dim {dim}]{_format_context_length(context_length)} context[/]" if context_length else ""
        left_lines.append(f"[{accent}]MoA: {preset_name}[/]{agg_str}{ctx_str} [dim {dim}]·[/] [dim {dim}]Nous Research[/]")
    else:
        if not (model or "").strip() or (model or "").strip().lower() == "unknown":
            # Unconfigured install: say so in red instead of a blank/"unknown"
            # slug — this is the single clearest place to tell the user what
            # is wrong and how to fix it.
            left_lines.append(
                f"[bold red]no model configured[/] "
                f"[dim {dim}]— run /model or hermes setup[/]"
            )
        else:
            model_short = model.split("/")[-1] if "/" in model else model
            if model_short.endswith(".gguf"):
                model_short = model_short[:-5]
            if len(model_short) > 28:
                model_short = model_short[:25] + "..."
            ctx_str = f" [dim {dim}]·[/] [dim {dim}]{_format_context_length(context_length)} context[/]" if context_length else ""
            left_lines.append(f"[{accent}]{model_short}[/]{ctx_str} [dim {dim}]·[/] [dim {dim}]Nous Research[/]")

    if os.getenv("HERMES_YOLO_MODE"):
        left_lines.append(f"[bold red]⚠ YOLO mode[/] [dim {dim}]— all approval prompts bypassed[/]")
    left_lines.append(f"[dim {dim}]{cwd}[/]")
    if session_id:
        left_lines.append(f"[dim {session_color}]Session: {session_id}[/]")
    left_content = "\n".join(left_lines)

    right_lines = [f"[bold {accent}]Available Tools[/]"]
    toolsets_dict: Dict[str, list] = {}

    for tool in tools:
        tool_name = tool["function"]["name"]
        toolset = _display_toolset_name(get_toolset_for_tool(tool_name) or "other")
        toolsets_dict.setdefault(toolset, []).append(tool_name)

    for item in unavailable_toolsets:
        toolset_id = item.get("id", item.get("name", "unknown"))
        display_name = _display_toolset_name(toolset_id)
        if display_name not in toolsets_dict:
            toolsets_dict[display_name] = []
        for tool_name in item.get("tools", []):
            if tool_name not in toolsets_dict[display_name]:
                toolsets_dict[display_name].append(tool_name)

    sorted_toolsets = sorted(toolsets_dict.keys())
    display_toolsets = sorted_toolsets[:8]
    remaining_toolsets = len(sorted_toolsets) - 8

    for toolset in display_toolsets:
        tool_names = toolsets_dict[toolset]
        colored_names = []
        for name in sorted(tool_names):
            if name in disabled_tools:
                colored_names.append(f"[red]{name}[/]")
            elif name in lazy_tools:
                colored_names.append(f"[yellow]{name}[/]")
            else:
                colored_names.append(f"[{text}]{name}[/]")

        tools_str = ", ".join(colored_names)
        if len(", ".join(sorted(tool_names))) > 45:
            short_names = []
            length = 0
            for name in sorted(tool_names):
                if length + len(name) + 2 > 42:
                    short_names.append("...")
                    break
                short_names.append(name)
                length += len(name) + 2
            colored_names = []
            for name in short_names:
                if name == "...":
                    colored_names.append("[dim]...[/]")
                elif name in disabled_tools:
                    colored_names.append(f"[red]{name}[/]")
                elif name in lazy_tools:
                    colored_names.append(f"[yellow]{name}[/]")
                else:
                    colored_names.append(f"[{text}]{name}[/]")
            tools_str = ", ".join(colored_names)

        right_lines.append(f"[dim {dim}]{toolset}:[/] {tools_str}")

    if remaining_toolsets > 0:
        right_lines.append(f"[dim {dim}](and {remaining_toolsets} more toolsets...)[/]")

    # MCP Servers section (only if configured)
    try:
        from tools.mcp_tool import get_mcp_status
        mcp_status = get_mcp_status()
    except Exception:
        mcp_status = []

    if mcp_status:
        right_lines.append("")
        right_lines.append(f"[bold {accent}]MCP Servers[/]")
        for srv in mcp_status:
            status = srv.get("status")
            if srv["connected"]:
                right_lines.append(
                    f"[dim {dim}]{srv['name']}[/] [{text}]({srv['transport']})[/] "
                    f"[dim {dim}]—[/] [{text}]{srv['tools']} tool(s)[/]"
                )
            elif srv.get("disabled") or status == "disabled":
                right_lines.append(
                    f"[dim {dim}]{srv['name']}[/] [dim]({srv['transport']})[/] "
                    f"[dim {dim}]— disabled[/]"
                )
            elif status == "connecting":
                right_lines.append(
                    f"[dim {dim}]{srv['name']}[/] [dim]({srv['transport']})[/] "
                    f"[yellow]— connecting[/]"
                )
            elif status == "configured":
                right_lines.append(
                    f"[dim {dim}]{srv['name']}[/] [dim]({srv['transport']})[/] "
                    f"[dim {dim}]— configured[/]"
                )
            else:
                right_lines.append(
                    f"[red]{srv['name']}[/] [dim]({srv['transport']})[/] "
                    f"[red]— failed[/]"
                )

    right_lines.append("")
    right_lines.append(f"[bold {accent}]Available Skills[/]")
    # The skills catalog is only reachable when the `skills` toolset is enabled
    # (it exposes skill_view / skill_manage). When it's disabled — e.g. a Blank
    # Slate install — the agent literally cannot load any skill, so advertising
    # the on-disk catalog here is misleading. Reflect the real state instead.
    _skills_enabled = (not _enabled_ts) or ("skills" in _enabled_ts)
    if _skills_enabled:
        skills_by_category = get_available_skills()
        total_skills = sum(len(s) for s in skills_by_category.values())
    else:
        skills_by_category = {}
        total_skills = 0

    # Dynamically size skills display based on terminal width.
    # Rich grid with 2 columns; right column gets roughly 60% of terminal.
    _term_cols = shutil.get_terminal_size().columns
    _right_col_width = max(int(_term_cols * 0.6) - 10, 30)

    if not _skills_enabled:
        right_lines.append(f"[dim {dim}]Skills toolset disabled[/]")
    elif skills_by_category:
        for category in sorted(skills_by_category.keys()):
            skill_names = sorted(skills_by_category[category])
            # Account for "category: " prefix
            _prefix_len = len(category) + 2
            _avail = max(_right_col_width - _prefix_len, 20)
            # Accumulate skills until we run out of space
            parts, length = [], 0
            for i, name in enumerate(skill_names):
                _sep = ", " if parts else ""
                _needed = len(_sep) + len(name)
                # Estimate indicator size IF we were to add this skill then stop
                _after = len(skill_names) - (i + 1)  # remaining after adding this
                _ind_len = len(f", +{_after} more") if _after > 0 else 0
                if parts and length + _needed + _ind_len > _avail:
                    remaining = len(skill_names) - len(parts)
                    parts.append(f"+{remaining} more")
                    break
                parts.append(name)
                length += _needed
            skills_str = ", ".join(parts)
            right_lines.append(f"[dim {dim}]{category}:[/] [{text}]{skills_str}[/]")
    else:
        right_lines.append(f"[dim {dim}]No skills installed[/]")

    right_lines.append("")
    mcp_connected = sum(1 for s in mcp_status if s["connected"]) if mcp_status else 0
    summary_parts = [f"{len(tools)} tools", f"{total_skills} skills"]
    if mcp_connected:
        summary_parts.append(f"{mcp_connected} MCP servers")
    summary_parts.append("/help for commands")
    # Indicate when the codex_app_server runtime is active so users
    # understand why tool counts may not match what's actually reachable
    # (codex builds its own tool list inside the spawned subprocess).
    try:
        from hermes_cli.codex_runtime_switch import get_current_runtime
        from hermes_cli.config import load_config as _load_cfg
        if get_current_runtime(_load_cfg()) == "codex_app_server":
            right_lines.append(
                f"[bold {accent}]Runtime:[/] [{text}]codex app-server[/] "
                f"[dim {dim}](terminal/file ops/MCP run inside codex)[/]"
            )
    except Exception:
        pass
    # Show active profile name when not 'default'
    try:
        from hermes_cli.profiles import get_active_profile_name
        _profile_name = get_active_profile_name()
        if _profile_name and _profile_name != "default":
            right_lines.append(f"[bold {accent}]Profile:[/] [{text}]{_profile_name}[/]")
    except Exception:
        pass  # Never break the banner over a profiles.py bug

    right_lines.append(f"[dim {dim}]{' · '.join(summary_parts)}[/]")

    # Update check — use prefetched result if available
    try:
        behind = get_update_result(timeout=0.5)
        if behind is not None and behind != 0:
            from hermes_cli.config import get_managed_update_command, recommended_update_command
            if behind > 0:
                commits_word = "commit" if behind == 1 else "commits"
                right_lines.append(
                    f"[bold yellow]⚠ {behind} {commits_word} behind[/]"
                    f"[dim yellow] — run [bold]{recommended_update_command()}[/bold] to update[/]"
                )
            else:
                # UPDATE_AVAILABLE_NO_COUNT: nix-built hermes; we know an update
                # exists but not by how much, and we don't know how the user
                # installed it (nix run, profile, system flake, home-manager).
                managed_cmd = get_managed_update_command()
                line = "[bold yellow]⚠ update available[/]"
                if managed_cmd:
                    line += f"[dim yellow] — run [bold]{managed_cmd}[/bold][/]"
                right_lines.append(line)
    except Exception:
        pass  # Never break the banner over an update check

    right_content = "\n".join(right_lines)
    layout_table.add_row(left_content, right_content)

    title_color = _skin_color("banner_title", "#FFD700")
    border_color = _skin_color("banner_border", "#CD7F32")
    version_label = format_banner_version_label()
    release_info = get_latest_release_tag()
    if release_info:
        _tag, _url = release_info
        title_markup = f"[bold {title_color}][link={_url}]{version_label}[/link][/]"
    else:
        title_markup = f"[bold {title_color}]{version_label}[/]"
    outer_panel = Panel(
        layout_table,
        title=title_markup,
        border_style=border_color,
        padding=(0, 2),
    )

    console.print()
    term_width = shutil.get_terminal_size().columns
    if term_width >= 95:
        _logo = _bskin.banner_logo if _bskin and hasattr(_bskin, 'banner_logo') and _bskin.banner_logo else HERMES_AGENT_LOGO
        console.print(_logo)
        console.print()
    console.print(outer_panel)
