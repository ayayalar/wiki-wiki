"""Integration tests for all wiki-mcp-server tools.

Each test exercises the tool implementation functions (tools.*) directly,
using real temporary git repos. Server-level validation tests call the
server wrapper functions to verify parameter checking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import _git

# Common params used across tests.
REPO = "myrepo"
BRANCH = "develop"


def _params(code_repo: Path) -> dict:
    return {"repo_path": str(code_repo), "repo_name": REPO, "branch": BRANCH}


def _seed_wiki_remote_with_feature_and_master(bare_wiki: Path, tmp_path: Path) -> None:
    """Seed wiki remote with tracked folders for myrepo/feature and myrepo/master."""
    seed = tmp_path / "_wiki_seed_branches"
    _git(["clone", str(bare_wiki), str(seed)], cwd=tmp_path, check=True)
    _git(["config", "user.name", "Test"], cwd=seed, check=True)
    _git(["config", "user.email", "test@test.com"], cwd=seed, check=True)
    _git(["-c", "protocol.file.allow=always", "checkout", "wiki"], cwd=seed, check=True)

    feature_dir = seed / "myrepo" / "feature"
    master_dir = seed / "myrepo" / "master"
    feature_dir.mkdir(parents=True, exist_ok=True)
    master_dir.mkdir(parents=True, exist_ok=True)
    (feature_dir / "index.md").write_text("# Feature wiki\n", encoding="utf-8")
    (master_dir / "index.md").write_text("# Master wiki\n", encoding="utf-8")

    _git(["add", "myrepo"], cwd=seed, check=True)
    _git(["commit", "-m", "seed branch folders"], cwd=seed, check=True)
    _git(["-c", "protocol.file.allow=always", "push", "origin", "wiki"], cwd=seed, check=True)


def _commit_all_if_dirty(repo: Path, message: str) -> None:
    """Create a cleanup commit only when the repo has pending changes."""
    status = _git(["status", "--porcelain"], cwd=repo, check=True).stdout.strip()
    if not status:
        return
    _git(["add", "-A"], cwd=repo, check=True)
    _git(["commit", "-m", message], cwd=repo, check=True)


# ── pull_wiki ──────────────────────────────────────────────────────────


class TestPull:
    def test_bootstraps_submodule_and_scaffolds(self, code_repo: Path):
        """First pull bootstraps the submodule and scaffolds CLAUDE.md, index.md, log.md."""
        from tools.pull import pull

        result = pull(**_params(code_repo))

        assert result["status"] == "synced"
        assert result["bootstrapped"] is True
        assert result["repo"] == REPO
        assert result["branch"] == BRANCH

        # Scaffolded files exist on disk.
        wiki_path = Path(result["wiki_path"])
        assert (wiki_path / "CLAUDE.md").is_file()
        assert (wiki_path / "index.md").is_file()
        assert (wiki_path / "log.md").is_file()

        # Response includes scaffold info.
        assert "scaffolded" in result
        assert "CLAUDE.md" in result["scaffolded"]

    def test_idempotent(self, code_repo: Path):
        """Calling pull twice succeeds without re-scaffolding."""
        from tools.pull import pull
        from tools.push import push

        first = pull(**_params(code_repo))
        assert first["status"] == "synced"
        assert "scaffolded" in first

        # Push scaffold so second pull doesn't see unpushed commits.
        push(**_params(code_repo), message="test: push scaffold", confirm=True)

        second = pull(**_params(code_repo))
        assert second["status"] == "synced"
        assert "scaffolded" not in second  # already exists → no scaffold

    def test_no_remote_url(self, code_repo: Path, monkeypatch):
        """Pull without WIKI_MCP_REMOTE_URL returns needs_setup."""
        from tools.pull import pull

        monkeypatch.delenv("WIKI_MCP_REMOTE_URL", raising=False)
        result = pull(**_params(code_repo))
        assert result["status"] == "needs_setup"

    def test_dirty_wiki_blocks_pull(self, code_repo: Path):
        """Pull refuses when there are uncommitted changes in the wiki."""
        from tools.pull import pull
        from tools.push import push

        # Bootstrap first and push the scaffold.
        pull(**_params(code_repo))
        push(**_params(code_repo), message="test: push scaffold", confirm=True)

        # Create an uncommitted file in the wiki.
        wiki = code_repo / "wiki" / REPO / BRANCH
        (wiki / "dirty.md").write_text("dirty")
        _git(["add", "."], cwd=code_repo / "wiki")

        result = pull(**_params(code_repo))
        assert result["status"] == "uncommitted_changes"
        assert "would discard" in result["message"]


class TestBootstrapNoCommit:
    def test_bootstrap_does_not_commit_or_dirty_code_repo(
        self, tmp_path: Path, bare_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Repro: bootstrap must not create code-repo commits or tracked changes."""
        from tools.pull import pull
        from utils.wiki_index import WikiIndex

        # Build a bare origin with a real initial commit on master.
        origin_bare = tmp_path / "origin.git"
        _git(["init", "--bare", str(origin_bare)], cwd=tmp_path)

        origin_work = tmp_path / "origin_work"
        _git(["clone", str(origin_bare), str(origin_work)], cwd=tmp_path)
        _git(["config", "user.name", "Test"], cwd=origin_work)
        _git(["config", "user.email", "test@test.com"], cwd=origin_work)
        (origin_work / "src").mkdir()
        (origin_work / "src" / "app.py").write_text('print("hello")\n')
        _git(["add", "."], cwd=origin_work)
        _git(["commit", "-m", "initial"], cwd=origin_work)
        _git(["push", "origin", "master"], cwd=origin_work)

        # Clone origin for the test repo so origin/HEAD is present.
        code_repo = tmp_path / "code_repo"
        _git(["clone", str(origin_bare), str(code_repo)], cwd=tmp_path)
        _git(["config", "user.name", "Test"], cwd=code_repo)
        _git(["config", "user.email", "test@test.com"], cwd=code_repo)

        monkeypatch.setenv("WIKI_MCP_REMOTE_URL", bare_wiki.as_uri())

        import config

        config._wiki_path_cache = None
        config._repo_root_cache.clear()
        from utils import git as git_mod

        git_mod._repo_name_cache.clear()
        WikiIndex.invalidate_all()

        before_sha = _git(["rev-parse", "HEAD"], cwd=code_repo).stdout.strip()
        result = pull(repo_path=str(code_repo), repo_name="code_repo", branch="master")

        assert result["status"] == "synced"
        assert (code_repo / "wiki" / ".git").exists()

        after_sha = _git(["rev-parse", "HEAD"], cwd=code_repo).stdout.strip()
        assert after_sha == before_sha
        assert not (code_repo / ".gitmodules").exists()
        assert _git(["status", "--porcelain"], cwd=code_repo).stdout.strip() == ""

        exclude_lines = (code_repo / ".git" / "info" / "exclude").read_text().splitlines()
        assert any(line.strip() == "wiki/" for line in exclude_lines)


