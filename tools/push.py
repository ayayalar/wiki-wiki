"""push: commit and push this repo's wiki folder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import wiki_path as _default_wiki_path
from tools.resolve import (
    MERGE_CLEAN,
    MERGE_REFUSED,
    _abort_merge_safe,
    _has_merge_in_progress,
    finish_merge,
    merge_remote_ref,
)
from utils.git import (
    WIKI_REMOTE_BRANCH,
    WIKI_REMOTE_REF,
    _no_verify_flag,
    get_current_branch,
    get_repo_name,
    ref_exists,
    run_git,
    wiki_is_initialized,
)
from utils.wiki import check_params, wiki_not_initialized_response
from utils.wiki_index import WikiIndex


def _sync_before_push(wiki: Path, repo_name: str) -> str | None:
    """Fetch + merge remote wiki before pushing.

    Returns:
        None — nothing to merge or clean merge
        "conflict" — merge started and produced unresolved conflicts (MERGE_HEAD present)
        "refused" — git declined to start the merge (e.g. the working tree has
            uncommitted changes outside this repo's folder); nothing to abort
    """
    fetch = run_git(
        ["-c", "protocol.file.allow=always", "fetch", "origin", WIKI_REMOTE_BRANCH, "--deepen=10"],
        cwd=wiki,
        check=False,
    )
    if fetch.returncode != 0:
        return None  # nothing to merge (remote branch may not exist yet)

    if not ref_exists(WIKI_REMOTE_REF, cwd=wiki):
        return None

    # Check if local is ahead of remote (has unpushed commits).
    # Uses rev-list which works correctly with shallow clones.
    ahead = run_git(
        ["rev-list", "--count", f"{WIKI_REMOTE_REF}..HEAD"],
        cwd=wiki,
        check=False,
    )
    if ahead.returncode == 0:
        count = ahead.stdout.strip()
        if count.isdigit() and int(count) > 0:
            # Local has unpushed commits — also check if remote has new content.
            # If remote is equal or behind, no merge needed.
            behind = run_git(
                ["rev-list", "--count", f"HEAD..{WIKI_REMOTE_REF}"],
                cwd=wiki,
                check=False,
            )
            behind_count = 0
            if behind.returncode == 0:
                bc = behind.stdout.strip()
                behind_count = int(bc) if bc.isdigit() else 0
            if behind_count == 0 and behind.returncode == 0:
                return None  # purely ahead — nothing to merge

    # Shared classifier (ff-only → fallback merge → classify). A started-but-
    # unresolved merge (conflict or stuck) leaves MERGE_HEAD and needs the
    # caller to abort; a refusal changed nothing.
    outcome = merge_remote_ref(wiki)
    if outcome == MERGE_CLEAN:
        return None
    if outcome == MERGE_REFUSED:
        return "refused"
    return "conflict"


def _classify_push_error(stderr: str) -> str:
    """Classify a push failure reason into an actionable hint."""
    if "non-fast-forward" in stderr:
        return (
            "Push rejected: remote has changes not in your local wiki. "
            "Call resolve_wiki_issue() to see sync options (pull_and_merge, reset_to_remote)."
        )
    if "rejected" in stderr and "force-with-lease" in stderr:
        return (
            "Push rejected: remote has new content from another agent. "
            "Call resolve_wiki_issue() with action='diverged:merge' to merge first."
        )
    if "authentication" in stderr.lower() or "permission denied" in stderr.lower():
        return (
            "Push rejected: authentication or permission error. "
            "Verify your git credentials and repository access."
        )
    if "is not a shallow superproject" in stderr or "unable to access" in stderr.lower():
        return (
            "Push rejected: remote may be unreachable or URL is incorrect. "
            "Call resolve_wiki_issue() to diagnose."
        )
    return f"Push failed: {stderr}. Call resolve_wiki_issue() to diagnose and resolve."


# ── PushBuilder ────────────────────────────────────────────────────────


class PushBuilder:
    """Builds and executes a push workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._repo_name: str = ""
        self._branch: str = ""
        self._wiki: Path = Path()
        self._rel_path: str = ""
        self._commit_msg: str = ""
        self._stashed: bool = False
        self._has_changes: bool = False
        self._push_ok: bool = False
        self._push_stderr: str = ""
        self._push_hint: str = ""
        self._accumulated: dict[str, Any] = {}

    # ── configuration ─────────────────────────────────────────────

    def for_repo_branch(
        self,
        repo_name: str | None,
        branch: str | None,
        repo_path: str | None,
        message: str | None = None,
    ) -> PushBuilder:
        """Resolve wiki path, repo name, branch, and commit message."""
        self._wiki = (Path(repo_path).resolve() / "wiki") if repo_path else _default_wiki_path()
        self._repo_name = repo_name or get_repo_name()
        self._branch = branch or get_current_branch()
        self._rel_path = f"{self._repo_name}/{self._branch}"
        self._commit_msg = message or f"wiki: update {self._repo_name} for {self._branch}"
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        """Validate wiki is initialized and params are safe (C1)."""
        if not wiki_is_initialized(self._wiki):
            return False, wiki_not_initialized_response(self._wiki)
        err = check_params(self._repo_name, self._branch)
        if err:
            return False, err
        return True, None

    # ── stage 2: check merge in progress ──────────────────────────

    def _check_merge_in_progress(self) -> tuple[bool, dict | None]:
        """Finish any in-progress merge; return error if still unresolved."""
        if not finish_merge(self._wiki) and _has_merge_in_progress(self._wiki):
            return False, {
                "status": "merge_in_progress",
                "resolve_action": "merge_conflict:keep_local",
                "repo": self._repo_name,
                "branch": self._branch,
                "message": (
                    "A merge is in progress with unresolved conflicts. "
                    "Call resolve_wiki_issue() to see resolution options."
                ),
            }
        return True, None

    # ── stage 3: detect and stash ─────────────────────────────────

    def _detect_and_stash(self) -> tuple[bool, dict | None]:
        """Check for changes in this repo's wiki folder; stash if dirty.

        Only this repo's wiki folder (``rel_path``) is stashed so the
        pre-commit remote sync can fast-forward cleanly; the stash is
        popped back in ``_unstash``. Changes elsewhere in the shared wiki
        tree are left untouched.
        """
        status_result = run_git(
            ["status", "--porcelain", "--", self._rel_path],
            cwd=self._wiki,
            check=False,
        )
        self._has_changes = bool(status_result.stdout.strip())

        # Gate on rel_path being dirty (not the whole tree): we only stash
        # rel_path, so dirt elsewhere in the shared wiki must not trigger a
        # phantom stash whose later `pop` would fail or hit an unrelated entry.
        if self._has_changes:
            stash_result = run_git(
                ["stash", "push", "-u", "-m", "wiki: auto-stash before push", "--", self._rel_path],
                cwd=self._wiki,
                check=False,
            )
            # `git stash push` exits 0 and prints "No local changes to save"
            # when it creates no entry. Only treat ourselves as stashed when
            # an entry was actually pushed — otherwise `_unstash` would pop a
            # pre-existing, unrelated stash. Never `stash clear`: entries left
            # by a prior failed pop (re-applied via `stash apply`) are the only
            # copy of a user's edits and must be preserved.
            combined = f"{stash_result.stdout}\n{stash_result.stderr}"
            self._stashed = (
                stash_result.returncode == 0 and "No local changes to save" not in combined
            )
        return True, None

    # ── stage 4: handle no changes ────────────────────────────────

    def _handle_no_changes(self) -> tuple[bool, dict | None]:
        """Short-circuit when there's nothing to commit locally.

        Checks for unpushed commits from a prior run and pushes them.
        Otherwise returns ``nothing to commit``.
        """
        if self._has_changes or self._stashed:
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
        if ref_exists(WIKI_REMOTE_REF, cwd=self._wiki):
            ahead = run_git(
                ["rev-list", "--count", f"{WIKI_REMOTE_REF}..HEAD"],
                cwd=self._wiki,
                check=False,
            )
            if ahead.returncode == 0:
                count = ahead.stdout.strip()
                if count.isdigit() and int(count) > 0:
                    merge_status = _sync_before_push(self._wiki, self._repo_name)
                    if merge_status == "conflict":
                        _abort_merge_safe(self._wiki)
                        return False, {
                            "status": "merge_conflict",
                            "resolve_action": "merge_conflict:keep_local",
                            "repo": self._repo_name,
                            "branch": self._branch,
                            "message": (
                                "Pre-push sync produced conflicts with remote changes. "
                                "Call resolve_wiki_issue() to see resolution options."
                            ),
                        }
                    if merge_status == "refused":
                        return False, {
                            "status": "sync_refused",
                            "repo": self._repo_name,
                            "branch": self._branch,
                            "message": (
                                "Pre-push sync could not merge remote changes: git "
                                "refused the merge, usually because the wiki working "
                                "tree has uncommitted changes. Commit or discard them, "
                                "then retry push_wiki(confirm=True)."
                            ),
                        }
                    push_result = run_git(
                        ["push", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
                        cwd=self._wiki,
                        check=False,
                    )
                    WikiIndex.invalidate(self._repo_name, self._branch)
                    if push_result.returncode == 0:
                        return False, {
                            "status": "pushed",
                            "branch": self._branch,
                            "repo": self._repo_name,
                            "message": "pushed unpushed commits",
                        }
                    self._push_ok = False
                    self._push_stderr = push_result.stderr.strip()
                    self._push_hint = _classify_push_error(self._push_stderr)
                    return False, {
                        "status": "committed_no_push",
                        "branch": self._branch,
                        "repo": self._repo_name,
                        "push_stderr": self._push_stderr,
                        "hint": self._push_hint,
                    }
        return False, {
            "status": "nothing to commit",
            "branch": self._branch,
            "repo": self._repo_name,
            "wiki_path": str(self._wiki / self._repo_name / self._branch),
        }

    # ── stage 5: sync with remote ─────────────────────────────────

    def _preview(self) -> tuple[bool, dict | None]:
        """Return a preview of what would be pushed without committing.

        Runs ``git status --porcelain`` to list changed files.  If nothing
        changed, returns ``nothing to commit``.  Otherwise returns
        ``pending_confirmation`` with the file list and proposed message.
        """
        status_result = run_git(
            ["status", "--porcelain", "--", self._rel_path],
            cwd=self._wiki,
            check=False,
        )
        changed = status_result.stdout.strip()
        if not changed:
            return False, {
                "status": "nothing to commit",
                "branch": self._branch,
                "repo": self._repo_name,
                "wiki_path": str(self._wiki / self._repo_name / self._branch),
            }
        changed_files = [line.strip() for line in changed.splitlines() if line.strip()]
        return False, {
            "status": "pending_confirmation",
            "repo": self._repo_name,
            "branch": self._branch,
            "message": self._commit_msg,
            "changed_files": changed_files,
            "instruction": (
                "SHOW THIS PREVIEW TO THE USER and wait for explicit approval "
                "before calling push_wiki(confirm=True). DO NOT auto-confirm."
            ),
        }

    def _sync_with_remote(self) -> tuple[bool, dict | None]:
        """Fetch + merge remote before committing (H1 fix: TOCTOU prevention).

        On merge conflict, aborts the merge and restores any stashed changes.
        """
        merge_status = _sync_before_push(self._wiki, self._repo_name)
        if merge_status == "conflict":
            _abort_merge_safe(self._wiki)
            if self._stashed:
                run_git(["stash", "pop"], cwd=self._wiki, check=False)
            return False, {
                "status": "merge_conflict",
                "resolve_action": "merge_conflict:keep_local",
                "repo": self._repo_name,
                "branch": self._branch,
                "message": (
                    "Pre-push sync produced conflicts with remote changes. "
                    "Call resolve_wiki_issue() to see resolution options."
                ),
            }
        if merge_status == "refused":
            # Nothing was merged and no MERGE_HEAD exists, so there is nothing
            # to abort. Restore the stashed edits and report the real cause.
            if self._stashed:
                run_git(["stash", "pop"], cwd=self._wiki, check=False)
            return False, {
                "status": "sync_refused",
                "repo": self._repo_name,
                "branch": self._branch,
                "message": (
                    "Pre-push sync could not merge remote changes: git refused "
                    "the merge, usually because the wiki working tree has "
                    "uncommitted changes outside this repo's folder. Commit or "
                    "discard them, then retry push_wiki(confirm=True)."
                ),
            }
        return True, None

    # ── stage 6: unstash ──────────────────────────────────────────

    def _unstash(self) -> tuple[bool, dict | None]:
        """Pop stashed changes after clean sync; surface stash conflicts."""
        if not self._stashed:
            return True, None
        pop_result = run_git(["stash", "pop"], cwd=self._wiki, check=False)
        if pop_result.returncode != 0:
            run_git(["stash", "apply"], cwd=self._wiki, check=False)
            return False, {
                "status": "stash_conflict",
                "resolve_action": "merge_conflict:keep_local",
                "repo": self._repo_name,
                "branch": self._branch,
                "message": (
                    "Stash pop produced conflicts with pulled changes. "
                    "Local changes were reapplied. "
                    "Call resolve_wiki_issue() to see resolution options."
                ),
            }
        return True, None

    # ── stage 7: commit and push ──────────────────────────────────

    def _commit_and_push(self) -> tuple[bool, dict | None]:
        """Add, commit, and push. Classifies push failures for actionable guidance."""
        add_result = run_git(["add", "--", self._rel_path], cwd=self._wiki, check=False)
        if add_result.returncode != 0:
            return False, {
                "status": "commit_failed",
                "repo": self._repo_name,
                "branch": self._branch,
                "message": "Failed to stage wiki changes — nothing was pushed.",
                "stderr": add_result.stderr.strip(),
            }
        commit_result = run_git(
            ["commit", *_no_verify_flag(), "-m", self._commit_msg],
            cwd=self._wiki,
            check=False,
        )
        if commit_result.returncode != 0:
            # "nothing to commit" is benign: the changes are already in HEAD
            # (e.g. identical content arrived via the pre-commit remote sync).
            # Fall through to push whatever HEAD holds. Any other non-zero exit
            # is a real failure (unset user.email/name, failing pre-commit hook
            # under WIKI_MCP_VERIFY=1) and must NOT be reported as a push.
            combined = f"{commit_result.stdout}\n{commit_result.stderr}".lower()
            if "nothing to commit" not in combined:
                return False, {
                    "status": "commit_failed",
                    "repo": self._repo_name,
                    "branch": self._branch,
                    "message": (
                        "Failed to commit wiki changes — nothing was pushed. "
                        "Common causes: git user.email/user.name unset, or a "
                        "pre-commit hook failed (WIKI_MCP_VERIFY=1)."
                    ),
                    "stderr": commit_result.stderr.strip() or commit_result.stdout.strip(),
                }
        push_result = run_git(
            ["push", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
            cwd=self._wiki,
            check=False,
        )
        self._push_ok = push_result.returncode == 0
        if not self._push_ok:
            self._push_stderr = push_result.stderr.strip()
            self._push_hint = _classify_push_error(self._push_stderr)
        return True, None

    # ── stage 8: invalidate cache ─────────────────────────────────

    def _invalidate_cache(self) -> tuple[bool, dict | None]:
        """Clear the in-memory search index for this repo+branch."""
        WikiIndex.invalidate(self._repo_name, self._branch)
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self, confirm: bool = False) -> dict:
        """Run all stages in order, short-circuiting on the first error.

        When *confirm* is False (default), runs only validation and preview
        stages — no commit or push.  When True, runs the full pipeline.
        """
        self._accumulated = {
            "branch": self._branch,
            "repo": self._repo_name,
            "message": self._commit_msg,
        }

        if confirm:
            stages = [
                self._resolve_and_validate,
                self._check_merge_in_progress,
                self._detect_and_stash,
                self._handle_no_changes,
                self._sync_with_remote,
                self._unstash,
                self._commit_and_push,
                self._invalidate_cache,
            ]
        else:
            stages = [
                self._resolve_and_validate,
                self._check_merge_in_progress,
                self._preview,
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
        """Assemble the final response from accumulated state."""
        response: dict[str, Any] = dict(self._accumulated)
        if self._push_ok:
            response["status"] = "pushed"
        else:
            response["status"] = "committed_no_push"
            response["push_stderr"] = self._push_stderr
            response["hint"] = self._push_hint
        return response


# ── backward-compatible entry point ────────────────────────────────────


def push(
    message: str | None = None,
    repo_name: str | None = None,
    branch: str | None = None,
    repo_path: str | None = None,
    confirm: bool = False,
) -> dict:
    return PushBuilder().for_repo_branch(repo_name, branch, repo_path, message).execute(confirm)
