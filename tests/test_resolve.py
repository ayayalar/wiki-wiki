"""Tests for resolve_wiki_issue comprehensive git issue handling.

Covers:
  - Branch state detection (behind, ahead, diverged, equal)
  - In-progress operation detection (rebase, cherry-pick, revert)
  - Diagnosis priority (blockers surface first)
  - Resolution execution (pull_and_merge, reset, abort, etc.)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Branch state detection ──────────────────────────────────────────────


class TestBranchStateDetection:
    """Test _is_behind_remote, _is_ahead_of_remote, _is_diverged.

    All three functions now use ``git rev-list --count`` rather than
    ``merge-base --is-ancestor`` so that shallow clones and stale ancestry
    info cannot cause false negatives.
    """

    def _make_count_fake(self, local_ahead: int, remote_ahead: int):
        """Return a fake run_git that answers rev-list --count calls.

        ``local_ahead``  = commits in HEAD not in origin (origin/wiki..HEAD)
        ``remote_ahead`` = commits in origin not in HEAD (HEAD..origin/wiki)
        """

        def fake_git(args, **kwargs):
            fake = MagicMock()
            fake.returncode = 0
            fake.stderr = ""
            if "rev-list" in args and "--count" in args:
                spec_idx = args.index("--count") + 1
                spec = args[spec_idx]  # e.g. "HEAD..origin/wiki" or "origin/wiki..HEAD"
                if spec.startswith("HEAD"):
                    # HEAD..origin/wiki → remote is ahead by this many
                    fake.stdout = str(remote_ahead)
                else:
                    # origin/wiki..HEAD → local is ahead by this many
                    fake.stdout = str(local_ahead)
            else:
                fake.stdout = ""
            return fake

        return fake_git

    def test_behind_remote_detected(self):
        """Local is 0 ahead, remote is 3 ahead → behind."""
        from tools.resolve import _is_ahead_of_remote, _is_behind_remote, _is_diverged

        fake = self._make_count_fake(local_ahead=0, remote_ahead=3)
        with (
            patch("tools.resolve.run_git", side_effect=fake),
            patch("tools.resolve.ref_exists", return_value=True),
        ):
            assert _is_behind_remote(Path("/fake"), "myrepo", "main") is True
            assert _is_ahead_of_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_diverged(Path("/fake"), "myrepo", "main") is False

    def test_ahead_of_remote_detected(self):
        """Local is 2 ahead, remote is 0 ahead → ahead."""
        from tools.resolve import _is_ahead_of_remote, _is_behind_remote, _is_diverged

        fake = self._make_count_fake(local_ahead=2, remote_ahead=0)
        with (
            patch("tools.resolve.run_git", side_effect=fake),
            patch("tools.resolve.ref_exists", return_value=True),
        ):
            assert _is_behind_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_ahead_of_remote(Path("/fake"), "myrepo", "main") is True
            assert _is_diverged(Path("/fake"), "myrepo", "main") is False

    def test_diverged_detected(self):
        """Local is 4 ahead AND remote is 1 ahead → diverged."""
        from tools.resolve import _is_ahead_of_remote, _is_behind_remote, _is_diverged

        fake = self._make_count_fake(local_ahead=4, remote_ahead=1)
        with (
            patch("tools.resolve.run_git", side_effect=fake),
            patch("tools.resolve.ref_exists", return_value=True),
        ):
            assert _is_behind_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_ahead_of_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_diverged(Path("/fake"), "myrepo", "main") is True

    def test_equal_not_detected_as_issue(self):
        """Local is 0 ahead, remote is 0 ahead → healthy (no issue)."""
        from tools.resolve import _is_ahead_of_remote, _is_behind_remote, _is_diverged

        fake = self._make_count_fake(local_ahead=0, remote_ahead=0)
        with (
            patch("tools.resolve.run_git", side_effect=fake),
            patch("tools.resolve.ref_exists", return_value=True),
        ):
            assert _is_behind_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_ahead_of_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_diverged(Path("/fake"), "myrepo", "main") is False

    def test_missing_remote_ref_returns_false(self):
        """If origin/wiki ref doesn't exist, all checks return False."""
        from tools.resolve import _is_ahead_of_remote, _is_behind_remote, _is_diverged

        with patch("tools.resolve.ref_exists", return_value=False):
            assert _is_behind_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_ahead_of_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_diverged(Path("/fake"), "myrepo", "main") is False

    def test_rev_list_failure_returns_false(self):
        """If rev-list returns non-zero, functions treat state as unknown (False)."""
        from tools.resolve import _is_ahead_of_remote, _is_behind_remote, _is_diverged

        def failing_git(args, **kwargs):
            fake = MagicMock()
            fake.returncode = 128
            fake.stdout = ""
            fake.stderr = "fatal: not a git repo"
            return fake

        with (
            patch("tools.resolve.run_git", side_effect=failing_git),
            patch("tools.resolve.ref_exists", return_value=True),
        ):
            assert _is_behind_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_ahead_of_remote(Path("/fake"), "myrepo", "main") is False
            assert _is_diverged(Path("/fake"), "myrepo", "main") is False


