"""Wiki filesystem helpers: domain detection, log tail, scaffolding, remote URL."""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PureWindowsPath

IGNORE_DOMAINS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".github",
    "__pycache__",
    ".venv",
    "venv",
    "vendor",
    "coverage",
    ".next",
    ".turbo",
    ".cache",
    "target",
    ".idea",
    ".vscode",
    # The wiki submodule itself must never appear as a domain — it is
    # not part of the codebase being documented.
    "wiki",
}

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def detect_domains(file_tree: list[str]) -> list[str]:
    """Top-level directories present in the file tree, minus the ignore set.

    Pure helper for unit-testable aggregation. Prefer
    `detect_domains_from_repo` for live repos — it avoids the cost of
    enumerating every file on large monorepos.
    """
    domains: set[str] = set()
    for f in file_tree:
        if not f:
            continue
        parts = Path(f).parts
        if len(parts) > 1 and parts[0] not in IGNORE_DOMAINS:
            domains.add(parts[0])
    return sorted(domains)


def detect_domains_from_repo(repo_root: Path) -> list[str]:
    """Top-level tracked directories in `repo_root`, minus the ignore set.

    Uses `git ls-tree --name-only HEAD` — runs in O(top-level entries)
    independent of total file count, so this stays fast on monorepos
    where `git ls-files` would return hundreds of thousands of paths
    that we'd then aggregate in Python and send back through MCP.

    Submodule gitlinks (mode 160000) are excluded: they are not source
    directories and must not appear as wiki domains.
    """
    from utils.git import run_git

    result = run_git(
        ["ls-tree", "HEAD"],
        cwd=repo_root,
        check=False,
    )
    if result.returncode != 0:
        return []

    # Collect submodule names from .gitmodules as belt-and-suspenders.
    submodule_names: set[str] = set()
    gitmodules = repo_root / ".gitmodules"
    if gitmodules.is_file():
        import configparser
        cfg = configparser.RawConfigParser()
        try:
            cfg.read(str(gitmodules))
            for section in cfg.sections():
                if section.startswith("submodule "):
                    path_val = cfg.get(section, "path", fallback="")
                    if path_val:
                        submodule_names.add(path_val.strip().split("/")[0])
        except Exception:  # noqa: BLE001
            pass

    domains: set[str] = set()
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        # ls-tree output: "<mode> <type> <object>\t<name>"
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        name = parts[1].strip()
        meta = parts[0].split()
        mode = meta[0] if meta else ""
        obj_type = meta[1] if len(meta) > 1 else ""
        # Skip gitlinks (submodules, mode 160000) and non-tree entries.
        if mode == "160000" or obj_type != "tree":
            continue
        if name in IGNORE_DOMAINS or name in submodule_names:
            continue
        domains.add(name)
    return sorted(domains)


def get_log_tail(log_path: Path, n: int) -> str:
    """Last `n` lines of the log file, or empty string if missing."""
    if not log_path.exists():
        return ""
    lines = log_path.read_text(encoding="utf-8", errors="replace").strip().split("\n")
    return "\n".join(lines[-n:])


def scaffold(repo_wiki_path: Path, repo_name: str) -> list[str]:
    """Create CLAUDE.md, index.md, and log.md under `repo_wiki_path`.

    CLAUDE.md is copied verbatim from the shipped template. index.md and
    log.md are written as minimal stubs.

    Returns the list of created files (relative paths).
    """
    repo_wiki_path.mkdir(parents=True, exist_ok=True)

    claude_dst = repo_wiki_path / "CLAUDE.md"
    index_dst = repo_wiki_path / "index.md"
    log_dst = repo_wiki_path / "log.md"

    shutil.copyfile(TEMPLATES_DIR / "CLAUDE.md", claude_dst)
    # Always UTF-8: the stubs contain em dashes and every reader in the
    # codebase decodes as UTF-8. Without this, write_text uses the locale
    # codec (cp1252 on Windows) → mojibake, and a non-ASCII repo_name raises
    # UnicodeEncodeError mid-scaffold.
    index_dst.write_text(_index_stub(repo_name), encoding="utf-8")
    log_dst.write_text(_log_stub(), encoding="utf-8")

    return ["CLAUDE.md", "index.md", "log.md"]


def _index_stub(repo_name: str) -> str:
    return (
        f"# {repo_name} — Wiki Index\n\n"
        "_Domain summaries will be added by the agent after init._\n\n"
        "<!-- One entry per domain: `## [Domain](domain/index.md) — one-line summary` -->\n"
    )


def _log_stub() -> str:
    return (
        "# Wiki Log\n\n"
        "<!-- Append-only. Each entry: `## [YYYY-MM-DD] <op> | <summary>` -->\n"
    )


def wiki_not_initialized_response(wiki_path: Path) -> dict:
    """Uniform error response returned by tools when wiki/ isn't a working submodule."""
    return {
        "status": "wiki_not_initialized",
        "error": (
            "Wiki submodule is not checked out. Call pull_wiki() first. "
            "If pull_wiki returns bootstrap_failed, verify WIKI_MCP_REMOTE_URL "
            "points to an existing reachable bare git repo."
        ),
        "wiki_path": str(wiki_path),
    }


def validate_wiki_params(repo_name: str, branch: str) -> str | None:
    """Validate repo_name and branch for path safety.

    Returns an error message if invalid, or None if safe.
    Rejects empty strings, absolute paths, '..' components, and null bytes.
    """
    for label, value in [("repo_name", repo_name), ("branch", branch)]:
        if not value or not value.strip():
            return f"{label} must not be empty."
        if "\0" in value:
            return f"{label} contains null bytes."
        # Interpret the value under BOTH POSIX and Windows path rules. The
        # host's own Path() misses escapes from the other OS's semantics —
        # e.g. on Windows "C:evil" is drive-relative and "\evil" is rooted,
        # yet neither is_absolute() nor contains ".."; joining either onto the
        # wiki dir replaces the anchor and escapes it. Rejecting drive/root on
        # a PureWindowsPath closes that gap regardless of the host OS.
        for flavor in (Path(value), PureWindowsPath(value)):
            if ".." in flavor.parts:
                return f"{label} contains '..' — path traversal not allowed."
            if flavor.is_absolute() or flavor.drive or flavor.root:
                return f"{label} must not be an absolute or drive/root-relative path."
    return None


def check_params(
    repo_name: str, branch: str,
) -> dict | None:
    """Validate repo_name and branch. Returns an error dict or None if safe.

    Call this at the top of every tool that accepts repo_name/branch params.
    Provides defense-in-depth so tools are safe even when called directly
    (bypassing the server layer).
    """
    err = validate_wiki_params(repo_name, branch)
    if err:
        return {"status": "invalid_params", "error": err}
    return None


def read_remote_url() -> str | None:
    """Return the wiki remote URL from WIKI_MCP_REMOTE_URL, or None.

    The env var is the single source of truth — set it in your MCP
    client config (e.g. ~/.copilot/mcp-config.json) alongside
    WIKI_MCP_REPO_ROOT. No file-based fallback.
    """
    value = os.environ.get("WIKI_MCP_REMOTE_URL", "").strip()
    return value or None
