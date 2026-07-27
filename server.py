"""FastMCP entry point for the repo wiki MCP server.

Every tool accepts one context parameter that the client should provide:
  - cwd: any absolute path inside the developer's code repository
         (e.g. "D:/users/ayayalar/divp-http-metrics" or a subdirectory)

Everything else is auto-derived via fast file IO (no subprocess):
  - repo root: found by walking up from cwd looking for .git
  - repo_name: parsed from .git/config origin URL, or directory basename
  - branch:    parsed from .git/HEAD

The wiki lives at <repo_root>/wiki/ — inside the developer's own repo,
just like a standard git submodule.
"""

from __future__ import annotations

import re
import sys
import threading

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from config import find_repo_root, RepoRootNotFound
from tools.delete import delete_wiki as delete_wiki_impl
from tools.fetch import fetch as fetch_impl
from tools.ingest import ingest as ingest_impl
from tools.init import init as init_impl
from tools.list import list_remote_wikis as list_remote_wikis_impl
from tools.lint import lint as lint_impl
from tools.pull import pull as pull_impl
from tools.push import push as push_impl
from tools.query import query as query_impl, query_remote as query_remote_impl
from tools.reset import reset_wiki as reset_wiki_impl
from tools.resolve import resolve as resolve_impl
from tools.status import status as status_impl
from tools.usage import record_tool_input, record_tool_output, usage as usage_impl
from utils.git import derive_branch, derive_repo_name, set_sparse_checkout_cone
from utils.wiki import validate_wiki_params

mcp = FastMCP("wiki-mcp")

# Track the last applied sparse-checkout pattern *per wiki path* so we can
# detect branch switches. Keyed by wiki dir (not a single global string):
# one server can serve multiple repos/clones, and a global would flip on
# every alternating call, churning the sparse-checkout. Guarded by a lock
# because FastMCP dispatches tool calls on concurrent threads.
_last_sparse_pattern: dict[str, str] = {}
_sparse_lock = threading.Lock()

# Repo paths where `submodule.wiki.update=none` has already been configured,
# so we don't spawn that git subprocess on every single tool call.
_submodule_update_configured: set[str] = set()


def _normalize_path(cwd: str) -> str:
    """Convert MSYS/Git Bash paths to native Windows paths.

    Git Bash and MSYS2 use /d/foo style paths. On Windows, Python's
    Path('/d/foo') resolves to 'D:\\d\\foo' (wrong). This converts
    '/d/foo' → 'D:/foo' before Path() touches it.
    """
    if sys.platform == "win32":
        m = re.match(r"^/([a-zA-Z])/(.*)", cwd)
        if m:
            return f"{m.group(1).upper()}:/{m.group(2)}"
    return cwd


