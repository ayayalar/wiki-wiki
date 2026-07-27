"""Shared fixtures for wiki-mcp-server integration tests.

Each test gets:
  - A fresh bare git repo (``bare_wiki``) with an orphan ``wiki`` branch.
  - A fresh "code repo" (``code_repo``) with an initial commit.
  - WIKI_MCP_REMOTE_URL pointed at the bare repo.
  - All caches cleared so tests are fully isolated.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(
    args: list[str], cwd: Path, check: bool = False, **kwargs
) -> subprocess.CompletedProcess[str]:
    """Run a git command in the test harness (not through production code)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        creationflags=_CREATIONFLAGS,
        timeout=120,
        check=check,
        **kwargs,
    )


def _make_bare_wiki(path: Path) -> None:
    """Create a bare git repo at ``path`` with a single commit on the ``wiki`` branch."""
    _git(["init", "--bare", str(path)], cwd=path.parent)

    # We need a temp working tree to create the initial commit.
    work = path.parent / "_wiki_seed"
    work.mkdir()
    _git(["clone", str(path), str(work)], cwd=path.parent)
    _git(["config", "user.name", "Test"], cwd=work)
    _git(["config", "user.email", "test@test.com"], cwd=work)
    _git(["-c", "protocol.file.allow=always", "checkout", "--orphan", "wiki"], cwd=work)
    (work / ".gitkeep").write_text("")
    _git(["add", "."], cwd=work)
    _git(["commit", "-m", "seed"], cwd=work)
    _git(["-c", "protocol.file.allow=always", "push", "origin", "wiki"], cwd=work)

    # Point bare repo HEAD at the wiki branch.
    head_file = path / "HEAD"
    head_file.write_text("ref: refs/heads/wiki\n")

    import shutil

    shutil.rmtree(work, ignore_errors=True)


def _make_code_repo(path: Path) -> None:
    """Create a minimal git repo with src/app.py."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    _git(["config", "user.email", "test@test.com"], cwd=path)
    # Add a fake origin remote so get_repo_name() derives the correct name.
    # Use as_uri() for proper forward slashes on Windows.
    _git(["remote", "add", "origin", path.as_uri()], cwd=path)
    src = path / "src"
    src.mkdir()
    (src / "app.py").write_text('print("hello")\n')
    _git(["add", "."], cwd=path)
    _git(["commit", "-m", "initial"], cwd=path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bare_wiki(tmp_path: Path) -> Path:
    """A fresh bare wiki repo with an orphan ``wiki`` branch."""
    bare = tmp_path / "wiki.git"
    bare.mkdir()
    _make_bare_wiki(bare)
    return bare


@pytest.fixture()
def code_repo(tmp_path: Path) -> Path:
    """A fresh code repo with ``src/app.py`` committed."""
    repo = tmp_path / "myrepo"
    _make_code_repo(repo)
    return repo


@pytest.fixture(autouse=True)
def _env_and_caches(bare_wiki: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars and clear caches before every test."""
    url = bare_wiki.as_uri()
    monkeypatch.setenv("WIKI_MCP_REMOTE_URL", url)
    monkeypatch.setenv("WIKI_MCP_GIT_TIMEOUT", "180")

    # Clear all module-level caches so each test is isolated.
    import config

    config._wiki_path_cache = None
    config._repo_root_cache.clear()

    from utils import git as git_mod

    git_mod._repo_name_cache.clear()
