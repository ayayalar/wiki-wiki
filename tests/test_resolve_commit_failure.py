"""Tests for the critical fix: resolve action handlers that ignored git commit return codes.

Three handlers in tools/resolve.py returned ``status: "resolved"``
unconditionally after ``git commit`` with ``check=False``, never inspecting
the return code.  When the commit failed (e.g. unset git identity in a
headless MCP server), the user was told the merge completed while
``MERGE_HEAD`` still lived on disk (for merge handlers) or the dirty
files remained uncommitted (for the commit_and_push handler).

The sibling function ``resolve_merge`` (lines 143-167) was patched in a
prior change with an explicit comment (lines 161-167) explaining this exact
hazard.  These tests verify the three action handlers were brought to parity.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import _git


def _make_remote_conflict(bare_wiki: Path, code_repo: Path, tmp_path: Path) -> Path:
    """Create a real merge-in-progress with conflicts in the local wiki clone.

    Prerequisites:
      - ``pull(repo_name="myrepo", branch="develop", ...)`` has been called.
      - The wiki clone has a local commit on top of the scaffold
        (so ``origin/wiki`` is one commit behind).

    Steps:
      1. Clone the bare wiki remote into a temp seed.
      2. Edit ``myrepo/develop/index.md`` differently and push.
      3. ``fetch`` + ``merge --no-ff`` in the local wiki clone → real conflict,
         ``MERGE_HEAD`` on disk.

    Returns the wiki ``Path``.
    """
    wiki = code_repo / "wiki"

    # --- remote edit via a seed clone ---
    seed = tmp_path / "_remote_seed"
    _git(["clone", bare_wiki.as_uri(), str(seed)], cwd=tmp_path)
    _git(["config", "user.name", "Remote"], cwd=seed)
    _git(["config", "user.email", "remote@remote"], cwd=seed)

    remote_idx = seed / "myrepo" / "develop" / "index.md"
    remote_idx.parent.mkdir(parents=True, exist_ok=True)
    remote_idx.write_text("remote line\n")
    _git(["add", "."], cwd=seed)
    _git(["commit", "-m", "remote edit"], cwd=seed)
    _git(["push", "origin", "wiki"], cwd=seed)

    # --- fetch + merge in local wiki clone → real conflict ---
    _git(
        ["-c", "protocol.file.allow=always", "fetch", "origin", "wiki", "--deepen=10"],
        cwd=wiki,
    )
    _git(
        ["merge", "--no-ff", "origin/wiki"],
        cwd=wiki,
        check=False,
    )
    return wiki


def _poison_identity(wiki: Path, monkeypatch) -> None:
    """Make every git commit in *wiki* fail by providing empty user.name/email.

    Empty values in the local repo config take priority over global config,
    and git rejects "empty ident name" with rc != 0.  As a defence in depth,
    also remove any ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` env vars so they
    cannot supply identity through that channel either.
    """
    _git(["config", "user.name", ""], cwd=wiki, check=False)
    _git(["config", "user.email", ""], cwd=wiki, check=False)
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)


# ── merge_conflict:keep_local ──────────────────────────────────────────────


class TestMergeConflictKeepLocal:
    """``merge_conflict:keep_local`` must not report ``resolved`` when commit fails."""

    def test_keep_local_reports_failure_when_commit_fails(
        self,
        bare_wiki: Path,
        code_repo: Path,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        from tools.pull import pull
        from tools.resolve import _has_merge_in_progress
        from tools.resolve import resolve as resolve_wiki
        from utils.wiki_index import WikiIndex

        WikiIndex.invalidate_all()

        # --- setup: pull + local commit + remote conflict → MERGE_HEAD ---
        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))
        wiki = code_repo / "wiki"

        # Local commit (identity from global gitconfig is still available).
        idx = wiki / "myrepo" / "develop" / "index.md"
        idx.write_text("local line\n")
        _git(["add", "myrepo/develop/index.md"], cwd=wiki)
        _git(
            ["-c", "protocol.file.allow=always", "commit", "--no-verify", "-m", "local edit"],
            cwd=wiki,
        )

        wiki = _make_remote_conflict(bare_wiki, code_repo, tmp_path)

        # --- poison identity so the resolve handler's commit will fail ---
        _poison_identity(wiki, monkeypatch)

        assert _has_merge_in_progress(wiki), "merge should have created MERGE_HEAD"

        # --- act ---
        result = resolve_wiki(
            action="merge_conflict:keep_local",
            repo_path=str(code_repo),
        )

        # --- assert ---
        assert result["status"] != "resolved", (
            f"commit failed but handler returned resolved: {result}"
        )
        assert result["action"] == "merge_conflict:keep_local"
        assert not _has_merge_in_progress(wiki), "MERGE_HEAD should have been cleaned up"
        assert result.get("commit_stderr"), (
            "response should contain commit_stderr with failure details"
        )

    def test_keep_local_resolved_when_commit_succeeds(
        self,
        bare_wiki: Path,
        code_repo: Path,
        tmp_path: Path,
    ) -> None:
        """Regression guard: keep_local returns resolved with healthy identity."""
        from tools.pull import pull
        from tools.resolve import _has_merge_in_progress
        from tools.resolve import resolve as resolve_wiki
        from utils.wiki_index import WikiIndex

        WikiIndex.invalidate_all()

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))
        wiki = code_repo / "wiki"

        idx = wiki / "myrepo" / "develop" / "index.md"
        idx.write_text("local line\n")
        _git(["add", "myrepo/develop/index.md"], cwd=wiki)
        _git(
            ["-c", "protocol.file.allow=always", "commit", "--no-verify", "-m", "local edit"],
            cwd=wiki,
        )

        wiki = _make_remote_conflict(bare_wiki, code_repo, tmp_path)
        _git(["config", "user.name", "Test"], cwd=wiki)
        _git(["config", "user.email", "test@test.com"], cwd=wiki)

        assert _has_merge_in_progress(wiki)

        result = resolve_wiki(
            action="merge_conflict:keep_local",
            repo_path=str(code_repo),
        )

        assert result["status"] == "resolved", (
            f"expected resolved with healthy identity, got: {result}"
        )
        assert not _has_merge_in_progress(wiki)


# ── merge_conflict:keep_remote ─────────────────────────────────────────────


class TestMergeConflictKeepRemote:
    """``merge_conflict:keep_remote`` must not report ``resolved`` when commit fails."""

    def test_keep_remote_reports_failure_when_commit_fails(
        self,
        bare_wiki: Path,
        code_repo: Path,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        from tools.pull import pull
        from tools.resolve import _has_merge_in_progress
        from tools.resolve import resolve as resolve_wiki
        from utils.wiki_index import WikiIndex

        WikiIndex.invalidate_all()

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))
        wiki = code_repo / "wiki"

        idx = wiki / "myrepo" / "develop" / "index.md"
        idx.write_text("local line\n")
        _git(["add", "myrepo/develop/index.md"], cwd=wiki)
        _git(
            ["-c", "protocol.file.allow=always", "commit", "--no-verify", "-m", "local edit"],
            cwd=wiki,
        )

        wiki = _make_remote_conflict(bare_wiki, code_repo, tmp_path)
        _poison_identity(wiki, monkeypatch)

        assert _has_merge_in_progress(wiki)

        result = resolve_wiki(
            action="merge_conflict:keep_remote",
            repo_path=str(code_repo),
        )

        assert result["status"] != "resolved", (
            f"commit failed but handler returned resolved: {result}"
        )
        assert result["action"] == "merge_conflict:keep_remote"
        assert not _has_merge_in_progress(wiki)
        assert result.get("commit_stderr")

    def test_keep_remote_resolved_when_commit_succeeds(
        self,
        bare_wiki: Path,
        code_repo: Path,
        tmp_path: Path,
    ) -> None:
        """Regression guard: keep_remote returns resolved with healthy identity."""
        from tools.pull import pull
        from tools.resolve import _has_merge_in_progress
        from tools.resolve import resolve as resolve_wiki
        from utils.wiki_index import WikiIndex

        WikiIndex.invalidate_all()

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))
        wiki = code_repo / "wiki"

        idx = wiki / "myrepo" / "develop" / "index.md"
        idx.write_text("local line\n")
        _git(["add", "myrepo/develop/index.md"], cwd=wiki)
        _git(
            ["-c", "protocol.file.allow=always", "commit", "--no-verify", "-m", "local edit"],
            cwd=wiki,
        )

        wiki = _make_remote_conflict(bare_wiki, code_repo, tmp_path)
        _git(["config", "user.name", "Test"], cwd=wiki)
        _git(["config", "user.email", "test@test.com"], cwd=wiki)

        assert _has_merge_in_progress(wiki)

        result = resolve_wiki(
            action="merge_conflict:keep_remote",
            repo_path=str(code_repo),
        )

        assert result["status"] == "resolved", (
            f"expected resolved with healthy identity, got: {result}"
        )
        assert not _has_merge_in_progress(wiki)


# ── dirty_worktree:commit_and_push ─────────────────────────────────────────


class TestDirtyWorktreeCommitAndPush:
    """``dirty_worktree:commit_and_push`` must not report ``resolved`` when commit fails."""

    def test_commit_and_push_reports_failure_when_commit_fails(
        self,
        bare_wiki: Path,
        code_repo: Path,
        monkeypatch,
    ) -> None:
        from tools.pull import pull
        from tools.resolve import resolve as resolve_wiki
        from utils.wiki_index import WikiIndex

        WikiIndex.invalidate_all()

        # --- setup: pull + dirty file + poisoned identity ---
        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))
        wiki = code_repo / "wiki"

        notes = wiki / "myrepo" / "develop" / "notes.md"
        notes.write_text("dirty content\n")

        _poison_identity(wiki, monkeypatch)

        # --- act ---
        result = resolve_wiki(
            action="dirty_worktree:commit_and_push",
            repo_path=str(code_repo),
        )

        # --- assert ---
        assert result["status"] != "resolved", (
            f"commit failed but handler returned resolved: {result}"
        )
        assert result["action"] == "dirty_worktree:commit_and_push"
        # The dirty file should still be uncommitted.
        status = _git(["status", "--porcelain"], cwd=wiki)
        assert "notes.md" in status.stdout, (
            f"notes.md should still be uncommitted, got: {status.stdout.strip()}"
        )
        # The wiki log should NOT contain the handler's commit message.
        log = _git(["log", "-1", "--format=%s"], cwd=wiki)
        assert "wiki: commit pending changes" not in log.stdout, (
            "the failing commit should not have been recorded in git log"
        )

    def test_commit_and_push_succeeds_when_identity_healthy(
        self,
        bare_wiki: Path,
        code_repo: Path,
    ) -> None:
        """Regression guard: commit_and_push returns resolved with healthy identity."""
        from tools.pull import pull
        from tools.resolve import resolve as resolve_wiki
        from utils.wiki_index import WikiIndex

        WikiIndex.invalidate_all()

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))
        wiki = code_repo / "wiki"
        _git(["config", "user.name", "Test"], cwd=wiki)
        _git(["config", "user.email", "test@test.com"], cwd=wiki)

        notes = wiki / "myrepo" / "develop" / "notes.md"
        notes.write_text("dirty content\n")

        result = resolve_wiki(
            action="dirty_worktree:commit_and_push",
            repo_path=str(code_repo),
        )

        assert result["status"] == "resolved", (
            f"expected resolved with healthy identity, got: {result}"
        )
