"""Repo-root and wiki-path discovery.

The wiki lives at ``<repo_root>/wiki`` as a git submodule inside the
developer's own code repo. ``find_repo_root`` walks up from cwd to locate
that repo root.
"""

from __future__ import annotations

import os
from pathlib import Path


class RepoRootNotFound(RuntimeError):
    pass


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default: cwd) to find the code repo root.

    Prefers a directory containing `.gitmodules`. Falls back to one
    containing `.git`. Raises RepoRootNotFound if neither is found before
    reaching the filesystem root.
    """
    current = (start or Path.cwd()).resolve()
    submodule_match: Path | None = None

    for candidate in [current, *current.parents]:
        # A .gitmodules marks a superproject root (the repo that owns the wiki
        # submodule) — prefer it.
        if (candidate / ".gitmodules").is_file():
            return candidate
        dot_git = candidate / ".git"
        # A .git *directory* is a real, independent repo boundary. Stop here:
        # walking past it to find some distant ancestor's .gitmodules would
        # resolve the wrong repo (e.g. a plain project nested under a
        # submodule-using dotfiles repo).
        if dot_git.is_dir():
            return candidate
        # A .git *file* is a submodule/worktree — record it as a fallback but
        # keep climbing so an enclosing superproject (.gitmodules) wins.
        if dot_git.is_file() and submodule_match is None:
            submodule_match = candidate

    if submodule_match is not None:
        return submodule_match

    raise RepoRootNotFound(
        f"Could not find a code repo root (looking for .gitmodules or .git) starting from {current}"
    )


_repo_root_cache: dict[tuple[str, str], Path] = {}


def repo_root() -> Path:
    """Cached repo root resolution. Honors WIKI_MCP_REPO_ROOT env var override.

    The result is memoized per (cwd, env-override) pair: under the
    `wiki-mcp-server` running model, cwd is fixed at server launch and
    the env var doesn't change mid-session — so this is effectively a
    one-time walk-up of the directory tree.
    """
    override = os.environ.get("WIKI_MCP_REPO_ROOT", "")
    key = (str(Path.cwd().resolve()), override)
    cached = _repo_root_cache.get(key)
    if cached is not None:
        return cached
    if override:
        resolved = Path(override).resolve()
    else:
        resolved = find_repo_root()
    _repo_root_cache[key] = resolved
    return resolved


def _clear_cache() -> None:
    """Test helper: invalidate the repo-root and wiki-path memos."""
    global _wiki_path_cache
    _repo_root_cache.clear()
    _wiki_path_cache = None


_wiki_path_cache: Path | None = None


def wiki_path() -> Path:
    """Path to the wiki directory: ``<repo_root>/wiki``.

    Used as the fallback wiki location for direct tool callers that don't
    pass an explicit ``repo_path``. Server-dispatched tools always pass a
    ``repo_path`` and build ``<repo_path>/wiki`` themselves.
    """
    global _wiki_path_cache
    if _wiki_path_cache is not None:
        return _wiki_path_cache
    _wiki_path_cache = repo_root() / "wiki"
    return _wiki_path_cache
