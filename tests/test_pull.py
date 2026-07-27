"""Unit tests for PullBuilder stage methods.

Each stage returns ``(ok, result)``. These tests mock git subprocess
calls and verify each stage in isolation, plus the full execute() flow.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.pull import PullBuilder
from tools.resolve import MERGE_CLEAN, MERGE_CONFLICT

# ── Helpers ────────────────────────────────────────────────────────────


def _builder(**kwargs) -> PullBuilder:
    """Return a PullBuilder configured with defaults for testing."""
    return PullBuilder().for_repo_branch(
        repo_name=kwargs.get("repo_name", "myrepo"),
        branch=kwargs.get("branch", "develop"),
        repo_path=kwargs.get("repo_path", str(kwargs.get("root", Path("/fake/repo")))),
    )


def _mock_subprocess(stdout="", stderr="", returncode=0):
    """Build a MagicMock mimicking a CompletedProcess."""
    cp = MagicMock()
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


# ── Stage 1: _validate ─────────────────────────────────────────────────


class TestValidate:
    def test_passes_with_valid_params(self, monkeypatch):
        monkeypatch.setenv("WIKI_MCP_REPO_ROOT", "")
        ok, result = _builder()._validate()
        assert ok is True
        assert result is None

    def test_fails_null_byte_in_repo_name(self):
        ok, result = _builder(repo_name="bad\0name")._validate()
        assert ok is False
        assert result["status"] == "invalid_params"

    def test_fails_null_byte_in_branch(self):
        ok, result = _builder(branch="bad\0branch")._validate()
        assert ok is False
        assert result["status"] == "invalid_params"

    def test_fails_empty_repo_name(self):
        """Empty repo_name gets replaced by get_repo_name() fallback — no failure."""
        ok, result = _builder(repo_name="")._validate()
        assert ok is True  # empty string → fallback to get_repo_name()
        assert result is None

    def test_fails_repo_root_mismatch(self, monkeypatch):
        monkeypatch.setenv("WIKI_MCP_REPO_ROOT", "/other/repo")
        ok, result = _builder()._validate()
        assert ok is False
        assert result["status"] == "bootstrap_refused"
        # configured_root is resolved, so on Windows it may include a drive letter
        assert "other" in result["configured_root"]


# ── Stage 2: _bootstrap ───────────────────────────────────────────────


class TestBootstrap:
    @patch("tools.pull.run_git")
    @patch("tools.pull.wiki_is_initialized", return_value=True)
    @patch("tools.pull.submodule_exists", return_value=True)
    def test_wiki_already_exists_returns_ok(self, mock_sm, mock_init, mock_git, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._bootstrap()
        assert ok is True
        assert result is None

    @patch("tools.pull.run_git")
    @patch("tools.pull.wiki_is_initialized", return_value=False)
    @patch("tools.pull.submodule_exists", return_value=False)
    @patch("tools.pull.read_remote_url", return_value=None)
    def test_no_url_returns_needs_setup(
        self,
        mock_url,
        mock_sm,
        mock_init,
        mock_git,
        tmp_path,
    ):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._bootstrap()
        assert ok is False
        assert result["status"] == "needs_setup"

    @patch("tools.pull.run_git")
    @patch("tools.pull.wiki_is_initialized", return_value=False)
    @patch("tools.pull.submodule_exists", return_value=False)
    @patch("tools.pull.read_remote_url", return_value="https://example.com/wiki.git")
    @patch("tools.pull._bootstrap_clone", return_value=None)
    @patch("tools.pull.init_submodule_config", return_value=True)
    def test_bootstraps_with_url(
        self,
        mock_isc,
        mock_bs,
        mock_url,
        mock_sm,
        mock_init,
        mock_git,
        tmp_path,
    ):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._bootstrap()
        assert ok is True
        assert result is None
        assert b._bootstrapped is True
        mock_bs.assert_called_once_with(
            tmp_path, "https://example.com/wiki.git", "myrepo", "develop"
        )


# ── Stage 2b: _configure_sparse_checkout ───────────────────────────────


class TestConfigureSparseCheckout:
    @patch("tools.pull.run_git")
    @patch("tools.pull.set_sparse_checkout_cone", return_value=True)
    def test_cone_success(self, mock_scc, mock_git, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._configure_sparse_checkout()
        assert ok is True
        assert result is None

    @patch("tools.pull.run_git")
    @patch("tools.pull.set_sparse_checkout_cone", return_value=False)
    def test_falls_back_to_non_cone(self, mock_scc, mock_git, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._configure_sparse_checkout()
        assert ok is True
        assert result is None
        # Should have called sparse-checkout set as fallback
        mock_git.assert_any_call(
            ["sparse-checkout", "set", "myrepo/develop"],
            cwd=tmp_path / "wiki",
            check=False,
        )

    @patch("tools.pull.run_git")
    @patch("tools.pull.set_sparse_checkout_cone", return_value=True)
    @patch("tools.pull.submodule_exists", return_value=True)
    def test_retries_submodule_checkout_when_update_none_skips_init(
        self, mock_sm, mock_scc, mock_git, tmp_path
    ):
        b = _builder(root=tmp_path)

        def _side_effect(args, cwd=None, check=False):
            cp = _mock_subprocess()
            if (
                args[:4]
                == ["-c", "protocol.file.allow=always", "-c", "submodule.wiki.update=checkout"]
                and "--remote" in args
            ):
                (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
                (tmp_path / "wiki" / ".git").write_text("gitdir: ../.git/modules/wiki\n")
            return cp

        mock_git.side_effect = _side_effect

        ok, result = b._configure_sparse_checkout()

        assert ok is True
        assert result is None
        mock_git.assert_any_call(
            [
                "-c",
                "protocol.file.allow=always",
                "-c",
                "submodule.wiki.update=checkout",
                "submodule",
                "update",
                "--init",
                "--depth",
                "1",
                "wiki",
            ],
            cwd=tmp_path,
            check=False,
        )
        mock_git.assert_any_call(
            [
                "-c",
                "protocol.file.allow=always",
                "-c",
                "submodule.wiki.update=checkout",
                "submodule",
                "update",
                "--init",
                "--remote",
                "wiki",
            ],
            cwd=tmp_path,
            check=False,
        )
        mock_scc.assert_called_once_with(tmp_path / "wiki", ["myrepo/develop"])


# ── Stage 3: _resolve_pre_sync_conflicts ───────────────────────────────


class TestResolvePreSyncConflicts:
    @patch("tools.pull.auto_resolve_conflicts")
    def test_calls_auto_resolve(self, mock_arc, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._resolve_pre_sync_conflicts()
        assert ok is True
        assert result is None
        mock_arc.assert_called_once_with(tmp_path / "wiki", "myrepo")

    @patch("tools.pull.auto_resolve_conflicts")
    def test_skips_when_no_wiki_git(self, mock_arc, tmp_path):
        b = _builder(root=tmp_path)
        ok, result = b._resolve_pre_sync_conflicts()
        assert ok is True
        assert result is None
        mock_arc.assert_not_called()


# ── Stage 4: _finish_in_progress_merge ─────────────────────────────────


class TestFinishInProgressMerge:
    @patch("tools.pull.finish_merge")
    def test_calls_finish_merge(self, mock_fm, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._finish_in_progress_merge()
        assert ok is True
        assert result is None
        mock_fm.assert_called_once_with(tmp_path / "wiki")

    @patch("tools.pull.finish_merge")
    def test_skips_when_no_wiki_git(self, mock_fm, tmp_path):
        b = _builder(root=tmp_path)
        ok, result = b._finish_in_progress_merge()
        assert ok is True
        assert result is None
        mock_fm.assert_not_called()


# ── Stage 5: _guard_unpushed ───────────────────────────────────────────


class TestGuardUnpushed:
    def _setup_wiki(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        return _builder(root=tmp_path), wiki

    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=False)
    def test_no_remote_ref_returns_ok(self, mock_ref, mock_git, tmp_path):
        b, _ = self._setup_wiki(tmp_path)
        mock_git.return_value = _mock_subprocess()
        ok, result = b._guard_unpushed()
        assert ok is True
        assert result is None

    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=True)
    def test_no_ahead_commits_returns_ok(self, mock_ref, mock_git, tmp_path):
        b, _ = self._setup_wiki(tmp_path)
        mock_git.side_effect = [
            _mock_subprocess(),  # fetch
            _mock_subprocess(stdout="0\n"),  # rev-list --count
        ]
        ok, result = b._guard_unpushed()
        assert ok is True
        assert result is None

    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=True)
    def test_ahead_commits_returns_error(self, mock_ref, mock_git, tmp_path):
        b, _ = self._setup_wiki(tmp_path)
        mock_git.side_effect = [
            _mock_subprocess(),  # fetch
            _mock_subprocess(stdout="3\n"),  # rev-list --count → 3 ahead
        ]
        ok, result = b._guard_unpushed()
        assert ok is False
        assert result["status"] == "unpushed_changes"
        assert result["commits_ahead"] == 3

    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=True)
    def test_rev_list_failure_returns_ok(self, mock_ref, mock_git, tmp_path):
        b, _ = self._setup_wiki(tmp_path)
        mock_git.side_effect = [
            _mock_subprocess(),  # fetch
            _mock_subprocess(returncode=1, stderr="fatal: bad revision"),  # rev-list fails
        ]
        ok, result = b._guard_unpushed()
        assert ok is True
        assert result is None


# ── Stage 6: _guard_uncommitted ────────────────────────────────────────


class TestGuardUncommitted:
    @patch("tools.pull.is_dirty", return_value=False)
    def test_clean_tree_passes(self, mock_dirty, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._guard_uncommitted()
        assert ok is True
        assert result is None

    @patch("tools.pull.is_dirty", return_value=True)
    def test_dirty_tree_blocks(self, mock_dirty, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._guard_uncommitted()
        assert ok is False
        assert result["status"] == "uncommitted_changes"
        assert "would discard" in result["message"]

    @patch("tools.pull.is_dirty", return_value=False)
    def test_no_wiki_git_passes(self, mock_dirty, tmp_path):
        b = _builder(root=tmp_path)
        ok, result = b._guard_uncommitted()
        assert ok is True
        assert result is None


# ── Stage 7: _fetch_and_merge ──────────────────────────────────────────


class TestFetchAndMerge:
    def _setup_wiki(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        return _builder(root=tmp_path), wiki

    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=False)
    def test_no_remote_skips_merge(self, mock_ref, mock_git, tmp_path):
        b, _ = self._setup_wiki(tmp_path)
        mock_git.return_value = _mock_subprocess()
        ok, result = b._fetch_and_merge()
        assert ok is True
        assert result is None

    # _fetch_and_merge delegates the actual merge to the shared
    # merge_remote_ref classifier (imported into tools.pull), so these tests
    # patch that seam and let the stage's own fetch/checkout run through the
    # mocked pull.run_git. The contract under test is unchanged: a clean
    # outcome → (True, None); a non-clean outcome → merge_conflict.
    @patch("tools.pull.merge_remote_ref", return_value=MERGE_CONFLICT)
    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=True)
    def test_merge_conflict_returns_error(self, mock_ref, mock_git, mock_merge, tmp_path):
        b, _ = self._setup_wiki(tmp_path)
        mock_git.side_effect = [
            _mock_subprocess(),  # fetch
            _mock_subprocess(),  # checkout -B
        ]
        ok, result = b._fetch_and_merge()
        assert ok is False
        assert result["status"] == "merge_conflict"
        assert result["resolve_action"] == "resolve_wiki_issue"

    @patch("tools.pull.merge_remote_ref", return_value=MERGE_CLEAN)
    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=True)
    def test_successful_merge(self, mock_ref, mock_git, mock_merge, tmp_path):
        b, _ = self._setup_wiki(tmp_path)
        mock_git.side_effect = [
            _mock_subprocess(),  # fetch
            _mock_subprocess(),  # checkout -B
        ]
        ok, result = b._fetch_and_merge()
        assert ok is True
        assert result is None

    @patch("tools.pull.merge_remote_ref", return_value=MERGE_CLEAN)
    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=True)
    def test_fetch_fallback_on_failure(self, mock_ref, mock_git, mock_merge, tmp_path):
        b, _ = self._setup_wiki(tmp_path)
        mock_git.side_effect = [
            _mock_subprocess(returncode=1, stderr="couldn't find remote ref"),  # fetch fails
            _mock_subprocess(),  # fallback fetch
            _mock_subprocess(),  # checkout -B
        ]
        ok, result = b._fetch_and_merge()
        assert ok is True
        assert result is None


# ── Stage 8: _sync_working_tree_and_unstash ────────────────────────────


class TestSyncWorkingTree:
    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=True)
    def test_syncs_when_no_errors(self, mock_ref, mock_git, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        b._sync_errors = []  # no errors
        ok, result = b._sync_working_tree_and_unstash()
        assert ok is True
        assert result is None
        assert b._remote_branch_existed is True
        # Should call read-tree + checkout
        calls = [c[0][0] for c in mock_git.call_args_list]
        assert any("read-tree" in c for c in calls)
        assert any("checkout" in c for c in calls)

    @patch("tools.pull.run_git")
    @patch("tools.pull.ref_exists", return_value=True)
    def test_skips_sync_on_errors(self, mock_ref, mock_git, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        b._sync_errors = ["fetch failed: ..."]
        ok, result = b._sync_working_tree_and_unstash()
        assert ok is True
        assert result is None


# ── Stage 9 & 10: _scaffold, _invalidate_cache ────────────────────────


class TestScaffold:
    @patch("tools.pull.scaffold_repo_wiki_if_empty", return_value=None)
    def test_no_scaffold_when_files_exist(self, mock_scaffold, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        b = _builder(root=tmp_path)
        ok, result = b._scaffold()
        assert ok is True
        assert result is None

    @patch("tools.pull.scaffold_repo_wiki_if_empty")
    def test_merges_scaffold_result(self, mock_scaffold, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()
        mock_scaffold.return_value = {
            "scaffolded": ["CLAUDE.md", "index.md"],
            "domains": ["test"],
        }
        b = _builder(root=tmp_path)
        ok, result = b._scaffold()
        assert ok is True
        assert result["scaffolded"] == ["CLAUDE.md", "index.md"]
        assert result["domains"] == ["test"]

    @patch("tools.pull.scaffold_repo_wiki_if_empty")
    def test_skips_when_no_wiki_git(self, mock_scaffold, tmp_path):
        b = _builder(root=tmp_path)
        ok, result = b._scaffold()
        assert ok is True
        assert result is None
        mock_scaffold.assert_not_called()


class TestInvalidateCache:
    @patch("tools.pull.WikiIndex")
    def test_invalidates_cache(self, mock_wiki_index_cls, tmp_path):
        b = _builder(root=tmp_path)
        ok, result = b._invalidate_cache()
        assert ok is True
        assert result is None
        mock_wiki_index_cls.invalidate.assert_called_once_with("myrepo", "develop")


# ── execute() orchestration ────────────────────────────────────────────


class TestExecute:
    def test_short_circuits_on_first_error(self, tmp_path):
        """execute() returns immediately when a stage returns (False, error)."""
        b = _builder(root=tmp_path)
        # Set up so _bootstrap fails
        with (
            patch.object(b, "_validate", return_value=(True, None)),
            patch.object(
                b, "_bootstrap", return_value=(False, {"status": "needs_setup", "error": "test"})
            ),
        ):
            result = b.execute()
            assert result["status"] == "needs_setup"

    @patch("tools.pull.merge_remote_ref", return_value=MERGE_CLEAN)
    @patch("tools.pull.run_git")
    @patch("tools.pull.submodule_exists", return_value=True)
    @patch("tools.pull.set_sparse_checkout_cone", return_value=True)
    @patch("tools.pull.ref_exists", side_effect=[False, True, True])
    @patch("tools.pull.is_dirty", return_value=False)
    @patch("tools.pull.scaffold_repo_wiki_if_empty", return_value=None)
    def test_full_execute_flow(
        self,
        mock_scaffold,
        mock_dirty,
        mock_ref,
        mock_scc,
        mock_sm,
        mock_git,
        mock_merge,
        tmp_path,
    ):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".git").mkdir()

        b = _builder(root=tmp_path)
        mock_git.return_value = _mock_subprocess()

        result = b.execute()
        assert result["status"] == "synced"
        assert result["repo"] == "myrepo"
        assert result["branch"] == "develop"
        assert "wiki_path" in result
        assert "path" in result

    def test_result_has_required_keys(self, tmp_path):
        """to_result() produces a dict with the standard keys when _accumulated is set."""
        b = _builder(root=tmp_path)
        b._repo_wiki_path = tmp_path / "wiki" / "myrepo" / "develop"
        b._bootstrapped = True
        b._remote_branch_existed = True
        b._accumulated = {
            "branch": b._branch,
            "repo": b._repo_name,
            "path": f"{b._repo_name}/{b._branch}/",
            "wiki_path": str(b._repo_wiki_path),
        }

        result = b.to_result()
        assert "branch" in result
        assert "repo" in result
        assert "path" in result
        assert "wiki_path" in result
        assert "status" in result
        assert "bootstrapped" in result
        assert "remote_branch_existed" in result
        assert result["status"] == "synced"
        assert result["bootstrapped"] is True

    def test_result_marks_sync_errors(self, tmp_path):
        b = _builder(root=tmp_path)
        b._repo_wiki_path = tmp_path / "wiki" / "myrepo" / "develop"
        b._sync_errors = ["fetch failed: network error"]
        result = b.to_result()
        assert result["status"] == "sync_errors"
        assert result["sync_errors"] == ["fetch failed: network error"]

    def test_for_repo_branch_chainable(self):
        b = PullBuilder()
        result = b.for_repo_branch("foo", "bar", "/some/path")
        assert result is b  # returns self for chaining
        assert b._repo_name == "foo"
        assert b._branch == "bar"