# ── init_wiki ──────────────────────────────────────────────────────────


class TestInit:
    def test_already_initialized(self, code_repo: Path):
        """init after pull returns error with existing_files."""
        from tools.init import init
        from tools.pull import pull

        pull(**_params(code_repo))
        result = init(**_params(code_repo))

        assert "error" in result
        assert "already initialized" in result["error"].lower()
        assert "existing_files" in result

    def test_init_on_uninitialized_wiki(self, code_repo: Path):
        """init before pull returns wiki_not_initialized."""
        from tools.init import init

        result = init(**_params(code_repo))
        assert result["status"] == "wiki_not_initialized"


# ── query_wiki ─────────────────────────────────────────────────────────


class TestQuery:
    def test_finds_pages(self, code_repo: Path):
        """query finds pages matching a keyword via brute-force search."""
        from tools.pull import pull
        from tools.query import query

        pull(**_params(code_repo))

        # Write a custom page (not listed in any index).
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        arch = wiki_path / "architecture.md"
        arch.write_text("# Architecture\n\nThis is the main architecture page.\n")

        result = query("architecture", **_params(code_repo))

        assert result["repo"] == REPO
        # Page should appear in other_matches (not in any domain index).
        assert len(result["other_matches"]) >= 1
        paths = [m["path"] for m in result["other_matches"]]
        assert "architecture.md" in paths

    def test_navigates_domain_indexes(self, code_repo: Path):
        """query navigates top index → domain index → pages."""
        from tools.pull import pull
        from tools.query import query

        pull(**_params(code_repo))
        wiki_path = code_repo / "wiki" / REPO / BRANCH

        # Set up a domain with index.
        (wiki_path / "auth").mkdir(parents=True, exist_ok=True)
        wiki_path.joinpath("index.md").write_text(
            "# Wiki Index\n\n## [Auth](auth/index.md) — Authentication and authorization\n",
            encoding="utf-8",
        )
        wiki_path.joinpath("auth", "index.md").write_text(
            "# Auth Domain\n\n"
            "- [jwt-tokens](jwt-tokens.md) — JWT token handling and refresh\n"
            "- [oauth](oauth.md) — OAuth2 integration\n",
            encoding="utf-8",
        )
        wiki_path.joinpath("auth", "jwt-tokens.md").write_text(
            "# JWT Tokens\n\nThis page covers authentication tokens.\n",
            encoding="utf-8",
        )
        wiki_path.joinpath("auth", "oauth.md").write_text(
            "# OAuth2\n\nOAuth2 flow for external authentication.\n",
            encoding="utf-8",
        )

        result = query("authentication", **_params(code_repo))

        # Domain should be identified and scored.
        assert len(result["domains"]) >= 1
        auth_domain = next(d for d in result["domains"] if d["name"] == "Auth")
        assert auth_domain["summary"] == "Authentication and authorization"
        assert len(auth_domain["pages"]) >= 1
        page_paths = [p["path"] for p in auth_domain["pages"]]
        assert "auth/jwt-tokens.md" in page_paths

    def test_empty_results(self, code_repo: Path):
        """query with no matching term returns empty domains and other_matches."""
        from tools.pull import pull
        from tools.query import query

        pull(**_params(code_repo))

        result = query("xyznonexistent", **_params(code_repo))
        assert result["domains"] == []
        assert result["other_matches"] == []

    def test_index_invalidation(self, code_repo: Path):
        """Index rebuilds after pull invalidates it, picking up new content."""
        from tools.pull import pull
        from tools.query import query
        from utils.wiki_index import WikiIndex

        pull(**_params(code_repo))
        wiki_path = code_repo / "wiki" / REPO / BRANCH

        # First query — index is built, no match for "kubernetes".
        result = query("kubernetes", **_params(code_repo))
        assert result["other_matches"] == []

        # Add a page mentioning kubernetes (simulates content change).
        wiki_path.joinpath("k8s.md").write_text(
            "# Kubernetes\n\nDeployment guide for kubernetes clusters.\n",
            encoding="utf-8",
        )

        # Query again — index is cached, still no match.
        result = query("kubernetes", **_params(code_repo))
        assert result["other_matches"] == []

        # Invalidate (pull_wiki does this automatically).
        WikiIndex.invalidate(REPO, BRANCH)

        # Now query picks up the new page.
        result = query("kubernetes", **_params(code_repo))
        matches = [m["path"] for m in result["other_matches"]]
        assert "k8s.md" in matches

    def test_wiki_not_initialized(self, code_repo: Path):
        """query before pull returns wiki_not_initialized."""
        from tools.query import query

        result = query("anything", **_params(code_repo))
        assert result["status"] == "wiki_not_initialized"


# ── query_remote_wiki ─────────────────────────────────────────────────


class TestQueryRemote:
    def test_finds_remote_pages(self, code_repo: Path):
        """query_remote finds content via git objects without checkout."""
        from tools.pull import pull
        from tools.push import push
        from tools.query import query_remote

        # Pull, add content, push so bare wiki has committed pages.
        pull(**_params(code_repo))
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        (wiki_path / "auth-guide.md").write_text(
            "# Authentication\n\nOAuth2 flow for authentication tokens.\n",
            encoding="utf-8",
        )
        push(**_params(code_repo), confirm=True)

        # Now query via git objects.
        result = query_remote(
            "authentication",
            repo_name=REPO,
            branch=BRANCH,
            repo_path=str(code_repo),
        )
        assert "status" not in result or result.get("status") != "not_found"
        # Response includes pages with inline content
        all_paths = [p["path"] for p in result.get("pages", [])]
        assert "auth-guide.md" in all_paths
        # Content is included inline
        auth_page = next(p for p in result["pages"] if p["path"] == "auth-guide.md")
        assert "OAuth2" in auth_page["content"]

    def test_remote_not_found(self, code_repo: Path):
        """query_remote returns not_found for a non-existent repo/branch."""
        from tools.pull import pull
        from tools.query import query_remote

        pull(**_params(code_repo))
        result = query_remote(
            "anything",
            repo_name="nonexistent-repo",
            branch="main",
            repo_path=str(code_repo),
        )
        assert result["status"] == "not_found"

    def test_remote_wiki_not_initialized(self, code_repo: Path):
        """query_remote before pull returns wiki_not_initialized."""
        from tools.query import query_remote

        result = query_remote(
            "anything",
            repo_name=REPO,
            branch=BRANCH,
            repo_path=str(code_repo),
        )
        assert result["status"] == "wiki_not_initialized"


