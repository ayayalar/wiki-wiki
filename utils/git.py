"""Git helpers wrapping `subprocess`.

All commands run with `check=True` by default and surface stderr in the
raised CalledProcessError so failures are visible to the agent. Every
git invocation is logged (start + completion + duration) to
`$XDG_CACHE_HOME/wiki-mcp/server.log` for live diagnosis — tail it from
another terminal to see what's running and where time is being spent.
"""

from __future__ import annotations

import configparser
import base64
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from config import repo_root


# ---------------------------------------------------------------------------
# Windows Defender warm-up
# ---------------------------------------------------------------------------
# On Windows, the first `git.exe` invocation in a session can hang for
# minutes while Defender scans the binary. We fire a no-op `git --version`
# in a background thread at import time so the scan completes before any
# tool actually needs git. Subsequent calls hit Defender's cache and are
# instant.
_warmup_done = threading.Event()


def _warmup_git() -> None:
    """Background thread: run `git --version` to prime Defender's cache."""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.run(
            ["git", "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    _warmup_done.set()


if sys.platform == "win32":
    _warmup_thread = threading.Thread(target=_warmup_git, daemon=True)
    _warmup_thread.start()
else:
    _warmup_done.set()


# ---------------------------------------------------------------------------
# CVE-2025-48384: git config CRLF parsing flaw
# ---------------------------------------------------------------------------
# CVE-2025-48384 is a config-parsing flaw exploitable via submodules.
# Mitigation: upgrade git to a patched version. `safe.directory` does NOT
# mitigate this CVE — it only suppresses the "owned by another user"
# warning. Patched versions per release branch:
#   v2.43.7, v2.44.4, v2.45.4, v2.46.4, v2.47.3, v2.48.2, v2.49.1, v2.50.1+
# Source: https://github.com/git/git/security/advisories/GHSA-992w-73f5-x28c
_CVE_2025_48384_PATCHED: dict[int, int] = {
    43: 7,
    44: 4,
    45: 4,
    46: 4,
    47: 3,
    48: 2,
    49: 1,
    50: 1,
}


def _parse_git_version(version_str: str) -> tuple[int, int, int] | None:
    """Parse git version string like 'git version 2.43.0.windows.1' → (2, 43, 0)."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version_str)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _git_is_patched_for_cve_2025_48384() -> bool:
    """Check if the installed git version is patched for CVE-2025-48384."""
    result = run_git(["version"], check=False)
    ver = _parse_git_version(result.stdout)
    if ver is None:
        return True  # Can't parse → assume OK (better than blocking)
    major, minor, patch = ver
    if major != 2:
        return True  # Unexpected major version → assume OK
    required_patch = _CVE_2025_48384_PATCHED.get(minor)
    if required_patch is None:
        return True  # Unknown minor version → assume OK
    return patch >= required_patch


def check_git_version() -> dict | None:
    """Check git version against CVE-2025-48384 patched versions.

    Returns None if the git version is safe, or a warning dict if the
    installed version is known to be vulnerable. The warning is advisory
    — the server continues to operate, but operators should upgrade git.
    """
    if _git_is_patched_for_cve_2025_48384():
        return None
    result = run_git(["version"], check=False)
    return {
        "status": "cve_warning",
        "cve": "CVE-2025-48384",
        "installed_version": result.stdout.strip(),
        "message": (
            "Your git version is vulnerable to CVE-2025-48384 "
            "(config CRLF parsing flaw exploitable via submodules). "
            "Upgrade to a patched version. "
            "See: https://github.com/git/git/security/advisories/GHSA-992w-73f5-x28c"
        ),
    }


def _resolve_git_dir(root: Path) -> Path | None:
    """Return the path to the git directory for `root`.

    Normally that's `<root>/.git`. For worktrees and submodules, `.git`
    is a *file* containing `gitdir: <path>`. Returns None if neither
    form is found.
    """
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            line = dot_git.read_text().strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            target = line[len("gitdir:") :].strip()
            p = Path(target)
            if not p.is_absolute():
                p = (root / p).resolve()
            return p if p.exists() else None
    return None


def _read_head_branch(root: Path) -> str | None:
    """Parse `.git/HEAD` directly. Returns the branch name, or None on
    detached HEAD / missing file / unexpected format (caller falls back
    to subprocess)."""
    git_dir = _resolve_git_dir(root)
    if git_dir is None:
        return None
    head = git_dir / "HEAD"
    try:
        content = head.read_text().strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if content.startswith(prefix):
        return content[len(prefix) :].strip() or None
    return None  # detached HEAD or anything else → caller decides


def _read_origin_url(root: Path) -> str | None:
    """Parse `[remote "origin"]` url from `.git/config`. Returns None
    if the section / key is missing or parsing fails."""
    git_dir = _resolve_git_dir(root)
    if git_dir is None:
        return None
    config_file = git_dir / "config"
    if not config_file.is_file():
        return None
    parser = configparser.RawConfigParser()
    try:
        parser.read(config_file)
    except (configparser.Error, OSError):
        return None
    section = 'remote "origin"'
    if section not in parser:
        return None
    url = parser[section].get("url", "").strip()
    return url or None


def _read_submodule_url(root: Path, name: str) -> str | None:
    """Parse `[submodule "<name>"]` url from `.gitmodules`. Returns None
    if `.gitmodules` is absent, the section is missing, or parsing fails."""
    gitmodules = root / ".gitmodules"
    if not gitmodules.is_file():
        return None
    parser = configparser.RawConfigParser()
    try:
        parser.read(gitmodules)
    except (configparser.Error, OSError):
        return None
    section = f'submodule "{name}"'
    if section not in parser:
        return None
    url = parser[section].get("url", "").strip()
    return url or None


def init_submodule_config(root: Path, name: str) -> bool:
    """Inline equivalent of `git submodule init <name>` — copy
    `submodule.<name>.url` from `.gitmodules` into `.git/config`.

    On Git for Windows, `git submodule init` runs through a bash wrapper
    that can hang for many minutes on AV-heavy machines. This pure-Python
    path takes microseconds and never spawns a subprocess.

    Returns True on success, False if either file is missing/unreadable
    so the caller can fall back to `git submodule init`.
    """
    url = _read_submodule_url(root, name)
    if url is None:
        return False
    git_dir = _resolve_git_dir(root)
    if git_dir is None:
        return False
    config_file = git_dir / "config"

    parser = configparser.RawConfigParser()
    try:
        if config_file.is_file():
            parser.read(config_file)
    except (configparser.Error, OSError):
        return False

    section = f'submodule "{name}"'
    if section not in parser:
        parser[section] = {}
    parser[section]["url"] = url
    parser[section]["active"] = "true"

    try:
        with open(config_file, "w") as f:
            parser.write(f, space_around_delimiters=True)
    except OSError:
        return False
    return True


def set_sparse_checkout_cone(repo: Path, patterns: list[str]) -> bool:
    """Inline equivalent of `git sparse-checkout set <patterns>` in cone mode.

    Writes the sparse-checkout file and enables cone-mode config entries
    directly, avoiding the `git sparse-checkout` subprocess that hangs on
    Windows under Defender AV scanning.

    Returns True on success, False if file IO fails (caller should fall
    back to the subprocess).
    """
    git_dir = _resolve_git_dir(repo)
    if git_dir is None:
        return False

    # Enable sparse checkout in config
    config_file = git_dir / "config"
    parser = configparser.RawConfigParser()
    try:
        if config_file.is_file():
            parser.read(config_file)
    except (configparser.Error, OSError):
        return False

    if "core" not in parser:
        parser["core"] = {}
    parser["core"]["sparseCheckout"] = "true"
    parser["core"]["sparseCheckoutCone"] = "true"

    try:
        with open(config_file, "w") as f:
            parser.write(f, space_around_delimiters=True)
    except OSError:
        return False

    # Write the sparse-checkout file (cone mode format)
    info_dir = git_dir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    sparse_file = info_dir / "sparse-checkout"
    # Cone mode format: root files included ("/*", "!/*/"), then for each
    # pattern we include every ancestor directory (excluding its siblings)
    # and finally the leaf directory itself.
    # E.g. pattern "a/b/c" produces:
    #   /a/  !/a/*/  /a/b/  !/a/b/*/  /a/b/c/
    lines = ["/*\n", "!/*/\n"]
    for p in patterns:
        parts = p.strip("/").split("/")
        for depth in range(len(parts)):
            prefix = "/".join(parts[: depth + 1])
            lines.append(f"/{prefix}/\n")
            if depth < len(parts) - 1:
                lines.append(f"!/{prefix}/*/\n")
    try:
        sparse_file.write_text("".join(lines))
    except OSError:
        return False

    return True


def reapply_sparse_checkout(wiki: Path | str) -> subprocess.CompletedProcess[str]:
    """Reconcile the working tree to the current sparse-checkout patterns.

    Runs ``git sparse-checkout reapply``, which materializes newly-included
    paths and removes now-excluded tracked content, while PRESERVING any
    uncommitted (modified or untracked) files — git leaves those in place and
    warns rather than destroying them. Used on a branch switch so the wiki
    working tree follows the parent repo's branch without risking unsaved work.

    Runs only on an actual branch switch (low frequency), so the extra
    ``git sparse-checkout`` subprocess is acceptable on Windows; it is still
    subject to run_git's timeout and CREATE_NO_WINDOW handling.
    """
    return run_git(["sparse-checkout", "reapply"], cwd=wiki, check=False)


_logger: logging.Logger | None = None


def _log_file() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "wiki-mcp" / "server.log"


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None and _logger.handlers:
        return _logger
    logger = logging.getLogger("wiki-mcp")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't leak to root logger / stdout
    path = _log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path)
    except OSError:
        # If we can't write the log file (read-only fs, etc.) fall back to
        # a no-op handler so logging never blocks the tool.
        handler = logging.NullHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    # Replace any prior handlers (e.g. from a previous test).
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.addHandler(handler)
    _logger = logger
    return logger


def _reset_logger_for_tests() -> None:
    """Pytest fixtures call this when XDG_CACHE_HOME changes between tests."""
    global _logger
    if _logger is not None:
        for h in list(_logger.handlers):
            _logger.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
    _logger = None


def _no_verify_flag() -> list[str]:
    """`['--no-verify']` by default; `[]` only when WIKI_MCP_VERIFY=1.

    Pre-commit hooks are designed to validate user code, not the
    mechanical submodule/metadata commits this server makes. Skipping
    them by default prevents slow lint/typecheck/test hooks from
    blocking auto-commits. Users who want hook enforcement can set
    WIKI_MCP_VERIFY=1.
    """
    return (
        []
        if os.environ.get("WIKI_MCP_VERIFY", "").lower() in ("1", "true", "yes")
        else ["--no-verify"]
    )


_DEFAULT_GIT_TIMEOUT_SECONDS = 60  # 1 minute — sufficient for small repos; prevents zombie accumulation.


def _git_timeout() -> int | None:
    """Resolve the per-call timeout. Set `WIKI_MCP_GIT_TIMEOUT=0` to
    disable entirely (legacy behavior); any positive int overrides the
    default."""
    raw = os.environ.get("WIKI_MCP_GIT_TIMEOUT", "").strip()
    if not raw:
        return _DEFAULT_GIT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_GIT_TIMEOUT_SECONDS
    if value <= 0:
        return None
    return value


# Per-directory locks to prevent concurrent git operations on the same repo.
# Git uses file-level locking (index.lock) that causes processes to block
# each other. When the MCP framework dispatches tool calls concurrently,
# two git commands targeting the same repo can deadlock.
_dir_locks: dict[str, threading.Lock] = {}
_dir_locks_meta = threading.Lock()


def _get_dir_lock(cwd: str) -> threading.Lock:
    """Get or create a lock for the given working directory."""
    with _dir_locks_meta:
        if cwd not in _dir_locks:
            _dir_locks[cwd] = threading.Lock()
    return _dir_locks[cwd]


def _extract_https_url_from_args(args: list[str]) -> str | None:
    """Return the first HTTPS URL present in git args, if any."""
    for token in args:
        if isinstance(token, str) and token.startswith("https://"):
            return token
    return None


def _pat_auth_env_for_git(args: list[str], cwd: Path | str | None) -> dict[str, str]:
    """Build process-local git config env vars for PAT auth.

    Uses ``WIKI_MCP_REMOTE_PAT`` and optional ``WIKI_MCP_REMOTE_USERNAME``.
    Applies only for HTTPS remotes. URL discovery order:
      1) First explicit HTTPS URL in ``args`` (e.g. ls-remote/submodule add)
      2) ``origin`` URL from repo git config in ``cwd``

    Returns an empty dict when PAT auth should not be applied.
    """
    pat = os.environ.get("WIKI_MCP_REMOTE_PAT", "").strip()
    if not pat:
        return {}

    explicit_url = _extract_https_url_from_args(args)
    repo_url = None
    if explicit_url:
        repo_url = explicit_url
    else:
        cwd_path = Path(cwd) if cwd is not None else Path.cwd()
        repo_url = _read_origin_url(cwd_path)

    if not repo_url:
        return {}

    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return {}

    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"

    username = os.environ.get("WIKI_MCP_REMOTE_USERNAME", "").strip() or "x-access-token"
    basic = base64.b64encode(f"{username}:{pat}".encode("utf-8")).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def run_git(
    args: list[str],
    cwd: Path | str | None = None,
    check: bool = True,
    timeout: int | None = ...,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing stdout/stderr as text.

    Each call is logged with the command, cwd, return code, and elapsed
    time so a `tail -f` of the log file reveals where time is going.
    A timeout (default 60s, overridable via WIKI_MCP_GIT_TIMEOUT; set 0 to
    disable) kills hung commands so a slow environment fails fast instead of
    freezing the agent. Pass an explicit `timeout` to override per-call.

    Serializes git commands per working directory to prevent index.lock
    contention when the MCP framework dispatches tool calls concurrently.
    """
    # Wait for the Defender warm-up so the real command doesn't compete
    # with the warm-up for the scan lock.
    _warmup_done.wait(timeout=120)
    log = _get_logger()
    cmd_str = " ".join(args)
    cwd_str = str(Path(cwd).resolve()) if cwd is not None else str(Path.cwd().resolve())
    effective_timeout = _git_timeout() if timeout is ... else timeout
    log.info(f"START git {cmd_str} (cwd={cwd_str}, timeout={effective_timeout}s)")

    dir_lock = _get_dir_lock(cwd_str)
    # Wait for the lock with a timeout to avoid indefinite blocking
    if not dir_lock.acquire(timeout=effective_timeout or 60):
        log.info(f"  BLOCKED git {cmd_str} — another git command is running in {cwd_str}")
        raise subprocess.TimeoutExpired(
            cmd=["git", *args], timeout=effective_timeout
        )

    t0 = time.time()

    try:
        # Use Popen so we can forcibly kill on timeout (subprocess.run's
        # timeout raises TimeoutExpired but may leave zombie processes on
        # Windows that hold index.lock and block all subsequent git calls).
        # CREATE_NO_WINDOW prevents hangs when the parent process (MCP server)
        # has no console — without this flag, git.exe may block waiting for
        # console allocation on Windows.
        # CREATE_NEW_PROCESS_GROUP allows killing the entire process tree
        # without elevation (via os.kill with CTRL_BREAK_EVENT, then
        # TerminateProcess on the group leader kills children too when
        # combined with a Job Object — but even without a Job, killing
        # the parent with stdout/stderr pipes closed causes children to
        # exit on broken pipe).
        creationflags = (
            (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            if sys.platform == "win32" else 0
        )
        # Build a clean environment:
        # - GIT_TERMINAL_PROMPT=0: credential prompts fail fast instead of blocking
        # - GIT_PAGER=cat: prevents pager from blocking on pipe
        # - GCM_INTERACTIVE=never: Git Credential Manager won't show UI prompts
        # - GIT_CONFIG_NOSYSTEM=1: skip system gitconfig (which may configure
        #   credential.helper=manager that blocks in headless environments)
        # - Remove GIT_ASKPASS / VSCODE_GIT_ASKPASS_* / SSH_ASKPASS: VS Code sets
        #   these to route credential/ssh requests through its internal UI which
        #   blocks indefinitely when the server runs as a headless subprocess.
        # - Remove GIT_CONFIG_*: VS Code injects safe.bareRepository=explicit which
        #   can interfere with operations on bare wiki repos.
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith("VSCODE_GIT_")
            and k not in ("GIT_ASKPASS", "SSH_ASKPASS")
            and not k.startswith("GIT_CONFIG_")
        }
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_PAGER"] = "cat"
        env["GCM_INTERACTIVE"] = "never"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env.update(_pat_auth_env_for_git(args, cwd))
        if env_extra:
            env.update(env_extra)
        # On Windows, using subprocess.PIPE for stdout/stderr causes
        # communicate() to block forever when git spawns child processes
        # (e.g. git-receive-pack for local pushes) that inherit the pipe
        # write handles. The child keeps those handles open even after
        # git.exe exits, so communicate() never gets EOF.
        #
        # Fix: redirect to temp files instead. proc.wait() only waits for
        # git.exe itself (not grandchildren), so it respects the timeout.
        # Grandchildren that inherit file handles are harmless — file writes
        # don't block, and we read the files after the process exits.
        if sys.platform == "win32":
            tmpdir = tempfile.mkdtemp(prefix="wiki_git_")
            out_path = os.path.join(tmpdir, "out")
            err_path = os.path.join(tmpdir, "err")
            out_f = open(out_path, "w", encoding="utf-8", errors="replace")
            err_f = open(err_path, "w", encoding="utf-8", errors="replace")
        else:
            tmpdir = None
            out_f = subprocess.PIPE
            err_f = subprocess.PIPE

        # Only skip optional locks for read-only commands.
        # Write commands (commit, merge, push, etc.) need index.lock
        # for data integrity. Read commands benefit from skipping it
        # to reduce lock contention during concurrent tool calls.
        _read_only_cmds = frozenset([
            "status", "log", "show", "rev-parse", "cat-file",
            "ls-files", "ls-tree", "diff", "diff-files", "diff-index",
            "branch", "tag", "describe", "merge-base", "for-each-ref",
            "remote", "config", "version", "symbolic-ref", "var",
            "rev-list", "verify-pack", "hash-object",
            "get-tar-commit-id", "check-attr", "check-ignore",
            "check-mailmap", "count-objects", "fsck", "notes",
            "range-diff", "shortlog", "ls-remote",
        ])
        _cmd = args[0] if args else ""
        _git_args = (
            ["git", "--no-optional-locks", *args]
            if _cmd in _read_only_cmds
            else ["git", *args]
        )
        try:
            proc = subprocess.Popen(
                _git_args,
                cwd=str(cwd) if cwd is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                text=(sys.platform != "win32"),  # text mode only for PIPE
                creationflags=creationflags,
                # On POSIX, put git in its own session/process group so the
                # timeout path can SIGKILL *its* group. Without this the child
                # shares the server's process group and killpg would take down
                # the MCP server itself.
                start_new_session=(sys.platform != "win32"),
                env=env,
            )
        except Exception:
            if sys.platform == "win32":
                out_f.close()
                err_f.close()
                shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        finally:
            if sys.platform == "win32" and not out_f.closed:
                # Close our handles — git has its own inherited copies.
                # Must close before wait() so our handles don't keep files locked.
                out_f.close()
                err_f.close()

        def _kill_and_drain() -> None:
            # H6 fix: kill the entire process group, not just git.exe.
            # Child processes (git-receive-pack, git-upload-pack, etc.)
            # inherit the process group and can hold index.lock even
            # after git.exe exits. We must kill them too.
            if sys.platform == "win32":
                # CREATE_NEW_PROCESS_GROUP makes the child a group leader whose
                # group id == its pid; CTRL_BREAK to that (positive) pid signals
                # the whole group. The POSIX negative-pid convention does not
                # exist on Windows. Delivery is not guaranteed (no console under
                # CREATE_NO_WINDOW), so always follow up with TerminateProcess
                # (proc.kill) to guarantee git.exe dies and releases index.lock.
                try:
                    import signal as _sig
                    os.kill(proc.pid, _sig.CTRL_BREAK_EVENT)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
                if proc.poll() is None:
                    proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            else:
                # On Unix, process groups are standard.
                try:
                    import signal as _sig
                    os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
                except (OSError, ProcessLookupError):
                    proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:  # noqa: BLE001
                    pass

        try:
            if sys.platform == "win32":
                proc.wait(timeout=effective_timeout)
            else:
                stdout_pipe, stderr_pipe = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            _kill_and_drain()
            elapsed = time.time() - t0
            log.info(
                f"  TIMEOUT git {cmd_str} after {elapsed:.2f}s "
                f"(timeout={effective_timeout}s) — killed process."
            )
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            raise

        if sys.platform == "win32":
            with open(out_path, encoding="utf-8", errors="replace") as f:
                stdout = f.read()
            with open(err_path, encoding="utf-8", errors="replace") as f:
                stderr = f.read()
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            stdout, stderr = stdout_pipe, stderr_pipe

        elapsed = time.time() - t0
        result = subprocess.CompletedProcess(
            args=_git_args,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )

        if check and proc.returncode != 0:
            log.info(
                f"  FAIL git {cmd_str} in {elapsed:.2f}s rc={proc.returncode} "
                f"stderr={stderr.strip()!r}"
            )
            raise subprocess.CalledProcessError(
                proc.returncode,
                result.args,
                output=stdout,
                stderr=stderr,
            )

        log.info(f"  done git {cmd_str} in {elapsed:.2f}s rc={proc.returncode}")
        return result
    finally:
        dir_lock.release()