# ── In-progress operation detection ─────────────────────────────────────


class TestInProgressOperations:
    """Test detection of rebase, cherry-pick, and revert in progress."""

    def test_rebase_in_progress_rebase_head(self, tmp_path: Path):
        """REBASE_HEAD indicates rebase in progress."""
        from tools.resolve import _has_rebase_in_progress

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "REBASE_HEAD").write_text("abc123\n")
        # Create .git as a directory (not submodule file)
        assert _has_rebase_in_progress(tmp_path) is True

    def test_rebase_in_progress_rebase_merge_dir(self, tmp_path: Path):
        """rebase-merge directory indicates interactive rebase in progress."""
        from tools.resolve import _has_rebase_in_progress

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "rebase-merge").mkdir()
        assert _has_rebase_in_progress(tmp_path) is True

    def test_rebase_in_progress_rebase_apply_dir(self, tmp_path: Path):
        """rebase-apply directory indicates apply-strategy rebase in progress."""
        from tools.resolve import _has_rebase_in_progress

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "rebase-apply").mkdir()
        assert _has_rebase_in_progress(tmp_path) is True

    def test_cherry_pick_in_progress(self, tmp_path: Path):
        """CHERRY_PICK_HEAD indicates cherry-pick in progress."""
        from tools.resolve import _has_cherry_pick_in_progress

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "CHERRY_PICK_HEAD").write_text("def456\n")
        assert _has_cherry_pick_in_progress(tmp_path) is True

    def test_revert_in_progress(self, tmp_path: Path):
        """REVERT_HEAD indicates revert in progress."""
        from tools.resolve import _has_revert_in_progress

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "REVERT_HEAD").write_text("ghi789\n")
        assert _has_revert_in_progress(tmp_path) is True

    def test_no_operation_in_progress(self, tmp_path: Path):
        """No state files → no operations in progress."""
        from tools.resolve import (
            _has_cherry_pick_in_progress,
            _has_rebase_in_progress,
            _has_revert_in_progress,
        )

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        assert _has_rebase_in_progress(tmp_path) is False
        assert _has_cherry_pick_in_progress(tmp_path) is False
        assert _has_revert_in_progress(tmp_path) is False


# ── Diagnosis priority ──────────────────────────────────────────────────


class TestDiagnosisPriority:
    """Test that higher-priority issues surface before lower ones."""

    def test_merge_conflict_takes_priority_over_dirty(self, code_repo: Path):
        """Merge conflict must be detected and returned before dirty worktree."""
        from tools.resolve import _diagnose, _get_git_dir

        wiki = code_repo / "wiki"
        # Pull to initialize
        from tools.pull import pull

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        # Simulate merge in progress + dirty state
        # For submodules, .git is a file pointing to the actual gitdir
        git_dir = _get_git_dir(wiki)
        assert git_dir is not None
        (git_dir / "MERGE_HEAD").write_text("abc123\n")
        (wiki / "untracked.md").write_text("dirty\n")

        issues = _diagnose(wiki)
        issue_names = [i["issue"] for i in issues]
        assert "merge_conflict" in issue_names
        # Should return early — dirty_worktree should not appear
        assert "dirty_worktree" not in issue_names

        # Cleanup
        (git_dir / "MERGE_HEAD").unlink(missing_ok=True)
        (wiki / "untracked.md").unlink(missing_ok=True)

    def test_rebase_takes_priority_over_cherry_pick(self, code_repo: Path):
        """When both REBASE_HEAD and CHERRY_PICK_HEAD exist, rebase wins."""
        from tools.resolve import _diagnose, _get_git_dir

        wiki = code_repo / "wiki"
        from tools.pull import pull

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        git_dir = _get_git_dir(wiki)
        assert git_dir is not None
        (git_dir / "REBASE_HEAD").write_text("abc123\n")
        (git_dir / "CHERRY_PICK_HEAD").write_text("def456\n")

        issues = _diagnose(wiki)
        issue_names = [i["issue"] for i in issues]
        assert "rebase_in_progress" in issue_names
        # Cherry-pick should NOT appear separately (rebase uses cherry-pick internally)
        assert "cherry_pick_in_progress" not in issue_names

        # Cleanup
        (git_dir / "REBASE_HEAD").unlink(missing_ok=True)
        (git_dir / "CHERRY_PICK_HEAD").unlink(missing_ok=True)