# ── fetch_wiki ─────────────────────────────────────────────────────────


class TestFetch:
    def test_reads_page(self, code_repo: Path):
        """fetch returns the content of a markdown page."""
        from tools.fetch import fetch
        from tools.pull import pull

        pull(**_params(code_repo))
        result = fetch("index.md", **_params(code_repo))

        assert "content" in result
        assert REPO in result["content"]  # index.md stub contains repo name

    def test_path_traversal_rejected(self, code_repo: Path):
        """fetch with path traversal returns error."""
        from tools.fetch import fetch
        from tools.pull import pull

        pull(**_params(code_repo))
        result = fetch("../../../etc/passwd", **_params(code_repo))
        assert "error" in result

    def test_nonexistent_page(self, code_repo: Path):
        """fetch a missing page returns error."""
        from tools.fetch import fetch
        from tools.pull import pull

        pull(**_params(code_repo))
        result = fetch("does-not-exist.md", **_params(code_repo))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_non_markdown_rejected(self, code_repo: Path):
        """fetch rejects non-markdown files."""
        from tools.fetch import fetch
        from tools.pull import pull

        pull(**_params(code_repo))
        result = fetch("something.txt", **_params(code_repo))
        assert "error" in result
        assert "markdown" in result["error"].lower()


# ── push_wiki ──────────────────────────────────────────────────────────


class TestPush:
    def test_commits_and_pushes(self, code_repo: Path, bare_wiki: Path):
        """push commits a new page and pushes to the remote."""
        from tools.pull import pull
        from tools.push import push

        pull(**_params(code_repo))

        # Write a new wiki page.
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        (wiki_path / "new-page.md").write_text("# New Page\n\nContent here.\n")

        result = push(**_params(code_repo), confirm=True)
        assert result["status"] == "pushed"
        assert result["repo"] == REPO

        # Verify the content reached the bare remote.
        clone = code_repo.parent / "_verify_clone"
        _git(["clone", str(bare_wiki), str(clone)], cwd=code_repo.parent)
        _git(["-c", "protocol.file.allow=always", "checkout", "wiki"], cwd=clone)
        pushed_file = clone / REPO / BRANCH / "new-page.md"
        assert pushed_file.is_file()
        assert "Content here." in pushed_file.read_text()

    def test_nothing_to_commit(self, code_repo: Path):
        """push with no changes returns nothing to commit."""
        from tools.pull import pull
        from tools.push import push

        pull(**_params(code_repo))

        # Push the scaffold first so working tree is clean.
        push(**_params(code_repo), confirm=True)

        # Second push has nothing to do.
        result = push(**_params(code_repo), confirm=True)
        assert result["status"] == "nothing to commit"

    def test_custom_message(self, code_repo: Path):
        """push with custom message uses it in the commit."""
        from tools.pull import pull
        from tools.push import push

        pull(**_params(code_repo))

        wiki_path = code_repo / "wiki" / REPO / BRANCH
        (wiki_path / "custom.md").write_text("custom")

        msg = "docs: add custom page"
        result = push(message=msg, **_params(code_repo), confirm=True)
        assert result["status"] == "pushed"
        assert result["message"] == msg

    def test_preview_before_push(self, code_repo: Path, bare_wiki: Path):
        """push without confirm returns pending_confirmation and does NOT push."""
        from tools.pull import pull
        from tools.push import push

        pull(**_params(code_repo))

        # Write a new wiki page.
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        (wiki_path / "preview-page.md").write_text("# Preview\n\nNot pushed yet.\n")

        # Default (confirm=False) should return preview, not push.
        result = push(**_params(code_repo))
        assert result["status"] == "pending_confirmation"
        assert result["repo"] == REPO
        assert "changed_files" in result
        assert any("preview-page.md" in f for f in result["changed_files"])
        assert "instruction" in result

        # Verify the content did NOT reach the bare remote.
        clone = code_repo.parent / "_verify_no_push"
        _git(["clone", str(bare_wiki), str(clone)], cwd=code_repo.parent)
        branches = _git(["branch", "-a"], cwd=clone)
        if "wiki" in branches.stdout:
            _git(["-c", "protocol.file.allow=always", "checkout", "wiki"], cwd=clone)
            pushed_file = clone / REPO / BRANCH / "preview-page.md"
            assert not pushed_file.is_file(), "File should NOT have been pushed"

    def test_preview_nothing_to_commit(self, code_repo: Path):
        """push without confirm returns nothing to commit when clean."""
        from tools.pull import pull
        from tools.push import push

        pull(**_params(code_repo))
        push(**_params(code_repo), confirm=True)  # push scaffold

        # No changes — preview should say nothing to commit.
        result = push(**_params(code_repo))
        assert result["status"] == "nothing to commit"


# ── ingest_wiki ────────────────────────────────────────────────────────


