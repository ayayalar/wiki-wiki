"""Unit tests for tools/status.py.

All tests are mock-based (no real git repos). The fixture provides a fake
wiki directory structure; git subprocess calls are patched out.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_fake(
    *,
    fetch_rc: int = 0,
    fetch_stderr: str = "",
    head_sha: str = "abc1234",
    wiki_updated: str = "2026-06-08 14:30:00 -0700",
    code_head: str = "ccc0001 Some code commit",
    remote_sha: str = "def5678",
    remote_message: str = "Some remote commit",
    commits_ahead: int = 0,
    commits_behind: int = 0,
    porcelain: str = "",
):
    """Build a fake run_git side_effect covering all calls made by status()."""

    def fake_run_git(args, **kwargs):
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""

        if "fetch" in args:
            r.returncode = fetch_rc
            r.stderr = fetch_stderr
        elif "log" in args and "--format=%h" in args:
            # git log -1 --format=%h <ref> [-- <path>]
            for arg in args:
                if arg == "HEAD":
                    r.stdout = head_sha
                    break
            else:
                r.stdout = remote_sha
        elif "log" in args and "--format=%ci" in args:
            r.stdout = wiki_updated
        elif "log" in args and "--format=%h %s" in args:
            # Distinguish code HEAD vs remote ref
            if "refs/remotes/origin/wiki" in args:
                r.stdout = f"{remote_sha} {remote_message}"
            else:
                r.stdout = code_head
        elif "rev-list" in args and "--count" in args:
            # git rev-list --count <spec> [-- <path>]
            # spec is the arg after --count
            spec_idx = args.index("--count") + 1
            spec = args[spec_idx]
            if "..HEAD" in spec:
                # origin/wiki..HEAD → ahead
                r.stdout = str(commits_ahead)
            else:
                # HEAD..origin/wiki → behind
                r.stdout = str(commits_behind)
        elif "status" in args and "--porcelain" in args:
            r.stdout = porcelain

        return r

    return fake_run_git


def _setup_wiki(tmp_path: Path, initialized: bool = True) -> Path:
    """Create a minimal fake wiki directory."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    if initialized:
        git_dir = wiki / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/wiki\n")
    return wiki


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWikiStatus:
    def test_not_initialized(self, tmp_path: Path):
        """Missing wiki dir → status not_initialized."""
        from tools.status import status

        repo = tmp_path / "myrepo"
        repo.mkdir()
        # No wiki/ subdir at all

        result = status(repo_name="myrepo", branch="main", repo_path=str(repo))

        assert result["status"] == "not_initialized"
        assert result["repo"] == "myrepo"
        assert result["branch"] == "main"

    def test_synced(self, tmp_path: Path):
        """ahead=0, behind=0, fetch ok → sync_state synced."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake()),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["sync_state"] == "synced"
        assert result["local"]["commits_ahead"] == 0
        assert result["local"]["commits_behind"] == 0
        assert result["remote"]["fetch_ok"] is True
        assert result["remote"]["fetch_error"] is None

    def test_ahead(self, tmp_path: Path):
        """ahead=2, behind=0 → sync_state ahead."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake(commits_ahead=2)),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["sync_state"] == "ahead"
        assert result["local"]["commits_ahead"] == 2
        assert result["local"]["commits_behind"] == 0

    def test_behind(self, tmp_path: Path):
        """ahead=0, behind=3 → sync_state behind."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake(commits_behind=3)),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["sync_state"] == "behind"
        assert result["local"]["commits_ahead"] == 0
        assert result["local"]["commits_behind"] == 3

    def test_diverged(self, tmp_path: Path):
        """ahead=4, behind=1 → sync_state diverged."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch(
                "tools.status.run_git",
                side_effect=_make_git_fake(commits_ahead=4, commits_behind=1),
            ),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["sync_state"] == "diverged"
        assert result["local"]["commits_ahead"] == 4
        assert result["local"]["commits_behind"] == 1

    def test_fetch_failed(self, tmp_path: Path):
        """Fetch failure → fetch_ok False, sync_state unknown."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch(
                "tools.status.run_git",
                side_effect=_make_git_fake(fetch_rc=1, fetch_stderr="fatal: unable to connect"),
            ),
            patch("tools.status.ref_exists", return_value=False),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["remote"]["fetch_ok"] is False
        assert result["remote"]["fetch_error"] == "fatal: unable to connect"
        assert result["sync_state"] == "unknown"

    def test_dirty_files_listed(self, tmp_path: Path):
        """Dirty porcelain output → dirty True, dirty_files populated."""
        from tools.status import status

        _setup_wiki(tmp_path)

        porcelain = " M myrepo/main/docs/architecture.md\n?? myrepo/main/scratch.txt\n"

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake(porcelain=porcelain)),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["local"]["dirty"] is True
        assert "docs/architecture.md" in result["local"]["dirty_files"]
        assert "scratch.txt" in result["local"]["dirty_files"]

    def test_clean_worktree(self, tmp_path: Path):
        """Empty porcelain → dirty False, dirty_files empty."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake(porcelain="")),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["local"]["dirty"] is False
        assert result["local"]["dirty_files"] == []

    def test_other_branch_files_excluded(self, tmp_path: Path):
        """Files from other branches must not appear in dirty_files."""
        from tools.status import status

        _setup_wiki(tmp_path)

        porcelain = (
            " M myrepo/main/docs/architecture.md\n"
            " M myrepo/develop/docs/feature.md\n"
            "?? myrepo/develop/scratch.txt\n"
            " M otherrepo/main/readme.md\n"
        )

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake(porcelain=porcelain)),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["local"]["dirty"] is True
        assert "docs/architecture.md" in result["local"]["dirty_files"]
        assert "docs/feature.md" not in result["local"]["dirty_files"]
        assert "scratch.txt" not in result["local"]["dirty_files"]
        assert "readme.md" not in result["local"]["dirty_files"]

    def test_in_progress_rebase(self, tmp_path: Path):
        """REBASE_HEAD present → in_progress rebase."""
        from tools.status import status

        wiki = _setup_wiki(tmp_path)
        (wiki / ".git" / "REBASE_HEAD").write_text("abc\n")

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake()),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["local"]["in_progress"] == "rebase"

    def test_in_progress_cherry_pick(self, tmp_path: Path):
        """CHERRY_PICK_HEAD present → in_progress cherry-pick."""
        from tools.status import status

        wiki = _setup_wiki(tmp_path)
        (wiki / ".git" / "CHERRY_PICK_HEAD").write_text("def\n")

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake()),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["local"]["in_progress"] == "cherry-pick"

    def test_in_progress_merge(self, tmp_path: Path):
        """MERGE_HEAD present → in_progress merge."""
        from tools.status import status

        wiki = _setup_wiki(tmp_path)
        (wiki / ".git" / "MERGE_HEAD").write_text("ghi\n")

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake()),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["local"]["in_progress"] == "merge"

    def test_in_progress_none(self, tmp_path: Path):
        """No state files → in_progress null."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake()),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["local"]["in_progress"] is None

    def test_missing_remote_ref(self, tmp_path: Path):
        """origin/wiki ref missing → remote sha null, sync_state unknown."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake()),
            patch("tools.status.ref_exists", return_value=False),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["remote"]["sha"] is None
        assert result["sync_state"] == "unknown"

    def test_head_sha_and_code_head_present(self, tmp_path: Path):
        """HEAD sha, wiki_updated, and code HEAD are surfaced in local section."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch(
                "tools.status.run_git",
                side_effect=_make_git_fake(
                    head_sha="aaa0001",
                    wiki_updated="2026-06-08 14:30:00 -0700",
                    code_head="ccc0001 Fix broken link",
                ),
            ),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["local"]["head_sha"] == "aaa0001"
        assert result["local"]["wiki_updated"] == "2026-06-08 14:30:00"
        assert result["local"]["code_head_sha"] == "ccc0001"
        assert result["local"]["code_head_message"] == "Fix broken link"

    def test_remote_sha_present(self, tmp_path: Path):
        """Remote sha and message are surfaced in remote section."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch(
                "tools.status.run_git",
                side_effect=_make_git_fake(remote_sha="bbb0002", remote_message="Fix broken link"),
            ),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["remote"]["sha"] == "bbb0002"
        assert result["remote"]["sha_message"] == "Fix broken link"
        assert result["remote"]["ref"] == "refs/remotes/origin/wiki"

    def test_repo_and_branch_in_result(self, tmp_path: Path):
        """repo and branch are always present in the top-level result."""
        from tools.status import status

        _setup_wiki(tmp_path)

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake()),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="acme", branch="feature-x", repo_path=str(tmp_path))

        assert result["repo"] == "acme"
        assert result["branch"] == "feature-x"

    def test_rebase_merge_dir_detected(self, tmp_path: Path):
        """rebase-merge directory → in_progress rebase (interactive rebase)."""
        from tools.status import status

        wiki = _setup_wiki(tmp_path)
        (wiki / ".git" / "rebase-merge").mkdir()

        with (
            patch("tools.status.wiki_is_initialized", return_value=True),
            patch("tools.status.run_git", side_effect=_make_git_fake()),
            patch("tools.status.ref_exists", return_value=True),
        ):
            result = status(repo_name="myrepo", branch="main", repo_path=str(tmp_path))

        assert result["local"]["in_progress"] == "rebase"
