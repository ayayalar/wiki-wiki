"""pull: sync the wiki from the remote with sparse-checkout.

All repos and code branches share a single remote branch named ``wiki``.
Content is stored under ``{repo_name}/{code_branch}/`` within that branch.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from config import repo_root as _default_repo_root
from tools.init import scaffold_repo_wiki_if_empty
from tools.resolve import MERGE_CLEAN, auto_resolve_conflicts, finish_merge, merge_remote_ref
from utils.git import (
    WIKI_REMOTE_BRANCH,
    WIKI_REMOTE_REF,
    derive_branch,
    derive_repo_name,
    init_submodule_config,
    is_dirty,
    ref_exists,
    run_git,
    set_sparse_checkout_cone,
    submodule_exists,
    wiki_is_initialized,
)
from utils.wiki import check_params, read_remote_url
from utils.wiki_index import WikiIndex


def _exclude_wiki_dir(root: Path) -> None:
    """Ensure `wiki/` is ignored by the parent repo via `.git/info/exclude`.

    This is local-only and intentionally untracked. If `<root>/.git` is a file
    (worktree/submodule layout), skip silently.
    """
    git_path = root / ".git"
    if git_path.is_file():
        return

    info_dir = git_path / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude_path = info_dir / "exclude"
    text = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if any(line.strip() == "wiki/" for line in text.splitlines()):
        return

    prefix = "" if text == "" or text.endswith("\n") else "\n"
    with exclude_path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(f"{prefix}wiki/\n")


def _bootstrap_clone(root: Path, url: str, repo_name: str, branch: str) -> dict | None:
    """Bootstrap wiki as a plain nested clone at `<root>/wiki` (no submodule commit)."""
    preflight = run_git(
        ["-c", "protocol.file.allow=always", "ls-remote", url],
        cwd=root,
        check=False,
    )
    if preflight.returncode != 0:
        return {
            "status": "bootstrap_failed",
            "error": (
                f"Wiki remote {url!r} is not reachable (git ls-remote failed). "
                f"Verify WIKI_MCP_REMOTE_URL points to an existing bare git repo. "
                f"Original git stderr: {preflight.stderr.strip()}"
            ),
            "remote_url": url,
        }

    clone = run_git(
        [
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--depth",
            "1",
            "--no-checkout",
            "--branch",
            WIKI_REMOTE_BRANCH,
            url,
            str(root / "wiki"),
        ],
        cwd=root,
        check=False,
    )
    if clone.returncode != 0:
        return {
            "status": "bootstrap_failed",
            "error": (
                f"git clone failed for {url!r}. Verify the URL points "
                f"to an existing reachable bare git repo. "
                f"Original git stderr: {clone.stderr.strip()}"
            ),
            "remote_url": url,
        }

    sparse_pattern = f"{repo_name}/{branch}"
    wiki = root / "wiki"
    if not set_sparse_checkout_cone(wiki, [sparse_pattern]):
        run_git(["sparse-checkout", "set", sparse_pattern], cwd=wiki, check=False)

    checkout = run_git(["checkout", WIKI_REMOTE_BRANCH], cwd=wiki, check=False)
    if checkout.returncode != 0:
        run_git(["checkout", "--force", "HEAD"], cwd=wiki, check=False)

    _exclude_wiki_dir(root)
    return None


class PullBuilder:
    """Builds and executes a pull workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._repo_name: str = ""
        self._branch: str = ""
        self._root: Path = Path()
        self._wiki: Path = Path()
        self._repo_wiki_path: Path | None = None
        self._bootstrapped: bool = False
        self._sync_errors: list[str] = []
        self._remote_branch_existed: bool = False
        self._accumulated: dict[str, Any] = {}

    # ── configuration ─────────────────────────────────────────────

    def for_repo_branch(
        self,
        repo_name: str | None,
        branch: str | None,
        repo_path: str | None,
    ) -> PullBuilder:
        """Resolve repo root, repo name, and branch from the given inputs."""
        self._root = Path(repo_path).resolve() if repo_path else _default_repo_root()
        self._wiki = self._root / "wiki"
        # Derive identity from the resolved repo root (not the server process's
        # cwd), so an explicit repo_path is honored — mirrors
        # server._resolve_context. get_repo_name()/get_current_branch() would
        # walk up from cwd and could resolve a different repo entirely.
        self._repo_name = repo_name or derive_repo_name(self._root)
        self._branch = branch or derive_branch(self._root)
        return self

    # ── stage 1: validate ─────────────────────────────────────────

    def _validate(self) -> tuple[bool, dict | None]:
        """Guard: WIKI_MCP_REPO_ROOT mismatch + param validation (C1)."""
        configured_root = os.environ.get("WIKI_MCP_REPO_ROOT", "")
        if configured_root and Path(configured_root).resolve() != self._root:
            return False, {
                "status": "bootstrap_refused",
                "error": (
                    f"WIKI_MCP_REPO_ROOT is set to {configured_root!r} but "
                    f"repo_path resolves to {self._root}. Refusing bootstrap to "
                    f"prevent accidental wiki initialization in the wrong repository."
                ),
                "configured_root": str(Path(configured_root).resolve()),
                "actual_root": str(self._root),
            }
        err = check_params(self._repo_name, self._branch)
        if err:
            return False, err
        return True, None

    # ── stage 2: bootstrap ────────────────────────────────────────

    def _bootstrap(self) -> tuple[bool, dict | None]:
        """Ensure the wiki checkout is ready (nested clone or legacy submodule)."""
        if wiki_is_initialized(self._wiki):
            if submodule_exists("wiki", self._root):
                run_git(
                    ["config", "submodule.wiki.update", "none"],
                    cwd=self._root,
                    check=False,
                )
            return True, None

        if not submodule_exists("wiki", self._root):
            if self._wiki.exists() and any(self._wiki.iterdir()):
                return False, {
                    "status": "wiki_dir_blocked",
                    "error": (
                        "wiki/ exists but is not a submodule. "
                        "Call resolve_wiki_issue() or reset_wiki(force=True) "
                        "to clean up and re-bootstrap."
                    ),
                    "wiki_path": str(self._wiki),
                    "repo": self._repo_name,
                    "branch": self._branch,
                }
            url = read_remote_url()
            if not url:
                return False, {
                    "status": "needs_setup",
                    "repo": self._repo_name,
                    "branch": self._branch,
                    "instruction": (
                        "No wiki checkout is configured and "
                        "WIKI_MCP_REMOTE_URL is not set. Add "
                        '"WIKI_MCP_REMOTE_URL": "<wiki-repo-url>" '
                        "to the MCP server's env block in your client config, "
                        "then restart the agent."
                    ),
                }
            err = _bootstrap_clone(self._root, url, self._repo_name, self._branch)
            if err is not None:
                return False, err
            self._bootstrapped = True

        if submodule_exists("wiki", self._root) and not init_submodule_config(self._root, "wiki"):
            run_git(["submodule", "init", "wiki"], cwd=self._root, check=False)

        return True, None

    # ── stage 2b: configure_sparse_checkout ───────────────────────

    def _configure_sparse_checkout(self) -> tuple[bool, dict | None]:
        """Set sparse-checkout to this repo+branch folder; clone if needed."""
        sparse_pattern = f"{self._repo_name}/{self._branch}"
        if self._wiki.exists() and (self._wiki / ".git").exists():
            if not set_sparse_checkout_cone(self._wiki, [sparse_pattern]):
                run_git(
                    ["sparse-checkout", "set", sparse_pattern],
                    cwd=self._wiki,
                    check=False,
                )
        elif submodule_exists("wiki", self._root):
            run_git(
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
                cwd=self._root,
                check=False,
            )
            if not (self._wiki / ".git").exists():
                run_git(
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
                    cwd=self._root,
                    check=False,
                )
            if (self._wiki / ".git").exists() and not set_sparse_checkout_cone(
                self._wiki, [sparse_pattern]
            ):
                run_git(
                    ["sparse-checkout", "set", sparse_pattern],
                    cwd=self._wiki,
                    check=False,
                )
        return True, None

    # ── stage 3: resolve_pre_sync_conflicts ───────────────────────

    def _resolve_pre_sync_conflicts(self) -> tuple[bool, dict | None]:
        """Auto-resolve merge conflicts before sync (H4 fix)."""
        if (self._wiki / ".git").exists():
            auto_resolve_conflicts(self._wiki, self._repo_name)
        return True, None

    # ── stage 4: finish_in_progress_merge ─────────────────────────

    def _finish_in_progress_merge(self) -> tuple[bool, dict | None]:
        """Complete any in-progress merge before fetch operations."""
        if (self._wiki / ".git").exists():
            finish_merge(self._wiki)
        return True, None

    # ── stage 5: guard_unpushed ───────────────────────────────────

    def _guard_unpushed(self) -> tuple[bool, dict | None]:
        """Refuse if local wiki commits exist that are not yet on the remote."""
        if not (self._wiki / ".git").exists():
            return True, None
        run_git(
            [
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "origin",
                WIKI_REMOTE_BRANCH,
                "--deepen=10",
            ],
            cwd=self._wiki,
            check=False,
        )
        remote_ref = WIKI_REMOTE_REF
        if not ref_exists(remote_ref, cwd=self._wiki):
            return True, None
        path = f"{self._repo_name}/{self._branch}/"
        ahead = run_git(
            ["rev-list", "--count", f"{remote_ref}..HEAD", "--", path],
            cwd=self._wiki,
            check=False,
        )
        if ahead.returncode != 0:
            return True, None
        ahead_count = ahead.stdout.strip()
        if not ahead_count.isdigit() or int(ahead_count) <= 0:
            return True, None
        return False, {
            "status": "unpushed_changes",
            "repo": self._repo_name,
            "branch": self._branch,
            "wiki_path": str(self._wiki),
            "commits_ahead": int(ahead_count),
            "message": (
                f"You have {ahead_count} local wiki commit(s) not yet pushed to the remote."
            ),
            "instruction": (
                "Call push_wiki to commit and publish your changes, then call pull_wiki again."
            ),
        }

    # ── stage 6: guard_uncommitted ────────────────────────────────

    def _guard_uncommitted(self) -> tuple[bool, dict | None]:
        """Refuse if the wiki working tree has uncommitted changes."""
        if (self._wiki / ".git").exists() and is_dirty(self._wiki):
            return False, {
                "status": "uncommitted_changes",
                "repo": self._repo_name,
                "branch": self._branch,
                "wiki_path": str(self._wiki),
                "message": (
                    "You have uncommitted changes in the wiki. Pulling would discard these changes."
                ),
                "instruction": (
                    "Call push_wiki to commit and publish your changes, then call pull_wiki again."
                ),
            }
        return True, None

    # ── stage 7: fetch_and_merge ──────────────────────────────────

    def _fetch_and_merge(self) -> tuple[bool, dict | None]:
        """Fetch from remote, checkout local branch, merge remote tracking."""
        self._sync_errors = []
        if not (self._wiki / ".git").exists():
            return True, None

        result = run_git(
            [
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "origin",
                WIKI_REMOTE_BRANCH,
                "--deepen=10",
            ],
            cwd=self._wiki,
            check=False,
        )
        if result.returncode != 0:
            fallback = run_git(
                ["-c", "protocol.file.allow=always", "fetch", "origin", "--deepen=10"],
                cwd=self._wiki,
                check=False,
            )
            if fallback.returncode != 0:
                self._sync_errors.append(f"fetch failed: {fallback.stderr.strip()}")

        checkout_result = run_git(
            ["checkout", "-B", WIKI_REMOTE_BRANCH],
            cwd=self._wiki,
            check=False,
        )
        if checkout_result.returncode != 0:
            self._sync_errors.append(f"checkout failed: {checkout_result.stderr.strip()}")

        # Shared classifier: ff-only → fallback merge → classify. Pull does
        # not auto-resolve; any non-clean outcome is handed to
        # resolve_wiki_issue (a conflicting merge is left in progress for it
        # to pick up, matching the prior behavior).
        if (
            ref_exists(WIKI_REMOTE_REF, cwd=self._wiki)
            and merge_remote_ref(self._wiki) != MERGE_CLEAN
        ):
            return False, {
                "status": "merge_conflict",
                "repo": self._repo_name,
                "branch": self._branch,
                "wiki_path": str(self._wiki),
                "resolve_action": "resolve_wiki_issue",
                "message": (
                    "Merge conflict with remote. Pull cannot "
                    "auto-resolve. Use resolve_wiki_issue to "
                    "diagnose and fix."
                ),
            }

        return True, None

    # ── stage 8: sync_working_tree_and_unstash ────────────────────

    def _sync_working_tree_and_unstash(self) -> tuple[bool, dict | None]:
        """Re-sync working tree (C2) after fetch/merge."""
        self._remote_branch_existed = (
            ref_exists(
                WIKI_REMOTE_REF,
                cwd=self._wiki,
            )
            if (self._wiki / ".git").exists()
            else False
        )

        if not self._sync_errors and (self._wiki / ".git").exists():
            run_git(["read-tree", "-mu", "HEAD"], cwd=self._wiki, check=False)
            run_git(["checkout", "--force", "HEAD"], cwd=self._wiki, check=False)

        return True, None

    # ── stage 9: scaffold ─────────────────────────────────────────

    def _scaffold(self) -> tuple[bool, dict | None]:
        """Auto-scaffold CLAUDE.md, index.md, log.md if folder is empty."""
        if not (self._wiki / ".git").exists():
            return True, None
        self._repo_wiki_path = self._wiki / self._repo_name / self._branch
        scaffold_result = scaffold_repo_wiki_if_empty(
            self._repo_wiki_path,
            self._repo_name,
            wiki_root=self._wiki,
            code_root=self._root,
        )
        if scaffold_result is not None:
            return True, scaffold_result
        return True, None

    # ── stage 10: invalidate_cache ────────────────────────────────

    def _invalidate_cache(self) -> tuple[bool, dict | None]:
        """Clear the in-memory search index for this repo+branch."""
        WikiIndex.invalidate(self._repo_name, self._branch)
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        """Run all stages in order, short-circuiting on the first error."""
        self._accumulated = {
            "branch": self._branch,
            "repo": self._repo_name,
            "path": f"{self._repo_name}/{self._branch}/",
            "wiki_path": str(self._repo_wiki_path or (self._wiki / self._repo_name / self._branch)),
        }

        stages = [
            self._validate,
            self._bootstrap,
            self._configure_sparse_checkout,
            self._resolve_pre_sync_conflicts,
            self._finish_in_progress_merge,
            self._guard_unpushed,
            self._guard_uncommitted,
            self._fetch_and_merge,
            self._sync_working_tree_and_unstash,
            self._scaffold,
            self._invalidate_cache,
        ]

        for stage in stages:
            ok, result = stage()
            if not ok:
                assert result is not None, f"stage {stage.__name__} returned (False, None)"
                return result
            if result is not None:
                self._accumulated.update(result)

        return self.to_result()

    def to_result(self) -> dict:
        """Assemble the final response dict from accumulated state."""
        response: dict[str, Any] = dict(self._accumulated)
        response.setdefault("status", "synced" if not self._sync_errors else "sync_errors")
        response["bootstrapped"] = self._bootstrapped
        response["remote_branch_existed"] = self._remote_branch_existed
        if self._sync_errors:
            response["sync_errors"] = self._sync_errors
        return response


# ── backward-compatible entry point ───────────────────────────────────


def pull(
    repo_name: str | None = None, branch: str | None = None, repo_path: str | None = None
) -> dict:
    return PullBuilder().for_repo_branch(repo_name, branch, repo_path).execute()