_repo_name_cache: dict[tuple[str, str], str] = {}


def _parse_repo_name_from_url(url: str) -> str | None:
    """Extract the repo name from a remote URL (last `/`-segment, stripping `.git`)."""
    url = url.strip()
    if not url:
        return None
    last = url.rsplit("/", 1)[-1]
    if last.endswith(".git"):
        last = last[:-4]
    return last or None


def get_repo_name() -> str:
    """Resolve the code repo's canonical name (cached per process).

    Priority:
      1. WIKI_MCP_REPO_NAME env var override.
      2. `.git/config` `[remote "origin"]` url → parse last path segment
         (fast path: pure file IO, no subprocess).
      3. `git remote get-url origin` (subprocess fallback for edge cases
         like worktrees with unusual config layouts).
      4. Basename of the repo root directory (local-testing fallback).

    The repo name never changes during a server session, so we memoize
    on (repo_root, env-override) — first call pays at most one config
    file read; every subsequent call is O(1).
    """
    override = os.environ.get("WIKI_MCP_REPO_NAME", "")
    root = repo_root()
    key = (str(root), override)
    cached = _repo_name_cache.get(key)
    if cached is not None:
        return cached

    if override:
        _repo_name_cache[key] = override
        return override

    # Fast path: parse .git/config directly.
    url = _read_origin_url(root)
    if url:
        name = _parse_repo_name_from_url(url)
        if name:
            _repo_name_cache[key] = name
            return name

    # Subprocess fallback for unusual git states.
    result = run_git(["remote", "get-url", "origin"], cwd=root, check=False)
    if result.returncode == 0:
        name = _parse_repo_name_from_url(result.stdout)
        if name:
            _repo_name_cache[key] = name
            return name

    fallback = root.name
    _repo_name_cache[key] = fallback
    return fallback


