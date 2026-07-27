"""Tests for session-derived fixes: codec errors, missing wiki_path, content corruption."""

from __future__ import annotations

from pathlib import Path


# ── Codec error fixes (lint.py, utils/wiki.py) ──────────────────────────────


class TestCodecErrorHandling:
    """Fix 1 & 2: lint and get_log_tail handle non-UTF-8 gracefully."""

    def test_lint_handles_non_utf8_files(self, code_repo: Path, bare_wiki: Path):
        """lint_wiki doesn't crash when wiki files contain non-UTF-8 bytes."""
        from tools.pull import pull
        from tools.lint import lint

        # Bootstrap wiki
        pull(repo_name="myrepo", branch="master", repo_path=str(code_repo))

        # Create a wiki file with non-UTF-8 bytes
        wiki = code_repo / "wiki" / "myrepo" / "master"
        services = wiki / "services"
        services.mkdir(parents=True, exist_ok=True)
        index_file = services / "index.md"
        
        # Write valid UTF-8 with a non-UTF-8 byte sequence
        with open(index_file, "wb") as f:
            f.write(b"# Services\n\nSome text with invalid byte: \xff\n")

        # lint_wiki should not crash
        result = lint(repo_name="myrepo", branch="master", repo_path=str(code_repo))
        
        assert result["repo"] == "myrepo"
        assert "domain_indexes" in result
        # The non-UTF-8 byte should be replaced with U+FFFD (�)
        # Check that services/index.md is present (either with / or \ separator)
        keys = list(result["domain_indexes"].keys())
        services_keys = [k for k in keys if "services" in k and "index.md" in k]
        assert len(services_keys) == 1, f"Expected one services/index.md key, got: {keys}"
        
        content = result["domain_indexes"][services_keys[0]]
        assert "Services" in content
        # Should not crash, and should have read the file (even if corrupted byte is replaced)

    def test_get_log_tail_handles_non_utf8(self, code_repo: Path, bare_wiki: Path):
        """get_log_tail doesn't crash when log.md contains non-UTF-8 bytes."""
        from tools.pull import pull
        from utils.wiki import get_log_tail

        # Bootstrap wiki
        pull(repo_name="myrepo", branch="master", repo_path=str(code_repo))

        wiki = code_repo / "wiki" / "myrepo" / "master"
        log_file = wiki / "log.md"
        
        # Write log with non-UTF-8 byte
        with open(log_file, "wb") as f:
            f.write(b"# Wiki Log\n\n## [2024-01-01] test | Some entry \xff\n")

        # Should not crash
        tail = get_log_tail(log_file, 5)
        assert "test" in tail or "entry" in tail


# ── Missing wiki_path in responses ───────────────────────────────────────────


class TestWikiPathInResponses:
    """Fix 3 & 4: ingest/push include wiki_path when no changes/nothing to commit."""

    def test_ingest_no_changes_includes_wiki_path(self, code_repo: Path, bare_wiki: Path):
        """ingest_wiki 'no changes' response includes wiki_path, index, log_tail."""
        from tools.pull import pull
        from tools.ingest import ingest
        from tools.push import push
        import subprocess
        import os

        # Bootstrap wiki
        pull(repo_name="myrepo", branch="master", repo_path=str(code_repo))
        
        # Create actual wiki content (not just scaffold)
        wiki = code_repo / "wiki" / "myrepo" / "master"
        src_dir = wiki / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "index.md").write_text("# src\n\nApplication source code.\n")
        
        # Update root index with domain entry
        (wiki / "index.md").write_text(
            "# myrepo — Wiki Index\n\n"
            "## [src](src/index.md) — Application source code\n"
        )
        
        # Update log
        (wiki / "log.md").write_text(
            "# Wiki Log\n\n"
            "## [2024-01-01] ingest | Initial wiki creation\n"
        )
        
        # Push the wiki content
        push(repo_name="myrepo", branch="master", repo_path=str(code_repo), confirm=True)
        
        # Make a trivial commit in code repo (no actual file changes)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "trigger"],
            cwd=str(code_repo),
            check=True,
            capture_output=True,
            env=env,
        )

        # Now ingest with no code changes (comparing last commit to previous, both empty)
        result = ingest(repo_name="myrepo", branch="master", repo_path=str(code_repo))

        assert result["status"] == "no changes", f"Expected 'no changes', got: {result}"
        assert "wiki_path" in result, "ingest 'no changes' should include wiki_path"
        assert "index" in result, "ingest 'no changes' should include index"
        assert "log_tail" in result, "ingest 'no changes' should include log_tail"
        assert "instruction" in result, "ingest 'no changes' should include instruction"

    def test_push_nothing_to_commit_includes_wiki_path(self, code_repo: Path, bare_wiki: Path):
        """push_wiki 'nothing to commit' response includes wiki_path."""
        from tools.pull import pull
        from tools.push import push

        # Bootstrap wiki
        pull(repo_name="myrepo", branch="master", repo_path=str(code_repo))
        
        # Push the scaffold
        push(repo_name="myrepo", branch="master", repo_path=str(code_repo), confirm=True)

        # Push again with no changes
        result = push(repo_name="myrepo", branch="master", repo_path=str(code_repo), confirm=True)

        assert result["status"] == "nothing to commit"
        assert "wiki_path" in result, "push 'nothing to commit' should include wiki_path"


