"""Tests for list_remote_wiki — verifies it reads from origin/wiki after fetch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.list import ListBuilder


def _mock_subprocess(stdout="", stderr="", returncode=0):
    cp = MagicMock()
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


def _builder(root: Path) -> ListBuilder:
    return ListBuilder().for_params(repo_path=str(root))


class TestFetchRemote:
    @patch("tools.list.run_git")
    @patch("tools.list.read_remote_url", return_value="https://example.com/wiki.git")
    def test_fetches_into_origin_wiki(self, mock_url, mock_git, tmp_path):
        """_fetch_remote should fetch into refs/remotes/origin/wiki."""
        mock_git.return_value = _mock_subprocess(returncode=0)
        builder = _builder(tmp_path)
        builder._wiki = tmp_path / "wiki"
        builder._remote_url = "https://example.com/wiki.git"

        builder._fetch_remote()

        call_args = mock_git.call_args
        cmd = call_args[0][0]
        assert "fetch" in cmd
        assert "+refs/heads/wiki:refs/remotes/origin/wiki" in cmd


class TestListRepoNames:
    @patch("tools.list.run_git")
    @patch("tools.list.read_remote_url", return_value="https://example.com/wiki.git")
    def test_reads_from_origin_wiki(self, mock_url, mock_git, tmp_path):
        """_list_repo_names must read origin/wiki, NOT local wiki branch."""
        mock_git.return_value = _mock_subprocess(stdout="repo-a\nrepo-b\n")
        builder = _builder(tmp_path)
        builder._wiki = tmp_path / "wiki"
        builder._remote_url = "https://example.com/wiki.git"

        builder._list_repo_names()

        call_args = mock_git.call_args
        cmd = call_args[0][0]
        assert "origin/wiki" in cmd, "Must read from origin/wiki, not local wiki branch"
        assert builder._repo_names == ["repo-a", "repo-b"]

    @patch("tools.list.run_git")
    @patch("tools.list.read_remote_url", return_value="https://example.com/wiki.git")
    def test_filters_by_pattern(self, mock_url, mock_git, tmp_path):
        """Pattern filtering should work with origin/wiki."""
        mock_git.return_value = _mock_subprocess(stdout="repo-alpha\nrepo-beta\nfoo-bar\n")
        builder = _builder(tmp_path).for_params(pattern="alpha", repo_path=str(tmp_path))
        builder._wiki = tmp_path / "wiki"
        builder._remote_url = "https://example.com/wiki.git"

        builder._list_repo_names()

        assert builder._repo_names == ["repo-alpha"]


class TestListBranches:
    @patch("tools.list.run_git")
    @patch("tools.list.read_remote_url", return_value="https://example.com/wiki.git")
    def test_reads_branches_from_origin_wiki(self, mock_url, mock_git, tmp_path):
        """_list_branches must read origin/wiki:repo_name, not wiki:repo_name."""
        mock_git.return_value = _mock_subprocess(stdout="main\ndevelop\n")
        builder = _builder(tmp_path)
        builder._wiki = tmp_path / "wiki"
        builder._remote_url = "https://example.com/wiki.git"
        builder._repo_names = ["repo-a"]

        builder._list_branches()

        call_args = mock_git.call_args
        cmd = call_args[0][0]
        assert "origin/wiki:repo-a" in cmd, "Must read from origin/wiki, not local wiki branch"


class TestExecute:
    @patch("tools.list.run_git")
    @patch("tools.list.read_remote_url", return_value="https://example.com/wiki.git")
    @patch("tools.list.wiki_is_initialized", return_value=True)
    def test_full_flow_uses_origin_wiki(self, mock_init, mock_url, mock_git, tmp_path):
        """Full execute() flow must read from origin/wiki after fetch."""
        mock_git.return_value = _mock_subprocess(stdout="repo-a\n")
        builder = _builder(tmp_path)
        builder._wiki = tmp_path / "wiki"

        result = builder.execute()

        # Verify ls-tree calls use origin/wiki
        ls_tree_calls = [c[0][0] for c in mock_git.call_args_list if "ls-tree" in c[0][0]]
        for cmd in ls_tree_calls:
            assert any("origin/wiki" in arg for arg in cmd), (
                f"ls-tree must reference origin/wiki: {cmd}"
            )
        assert result["count"] == 1