# ── Resolution execution ────────────────────────────────────────────────


class TestResolutions:
    """Test resolution action execution."""

    def test_behind_remote_pull_and_merge(self, code_repo: Path):
        """behind_remote:pull_and_merge fetches and merges remote."""
        from tools.resolve import resolve

        code_repo / "wiki"
        from tools.pull import pull

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        call_log: list[list[str]] = []

        def track_git(args, **kwargs):
            call_log.append(list(args))
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = ""
            fake.stderr = ""
            return fake

        with patch("tools.resolve.run_git", side_effect=track_git):
            result = resolve(
                action="behind_remote:pull_and_merge",
                repo_path=str(code_repo),
            )

        # Should have fetch + merge calls
        assert any("fetch" in c for c in call_log)
        assert any("merge" in c for c in call_log)
        assert result["status"] == "resolved"

    def test_behind_remote_reset_to_remote(self, code_repo: Path):
        """behind_remote:reset_to_remote does hard reset."""
        from tools.resolve import resolve

        code_repo / "wiki"
        from tools.pull import pull

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        call_log: list[list[str]] = []

        def track_git(args, **kwargs):
            call_log.append(list(args))
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = ""
            fake.stderr = ""
            return fake

        with patch("tools.resolve.run_git", side_effect=track_git):
            result = resolve(
                action="behind_remote:reset_to_remote",
                repo_path=str(code_repo),
            )

        assert any("reset" in c and "--hard" in c for c in call_log)
        assert result["status"] == "resolved"

    def test_ahead_of_remote_push_includes_pre_push_sync(self, code_repo: Path):
        """ahead_of_remote:push includes fetch + merge before push."""
        from tools.resolve import resolve

        code_repo / "wiki"
        from tools.pull import pull

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        call_log: list[list[str]] = []

        def track_git(args, **kwargs):
            call_log.append(list(args))
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = ""
            fake.stderr = ""
            return fake

        with (
            patch("tools.resolve.run_git", side_effect=track_git),
            patch("tools.resolve.ref_exists", return_value=True),
        ):
            result = resolve(
                action="ahead_of_remote:push",
                repo_path=str(code_repo),
            )

        # Should have fetch, merge, and push calls
        assert any("fetch" in c for c in call_log)
        assert any("merge" in c for c in call_log)
        assert any("push" in c for c in call_log)
        assert result["status"] == "resolved"

    def test_rebase_abort(self, code_repo: Path):
        """rebase_in_progress:abort_rebase runs git rebase --abort."""
        from tools.resolve import resolve

        code_repo / "wiki"
        from tools.pull import pull

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        call_log: list[list[str]] = []

        def track_git(args, **kwargs):
            call_log.append(list(args))
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = ""
            fake.stderr = ""
            return fake

        with patch("tools.resolve.run_git", side_effect=track_git):
            result = resolve(
                action="rebase_in_progress:abort_rebase",
                repo_path=str(code_repo),
            )

        assert any("rebase" in c and "--abort" in c for c in call_log)
        assert result["status"] == "resolved"

    def test_cherry_pick_abort(self, code_repo: Path):
        """cherry_pick_in_progress:abort_cherry_pick runs git cherry-pick --abort."""
        from tools.resolve import resolve

        code_repo / "wiki"
        from tools.pull import pull

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        call_log: list[list[str]] = []

        def track_git(args, **kwargs):
            call_log.append(list(args))
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = ""
            fake.stderr = ""
            return fake

        with patch("tools.resolve.run_git", side_effect=track_git):
            result = resolve(
                action="cherry_pick_in_progress:abort_cherry_pick",
                repo_path=str(code_repo),
            )

        assert any("cherry-pick" in c and "--abort" in c for c in call_log)
        assert result["status"] == "resolved"

    def test_missing_remote_reconfigure(self, code_repo: Path, monkeypatch):
        """missing_remote:reconfigure_remote sets URL from env var."""
        from tools.resolve import resolve

        code_repo / "wiki"
        from tools.pull import pull

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        monkeypatch.setenv("WIKI_MCP_REMOTE_URL", "https://example.com/test/wiki.git")

        call_log: list[list[str]] = []

        def track_git(args, **kwargs):
            call_log.append(list(args))
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = ""
            fake.stderr = ""
            return fake

        with patch("tools.resolve.run_git", side_effect=track_git):
            result = resolve(
                action="missing_remote:reconfigure_remote",
                repo_path=str(code_repo),
            )

        assert any("remote" in c and "set-url" in c for c in call_log)
        assert result["status"] == "resolved"

    def test_missing_remote_no_env_var_fails(self, code_repo: Path, monkeypatch):
        """missing_remote:reconfigure_remote fails when WIKI_MCP_REMOTE_URL is not set."""
        from tools.resolve import resolve

        code_repo / "wiki"
        from tools.pull import pull

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        monkeypatch.delenv("WIKI_MCP_REMOTE_URL", raising=False)

        result = resolve(
            action="missing_remote:reconfigure_remote",
            repo_path=str(code_repo),
        )

        assert result["status"] == "error"
        assert "WIKI_MCP_REMOTE_URL is not set" in result["error"]