def _resolve_context(
    cwd: str,
    repo_name: str | None = None,
    branch: str | None = None,
) -> tuple[str, str, str, dict | None]:
    """Derive repo_path, repo_name, and branch from a working directory.

    All derivation uses fast file IO (no git subprocess calls).
    Returns (repo_path, repo_name, branch, error_dict_or_None).

    When the branch changes (e.g. git checkout), the wiki sparse-checkout
    is updated automatically so the new branch's wiki folder is checked out.
    """
    cwd = _normalize_path(cwd)
    try:
        repo_path = str(find_repo_root(Path(cwd)))
    except RepoRootNotFound:
        return cwd, repo_name or "", branch or "", {
            "status": "invalid_params",
            "error": f"No git repository found at or above: {cwd}",
        }
    if not repo_name:
        repo_name = derive_repo_name(repo_path)
    if not branch:
        branch = derive_branch(repo_path)
    err = validate_wiki_params(repo_name, branch)
    if err:
        return repo_path, repo_name, branch, {"status": "invalid_params", "error": err}

    # Detect branch switch and refresh wiki sparse-checkout.
    new_pattern = f"{repo_name}/{branch}"
    wiki = Path(repo_path) / "wiki"
    wiki_key = str(wiki)
    with _sparse_lock:
        changed = _last_sparse_pattern.get(wiki_key) != new_pattern
        if changed and wiki.is_dir() and (wiki / ".git").exists():
            from utils.git import reapply_sparse_checkout
            # Update sparse-checkout to the new branch folder. Only record the
            # switch as applied if the sparse write succeeded, so a transient
            # failure (config locked, etc.) is retried on the next call instead
            # of leaving the working tree pinned to the old branch forever.
            if set_sparse_checkout_cone(wiki, [new_pattern]):
                _last_sparse_pattern[wiki_key] = new_pattern
                # Reconcile the working tree to the new sparse view so the wiki
                # follows the parent repo's branch. `sparse-checkout reapply`
                # prunes now-excluded tracked content and materializes the new
                # branch's folder, while PRESERVING any uncommitted/untracked
                # files (git warns instead of deleting them) — so this is safe
                # to run unconditionally, even when the tree is dirty.
                reapply_sparse_checkout(wiki)
                # Invalidate in-memory search index so next query rebuilds
                from utils.wiki_index import WikiIndex
                WikiIndex.invalidate(repo_name, branch)
        elif changed:
            # Wiki not bootstrapped yet — record the pattern so we don't probe
            # the filesystem on every call; pull_wiki performs the real setup.
            _last_sparse_pattern[wiki_key] = new_pattern

    # Configure main repo to never touch wiki submodule on branch switch
    # (prevents "unable to rmdir 'wiki'" warnings). Idempotent, so do it once
    # per repo_path rather than spawning a git subprocess on every tool call.
    wiki_path = Path(repo_path) / "wiki"
    if wiki_path.is_dir():
        with _sparse_lock:
            already = repo_path in _submodule_update_configured
        if not already:
            from utils.git import run_git
            run_git(["config", "submodule.wiki.update", "none"], cwd=repo_path, check=False)
            with _sparse_lock:
                _submodule_update_configured.add(repo_path)

    return repo_path, repo_name, branch, None


def _run_with_usage(tool_name: str, input_payload: dict, fn) -> dict:
    """Record estimated input/output usage around a tool execution."""
    record_tool_input(tool_name, input_payload)
    result = fn()
    record_tool_output(tool_name, result)
    return result



@mcp.tool()
def list_remote_wiki(cwd: str, pattern: str | None = None) -> dict:
    """List remote wiki repositories.

    Returns a list of repos with their available branches.
    Optionally filters by a pattern (case-insensitive substring match on repo name).

    cwd: any absolute path inside any code repo that has the wiki submodule.
    pattern: optional filter to match against repo names.
    """
    def _impl() -> dict:
        repo_path, _, _, err = _resolve_context(cwd)
        if err:
            return err
        return list_remote_wikis_impl(pattern=pattern, repo_path=repo_path)

    return _run_with_usage("list_remote_wiki", {"cwd": cwd, "pattern": pattern}, _impl)


@mcp.tool()
def pull_wiki(cwd: str, repo_name: str | None = None, branch: str | None = None) -> dict:
    """Sync the wiki from the remote (sparse-checkout). Call at session start.

    cwd: any absolute path inside the developer's code repo (e.g. their working directory).
         The git repo root, repo name, and branch are auto-detected from this path.

    Auto-bootstraps the wiki submodule on first use via WIKI_MCP_REMOTE_URL.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return pull_impl(repo_name=resolved_repo_name, branch=resolved_branch, repo_path=repo_path)

    return _run_with_usage(
        "pull_wiki",
        {"cwd": cwd, "repo_name": repo_name, "branch": branch},
        _impl,
    )


@mcp.tool()
def init_wiki(cwd: str, repo_name: str | None = None, branch: str | None = None) -> dict:
    """Bootstrap this repo's wiki folder for the first time.

    cwd: any absolute path inside the developer's code repo.
    Returns file tree + detected domains.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return init_impl(repo_name=resolved_repo_name, branch=resolved_branch, repo_path=repo_path)

    return _run_with_usage(
        "init_wiki",
        {"cwd": cwd, "repo_name": repo_name, "branch": branch},
        _impl,
    )


