"""reset_wiki: full clean-slate reset of the wiki for this repo/branch.

Removes:
  1. This repo's content from the remote wiki branch (push deletion).
  2. The local wiki submodule (deinit, rm, .git/modules cleanup).
  3. Any polluting commit left behind.

After reset, the wiki is completely gone (local + remote). The next
pull_wiki will bootstrap a fresh submodule with empty scaffolding.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

from config import repo_root as _default_repo_root
from utils.git import (
    WIKI_REMOTE_BRANCH,
    WIKI_REMOTE_REF,
    _no_verify_flag,
    get_current_branch,
    get_repo_name,
    ref_exists,
    run_git,
    submodule_exists,
    wiki_is_initialized,
)
from utils.wiki import check_params

PROTECTED_BRANCHES = frozenset({"main", "master"})


def _rm_readonly(func, path, _exc_info):
    os.chmod(path, stat.S_IWRITE)
    os.remove(path)


class ResetBuilder:
    """Builds and executes a reset workflow in discrete stages.

    Two modes:
    - Dry-run (force=False): resolves context and returns planned actions.
    - Execute (force=True): runs all destructive stages sequentially.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._root: Path = Path()
        self._wiki: Path = Path()
        self._repo_name: str = ""
        self._branch: str = ""
        self._rel_path: str = ""
        self._remote_deleted: bool = False
        self._steps_done: list[str] = []
        self._accumulated: dict[str, Any] = {}

    def for_repo_branch(
        self,
        repo_name: str | None,
        branch: str | None,
        repo_path: str | None,
    ) -> ResetBuilder:
        self._root = Path(repo_path).resolve() if repo_path else _default_repo_root()
        self._wiki = self._root / "wiki"
        self._repo_name = repo_name or get_repo_name()
        self._branch = branch or get_current_branch()
        self._rel_path = f"{self._repo_name}/{self._branch}"
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        err = check_params(self._repo_name, self._branch)
        if err:
            return False, err

        # reset is strictly more destructive than delete (it wipes the remote
        # namespace AND the local submodule), so it must honor the same
        # protected-branch refusal — otherwise an agent auto-supplying
        # force=True could erase the main/master wiki from the shared remote.
        if self._branch in PROTECTED_BRANCHES:
            return False, {
                "status": "refused",
                "error": (
                    f"Refusing to reset wiki content for protected branch {self._branch!r}. "
                    "main/master reset is intentional infra work — do it manually."
                ),
                "branch": self._branch,
            }
        if not self._branch or self._branch == "HEAD":
            return False, {
                "status": "refused",
                "error": (
                    "Cannot determine a branch to reset (detached HEAD or empty). "
                    "Pass branch=<name> explicitly."
                ),
            }

        has_submodule = wiki_is_initialized(self._wiki) or submodule_exists("wiki", self._root)
        has_wiki_dir = self._wiki.is_dir()

        if not has_submodule and not has_wiki_dir:
            return False, {
                "status": "noop",
                "reason": "Nothing to reset. No wiki submodule or directory exists.",
            }
        return True, None

    # ── stage 2: dry-run ──────────────────────────────────────────

    def _dry_run(self) -> tuple[bool, dict | None]:
        actions = ["Remove local wiki submodule and all local wiki files"]
        actions.append(f"Delete '{self._rel_path}/' from the remote wiki branch")
        return False, {
            "status": "would_do",
            "actions": actions,
            "description": (
                f"This will permanently delete all wiki content for "
                f"{self._repo_name}/{self._branch} from both local and remote. "
                f"The next pull_wiki will start fresh with empty scaffolding."
            ),
            "instruction": "Re-call reset_wiki(force=True) to execute.",
        }

    # ── stage 2b: delete from remote ──────────────────────────────

    def _delete_from_remote(self) -> tuple[bool, dict | None]:
        if wiki_is_initialized(self._wiki):
            from tools.resolve import _has_merge_in_progress
            if _has_merge_in_progress(self._wiki):
                run_git(["merge", "--abort"], cwd=self._wiki, check=False)

            run_git(
                ["-c", "protocol.file.allow=always",
                 "fetch", "origin", WIKI_REMOTE_BRANCH, "--depth", "1"],
                cwd=self._wiki, check=False,
            )
            run_git(["checkout", "-B", WIKI_REMOTE_BRANCH], cwd=self._wiki, check=False)
            if ref_exists(WIKI_REMOTE_REF, cwd=self._wiki):
                run_git(
                    ["reset", "--hard", WIKI_REMOTE_REF],
                    cwd=self._wiki, check=False,
                )

            repo_folder = self._wiki / self._repo_name / self._branch
            if repo_folder.exists():
                shutil.rmtree(str(repo_folder), onerror=_rm_readonly)
                parent = self._wiki / self._repo_name
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                run_git(["add", "--sparse", "--", f"{self._repo_name}/{self._branch}/"], cwd=self._wiki, check=False)
                if not (self._wiki / self._repo_name).exists():
                    run_git(["add", "--sparse", "--", f"{self._repo_name}/"], cwd=self._wiki, check=False)
                commit_result = run_git(
                    ["commit", *_no_verify_flag(), "-m",
                     f"wiki: reset {self._repo_name}/{self._branch} (full deletion)"],
                    cwd=self._wiki, check=False,
                )
                if commit_result.returncode == 0:
                    push_result = run_git(
                        ["push", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
                        cwd=self._wiki, check=False,
                    )
                    if push_result.returncode == 0:
                        self._remote_deleted = True
                        self._steps_done.append(f"Deleted '{self._rel_path}/' from remote wiki branch")
                    else:
                        self._steps_done.append(
                            f"Remote deletion committed but push failed: "
                            f"{push_result.stderr.strip()}"
                        )
                        self._steps_done.append(
                            "Call resolve_wiki_issue() to diagnose and fix the push failure."
                        )
            else:
                self._steps_done.append(f"'{self._rel_path}/' not found on remote — nothing to delete")
                self._remote_deleted = True
        return True, None

    # ── stage 3: remove local submodule ───────────────────────────

    def _remove_wiki_from_exclude(self) -> None:
        """Remove `wiki/` ignore rule from `<root>/.git/info/exclude` if present."""
        git_dir = self._root / ".git"
        if not git_dir.is_dir():
            return
        exclude = git_dir / "info" / "exclude"
        if not exclude.exists():
            return

        lines = exclude.read_text(encoding="utf-8").splitlines()
        filtered = [line for line in lines if line.strip() != "wiki/"]
        if filtered == lines:
            return

        if filtered:
            exclude.write_text("\n".join(filtered) + "\n", encoding="utf-8")
        else:
            exclude.write_text("", encoding="utf-8")

    def _remove_local_submodule(self) -> tuple[bool, dict | None]:
        legacy_submodule = submodule_exists("wiki", self._root) or (self._wiki / ".git").is_file()

        if legacy_submodule:
            run_git(["submodule", "deinit", "-f", "wiki"], cwd=self._root, check=False)
            run_git(["rm", "-f", "wiki"], cwd=self._root, check=False)

            git_modules_wiki = self._root / ".git" / "modules" / "wiki"
            if git_modules_wiki.is_dir():
                shutil.rmtree(str(git_modules_wiki), onerror=_rm_readonly)

            gitmodules = self._root / ".gitmodules"
            if gitmodules.is_file():
                content = gitmodules.read_text(encoding="utf-8").strip()
                if not content:
                    run_git(["rm", "-f", ".gitmodules"], cwd=self._root, check=False)

            # Commit ONLY the wiki/.gitmodules changes. A bare `git commit`
            # here would sweep any unrelated work the user had staged in their
            # code repo into this reset commit (with hooks skipped). Scope the
            # commit to exactly the paths we touched, and only if they were
            # actually staged.
            staged = run_git(
                ["diff", "--cached", "--name-only", "--", "wiki", ".gitmodules"],
                cwd=self._root, check=False,
            )
            paths_to_commit = [p.strip() for p in staged.stdout.splitlines() if p.strip()]
            if paths_to_commit:
                run_git(
                    ["commit", *_no_verify_flag(),
                     "-m", "wiki: remove submodule (reset)",
                     "--", *paths_to_commit],
                    cwd=self._root, check=False,
                )
            self._steps_done.append("Removed wiki submodule from local repo")
        elif wiki_is_initialized(self._wiki):
            self._remove_wiki_from_exclude()
            self._steps_done.append("Removed nested wiki clone from local repo")
        return True, None

    # ── stage 4: cleanup wiki dir ─────────────────────────────────

    def _cleanup_wiki_dir(self) -> tuple[bool, dict | None]:
        if self._wiki.exists():
            shutil.rmtree(str(self._wiki), onerror=_rm_readonly)
            self._steps_done.append("Removed wiki/ directory")
        return True, None

    # ── stage 5: invalidate cache ─────────────────────────────────

    def _invalidate_cache(self) -> tuple[bool, dict | None]:
        from utils.wiki_index import WikiIndex
        WikiIndex.invalidate(self._repo_name, self._branch)
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self, force: bool = False) -> dict:
        self._accumulated = {}
        self._steps_done = []

        if force:
            stages = [
                self._resolve_and_validate,
                self._delete_from_remote,
                self._remove_local_submodule,
                self._cleanup_wiki_dir,
                self._invalidate_cache,
            ]
        else:
            stages = [
                self._resolve_and_validate,
                self._dry_run,
            ]

        for stage in stages:
            ok, result = stage()
            if not ok:
                assert result is not None, f"stage {stage.__name__} returned (False, None)"
                return result
            if result is not None:
                self._accumulated.update(result)

        return self.to_result(force)

    def to_result(self, force: bool = False) -> dict:
        if not force:
            return dict(self._accumulated)
        return {
            "status": "reset",
            "repo": self._repo_name,
            "branch": self._branch,
            "remote_deleted": self._remote_deleted,
            "steps": self._steps_done,
        }


# ── backward-compatible entry point ────────────────────────────────────


def reset_wiki(force: bool = False, repo_path: str | None = None,
               repo_name: str | None = None, branch: str | None = None) -> dict:
    return (
        ResetBuilder()
        .for_repo_branch(repo_name, branch, repo_path)
        .execute(force)
    )