# ── Helper functions ────────────────────────────────────────────────────


class TestHelperFunctions:
    """Test standalone helper functions."""

    def test_get_git_dir_regular(self, tmp_path: Path):
        """_get_git_dir resolves .git directory for regular repos."""
        from tools.resolve import _get_git_dir

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        assert _get_git_dir(tmp_path) == git_dir

    def test_get_git_dir_submodule(self, tmp_path: Path):
        """_get_git_dir resolves gitdir from .git file for submodules."""
        from tools.resolve import _get_git_dir

        # For submodules, .git is a file (not a directory)
        actual_git = tmp_path / "gitdir" / "modules" / "wiki"
        actual_git.mkdir(parents=True)
        (tmp_path / ".git").write_text(f"gitdir: {actual_git}\n")
        assert _get_git_dir(tmp_path) == actual_git

    def test_is_detached_head_true(self, tmp_path: Path):
        """Detached HEAD (SHA in HEAD file) is detected."""
        from tools.resolve import _is_detached_head

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("abc123def456789012345678901234567890abcd\n")
        assert _is_detached_head(tmp_path) is True

    def test_is_detached_head_false(self, tmp_path: Path):
        """Normal HEAD (ref: refs/heads/...) is not detached."""
        from tools.resolve import _is_detached_head

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/wiki\n")
        assert _is_detached_head(tmp_path) is False

    def test_sparse_checkout_current_true(self, tmp_path: Path):
        """Sparse-checkout that includes the pattern returns True."""
        from tools.resolve import _sparse_checkout_current

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        info_dir = git_dir / "info"
        info_dir.mkdir()
        (info_dir / "sparse-checkout").write_text("/*\n!/*/\n/myrepo/\n/myrepo/master/\n")
        assert _sparse_checkout_current(tmp_path, "myrepo/master") is True

    def test_sparse_checkout_current_false(self, tmp_path: Path):
        """Sparse-checkout that doesn't include the pattern returns False."""
        from tools.resolve import _sparse_checkout_current

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        info_dir = git_dir / "info"
        info_dir.mkdir()
        (info_dir / "sparse-checkout").write_text("/*\n!/*/\n/other/\n/other/main/\n")
        assert _sparse_checkout_current(tmp_path, "myrepo/master") is False


# ── Non-interactive editor suppression ─────────────────────────────────