@mcp.tool()
def query_wiki(topic: str, cwd: str, repo_name: str | None = None, branch: str | None = None) -> dict:
    """Search the CURRENT repo's wiki by keyword. Use this when the topic relates to the repo you're already working in.

    For searching a DIFFERENT repo's wiki, use query_remote_wiki instead.

    Returns index + ranked page paths.
    cwd: any absolute path inside the developer's code repo.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return query_impl(topic, repo_name=resolved_repo_name, branch=resolved_branch, repo_path=repo_path)

    return _run_with_usage(
        "query_wiki",
        {"topic": topic, "cwd": cwd, "repo_name": repo_name, "branch": branch},
        _impl,
    )


@mcp.tool()
def query_remote_wiki(topic: str, cwd: str, repo_name: str, branch: str = "master") -> dict:
    """Search a DIFFERENT repo's wiki by keyword. Use this when the topic relates to a repo OTHER than the one you're working in.

    Example: you're in repo "frontend-app" but need docs from repo "divp-http-metrics" → use query_remote_wiki with repo_name="divp-http-metrics".

    Returns page contents inline — no follow-up fetch_wiki calls needed.
    cwd: any absolute path inside any code repo that has the wiki submodule.
    repo_name: the TARGET repo whose wiki you want to search (required, not your current repo).
    branch: the branch to search in (default: "master").
    """
    def _impl() -> dict:
        caller_repo_path, _, _, err = _resolve_context(cwd)
        if err:
            return err
        return query_remote_impl(topic, repo_name=repo_name, branch=branch, repo_path=caller_repo_path)

    return _run_with_usage(
        "query_remote_wiki",
        {"topic": topic, "cwd": cwd, "repo_name": repo_name, "branch": branch},
        _impl,
    )


@mcp.tool()
def fetch_wiki(path: str, cwd: str, repo_name: str | None = None, branch: str | None = None) -> dict:
    """Load a specific wiki page (path relative to this repo's wiki folder).

    cwd: any absolute path inside the developer's code repo.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return fetch_impl(path, repo_name=resolved_repo_name, branch=resolved_branch, repo_path=repo_path)

    return _run_with_usage(
        "fetch_wiki",
        {"path": path, "cwd": cwd, "repo_name": repo_name, "branch": branch},
        _impl,
    )


@mcp.tool()
def ingest_wiki(
    cwd: str,
    repo_name: str | None = None,
    branch: str | None = None,
    paths: list[str] | None = None,
    topic: str | None = None,
) -> dict:
    """Get the git diff of code changes plus the wiki index so the agent can update pages.

    cwd: any absolute path inside the developer's code repo.

    Optional scope controls:
    - paths: repo-relative globs/dir prefixes (e.g., ["src/auth", "src/**/*.py"]).
    - topic: case-insensitive keyword matched against file paths.

    paths and topic are mutually exclusive; both are optional. Omit both for
    full-repo ingest behavior.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return ingest_impl(
            repo_name=resolved_repo_name,
            branch=resolved_branch,
            repo_path=repo_path,
            paths=paths,
            topic=topic,
        )

    return _run_with_usage(
        "ingest_wiki",
        {
            "cwd": cwd,
            "repo_name": repo_name,
            "branch": branch,
            "paths": paths,
            "topic": topic,
        },
        _impl,
    )


@mcp.tool()
def lint_wiki(cwd: str, repo_name: str | None = None, branch: str | None = None) -> dict:
    """Get domain indexes and the repo file tree so the agent can audit wiki health.

    cwd: any absolute path inside the developer's code repo.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return lint_impl(repo_name=resolved_repo_name, branch=resolved_branch, repo_path=repo_path)

    return _run_with_usage(
        "lint_wiki",
        {"cwd": cwd, "repo_name": repo_name, "branch": branch},
        _impl,
    )


@mcp.tool()
def push_wiki(cwd: str, repo_name: str | None = None, branch: str | None = None, message: str | None = None, confirm: bool = False) -> dict:
    """Commit and push wiki changes to the remote wiki branch.

    cwd: any absolute path inside the developer's code repo.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return push_impl(
            message=message,
            repo_name=resolved_repo_name,
            branch=resolved_branch,
            repo_path=repo_path,
            confirm=confirm,
        )

    return _run_with_usage(
        "push_wiki",
        {
            "cwd": cwd,
            "repo_name": repo_name,
            "branch": branch,
            "message": message,
            "confirm": confirm,
        },
        _impl,
    )


@mcp.tool()
def delete_wiki(cwd: str, repo_name: str | None = None, branch: str | None = None) -> dict:
    """Delete a repo/branch wiki folder from the remote. Refuses to delete main or master.

    cwd: any absolute path inside the developer's code repo.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return delete_wiki_impl(branch=resolved_branch, repo_name=resolved_repo_name, repo_path=repo_path)

    return _run_with_usage(
        "delete_wiki",
        {"cwd": cwd, "repo_name": repo_name, "branch": branch},
        _impl,
    )


@mcp.tool()
def reset_wiki(cwd: str, repo_name: str | None = None, branch: str | None = None, force: bool = False) -> dict:
    """Full clean-slate reset: deletes wiki content from both local AND remote.

    Removes this repo/branch's wiki pages from the remote wiki branch,
    removes the local submodule, and cleans up all artifacts.
    After reset, pull_wiki will start fresh with empty scaffolding.

    cwd: any absolute path inside the developer's code repo.
    Dry-run by default (force=False). Call again with force=True to execute.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return reset_wiki_impl(
            force,
            repo_path=repo_path,
            repo_name=resolved_repo_name,
            branch=resolved_branch,
        )

    return _run_with_usage(
        "reset_wiki",
        {"cwd": cwd, "repo_name": repo_name, "branch": branch, "force": force},
        _impl,
    )


@mcp.tool()
def wiki_status(cwd: str, repo_name: str | None = None, branch: str | None = None) -> dict:
    """Show the current sync status of the wiki for the active repo and branch.

    Fetches from the remote first to ensure accurate behind/ahead counts.
    Use pull_wiki to sync, push_wiki to publish, or resolve_wiki_issue to fix problems.

    cwd: any absolute path inside the developer's code repo.
    repo_name: optional override (auto-derived from cwd if omitted).
    branch: optional override (auto-derived from cwd if omitted).
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return status_impl(repo_name=resolved_repo_name, branch=resolved_branch, repo_path=repo_path)

    return _run_with_usage(
        "wiki_status",
        {"cwd": cwd, "repo_name": repo_name, "branch": branch},
        _impl,
    )


@mcp.tool()
def resolve_wiki_issue(cwd: str, action: str | None = None) -> dict:
    """Diagnose and fix wiki problems. Never ask the user to run git commands — use this tool instead.

    Call without action to diagnose issues and see resolution options.
    Call with action (e.g. "merge_conflict:keep_local") to execute a fix.

    Each resolution option has a plain-language description of what it will do.
    Present these to the user and let them choose.

    cwd: any absolute path inside the developer's code repo.
    action: qualified resolution ID from diagnosis (e.g. "diverged:merge"). Omit to diagnose.
    """
    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd)
        if err:
            return err
        return resolve_impl(
            action=action,
            repo_path=repo_path,
            repo_name=resolved_repo_name,
            branch=resolved_branch,
        )

    return _run_with_usage("resolve_wiki_issue", {"cwd": cwd, "action": action}, _impl)


@mcp.tool()
def wiki_usage(cwd: str, repo_name: str | None = None, branch: str | None = None) -> dict:
    """Show estimated wiki MCP input/output token usage for the current session.

    Returns session totals and a per-tool breakdown.
    """

    def _impl() -> dict:
        repo_path, resolved_repo_name, resolved_branch, err = _resolve_context(cwd, repo_name, branch)
        if err:
            return err
        return usage_impl(repo_name=resolved_repo_name, branch=resolved_branch, repo_path=repo_path)

    return _run_with_usage(
        "wiki_usage",
        {"cwd": cwd, "repo_name": repo_name, "branch": branch},
        _impl,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