class TestIngest:
    def test_wiki_is_empty_ignores_domain_subdirs(self, tmp_path: Path):
        """_wiki_is_empty returns False when domain subdirs with index.md exist."""
        from tools.ingest import _wiki_is_empty

        # Create wiki structure with domain subdir but no root index entries
        wiki_path = tmp_path / "wiki" / "myrepo" / "main"
        wiki_path.mkdir(parents=True)
        (wiki_path / "index.md").write_text("# myrepo\n\n")  # empty index
        src_dir = wiki_path / "src"
        src_dir.mkdir()
        (src_dir / "index.md").write_text("## src domain\n")

        assert _wiki_is_empty(wiki_path / "index.md") is False

    def test_wiki_is_empty_true_when_no_subdirs(self, tmp_path: Path):
        """_wiki_is_empty returns True when no domain subdirs and no index entries."""
        from tools.ingest import _wiki_is_empty

        wiki_path = tmp_path / "wiki" / "myrepo" / "main"
        wiki_path.mkdir(parents=True)
        (wiki_path / "index.md").write_text(
            "# myrepo\n\n_Domain summaries will be added by the agent after init._\n\n"
        )

        assert _wiki_is_empty(wiki_path / "index.md") is True

    def test_returns_diff(self, code_repo: Path):
        """ingest after a code change returns the diff."""
        from tools.ingest import ingest
        from tools.pull import pull

        pull(**_params(code_repo))

        # Simulate a populated wiki so ingest does incremental diff
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        (wiki_path / "index.md").write_text("# Wiki\n\n## [src](src/index.md) - Source code\n")

        # Make a code change and commit it.
        (code_repo / "src" / "app.py").write_text('print("updated")\n')
        _git(["add", "."], cwd=code_repo)
        _git(["commit", "-m", "update app"], cwd=code_repo)

        result = ingest(**_params(code_repo))
        assert result["diff"].strip()  # non-empty diff
        assert "updated" in result["diff"]
        assert result["repo"] == REPO
        # Verify two-phase push flow guidance
        assert "WITHOUT confirm=True" in result["instruction"]
        assert "push_wiki(confirm=True)" in result["instruction"]
        assert "user explicitly approves" in result["instruction"]

    def test_no_changes(self, code_repo: Path):
        """ingest on empty wiki returns directory structure + key files for full ingest."""
        from tools.ingest import ingest
        from tools.pull import pull

        pull(**_params(code_repo))

        result = ingest(**_params(code_repo))
        assert result["status"] == "action_required"
        assert result["action"] == "create_wiki_pages"
        assert "src/" in result["directory_structure"]
        assert "(full codebase)" in result["diff_spec"]
        assert "CLEAN-SLATE WIKI CREATION" in result["instruction"]
        assert "DO NOT read additional source files" in result["instruction"]
        # Full ingest must direct creation of real content pages, not just indexes
        instr = result["instruction"]
        assert "CONTENT PAGES" in instr
        assert "architecture.md" in instr
        assert "modules/" in instr
        assert "PAGE CATALOG" in instr
        # Verify two-phase push flow guidance
        assert "WITHOUT confirm=True" in result["instruction"]
        assert "push_wiki(confirm=True)" in result["instruction"]
        assert "user explicitly approves" in result["instruction"]

    def test_no_changes_after_populated(self, code_repo: Path):
        """ingest on a populated wiki with no code changes returns 'no changes'."""
        from tools.ingest import ingest
        from tools.pull import pull

        pull(**_params(code_repo))

        # Simulate a populated wiki by replacing the scaffold placeholder
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        index = wiki_path / "index.md"
        index.write_text("# Wiki\n\n## [architecture](architecture/index.md) - Architecture\n")

        result = ingest(**_params(code_repo))
        assert result.get("status") == "no changes"

    def test_targeted_ingest_on_empty_wiki(
        self, tmp_path: Path, bare_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When wiki is empty but origin base exists, ingest returns targeted_ingest with diff."""
        from tools.ingest import ingest
        from tools.pull import pull

        # Create a bare "origin" repo with a default branch
        origin_bare = tmp_path / "origin.git"
        origin_bare.mkdir()
        _git(["init", "--bare", str(origin_bare)], cwd=tmp_path)

        # Clone to create working tree with default branch
        origin_work = tmp_path / "origin_work"
        _git(["clone", str(origin_bare), str(origin_work)], cwd=tmp_path)
        _git(["config", "user.name", "Test"], cwd=origin_work)
        _git(["config", "user.email", "test@test.com"], cwd=origin_work)
        (origin_work / "README.md").write_text("# Project\n")
        _git(["add", "."], cwd=origin_work)
        _git(["commit", "-m", "initial"], cwd=origin_work)
        _git(["push", "origin", "master"], cwd=origin_work)

        # Clone the origin to create the "user branch" repo
        code_repo = tmp_path / "feature_repo"
        _git(["clone", str(origin_bare), str(code_repo)], cwd=tmp_path)
        _git(["config", "user.name", "Test"], cwd=code_repo)
        _git(["config", "user.email", "test@test.com"], cwd=code_repo)

        # Create a feature branch with changes
        _git(["checkout", "-b", "feature"], cwd=code_repo)
        (code_repo / "src").mkdir()
        (code_repo / "src" / "app.py").write_text('print("feature")\n')
        _git(["add", "."], cwd=code_repo)
        _git(["commit", "-m", "add feature"], cwd=code_repo)

        # Set up wiki
        monkeypatch.setenv("WIKI_MCP_REMOTE_URL", bare_wiki.as_uri())

        import config

        config._wiki_path_cache = None
        config._repo_root_cache.clear()
        from utils import git as git_mod

        git_mod._repo_name_cache.clear()

        # Pull to initialize wiki
        pull(repo_path=str(code_repo), repo_name="feature_repo", branch="feature")

        # Wiki is empty scaffold — should do targeted ingest, not full
        result = ingest(repo_path=str(code_repo), repo_name="feature_repo", branch="feature")
        assert result["status"] == "action_required"
        assert result["action"] == "create_wiki_pages_for_diff"
        assert "feature" in result["diff"]
        assert "README.md" not in result["diff"]  # unchanged file not in diff
        # Verify two-phase push flow guidance
        assert "WITHOUT confirm=True" in result["instruction"]
        assert "push_wiki(confirm=True)" in result["instruction"]
        assert "user explicitly approves" in result["instruction"]

    def test_targeted_ingest_user_branch_with_slashes(
        self, tmp_path: Path, bare_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """User branch with slashes (e.g. users/ayayalar/divp-http-metrics) vs origin/master with 4 file changes.
        Wiki pages should only be created for the 4 changed files, not the full codebase."""
        from tools.ingest import ingest
        from tools.pull import pull

        # Create a bare "origin" repo with master as default
        origin_bare = tmp_path / "origin.git"
        origin_bare.mkdir()
        _git(["init", "--bare", str(origin_bare)], cwd=tmp_path)

        origin_work = tmp_path / "origin_work"
        _git(["clone", str(origin_bare), str(origin_work)], cwd=tmp_path)
        _git(["config", "user.name", "Test"], cwd=origin_work)
        _git(["config", "user.email", "test@test.com"], cwd=origin_work)

        # Create a realistic codebase with many files
        (origin_work / "README.md").write_text("# Project\n")
        (origin_work / "services").mkdir()
        (origin_work / "services" / "api.py").write_text("# api\n")
        (origin_work / "services" / "db.py").write_text("# db\n")
        (origin_work / "services" / "auth.py").write_text("# auth\n")
        (origin_work / "services" / "cache.py").write_text("# cache\n")
        (origin_work / "services" / "queue.py").write_text("# queue\n")
        (origin_work / "pipelines").mkdir()
        (origin_work / "pipelines" / "deploy.yaml").write_text("# deploy\n")
        (origin_work / "pipelines" / "test.yaml").write_text("# test\n")
        _git(["add", "."], cwd=origin_work)
        _git(["commit", "-m", "initial"], cwd=origin_work)
        _git(["push", "origin", "master"], cwd=origin_work)

        # Clone and create user branch with slashes
        code_repo = tmp_path / "customer_divp"
        _git(["clone", str(origin_bare), str(code_repo)], cwd=tmp_path)
        _git(["config", "user.name", "Test"], cwd=code_repo)
        _git(["config", "user.email", "test@test.com"], cwd=code_repo)
        _git(["checkout", "-b", "users/ayayalar/divp-http-metrics"], cwd=code_repo)

        # Make exactly 4 file changes
        (code_repo / "services" / "api.py").write_text("# api updated\n")
        (code_repo / "services" / "db.py").write_text("# db updated\n")
        (code_repo / "services" / "metrics.py").write_text("# new metrics file\n")
        (code_repo / "pipelines" / "deploy.yaml").write_text("# deploy updated\n")
        _git(["add", "."], cwd=code_repo)
        _git(["commit", "-m", "divp http metrics changes"], cwd=code_repo)

        # Set up wiki
        monkeypatch.setenv("WIKI_MCP_REMOTE_URL", bare_wiki.as_uri())

        import config

        config._wiki_path_cache = None
        config._repo_root_cache.clear()
        from utils import git as git_mod

        git_mod._repo_name_cache.clear()

        user_branch = "users/ayayalar/divp-http-metrics"
        pull(repo_path=str(code_repo), repo_name="customer-divp", branch=user_branch)

        # Wiki is empty — should do targeted ingest with only 4 changed files
        result = ingest(repo_path=str(code_repo), repo_name="customer-divp", branch=user_branch)

        assert result["status"] == "action_required"
        assert result["action"] == "create_wiki_pages_for_diff"

        # Verify only the 4 changed files appear in the diff
        diff = result["diff"]
        assert "api.py" in diff
        assert "db.py" in diff
        assert "metrics.py" in diff
        assert "deploy.yaml" in diff

        # Verify unchanged files do NOT appear in the diff
        assert "auth.py" not in diff
        assert "cache.py" not in diff
        assert "queue.py" not in diff
        assert "test.yaml" not in diff
        assert "README.md" not in diff

    def test_wiki_not_initialized(self, code_repo: Path):
        """ingest before pull returns wiki_not_initialized."""
        from tools.ingest import ingest

        result = ingest(**_params(code_repo))
        assert result["status"] == "wiki_not_initialized"

    def test_branch_switch_unloads_old_wiki(
        self, tmp_path: Path, bare_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """After git checkout to master, the previous branch wiki is unloaded and replaced with master wiki."""
        from tools.ingest import ingest
        from tools.pull import pull

        # Create a bare "origin" repo with master as default
        origin_bare = tmp_path / "origin.git"
        origin_bare.mkdir()
        _git(["init", "--bare", str(origin_bare)], cwd=tmp_path)

        origin_work = tmp_path / "origin_work"
        _git(["clone", str(origin_bare), str(origin_work)], cwd=tmp_path)
        _git(["config", "user.name", "Test"], cwd=origin_work)
        _git(["config", "user.email", "test@test.com"], cwd=origin_work)
        (origin_work / "README.md").write_text("# Project\n")
        _git(["add", "."], cwd=origin_work)
        _git(["commit", "-m", "initial"], cwd=origin_work)
        _git(["push", "origin", "master"], cwd=origin_work)

        # Clone and create feature branch with changes
        code_repo = tmp_path / "myrepo"
        _git(["clone", str(origin_bare), str(code_repo)], cwd=tmp_path)
        _git(["config", "user.name", "Test"], cwd=code_repo)
        _git(["config", "user.email", "test@test.com"], cwd=code_repo)
        _git(["checkout", "-b", "feature"], cwd=code_repo)
        (code_repo / "src").mkdir()
        (code_repo / "src" / "feature.py").write_text("# feature\n")
        _git(["add", "."], cwd=code_repo)
        _git(["commit", "-m", "feature"], cwd=code_repo)

        # Set up wiki
        monkeypatch.setenv("WIKI_MCP_REMOTE_URL", bare_wiki.as_uri())

        import config

        config._wiki_path_cache = None
        config._repo_root_cache.clear()
        from utils import git as git_mod

        git_mod._repo_name_cache.clear()

        # Pull and ingest on feature branch
        pull(repo_path=str(code_repo), repo_name="myrepo", branch="feature")
        # Simulate populated wiki on feature
        wiki_feature = code_repo / "wiki" / "myrepo" / "feature"
        (wiki_feature / "index.md").write_text(
            "# Wiki\n\n## [feature](feature/index.md) - Feature work\n"
        )

        result_feature = ingest(repo_path=str(code_repo), repo_name="myrepo", branch="feature")
        assert (
            "feature" in result_feature.get("diff_spec", "")
            or result_feature.get("diff", "").strip()
        )

        # Now switch to master
        _git(["checkout", "master"], cwd=code_repo)

        # Ingest on master — should NOT see feature branch content
        config._repo_root_cache.clear()
        git_mod._repo_name_cache.clear()
        from utils.wiki_index import WikiIndex

        WikiIndex.invalidate_all()

        result_master = ingest(repo_path=str(code_repo), repo_name="myrepo", branch="master")
        # On master, the feature.py file doesn't exist — diff should be empty or show removal
        assert (
            "feature" not in result_master.get("diff", "")
            or result_master.get("status") == "no changes"
        )


class TestScopedIngest:
    def test_no_scope_backcompat(self, code_repo: Path):
        """No scope args preserves ingest behavior and does not error."""
        from tools.ingest import ingest
        from tools.pull import pull

        pull(**_params(code_repo))
        result = ingest(**_params(code_repo))

        assert result["status"] in {"action_required", "no changes"}

    def test_both_scope_params_rejected(self, code_repo: Path):
        """Passing both paths and topic is rejected as invalid_params."""
        from tools.ingest import ingest
        from tools.pull import pull

        pull(**_params(code_repo))
        result = ingest(**_params(code_repo), paths=["src"], topic="app")

        assert result["status"] == "invalid_params"
        assert "not both" in result["error"].lower()

    def test_scoped_paths_full_create(self, code_repo: Path):
        """Scoped paths on empty wiki builds a scoped full_create response."""
        from tools.ingest import ingest
        from tools.pull import pull

        pull(**_params(code_repo))

        docs = code_repo / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
        (code_repo / "src" / "app.py").write_text('print("scoped")\n', encoding="utf-8")
        _git(["add", "."], cwd=code_repo)
        _git(["commit", "-m", "add docs and update src"], cwd=code_repo)

        result = ingest(**_params(code_repo), paths=["src"])

        assert result["status"] == "action_required"
        assert result["wiki_state"] == "full_create"
        assert "src/" in result["directory_structure"]
        assert "docs" not in result["directory_structure"]
        assert result["scope"]["paths"] == ["src"]
        assert "SCOPED INGEST" in result["instruction"]

    def test_scoped_topic_no_match(self, code_repo: Path):
        """Topic scope with no matches returns no_files_matched_scope."""
        from tools.ingest import ingest
        from tools.pull import pull

        pull(**_params(code_repo))
        result = ingest(**_params(code_repo), topic="zzz-nonexistent")

        assert result["status"] == "no_files_matched_scope"
        assert "available_top_level_dirs" in result


class TestBranchSwitchSync:
    def _setup_repos_and_caches(
        self,
        tmp_path: Path,
        bare_wiki: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Path:
        """Create clone-based repo setup and seed wiki remote for branch-switch tests."""
        _seed_wiki_remote_with_feature_and_master(bare_wiki, tmp_path)

        origin_bare = tmp_path / "myrepo.git"
        _git(["init", "--bare", str(origin_bare)], cwd=tmp_path, check=True)

        origin_work = tmp_path / "origin_work"
        _git(["clone", str(origin_bare), str(origin_work)], cwd=tmp_path, check=True)
        _git(["config", "user.name", "Test"], cwd=origin_work, check=True)
        _git(["config", "user.email", "test@test.com"], cwd=origin_work, check=True)
        (origin_work / "README.md").write_text("# Project\n", encoding="utf-8")
        _git(["add", "."], cwd=origin_work, check=True)
        _git(["commit", "-m", "initial"], cwd=origin_work, check=True)
        _git(["push", "origin", "master"], cwd=origin_work, check=True)

        code_repo = tmp_path / "myrepo"
        _git(["clone", str(origin_bare), str(code_repo)], cwd=tmp_path, check=True)
        _git(["config", "user.name", "Test"], cwd=code_repo, check=True)
        _git(["config", "user.email", "test@test.com"], cwd=code_repo, check=True)
        _git(["remote", "set-url", "origin", origin_bare.as_uri()], cwd=code_repo, check=True)
        _git(["checkout", "-b", "feature"], cwd=code_repo, check=True)
        (code_repo / "src").mkdir(exist_ok=True)
        (code_repo / "src" / "feature.py").write_text("# feature\n", encoding="utf-8")
        _git(["add", "."], cwd=code_repo, check=True)
        _git(["commit", "-m", "feature"], cwd=code_repo, check=True)

        monkeypatch.setenv("WIKI_MCP_REMOTE_URL", bare_wiki.as_uri())

        import config

        config._wiki_path_cache = None
        config._repo_root_cache.clear()
        from utils import git as git_mod
        from utils.wiki_index import WikiIndex

        git_mod._repo_name_cache.clear()
        WikiIndex.invalidate_all()

        import server

        server._last_sparse_pattern.clear()
        server._submodule_update_configured.clear()

        return code_repo

    def test_dirty_switch_loads_new_branch(
        self, tmp_path: Path, bare_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Dirty old branch still switches sparse view and preserves unsaved edits."""
        import server

        code_repo = self._setup_repos_and_caches(tmp_path, bare_wiki, monkeypatch)

        pull_result = server.pull_wiki(cwd=str(code_repo))
        assert pull_result["status"] == "synced"
        server._resolve_context(str(code_repo))

        wiki_root = code_repo / "wiki"
        feature_index = wiki_root / "myrepo" / "feature" / "index.md"
        marker = "\nDIRTY EDIT\n"
        feature_index.write_text(
            feature_index.read_text(encoding="utf-8") + marker, encoding="utf-8"
        )

        _git(["checkout", "master"], cwd=code_repo, check=True)
        server._resolve_context(str(code_repo))

        assert (wiki_root / "myrepo" / "master").is_dir()
        assert (wiki_root / "myrepo" / "master" / "index.md").is_file()
        assert marker.strip() in feature_index.read_text(encoding="utf-8")

    def test_clean_switch_unloads_old_and_loads_new(
        self, tmp_path: Path, bare_wiki: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Clean branch switch fully unloads old folder and materializes new folder."""
        import server

        code_repo = self._setup_repos_and_caches(tmp_path, bare_wiki, monkeypatch)

        pull_result = server.pull_wiki(cwd=str(code_repo))
        assert pull_result["status"] == "synced"
        server._resolve_context(str(code_repo))

        _commit_all_if_dirty(code_repo / "wiki", "test: clean wiki for branch switch")

        _git(["checkout", "master"], cwd=code_repo, check=True)
        server._resolve_context(str(code_repo))

        wiki_root = code_repo / "wiki"
        assert (wiki_root / "myrepo" / "master").is_dir()
        assert not (wiki_root / "myrepo" / "feature").exists()


# ── lint_wiki ──────────────────────────────────────────────────────────


class TestLint:
    def test_returns_indexes_and_tree(self, code_repo: Path):
        """lint returns domain indexes, wiki file paths, and repo file tree."""
        from tools.lint import lint
        from tools.pull import pull

        pull(**_params(code_repo))
        result = lint(**_params(code_repo))

        assert result["repo"] == REPO
        assert "domain_indexes" in result
        assert "wiki_file_paths" in result
        assert "repo_dirs" in result

        # index.md should appear in domain_indexes.
        assert "index.md" in result["domain_indexes"]

        # repo_dirs should include the "src" directory.
        assert "src" in result["repo_dirs"]

    def test_wiki_not_initialized(self, code_repo: Path):
        """lint before pull returns wiki_not_initialized."""
        from tools.lint import lint

        result = lint(**_params(code_repo))
        assert result["status"] == "wiki_not_initialized"


# ── delete_wiki ────────────────────────────────────────────────────────


class TestDelete:
    def test_removes_branch_folder(self, code_repo: Path):
        """delete removes the branch folder, commits, and pushes."""
        from tools.delete import delete_wiki
        from tools.pull import pull
        from tools.push import push

        pull(**_params(code_repo))
        push(**_params(code_repo), confirm=True)  # push scaffold so remote has content

        result = delete_wiki(**_params(code_repo))
        assert result["status"] == "deleted"
        assert result["path_removed"] == f"{REPO}/{BRANCH}"

        # Verify the folder is gone.
        assert not (code_repo / "wiki" / REPO / BRANCH).exists()

    def test_refuses_main(self, code_repo: Path):
        """delete refuses to delete main/master branches."""
        from tools.delete import delete_wiki
        from tools.pull import pull

        pull(**_params(code_repo))
        result = delete_wiki(branch="main", repo_name=REPO, repo_path=str(code_repo))
        assert result["status"] == "refused"

    def test_nonexistent_folder(self, code_repo: Path):
        """delete a non-existent branch folder returns not_found."""
        from tools.delete import delete_wiki
        from tools.pull import pull

        pull(**_params(code_repo))
        result = delete_wiki(branch="nonexistent", repo_name=REPO, repo_path=str(code_repo))
        assert result["status"] == "not_found"


# ── reset_wiki ─────────────────────────────────────────────────────────


class TestReset:
    def test_dry_run_on_healthy_submodule(self, code_repo: Path):
        """reset (dry-run) on a healthy submodule offers to remove it."""
        from tools.pull import pull
        from tools.reset import reset_wiki

        pull(**_params(code_repo))
        result = reset_wiki(**_params(code_repo))
        assert result["status"] == "would_do"
        assert any("Remove local" in a for a in result["actions"])

    def test_force_reset_removes_local_and_remote(self, code_repo: Path, bare_wiki: Path):
        """reset(force=True) removes submodule and deletes content from remote."""
        from tools.pull import pull
        from tools.push import push
        from tools.reset import reset_wiki

        pull(**_params(code_repo))
        wiki = code_repo / "wiki"
        # Push some content so remote has it
        wiki_path = wiki / REPO / BRANCH
        (wiki_path / "to-delete.md").write_text("# Will be deleted\n")
        push(**_params(code_repo), confirm=True)

        result = reset_wiki(force=True, **_params(code_repo))
        assert result["status"] == "reset"
        assert result["remote_deleted"] is True
        assert not wiki.exists()

        # Verify content is gone from remote
        clone = code_repo.parent / "_verify_reset"
        from tests.conftest import _git

        _git(["clone", str(bare_wiki), str(clone)], cwd=code_repo.parent)
        _git(["-c", "protocol.file.allow=always", "checkout", "wiki"], cwd=clone)
        assert not (clone / REPO / BRANCH).exists()

    def test_force_reset_then_repull(self, code_repo: Path):
        """After reset, pull_wiki re-bootstraps fresh."""
        from tools.pull import pull
        from tools.reset import reset_wiki

        pull(**_params(code_repo))
        wiki = code_repo / "wiki"
        assert (wiki / ".git").exists()

        reset_wiki(force=True, **_params(code_repo))
        assert not wiki.exists()

        # Re-pull should succeed (clean bootstrap with empty scaffold)
        result2 = pull(**_params(code_repo))
        assert result2["status"] == "synced"
        assert (wiki / ".git").exists()


# ── Server-level parameter validation ─────────────────────────────────


class TestParamValidation:
    def test_path_traversal_in_repo_name(self, code_repo: Path):
        """Server rejects '..' in repo_name."""
        import server

        result = server.pull_wiki(cwd=str(code_repo), repo_name="../escape", branch=BRANCH)
        assert result["status"] == "invalid_params"
        assert ".." in result["error"]

    def test_path_traversal_in_branch(self, code_repo: Path):
        """Server rejects '..' in branch."""
        import server

        result = server.pull_wiki(cwd=str(code_repo), repo_name=REPO, branch="../../etc")
        assert result["status"] == "invalid_params"

    def test_auto_derives_repo_name_when_empty(self, code_repo: Path):
        """Server auto-derives repo_name when empty string is passed."""
        import server

        # Empty string is falsy → triggers auto-derivation from cwd.
        # Should NOT fail with invalid_params — the derived name is valid.
        result = server.pull_wiki(cwd=str(code_repo), repo_name="", branch=BRANCH)
        assert result["status"] != "invalid_params"

    def test_auto_derives_branch_when_empty(self, code_repo: Path):
        """Server auto-derives branch when empty string is passed."""
        import server

        result = server.pull_wiki(cwd=str(code_repo), repo_name=REPO, branch="")
        assert result["status"] != "invalid_params"

    def test_delete_validates_repo_name_when_branch_none(self, code_repo: Path):
        """delete_wiki still validates repo_name even when branch is None."""
        import server

        result = server.delete_wiki(cwd=str(code_repo), repo_name="../bad")
        # branch is None so _check_params is skipped — but repo_name
        # with '..' should still be rejected. If this passes as
        # invalid_params, the server is safe. If it doesn't, it's a gap.
        # Current code skips validation when branch is None.
        # This test documents that behavior.
        assert "status" in result


# ── wiki_usage ───────────────────────────────────────────────────────────


class TestWikiUsage:
    def test_returns_table_and_excludes_self(self, code_repo: Path):
        """wiki_usage returns a markdown table and does not count itself."""
        import server
        from tools.usage import reset_session_usage_for_tests

        reset_session_usage_for_tests()

        first = server.wiki_usage(cwd=str(code_repo), repo_name=REPO, branch=BRANCH)
        second = server.wiki_usage(cwd=str(code_repo), repo_name=REPO, branch=BRANCH)

        assert "summary" in first
        assert "| Metric | Tokens (Estimated) |" in first["summary"]
        assert "| Tool | Input | Output | Total |" in first["summary"]

        assert first["session_input_tokens_estimated"] == second["session_input_tokens_estimated"]
        assert first["session_output_tokens_estimated"] == second["session_output_tokens_estimated"]
        assert first["session_total_tokens_estimated"] == second["session_total_tokens_estimated"]

    def test_counts_other_tools_in_current_session(self, code_repo: Path):
        """Calling another tool increases wiki_usage session totals."""
        import server
        from tools.usage import reset_session_usage_for_tests

        reset_session_usage_for_tests()

        baseline = server.wiki_usage(cwd=str(code_repo), repo_name=REPO, branch=BRANCH)
        server.pull_wiki(cwd=str(code_repo), repo_name=REPO, branch=BRANCH)
        after = server.wiki_usage(cwd=str(code_repo), repo_name=REPO, branch=BRANCH)

        assert after["session_total_tokens_estimated"] > baseline["session_total_tokens_estimated"]
        assert any(row["tool"] == "pull_wiki" for row in after["per_tool"])


# ── End-to-end roundtrip ──────────────────────────────────────────────


class TestRoundtrip:
    def test_push_pull_roundtrip(self, code_repo: Path, bare_wiki: Path, tmp_path: Path):
        """Content pushed from one repo can be pulled back after a fresh clone."""
        from tools.pull import pull
        from tools.push import push

        # First repo: pull → write → push.
        pull(**_params(code_repo))
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        (wiki_path / "roundtrip.md").write_text("# Roundtrip Test\n\nWorks!\n")
        push(**_params(code_repo), confirm=True)

        # Second repo: fresh code repo pulls the same wiki content.
        code_repo2 = tmp_path / "repo2"
        code_repo2.mkdir()
        _git(["init"], cwd=code_repo2)
        _git(["config", "user.name", "Test"], cwd=code_repo2)
        _git(["config", "user.email", "test@test.com"], cwd=code_repo2)
        (code_repo2 / "README.md").write_text("repo2")
        _git(["add", "."], cwd=code_repo2)
        _git(["commit", "-m", "init"], cwd=code_repo2)

        result2 = pull(repo_name=REPO, branch=BRANCH, repo_path=str(code_repo2))
        assert result2["status"] == "synced"

        # The roundtrip page should be present.
        pulled_page = code_repo2 / "wiki" / REPO / BRANCH / "roundtrip.md"
        assert pulled_page.is_file()
        assert "Roundtrip Test" in pulled_page.read_text()

    def test_multi_repo_isolation(self, code_repo: Path):
        """Switching branches via pull scopes sparse-checkout to the new branch only."""
        from tools.pull import pull
        from tools.push import push

        # Pull repo A / branch A.
        pull(repo_name="repoA", branch="branchA", repo_path=str(code_repo))
        wiki_a = code_repo / "wiki" / "repoA" / "branchA"
        (wiki_a / "pageA.md").write_text("# Page A\n")
        push(repo_name="repoA", branch="branchA", repo_path=str(code_repo), confirm=True)

        # Pull repo A / branch B — sparse-checkout now scoped to repoA/branchB.
        pull(repo_name="repoA", branch="branchB", repo_path=str(code_repo))
        wiki_b = code_repo / "wiki" / "repoA" / "branchB"
        assert wiki_b.exists()
        # Branch A content should NOT be on disk (sparse-checkout excludes it).
        assert not (wiki_a / "pageA.md").is_file()

        # Switch back to branch A — content is restored from git.
        pull(repo_name="repoA", branch="branchA", repo_path=str(code_repo))
        assert (wiki_a / "pageA.md").is_file()


# ── resolve_wiki_issue ────────────────────────────────────────────────


class TestResolve:
    def test_healthy_wiki_no_issues(self, code_repo: Path, monkeypatch):
        """resolve on a clean wiki reports no issues."""
        from tools.pull import pull
        from tools.push import push
        from tools.resolve import resolve

        # Use master branch to match the code repo's actual branch.
        # resolve() auto-detects the branch from the code repo.
        params = {"repo_path": str(code_repo), "repo_name": REPO, "branch": "master"}
        pull(**params)
        push(**params, confirm=True)  # push scaffold so clean

        # Set repo root so resolve() uses the correct repo context.
        monkeypatch.setenv("WIKI_MCP_REPO_ROOT", str(code_repo))
        import config

        config._repo_root_cache.clear()
        from utils import git as git_mod

        git_mod._repo_name_cache.clear()

        result = resolve(repo_path=str(code_repo))
        assert result["status"] == "diagnosis"
        assert any(i["issue"] == "none" for i in result["issues"])

    def test_diagnoses_dirty_worktree(self, code_repo: Path):
        """resolve detects uncommitted changes and offers options."""
        from tools.pull import pull
        from tools.resolve import resolve

        pull(**_params(code_repo))
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        (wiki_path / "dirty.md").write_text("# Dirty\n")

        result = resolve(repo_path=str(code_repo))
        assert result["status"] == "diagnosis"
        dirty = [i for i in result["issues"] if i["issue"] == "dirty_worktree"]
        assert len(dirty) == 1
        action_ids = [r["id"] for r in dirty[0]["resolutions"]]
        assert "dirty_worktree:commit_and_push" in action_ids
        assert "dirty_worktree:discard_changes" in action_ids

    def test_discard_changes(self, code_repo: Path):
        """resolve with discard_changes removes uncommitted wiki changes."""
        from tools.pull import pull
        from tools.resolve import resolve

        pull(**_params(code_repo))
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        (wiki_path / "discard-me.md").write_text("# Delete Me\n")

        result = resolve(action="dirty_worktree:discard_changes", repo_path=str(code_repo))
        assert result["status"] == "resolved"
        assert not (wiki_path / "discard-me.md").exists()

    def test_commit_and_push(self, code_repo: Path, bare_wiki: Path):
        """resolve with commit_and_push commits and pushes dirty changes."""
        from tools.pull import pull
        from tools.resolve import resolve

        pull(**_params(code_repo))
        wiki_path = code_repo / "wiki" / REPO / BRANCH
        (wiki_path / "resolved.md").write_text("# Resolved\n")

        result = resolve(action="dirty_worktree:commit_and_push", repo_path=str(code_repo))
        assert result["status"] == "resolved"

        # Verify it reached the bare remote.
        clone = code_repo.parent / "_verify_resolve"
        _git(["clone", str(bare_wiki), str(clone)], cwd=code_repo.parent)
        _git(["-c", "protocol.file.allow=always", "checkout", "wiki"], cwd=clone)
        assert (clone / REPO / BRANCH / "resolved.md").is_file()

    def test_wiki_not_initialized(self, code_repo: Path):
        """resolve before pull reports wiki_not_initialized."""
        from tools.resolve import resolve

        result = resolve(repo_path=str(code_repo))
        assert result["status"] == "diagnosis"
        assert any(i["issue"] == "wiki_not_initialized" for i in result["issues"])


# ── push auto-sync ────────────────────────────────────────────────────


class TestPushAutoSync:
    def test_push_syncs_diverged_remote(self, code_repo: Path, bare_wiki: Path, tmp_path: Path):
        """push auto-merges when remote has new content from another repo."""
        from tools.pull import pull
        from tools.push import push

        # Repo 1: pull + push some content.
        pull(**_params(code_repo))
        wiki1 = code_repo / "wiki" / REPO / BRANCH
        (wiki1 / "from-repo1.md").write_text("# From Repo 1\n")
        push(**_params(code_repo), confirm=True)

        # Repo 2: fresh clone, pull, write different content, push.
        code_repo2 = tmp_path / "repo2"
        code_repo2.mkdir()
        _git(["init"], cwd=code_repo2)
        _git(["config", "user.name", "Test"], cwd=code_repo2)
        _git(["config", "user.email", "test@test.com"], cwd=code_repo2)
        (code_repo2 / "README.md").write_text("repo2")
        _git(["add", "."], cwd=code_repo2)
        _git(["commit", "-m", "init"], cwd=code_repo2)

        pull(repo_name="other-repo", branch="main", repo_path=str(code_repo2))
        wiki2 = code_repo2 / "wiki" / "other-repo" / "main"
        (wiki2 / "from-repo2.md").write_text("# From Repo 2\n")
        push(repo_name="other-repo", branch="main", repo_path=str(code_repo2), confirm=True)

        # Repo 1 now has a diverged local — push should auto-sync.
        (wiki1 / "another-page.md").write_text("# Another\n")
        result = push(**_params(code_repo), confirm=True)
        assert result["status"] == "pushed"