class TestNonInteractiveEditor:
    """Test that rebase/cherry-pick/revert --continue pass GIT_EDITOR via env_extra.

    In a headless MCP server there is no TTY. Without GIT_EDITOR=true git
    tries to launch the configured editor (e.g. vi) for the commit message,
    which blocks indefinitely or errors out. The fix passes GIT_EDITOR=true
    via env_extra to run_git() so the subprocess inherits it without modifying
    the global os.environ (which was not thread-safe under concurrent tool calls).
    """

    def _make_tracking_git(self, target_subcmd: str, captured: list):
        """Return a run_git side-effect that records env_extra when target_subcmd is seen."""

        def track(args, **kwargs):
            from unittest.mock import MagicMock

            if target_subcmd in args:
                captured.append(kwargs.get("env_extra"))
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = ""
            fake.stderr = ""
            return fake

        return track

    def test_rebase_continue_passes_git_editor_env_extra(self, code_repo: Path):
        """GIT_EDITOR=true is passed via env_extra during 'git rebase --continue'."""
        import os
        from unittest.mock import patch

        from tools.pull import pull
        from tools.resolve import resolve

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        captured: list = []
        side_effect = self._make_tracking_git("--continue", captured)

        # Establish a clean baseline: the assertion below verifies resolve
        # does NOT set GIT_EDITOR globally, so a value inherited from the
        # ambient shell would otherwise cause a false failure.
        os.environ.pop("GIT_EDITOR", None)

        with patch("tools.resolve.run_git", side_effect=side_effect):
            result = resolve(
                action="rebase_in_progress:continue_rebase",
                repo_path=str(code_repo),
            )

        assert result["status"] == "resolved"
        assert captured, "run_git was never called with '--continue'"
        assert captured[0].get("GIT_EDITOR") == "true", (
            "GIT_EDITOR was not passed via env_extra during rebase --continue"
        )
        # GIT_EDITOR should not be set globally
        assert os.environ.get("GIT_EDITOR") is None, (
            "GIT_EDITOR should not be set globally on os.environ"
        )

    def test_cherry_pick_continue_passes_git_editor_env_extra(self, code_repo: Path):
        """GIT_EDITOR=true is passed via env_extra during 'git cherry-pick --continue'."""
        import os
        from unittest.mock import patch

        from tools.pull import pull
        from tools.resolve import resolve

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        captured: list = []
        side_effect = self._make_tracking_git("--continue", captured)

        # Establish a clean baseline: the assertion below verifies resolve
        # does NOT set GIT_EDITOR globally, so a value inherited from the
        # ambient shell would otherwise cause a false failure.
        os.environ.pop("GIT_EDITOR", None)

        with patch("tools.resolve.run_git", side_effect=side_effect):
            result = resolve(
                action="cherry_pick_in_progress:continue_cherry_pick",
                repo_path=str(code_repo),
            )

        assert result["status"] == "resolved"
        assert captured, "run_git was never called with '--continue'"
        assert captured[0].get("GIT_EDITOR") == "true", (
            "GIT_EDITOR was not passed via env_extra during cherry-pick --continue"
        )
        assert os.environ.get("GIT_EDITOR") is None

    def test_revert_continue_passes_git_editor_env_extra(self, code_repo: Path):
        """GIT_EDITOR=true is passed via env_extra during 'git revert --continue'."""
        import os
        from unittest.mock import patch

        from tools.pull import pull
        from tools.resolve import resolve

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        captured: list = []
        side_effect = self._make_tracking_git("--continue", captured)

        # Establish a clean baseline: the assertion below verifies resolve
        # does NOT set GIT_EDITOR globally, so a value inherited from the
        # ambient shell would otherwise cause a false failure.
        os.environ.pop("GIT_EDITOR", None)

        with patch("tools.resolve.run_git", side_effect=side_effect):
            result = resolve(
                action="revert_in_progress:continue_revert",
                repo_path=str(code_repo),
            )

        assert result["status"] == "resolved"
        assert captured, "run_git was never called with '--continue'"
        assert captured[0].get("GIT_EDITOR") == "true", (
            "GIT_EDITOR was not passed via env_extra during revert --continue"
        )
        assert os.environ.get("GIT_EDITOR") is None

    def test_no_editor_via_env_extra_not_global(self, code_repo: Path):
        """GIT_EDITOR is passed via env_extra, not set globally on os.environ.

        This prevents thread-safety issues when concurrent tool calls each
        need their own editor setting. The old _no_editor() context manager
        modified os.environ globally, which caused corruption under concurrency.
        """
        import os
        from unittest.mock import MagicMock, patch

        from tools.pull import pull
        from tools.resolve import resolve

        pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        # Ensure GIT_EDITOR is not set globally before the call.
        os.environ.pop("GIT_EDITOR", None)

        captured_env_extras: list[dict | None] = []

        def track_env_extra(args, **kwargs):
            captured_env_extras.append(kwargs.get("env_extra"))
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = ""
            fake.stderr = ""
            return fake

        with patch("tools.resolve.run_git", side_effect=track_env_extra):
            resolve(
                action="rebase_in_progress:continue_rebase",
                repo_path=str(code_repo),
            )

        # GIT_EDITOR should NOT be in global os.environ
        assert os.environ.get("GIT_EDITOR") is None, (
            "GIT_EDITOR should not be set globally on os.environ"
        )
        # But it should be passed via env_extra to the --continue call
        continue_calls = [e for e in captured_env_extras if e is not None]
        assert any(e.get("GIT_EDITOR") == "true" for e in continue_calls), (
            "GIT_EDITOR=true was not passed via env_extra to run_git"
        )
