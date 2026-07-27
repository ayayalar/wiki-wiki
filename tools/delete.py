"""delete_wiki: delete this repo's wiki folder for the current branch."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from config import wiki_path as _default_wiki_path
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

PROTECTED_BRANCHES = frozenset({"main", "master"})


class DeleteBuilder:
    """Builds and executes a delete workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._wiki: Path = Path()
        self._repo_name: str = ""
        self._branch: str = ""
        self._rel_path: str = ""
        self._repo_wiki_path: Path = Path()
        self._push_ok: bool = False
        self._push_stderr: str = ""
        self._accumulated: dict[str, Any] = {}

    def for_repo_branch(
        self,
        repo_name: str | None,
        branch: str | None,
        repo_path: str | None,
    ) -> DeleteBuilder:
        self._wiki = (Path(repo_path).resolve() / "wiki") if repo_path else _default_wiki_path()
        self._repo_name = repo_name or get_repo_name()
        self._branch = (branch or get_current_branch()).strip()
        self._rel_path = f"{self._repo_name}/{self._branch}"
        self._repo_wiki_path = self._wiki / self._repo_name / self._branch
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        if not wiki_is_initialized(self._wiki):
            return False, wiki_not_initialized_response(self._wiki)
        err = check_params(self._repo_name, self._branch)
        if err:
            return False, err

        if not self._branch or self._branch == "HEAD":
            return False, {
                "status": "refused",
                "error": (
                    "Cannot determine a branch to delete (detached HEAD or empty). "
                    "Pass branch=<name> explicitly."
                ),
            }
        if self._branch in PROTECTED_BRANCHES:
            return False, {
                "status": "refused",
                "error": (
                    f"Refusing to delete wiki content for protected branch {self._branch!r}. "
                    "main/master deletion is intentional infra work — do it manually."
                ),
                "branch": self._branch,
            }
        return True, None

    # ── stage 2: check exists ─────────────────────────────────────

    def _check_exists(self) -> tuple[bool, dict | None]:
        if not self._repo_wiki_path.exists():
            return False, {
                "status": "not_found",
                "branch": self._branch,
                "repo": self._repo_name,
                "error": f"Wiki folder {self._rel_path!r} does not exist in the working tree.",
            }
        return True, None

    # ── stage 3: delete and sync ──────────────────────────────────

    def _delete_and_sync(self) -> tuple[bool, dict | None]:
        # Sync with the remote FIRST so the delete commit lands on top of the
        # current remote tip and the push fast-forwards. Committing the delete
        # before fetching makes local HEAD undivergeable: any concurrent push
        # to the shared wiki branch then rejects ours non-fast-forward and the
        # ff-only merge can never help (HEAD already moved past the base).
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
            run_git(
                ["merge", "--ff-only", WIKI_REMOTE_REF],
                cwd=self._wiki,
                check=False,
            )

        # Now remove this repo/branch folder and commit the deletion on top.
        if self._repo_wiki_path.exists():
            shutil.rmtree(self._repo_wiki_path)

        run_git(["add", "--", self._rel_path], cwd=self._wiki, check=False)
        run_git(
            [
                "commit",
                *_no_verify_flag(),
                "-m",
                f"wiki: delete {self._repo_name} for {self._branch}",
            ],
            cwd=self._wiki,
            check=False,
        )

        push_result = run_git(
            ["push", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
            cwd=self._wiki,
            check=False,
        )
        self._push_ok = push_result.returncode == 0
        if not self._push_ok:
            self._push_stderr = push_result.stderr.strip()

        # Drop any cached search index for this repo/branch so query_wiki
        # doesn't keep serving the just-deleted pages from memory.
        from utils.wiki_index import WikiIndex

        WikiIndex.invalidate(self._repo_name, self._branch)
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        self._accumulated = {}

        stages = [
            self._resolve_and_validate,
            self._check_exists,
            self._delete_and_sync,
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
        response: dict[str, Any] = dict(self._accumulated)
        response["status"] = "deleted" if self._push_ok else "committed_no_push"
        response["branch"] = self._branch
        response["repo"] = self._repo_name
        response["path_removed"] = self._rel_path
        if not self._push_ok:
            response["push_stderr"] = self._push_stderr
        return response


# ── backward-compatible entry point ────────────────────────────────────


def delete_wiki(
    branch: str | None = None, repo_name: str | None = None, repo_path: str | None = None
) -> dict:
    return DeleteBuilder().for_repo_branch(repo_name, branch, repo_path).execute()
