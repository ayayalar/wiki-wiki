"""wiki_status tool — fetch-first sync status for the current repo/branch wiki."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from utils.git import (
    WIKI_REMOTE_BRANCH,
    WIKI_REMOTE_REF,
    ref_exists,
    run_git,
    wiki_is_initialized,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_git_dir(wiki: Path) -> Path | None:
    git_entry = wiki / ".git"
    if not git_entry.exists():
        return None
    if git_entry.is_dir():
        return git_entry
    try:
        text = git_entry.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            rel = text[len("gitdir:") :].strip()
            candidate = (wiki / rel).resolve()
            if candidate.is_dir():
                return candidate
    except OSError:
        pass
    return None


def _in_progress_operation(wiki: Path) -> str | None:
    git_dir = _get_git_dir(wiki)
    if git_dir is None:
        return None
    if (git_dir / "REBASE_HEAD").exists():
        return "rebase"
    if (git_dir / "rebase-merge").is_dir():
        return "rebase"
    if (git_dir / "rebase-apply").is_dir():
        return "rebase"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    if (git_dir / "REVERT_HEAD").exists():
        return "revert"
    if (git_dir / "MERGE_HEAD").exists():
        return "merge"
    return None


def _dirty_files(wiki: Path, pattern: str) -> list[str]:
    result = run_git(["status", "--porcelain"], cwd=wiki, check=False, timeout=30)
    if result.returncode != 0:
        return []
    prefix = pattern + "/"
    files = []
    for line in result.stdout.splitlines():
        if line.strip():
            path_part = line[3:]
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1]
            path_part = path_part.strip()
            if path_part.startswith(prefix):
                files.append(path_part[len(prefix) :])
    return files


def _rev_parse_short(ref: str, wiki: Path, path: str | None = None) -> str | None:
    args = ["log", "-1", "--format=%h", ref]
    if path:
        args.extend(["--", path])
    result = run_git(args, cwd=wiki, check=False, timeout=30)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _head_date(wiki: Path, path: str | None = None) -> str | None:
    args = ["log", "-1", "--format=%ci"]
    if path:
        args.extend(["--", path])
    result = run_git(args, cwd=wiki, check=False, timeout=30)
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    # Trim timezone offset for readability (e.g. "2026-06-08 14:30:00 -0700" → "2026-06-08 14:30:00")
    if raw and " " in raw:
        parts = raw.rsplit(" ", 1)
        if len(parts) == 2 and (parts[1].startswith("+") or parts[1].startswith("-")):
            return parts[0]
    return raw or None


def _rev_list_count(spec: str, wiki: Path, path: str | None = None) -> int | None:
    args = ["rev-list", "--count", spec]
    if path:
        args.extend(["--", path])
    result = run_git(args, cwd=wiki, check=False, timeout=30)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# StatusBuilder
# ---------------------------------------------------------------------------


class StatusBuilder:
    """Builds and executes a status workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._wiki: Path = Path()
        self._repo_name: str = ""
        self._branch: str = ""
        self._fetch_ok: bool = False
        self._fetch_error: str | None = None
        self._accumulated: dict[str, Any] = {}

    def for_repo_branch(
        self,
        repo_name: str,
        branch: str,
        repo_path: str,
    ) -> StatusBuilder:
        self._wiki = Path(repo_path) / "wiki"
        self._repo_name = repo_name
        self._branch = branch
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        if not self._wiki.exists() or not wiki_is_initialized(self._wiki):
            return False, {
                "status": "not_initialized",
                "repo": self._repo_name,
                "branch": self._branch,
                "wiki_path": str(self._wiki),
                "message": "Wiki is not initialized. Run pull_wiki to set it up.",
            }
        return True, None

    # ── stage 2: fetch remote ─────────────────────────────────────

    def _fetch_remote(self) -> tuple[bool, dict | None]:
        log.info("wiki_status: fetching remote for %s/%s", self._repo_name, self._branch)
        fetch = run_git(
            [
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "origin",
                WIKI_REMOTE_BRANCH,
                "--depth",
                "100",
            ],
            cwd=self._wiki,
            check=False,
        )
        self._fetch_ok = fetch.returncode == 0
        if not self._fetch_ok:
            self._fetch_error = fetch.stderr.strip()
        return True, None

    # ── stage 3: gather local state ───────────────────────────────

    def _gather_local_state(self) -> tuple[bool, dict | None]:
        pattern = f"{self._repo_name}/{self._branch}"
        path = pattern + "/"
        head_sha = _rev_parse_short("HEAD", self._wiki, path)
        wiki_updated = _head_date(self._wiki, path)
        dirty_files_list = _dirty_files(self._wiki, pattern)
        in_progress = _in_progress_operation(self._wiki)

        # Get code repo HEAD (the commit the user actually cares about)
        code_root = self._wiki.parent
        code_head_sha: str | None = None
        code_head_message: str | None = None
        code_result = run_git(
            ["log", "-1", "--format=%h %s"],
            cwd=code_root,
            check=False,
            timeout=30,
        )
        if code_result.returncode == 0 and code_result.stdout.strip():
            parts = code_result.stdout.strip().split(" ", 1)
            code_head_sha = parts[0]
            code_head_message = parts[1] if len(parts) > 1 else None

        self._accumulated["local"] = {
            "head_sha": head_sha,
            "wiki_updated": wiki_updated,
            "code_head_sha": code_head_sha,
            "code_head_message": code_head_message,
            "dirty": bool(dirty_files_list),
            "dirty_files": dirty_files_list,
            "in_progress": in_progress,
        }
        return True, None

    # ── stage 4: gather remote state ──────────────────────────────

    def _gather_remote_state(self) -> tuple[bool, dict | None]:
        remote_ref = WIKI_REMOTE_REF
        remote_ref_exists_flag = ref_exists(remote_ref, cwd=self._wiki)
        path = f"{self._repo_name}/{self._branch}/"
        remote_sha = (
            _rev_parse_short(remote_ref, self._wiki, path) if remote_ref_exists_flag else None
        )

        # Get remote commit message (same pattern as Code HEAD)
        remote_sha_message: str | None = None
        if remote_sha and remote_ref_exists_flag:
            args = ["log", "-1", "--format=%h %s", remote_ref]
            if path:
                args.extend(["--", path])
            msg_result = run_git(args, cwd=self._wiki, check=False, timeout=30)
            if msg_result.returncode == 0 and msg_result.stdout.strip():
                parts = msg_result.stdout.strip().split(" ", 1)
                remote_sha_message = parts[1] if len(parts) > 1 else None

        self._accumulated["remote"] = {
            "ref": remote_ref,
            "sha": remote_sha,
            "sha_message": remote_sha_message,
            "reachable": self._fetch_ok,
            "fetch_ok": self._fetch_ok,
            "fetch_error": self._fetch_error,
        }
        return True, None

    # ── stage 5: compute sync state ───────────────────────────────

    def _compute_sync_state(self) -> tuple[bool, dict | None]:
        remote_ref = WIKI_REMOTE_REF
        remote_ref_exists_flag = ref_exists(remote_ref, cwd=self._wiki)
        path = f"{self._repo_name}/{self._branch}/"
        commits_ahead: int | None = None
        commits_behind: int | None = None
        if remote_ref_exists_flag:
            commits_ahead = _rev_list_count(f"{remote_ref}..HEAD", self._wiki, path)
            commits_behind = _rev_list_count(f"HEAD..{remote_ref}", self._wiki, path)

        if not remote_ref_exists_flag or commits_ahead is None or commits_behind is None:
            sync_state = "unknown"
        elif commits_ahead == 0 and commits_behind == 0:
            sync_state = "synced"
        elif commits_ahead > 0 and commits_behind == 0:
            sync_state = "ahead"
        elif commits_behind > 0 and commits_ahead == 0:
            sync_state = "behind"
        else:
            sync_state = "diverged"

        local = self._accumulated.get("local", {})
        if isinstance(local, dict):
            local["commits_ahead"] = commits_ahead
            local["commits_behind"] = commits_behind

        self._accumulated["sync_state"] = sync_state
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        self._accumulated = {}

        stages = [
            self._resolve_and_validate,
            self._fetch_remote,
            self._gather_local_state,
            self._gather_remote_state,
            self._compute_sync_state,
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
        local = self._accumulated.get("local", {})
        remote = self._accumulated.get("remote", {})
        sync_state = self._accumulated.get("sync_state", "unknown")

        summary = self._format_summary(local, remote, sync_state)
        response: dict[str, Any] = {
            "summary": summary,
            "repo": self._repo_name,
            "branch": self._branch,
            "wiki_path": str(self._wiki),
            "local": local,
            "remote": remote,
            "sync_state": sync_state,
            "instruction": "Display the markdown table in the 'summary' field to the user.",
        }
        return response

    def _format_summary(self, local: dict, remote: dict, sync_state: str) -> str:
        rows: list[tuple[str, str]] = []
        rows.append(("Repo", self._repo_name))
        rows.append(("Branch", self._branch))

        ahead = local.get("commits_ahead")
        behind = local.get("commits_behind")
        if sync_state == "synced":
            rows.append(("Status", "In sync"))
        elif sync_state == "ahead":
            rows.append(("Status", f"{ahead} local commit(s) ahead"))
        elif sync_state == "behind":
            rows.append(("Status", f"{behind} remote commit(s) behind"))
        elif sync_state == "diverged":
            parts = []
            if ahead:
                parts.append(f"{ahead} ahead")
            if behind:
                parts.append(f"{behind} behind")
            rows.append(("Status", f"Diverged — {', '.join(parts)}"))
        else:
            rows.append(("Status", sync_state))

        code_sha = local.get("code_head_sha")
        code_msg = local.get("code_head_message")
        if code_sha:
            value = code_sha
            if code_msg:
                value += f" — {code_msg}"
            rows.append(("Code HEAD", value))

        wiki_updated = local.get("wiki_updated")
        if wiki_updated:
            rows.append(("Wiki Updated", wiki_updated))

        dirty = local.get("dirty", False)
        dirty_files = local.get("dirty_files", [])
        if dirty:
            rows.append(("Uncommitted", f"Yes ({len(dirty_files)} files)"))

        in_progress = local.get("in_progress")
        if in_progress:
            rows.append(("Git Operation", str(in_progress)))

        remote_sha = remote.get("sha")
        if remote_sha:
            value = remote_sha
            remote_msg = remote.get("sha_message")
            if remote_msg:
                value += f" — {remote_msg}"
            rows.append(("Wiki HEAD", value))
            if remote.get("fetch_error"):
                rows.append(("Fetch Error", remote["fetch_error"]))
        elif not remote.get("reachable"):
            rows.append(("Wiki HEAD", "not reachable"))

        table = "| Key | Value |\n|-----|-------|\n"
        for key, value in rows:
            table += f"| {key} | {value} |\n"
        return table


# ── backward-compatible entry point ────────────────────────────────────


def status(
    repo_name: str,
    branch: str,
    repo_path: str,
) -> dict:
    return StatusBuilder().for_repo_branch(repo_name, branch, repo_path).execute()