# ── Content corruption detection (resolve.py) ────────────────────────────────


class TestContentCorruptionDetection:
    """Fix 5: resolve_wiki_issue detects non-UTF-8 files and offers sanitize."""

    def test_resolve_detects_corrupted_files(self, code_repo: Path, bare_wiki: Path):
        """resolve_wiki_issue detects wiki files with non-UTF-8 content."""
        from tools.pull import pull
        from tools.resolve import resolve

        # Bootstrap wiki
        pull(repo_name="myrepo", branch="master", repo_path=str(code_repo))

        # Create a corrupted file
        wiki = code_repo / "wiki" / "myrepo" / "master"
        services = wiki / "services"
        services.mkdir(parents=True, exist_ok=True)
        corrupted = services / "index.md"
        
        with open(corrupted, "wb") as f:
            f.write(b"# Corrupted\n\nBad byte: \xff\n")

        # Diagnose should detect corruption
        result = resolve(repo_name="myrepo", branch="master", repo_path=str(code_repo))

        assert result["status"] == "diagnosis"
        issues = result["issues"]
        
        # Should have wiki_content_corrupt issue
        corrupt_issues = [i for i in issues if i["issue"] == "wiki_content_corrupt"]
        assert len(corrupt_issues) == 1, f"Expected 1 corrupt issue, got: {issues}"
        
        issue = corrupt_issues[0]
        assert "non-UTF-8" in issue["description"]
        assert "corrupted_files" in issue
        assert len(issue["corrupted_files"]) > 0
        
        # Should offer sanitize resolution
        resolutions = issue["resolutions"]
        assert len(resolutions) == 1
        assert resolutions[0]["id"] == "wiki_content_corrupt:sanitize"

    def test_resolve_sanitize_fixes_corrupted_files(self, code_repo: Path, bare_wiki: Path):
        """resolve_wiki_issue with sanitize action fixes non-UTF-8 files."""
        from tools.pull import pull
        from tools.resolve import resolve

        # Bootstrap wiki
        pull(repo_name="myrepo", branch="master", repo_path=str(code_repo))

        wiki = code_repo / "wiki" / "myrepo" / "master"
        services = wiki / "services"
        services.mkdir(parents=True, exist_ok=True)
        corrupted = services / "index.md"
        
        with open(corrupted, "wb") as f:
            f.write(b"# Test\n\nBad: \xff\n")

        # Execute sanitize
        result = resolve(
            action="wiki_content_corrupt:sanitize",
            repo_name="myrepo",
            branch="master",
            repo_path=str(code_repo),
        )

        assert result["status"] == "resolved"
        assert "sanitized" in result["message"].lower()
        assert "sanitized_files" in result
        
        # File should now be valid UTF-8
        content = corrupted.read_text(encoding="utf-8", errors="strict")
        assert "Test" in content
        # The bad byte should be replaced with U+FFFD
        assert "\ufffd" in content or "�" in content