def _clear_cache() -> None:
    """Test helper: invalidate the repo-name memo."""
    _repo_name_cache.clear()


def get_current_branch() -> str:
    """Return the current code repo branch, or 'HEAD' if detached.

    Fast path: read `.git/HEAD` directly. This is critical on Windows
    machines where Defender real-time protection can make the first
    `git` subprocess of a session take many minutes. File IO doesn't
    trigger the same scan, so we avoid that tax for trivial lookups.
    Falls back to `git branch --show-current` if the file IO path
    can't resolve a clean answer.
    """
    root = repo_root()
    branch = _read_head_branch(root)
    if branch:
        return branch
    result = run_git(["branch", "--show-current"], cwd=root, check=False)
    branch = result.stdout.strip()
    return branch or "HEAD"


def get_merge_base(ref_a: str, ref_b: str = "HEAD", cwd: Path | str | None = None) -> str | None:
    """Return the merge-base commit between two refs, or None if either is missing."""
    result = run_git(
        ["merge-base", ref_a, ref_b], cwd=cwd or repo_root(), check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get_origin_default_branch(cwd: Path | str) -> str | None:
    """Return the origin default branch ref (e.g. ``"origin/main"``), or ``None``.

    Resolves from the ``refs/remotes/origin/HEAD`` symbolic ref, which is set
    automatically by ``git clone`` to point at the remote's default branch.
    Falls back to ``packed-refs`` if the loose ref file doesn't exist.
    """
    cwd_path = Path(cwd)
    git_dir = _resolve_git_dir(cwd_path)
    if git_dir is None:
        return None

    # Loose ref file
    origin_head = git_dir / "refs/remotes/origin/HEAD"
    if origin_head.is_file():
        try:
            content = origin_head.read_text().strip()
        except OSError:
            content = ""
        match = re.match(r"ref:\s*refs/remotes/origin/(.+)", content)
        if match:
            return f"origin/{match.group(1)}"

    # packed-refs
    packed = git_dir / "packed-refs"
    if packed.is_file():
        try:
            content = packed.read_text()
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("refs/remotes/origin/HEAD"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].startswith("refs/remotes/origin/"):
                        branch = parts[1].split("refs/remotes/origin/", 1)[1]
                        if branch:
                            return f"origin/{branch}"
        except OSError:
            pass

    return None


def is_dirty(path: Path | str) -> bool:
    """True if the git working tree at `path` has untracked or modified files.

    Uses a short timeout (30s) because this is a pre-flight safety check.
    On Windows under heavy AV, even simple git commands can hang — if the
    check times out we assume clean and let downstream --ff-only merge
    act as the safety net.
    """
    try:
        result = run_git(["status", "--porcelain"], cwd=path, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return False  # Assume clean; --ff-only merge is safe regardless
    return bool(result.stdout.strip())


def confirmed_clean(path: Path | str) -> bool:
    """True only if the working tree at `path` is *confirmed* clean.

    Unlike ``not is_dirty(...)``, this returns False when the status check
    times out or errors, so callers that gate a destructive operation
    (e.g. ``checkout --force``) never discard uncommitted work just because
    a status probe hung under AV. Use this — not ``is_dirty`` — whenever a
    false "clean" reading would cause data loss.
    """
    try:
        result = run_git(["status", "--porcelain"], cwd=path, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return False  # Could not confirm clean — treat as unsafe to force.
    return result.returncode == 0 and not result.stdout.strip()


def _ref_exists_fast(ref: str, cwd: Path | str) -> bool | None:
    """Check if a ref exists using direct file IO. Returns None if
    indeterminate (caller should fall back to subprocess).

    Only handles simple ref names (branches, remotes, full refs/).
    Returns None for revision expressions (HEAD~, HEAD^, SHAs, tags
    without refs/ prefix) so the caller falls back to git rev-parse.
    """
    # Revision syntax we can't resolve via file IO — fall back to git.
    if any(c in ref for c in ("~", "^", ":", "@")):
        return None
    cwd_path = Path(cwd)
    git_dir = _resolve_git_dir(cwd_path)
    if git_dir is None:
        return None
    # Check packed-refs and loose refs for the ref
    # Handle refs like "origin/branch" → "refs/remotes/origin/branch"
    if ref.startswith("origin/"):
        ref_path = f"refs/remotes/{ref}"
    elif not ref.startswith("refs/"):
        ref_path = f"refs/heads/{ref}"
    else:
        ref_path = ref
    # Loose ref file
    if (git_dir / ref_path).is_file():
        return True
    # Check packed-refs
    packed = git_dir / "packed-refs"
    if packed.is_file():
        try:
            target = f" {ref_path}\n"
            content = packed.read_text()
            if target in content or content.endswith(f" {ref_path}"):
                return True
        except OSError:
            return None
    return False


def ref_exists(ref: str, cwd: Path | str | None = None) -> bool:
    """True if a git ref resolves in the given repo.

    Fast path: checks loose refs and packed-refs via file IO (avoids
    subprocess on Windows). Falls back to `git rev-parse` if the fast
    path can't determine the answer.
    """
    if cwd is not None:
        fast = _ref_exists_fast(ref, cwd)
        if fast is not None:
            return fast
    try:
        result = run_git(
            ["rev-parse", "--verify", "--quiet", ref], cwd=cwd, check=False, timeout=30
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def wiki_is_initialized(wiki_path: Path) -> bool:
    """True if `wiki/` exists and is a checked-out git working tree.

    Submodules use a `.git` *file* pointing at `<root>/.git/modules/wiki`;
    a plain `.git` directory is also valid. Either form proves the
    submodule was successfully bootstrapped — without this, `git -C
    wiki <cmd>` will walk up and operate on the parent code repo, which
    is exactly the silent-cascade bug we're guarding against.
    """
    return wiki_path.is_dir() and (wiki_path / ".git").exists()


def submodule_exists(name: str, root: Path | str) -> bool:
    """True if `.gitmodules` at `root` declares a submodule named `name`.

    Fast path: parse `.gitmodules` directly via configparser. Falls
    back to `git config --file .gitmodules` only if the file exists but
    configparser can't read it (unusual git config syntax).
    """
    root_path = Path(root)
    gitmodules = root_path / ".gitmodules"
    if not gitmodules.is_file():
        return False
    url = _read_submodule_url(root_path, name)
    if url is not None:
        return True
    # Fallback: configparser may have refused; let git decide.
    result = run_git(
        ["config", "--file", str(gitmodules), "--get", f"submodule.{name}.url"],
        cwd=root,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def derive_repo_name(repo_path: str | Path) -> str:
    """Derive the repo name from a repo path using fast file IO (no subprocess).

    Priority: origin URL → directory basename.
    """
    root = Path(repo_path)
    url = _read_origin_url(root)
    if url:
        name = _parse_repo_name_from_url(url)
        if name:
            return name
    return root.name


def derive_branch(repo_path: str | Path) -> str:
    """Derive the current branch from a repo path using fast file IO (no subprocess).

    Returns 'HEAD' if detached or unreadable.
    """
    return _read_head_branch(Path(repo_path)) or "HEAD"


# The single branch used in the shared wiki remote. All repos and code
# branches store their content as subdirectories on this one branch:
#   wiki/{repo_name}/{code_branch}/
WIKI_REMOTE_BRANCH = "wiki"
WIKI_REMOTE_REF = f"refs/remotes/origin/{WIKI_REMOTE_BRANCH}"
