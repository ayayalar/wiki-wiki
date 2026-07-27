"""Tests for verified security and correctness fixes.

Covers:
  C1: CVE-2025-48384 git version check (utils/git.py)
  C2: Guarded post-sync force checkout (tools/pull.py)
  H1: Clone failure handling during bootstrap (tools/pull.py)
  H2: Log.md merge formatting (tools/resolve.py)
  H3: -X theirs merge strategy (tools/resolve.py, tools/pull.py)
  H4: Pre-sync merge-only auto-resolve (tools/pull.py)
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch, MagicMock

from tests.conftest import _git

# ── C1: CVE-2025-48384 version check ───────────────────────────────────


class TestCVEVersionCheck:
    """Test CVE-2025-48384 git version detection."""

    def test_parse_version_standard(self):
        """Parse standard git version string."""
        from utils.git import _parse_git_version

        ver = _parse_git_version("git version 2.45.4")
        assert ver == (2, 45, 4)

    def test_parse_version_windows(self):
        """Parse Windows git version string."""
        from utils.git import _parse_git_version

        ver = _parse_git_version("git version 2.43.0.windows.1")
        assert ver == (2, 43, 0)

    def test_parse_version_unparseable(self):
        """Unparseable version returns None."""
        from utils.git import _parse_git_version

        assert _parse_git_version("not a version") is None
        assert _parse_git_version("") is None

    def test_patched_version_v2454(self):
        """Git 2.45.4 is patched for CVE-2025-48384."""
        from utils.git import _git_is_patched_for_cve_2025_48384

        with patch("utils.git.run_git") as mock_git:
            mock_git.return_value = MagicMock(stdout="git version 2.45.4\n")
            assert _git_is_patched_for_cve_2025_48384() is True

    def test_vulnerable_version_v2453(self):
        """Git 2.45.3 is NOT patched for CVE-2025-48384."""
        from utils.git import _git_is_patched_for_cve_2025_48384

        with patch("utils.git.run_git") as mock_git:
            mock_git.return_value = MagicMock(stdout="git version 2.45.3\n")
            assert _git_is_patched_for_cve_2025_48384() is False

    def test_vulnerable_version_v2436(self):
        """Git 2.43.6 is NOT patched (needs 2.43.7+)."""
        from utils.git import _git_is_patched_for_cve_2025_48384

        with patch("utils.git.run_git") as mock_git:
            mock_git.return_value = MagicMock(stdout="git version 2.43.6\n")
            assert _git_is_patched_for_cve_2025_48384() is False

    def test_patched_version_v2437(self):
        """Git 2.43.7 IS patched."""
        from utils.git import _git_is_patched_for_cve_2025_48384

        with patch("utils.git.run_git") as mock_git:
            mock_git.return_value = MagicMock(stdout="git version 2.43.7\n")
            assert _git_is_patched_for_cve_2025_48384() is True

    def test_patched_version_v2501(self):
        """Git 2.50.1 IS patched."""
        from utils.git import _git_is_patched_for_cve_2025_48384

        with patch("utils.git.run_git") as mock_git:
            mock_git.return_value = MagicMock(stdout="git version 2.50.1\n")
            assert _git_is_patched_for_cve_2025_48384() is True

    def test_unknown_minor_version_assumes_ok(self):
        """Unknown minor version (e.g. 2.60.0) assumes OK (not blocking)."""
        from utils.git import _git_is_patched_for_cve_2025_48384

        with patch("utils.git.run_git") as mock_git:
            mock_git.return_value = MagicMock(stdout="git version 2.60.0\n")
            assert _git_is_patched_for_cve_2025_48384() is True

    def test_check_git_version_returns_none_when_safe(self):
        """check_git_version returns None when git is patched."""
        from utils.git import check_git_version

        with patch("utils.git._git_is_patched_for_cve_2025_48384", return_value=True):
            assert check_git_version() is None

    def test_check_git_version_returns_warning_when_vulnerable(self):
        """check_git_version returns warning dict when git is vulnerable."""
        from utils.git import check_git_version

        with patch("utils.git._git_is_patched_for_cve_2025_48384", return_value=False):
            with patch("utils.git.run_git") as mock_git:
                mock_git.return_value = MagicMock(stdout="git version 2.45.3\n")
                result = check_git_version()
                assert result is not None
                assert result["status"] == "cve_warning"
                assert result["cve"] == "CVE-2025-48384"
                assert "2.45.3" in result["installed_version"]
                assert "GHSA-992w-73f5-x28c" in result["message"]


# ── PAT auth injection for git remotes ──────────────────────────────────


class TestRemotePatAuth:
    """Test PAT env vars are honored for HTTPS remotes in run_git()."""

    def test_run_git_injects_pat_header_for_https_origin(
        self, tmp_path: Path, monkeypatch
    ):
        from utils.git import run_git

        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init"], cwd=repo)
        _git(["remote", "add", "origin", "https://github.com/acme/private-wiki.git"], cwd=repo)

        monkeypatch.setenv("WIKI_MCP_REMOTE_PAT", "pat_123")
        monkeypatch.delenv("WIKI_MCP_REMOTE_USERNAME", raising=False)

        captured: dict = {}

        class DummyProc:
            pid = 123
            returncode = 0

            def wait(self, timeout=None):
                return None

        def _fake_popen(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return DummyProc()

        with patch("utils.git.subprocess.Popen", side_effect=_fake_popen):
            run_git(["fetch", "origin", "wiki"], cwd=repo, check=False, timeout=5)

        env = captured["kwargs"]["env"]
        expected = base64.b64encode(b"x-access-token:pat_123").decode("ascii")

        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"

    def test_run_git_does_not_inject_pat_for_ssh_origin(self, tmp_path: Path, monkeypatch):
        from utils.git import run_git

        repo = tmp_path / "repo_ssh"
        repo.mkdir()
        _git(["init"], cwd=repo)
        _git(["remote", "add", "origin", "git@github.com:acme/private-wiki.git"], cwd=repo)

        monkeypatch.setenv("WIKI_MCP_REMOTE_PAT", "pat_123")
        monkeypatch.setenv("WIKI_MCP_REMOTE_USERNAME", "x-access-token")

        captured: dict = {}

        class DummyProc:
            pid = 123
            returncode = 0

            def wait(self, timeout=None):
                return None

        def _fake_popen(*args, **kwargs):
            captured["kwargs"] = kwargs
            return DummyProc()

        with patch("utils.git.subprocess.Popen", side_effect=_fake_popen):
            run_git(["fetch", "origin", "wiki"], cwd=repo, check=False, timeout=5)

        env = captured["kwargs"]["env"]
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env

    def test_run_git_does_not_inject_pat_for_file_scheme_url(self, tmp_path: Path, monkeypatch):
        from utils.git import run_git

        remote_repo = tmp_path / "remote.git"
        _git(["init", "--bare", str(remote_repo)], cwd=tmp_path)

        repo = tmp_path / "repo_file"
        repo.mkdir()
        _git(["init"], cwd=repo)
        _git(["remote", "add", "origin", remote_repo.as_uri()], cwd=repo)

        monkeypatch.setenv("WIKI_MCP_REMOTE_PAT", "pat_123")
        monkeypatch.setenv("WIKI_MCP_REMOTE_USERNAME", "x-access-token")

        captured: dict = {}

        class DummyProc:
            pid = 123
            returncode = 0

            def wait(self, timeout=None):
                return None

        def _fake_popen(*args, **kwargs):
            captured["kwargs"] = kwargs
            return DummyProc()

        with patch("utils.git.subprocess.Popen", side_effect=_fake_popen):
            run_git(["fetch", "origin", "wiki"], cwd=repo, check=False, timeout=5)

        env = captured["kwargs"]["env"]
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env


# ── C2: Guarded force checkout ─────────────────────────────────────────


class TestGuardedForceCheckout:
    """Test C2: force checkout only runs when sync succeeds."""

    def test_force_checkout_guarded_on_sync_errors(self, code_repo: Path, monkeypatch):
        """When fetch fails, read-tree and checkout --force must NOT run."""
        from tools.pull import pull

        # Bootstrap first
        result = pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))
        assert result["status"] == "synced"

        # Track git calls to detect read-tree / checkout --force after fetch failure
        call_log: list[list[str]] = []
        fetch_failed = False

        def side_effect(args, **kwargs):
            nonlocal fetch_failed
            call_log.append(list(args))
            if "fetch" in args:
                fetch_failed = True
                fake = MagicMock()
                fake.returncode = 1
                fake.stderr = "simulated fetch failure"
                return fake
            # For all other calls, return success so pull() continues
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = ""
            fake.stderr = ""
            return fake

        import config

        config._wiki_path_cache = None
        config._repo_root_cache.clear()
        from utils import git as git_mod

        git_mod._repo_name_cache.clear()

        # Patch where pull() imports run_git (tools.pull module level)
        with patch("tools.pull.run_git", side_effect=side_effect):
            result = pull(repo_name="myrepo", branch="develop", repo_path=str(code_repo))

        # Verify fetch failed
        assert fetch_failed
        # With C2 fix, read-tree and checkout --force must NOT appear after fetch failure
        destructive_calls = [
            c for c in call_log if ("read-tree" in c or ("checkout" in c and "--force" in c))
        ]
        assert len(destructive_calls) == 0, (
            f"C2 fix not working: read-tree/checkout --force ran after fetch failure: {destructive_calls}"
        )


# ── H1: Clone failure handling ──────────────────────────────────────────


class TestBootstrapCloneFailure:
    """Test H1: clone failure returns bootstrap_failed with remote URL context."""

    def test_bootstrap_fails_on_clone_error(self, tmp_path: Path, bare_wiki: Path, monkeypatch):
        """_bootstrap_clone returns error when git clone fails."""
        from tools.pull import _bootstrap_clone

        root = tmp_path / "code"
        root.mkdir()
        from tests.conftest import _git

        _git(["init"], cwd=root)
        _git(["config", "user.name", "Test"], cwd=root)
        _git(["config", "user.email", "test@test.com"], cwd=root)
        (root / "README.md").write_text("test")
        _git(["add", "."], cwd=root)
        _git(["commit", "-m", "initial"], cwd=root)

        url = bare_wiki.as_uri()
        monkeypatch.setenv("WIKI_MCP_REMOTE_URL", url)

        # Track call sequence to simulate: ls-remote OK, clone FAIL
        call_sequence = [0]

        def side_effect(args, **kwargs):
            idx = call_sequence[0]
            call_sequence[0] += 1
            fake = MagicMock()
            if idx == 0:
                # ls-remote preflight → success
                fake.returncode = 0
                fake.stdout = "abc123 refs/heads/wiki"
                fake.stderr = ""
            elif idx == 1:
                # clone → failure
                fake.returncode = 1
                fake.stdout = ""
                fake.stderr = "clone failed"
            else:
                fake.returncode = 0
                fake.stdout = ""
                fake.stderr = ""
            return fake

        # Patch at the module level where _bootstrap_clone imports it
        with patch("tools.pull.run_git", side_effect=side_effect):
            result = _bootstrap_clone(root, url, repo_name="myrepo", branch="master")

        assert result is not None
        assert result["status"] == "bootstrap_failed"
        assert "git clone failed" in result["error"]
        assert "Original git stderr: clone failed" in result["error"]
        assert result["remote_url"] == url


# ── H2: Log merge formatting ───────────────────────────────────────────


class TestLogMergeFormatting:
    """Test H2: clean markdown output from log merge."""

    def test_merge_log_file_produces_clean_markdown(self, tmp_path: Path):
        """_merge_log_file produces clean markdown with proper separators."""
        from tools.resolve import _merge_log_file

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        filepath = "repo/branch/log.md"
        (wiki / "repo" / "branch").mkdir(parents=True)
        log_file = wiki / filepath

        # Simulate a conflicted log file
        log_file.write_text(
            "# Log\n\n"
            "<<<<<<< ours\n"
            "## [2024-01-01] Local entry\n"
            "=======\n"
            "## [2024-01-02] Remote entry\n"
            ">>>>>>> theirs\n"
        )

        _merge_log_file(wiki, filepath)
        content = log_file.read_text()

        # Should have clean header + entries separated by blank line
        assert "# Log" in content
        assert "<<<<<<" not in content  # no conflict markers
        assert "## [2024-01-01] Local entry" in content
        assert "## [2024-01-02] Remote entry" in content
        # Entries separated by \n\n (single blank line)
        assert (
            "\n\n".join(["## [2024-01-01] Local entry", "## [2024-01-02] Remote entry"]) in content
        )


# ── H3: Merge strategy (preserve local changes) ─────────────────────────


class TestMergeStrategy:
    """Test H3: diverged:merge preserves local changes via resolve_merge."""

    def test_resolve_diverged_merge_no_x_theirs(self, code_repo: Path):
        """diverged:merge does NOT use -X theirs (which would discard local changes)."""
        import inspect
        from tools import resolve as resolve_mod

        handler = resolve_mod._ACTION_HANDLERS.get("diverged:merge")
        assert handler is not None, "diverged:merge handler not registered"
        # The handler delegates the actual merge to the shared merge_and_autoresolve
        # / merge_remote_ref helpers, so inspect the whole merge code path.
        source = (
            inspect.getsource(handler)
            + inspect.getsource(resolve_mod.merge_and_autoresolve)
            + inspect.getsource(resolve_mod.merge_remote_ref)
        )
        # -X theirs must not appear (it silently discards local content)
        assert '"-X"' not in source and "'-X'" not in source, (
            "diverged:merge must not use -X theirs — it discards local changes"
        )
        # --allow-unrelated-histories must still be present for shallow clone support
        assert (
            '"--allow-unrelated-histories"' in source or "'--allow-unrelated-histories'" in source
        ), "diverged:merge must still use --allow-unrelated-histories for shallow clone support"

    def test_pull_merge_surfaces_conflicts(self):
        """pull.py does NOT auto-resolve merge conflicts (-X theirs removed).
        Conflicts are surfaced to resolve_wiki_issue instead."""
        import inspect
        from tools import pull as pull_mod

        source = inspect.getsource(pull_mod.PullBuilder._fetch_and_merge)
        # Should NOT use -X theirs (conflicts go to resolve_wiki_issue)
        assert '"-X"' not in source and "'-X'" not in source, (
            "pull merge should NOT use -X theirs (conflicts delegated to resolve_wiki_issue)"
        )
        # Should surface merge_conflict status
        assert '"merge_conflict"' in source or "'merge_conflict'" in source, (
            "pull should return merge_conflict status for unresolved conflicts"
        )
        assert '"resolve_action"' in source or "'resolve_action'" in source, (
            "pull should return resolve_action for conflict resolution"
        )


# ── H4: Pre-sync merge completion (finish_merge, not auto-resolve) ──────


class TestPreSyncAutoResolve:
    """Test H4: finish_merge completes resolved merges before sync, no auto-commit."""

    def test_finish_merge_called_before_sync(self, code_repo: Path):
        """finish_merge is called before fetch/merge to complete resolved merges."""
        import inspect
        from tools import pull as pull_mod

        source = inspect.getsource(pull_mod.PullBuilder.execute)

        # finish_merge should appear before fetch in the execute() stage list
        fmi_idx = source.index("_finish_in_progress_merge")
        fetch_idx = source.index("_fetch_and_merge")

        assert fmi_idx < fetch_idx, (
            "finish_merge must be called BEFORE fetch to complete resolved merges"
        )

    def test_dirty_blocks_pull(self, code_repo: Path):
        """Dirty state causes pull to block, never auto-commit or stash."""
        import inspect
        from tools import pull as pull_mod

        source = inspect.getsource(pull_mod.PullBuilder._guard_uncommitted)

        # Should NOT auto-commit dirty state
        assert "auto-commit pending changes" not in source, (
            "pull should NOT auto-commit dirty state"
        )
        # Should NOT stash dirty state
        assert "stash" not in source.lower(), "pull should not stash dirty state"
        # Should block with uncommitted_changes
        assert "uncommitted_changes" in source, "pull should return uncommitted_changes status"


# ── Bootstrap guard: wrong repo_path ────────────────────────────────────


class TestBootstrapRefusedWrongRepoPath:
    """Guard: refuse to bootstrap when repo_path points to a different repo than the server root."""

    def test_bootstrap_refused_wrong_repo_path(
        self, bare_wiki: Path, code_repo: Path, tmp_path: Path, monkeypatch
    ):
        """pull() returns bootstrap_refused when WIKI_MCP_REPO_ROOT is set and repo_path differs."""
        from tools.pull import pull

        # Set WIKI_MCP_REPO_ROOT to code_repo (the intended root).
        monkeypatch.setenv("WIKI_MCP_REPO_ROOT", str(code_repo))

        # Create a second unrelated repo to simulate the wrong-repo scenario.
        other_repo = tmp_path / "other_repo"
        other_repo.mkdir()
        import subprocess

        subprocess.run(["git", "init", str(other_repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=str(other_repo),
            check=True,
            capture_output=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

        # LLM passes wrong repo_path → should be blocked.
        result = pull(repo_path=str(other_repo))

        assert result["status"] == "bootstrap_refused", (
            f"Expected bootstrap_refused when repo_path differs from WIKI_MCP_REPO_ROOT, got: {result}"
        )
        assert "WIKI_MCP_REPO_ROOT" in result["error"], (
            f"Error message should mention WIKI_MCP_REPO_ROOT: {result['error']}"
        )
