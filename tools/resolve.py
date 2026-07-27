"""resolve_wiki_issue: diagnose and fix wiki problems without manual git commands.

When called without an action, diagnoses the current wiki state and returns
a list of detected issues with resolution options (plain-language descriptions).
When called with an action, executes that specific fix.

Action IDs are qualified as ``issue:resolution`` to avoid ambiguity when
multiple issues coexist (e.g. ``merge_conflict:keep_local``).
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.git import (
    WIKI_REMOTE_BRANCH,
    WIKI_REMOTE_REF,
    _no_verify_flag,
    is_dirty,
    ref_exists,
    run_git,
    set_sparse_checkout_cone,
    wiki_is_initialized,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Diagnosis helpers
# ---------------------------------------------------------------------------


def _has_merge_in_progress(wiki: Path) -> bool:
    git_dir = _get_git_dir(wiki)
    if git_dir is None:
        return False
    return (git_dir / "MERGE_HEAD").exists()


def _has_unmerged_files(wiki: Path) -> list[str]:
    result = run_git(["status", "--porcelain"], cwd=wiki, check=False, timeout=30)
    unmerged = []
    for line in result.stdout.strip().splitlines():
        if len(line) >= 3 and line[0] in "UDA" and line[1] in "UDA":
            unmerged.append(line[3:].strip())
    return unmerged


def _abort_merge_safe(wiki: Path) -> None:
    abort = run_git(["merge", "--abort"], cwd=wiki, check=False)
    if abort.returncode == 0:
        return
    git_dir = _get_git_dir(wiki)
    if git_dir is not None:
        for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "MERGE_AUTOSTASH"):
            path = git_dir / name
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass


def _merge_log_file(wiki: Path, filepath: str) -> None:
    full = wiki / filepath
    if not full.exists():
        return
    content = full.read_text(encoding="utf-8", errors="replace")
    if "<<<<<<" not in content:
        return

    header_lines: list[str] = []
    ours_entries: list[str] = []
    theirs_entries: list[str] = []

    in_ours = False
    in_theirs = False
    for line in content.splitlines():
        if line.startswith("<<<<<<<"):
            in_ours = True
            continue
        elif line.startswith("|||||||"):
            in_ours = False
            continue
        elif line.startswith("======="):
            in_ours = False
            in_theirs = True
            continue
        elif line.startswith(">>>>>>>"):
            in_theirs = False
            continue

        if in_ours:
            if line.startswith("## ["):
                ours_entries.append(line)
        elif in_theirs:
            if line.startswith("## ["):
                theirs_entries.append(line)
        else:
            if line.startswith("## ["):
                ours_entries.append(line)
            else:
                header_lines.append(line)

    seen: set[str] = set()
    all_entries: list[str] = []
    for entry in ours_entries + theirs_entries:
        if entry not in seen:
            seen.add(entry)
            all_entries.append(entry)

    header = "\n".join(header_lines).rstrip()
    if all_entries:
        entries_text = "\n\n".join(all_entries)
        merged = header + ("\n\n" if header else "") + entries_text + "\n"
    else:
        merged = header + ("\n" if header else "")

    full.write_text(merged, encoding="utf-8")


def finish_merge(wiki: Path) -> bool:
    if not _has_merge_in_progress(wiki):
        return False
    if _has_unmerged_files(wiki):
        return False
    run_git(["add", "--sparse", "-A"], cwd=wiki, check=False)
    commit_result = run_git(
        ["commit", *_no_verify_flag(), "--no-edit"],
        cwd=wiki,
        check=False,
    )
    return commit_result.returncode == 0


def resolve_merge(wiki: Path, repo_name: str) -> bool:
    if not _has_merge_in_progress(wiki):
        return False
    unmerged = _has_unmerged_files(wiki)
    local_prefix = f"{repo_name}/"
    for f in unmerged:
        if f.endswith("/log.md"):
            _merge_log_file(wiki, f)
        elif f.startswith(local_prefix):
            run_git(["checkout", "--ours", "--", f], cwd=wiki, check=False)
        else:
            run_git(["checkout", "--theirs", "--", f], cwd=wiki, check=False)
        run_git(["add", "--sparse", "--", f], cwd=wiki, check=False)
    run_git(["add", "--sparse", "-A"], cwd=wiki, check=False)
    commit_result = run_git(
        ["commit", *_no_verify_flag(), "--no-edit"],
        cwd=wiki,
        check=False,
    )
    # A non-zero commit means the merge is NOT resolved (identity unset, a
    # failing pre-commit hook, or files still unmerged). Report failure so
    # callers don't push a pre-merge HEAD with MERGE_HEAD still on disk and
    # then report success. Mirrors finish_merge's commit check.
    return commit_result.returncode == 0


def auto_resolve_conflicts(wiki: Path, repo_name: str) -> bool:
    if _has_merge_in_progress(wiki):
        return resolve_merge(wiki, repo_name)
    return False


# ── shared merge core ──────────────────────────────────────────────────────
# One classifier for "merge origin/wiki into the local wiki branch", reused by
# pull, push, and the resolve handlers. Previously each site hand-rolled the
# same ff-then-fallback-merge-then-classify block and they had drifted apart —
# the same failure state produced "resolved", "partial", "error", or
# "merge_conflict" depending on entry point.
MERGE_CLEAN = "clean"  # fast-forwarded or merged with no conflicts
MERGE_CONFLICT = "conflict"  # merge started, unmerged files present (MERGE_HEAD live)
MERGE_STUCK = "stuck"  # merge started but failed with no unmerged files (MERGE_HEAD live)
MERGE_REFUSED = "refused"  # git declined to start the merge; nothing changed


def merge_remote_ref(wiki: Path) -> str:
    """Try ff-only then a fallback merge of WIKI_REMOTE_REF; classify the result.

    Pure merge + classification: does NOT fetch, auto-resolve, abort, or push,
    so every caller shares one correct classifier and layers its own policy on
    top. Returns one of the ``MERGE_*`` constants. Assumes the caller has
    already confirmed WIKI_REMOTE_REF exists (a missing ref classifies as
    ``MERGE_REFUSED``).
    """
    ff = run_git(["merge", "--ff-only", WIKI_REMOTE_REF], cwd=wiki, check=False)
    if ff.returncode == 0:
        return MERGE_CLEAN
    # Shallow clones may have unrelated histories — retry with a real merge.
    merge = run_git(
        ["merge", "--allow-unrelated-histories", "--no-edit", WIKI_REMOTE_REF],
        cwd=wiki,
        check=False,
    )
    if merge.returncode == 0:
        return MERGE_CLEAN
    if _has_merge_in_progress(wiki):
        return MERGE_CONFLICT if _has_unmerged_files(wiki) else MERGE_STUCK
    return MERGE_REFUSED


def merge_and_autoresolve(wiki: Path, repo_name: str, action: str) -> dict | None:
    """Merge origin/wiki, auto-resolving conflicts where possible.

    Returns ``None`` when the working tree ends clean (fast-forward, clean
    merge, or conflicts resolved and committed) so the caller can push.
    Otherwise returns a partial/error response dict and leaves the tree clean
    (any partial merge is aborted). ``action`` is echoed into the response so
    the caller's diagnosis id is preserved. Assumes WIKI_REMOTE_REF exists.
    """
    outcome = merge_remote_ref(wiki)
    if outcome == MERGE_CLEAN:
        return None
    if outcome == MERGE_CONFLICT:
        if repo_name and resolve_merge(wiki, repo_name):
            return None
        _abort_merge_safe(wiki)
        return {
            "status": "partial",
            "action": action,
            "message": (
                "Merge produced conflicts that could not be auto-resolved. "
                "The merge was aborted to leave the wiki in a clean state. "
                "Call resolve_wiki_issue again to retry or choose a different strategy."
            ),
        }
    if outcome == MERGE_STUCK:
        _abort_merge_safe(wiki)
        return {
            "status": "partial",
            "action": action,
            "message": (
                "Merge failed without conflict markers. "
                "The merge was aborted to leave the wiki in a clean state."
            ),
        }
    # MERGE_REFUSED — git never started the merge; nothing to abort.
    return {
        "status": "error",
        "action": action,
        "error": (
            "Could not merge remote changes: git refused the merge, usually "
            "because the wiki working tree has uncommitted changes. Commit or "
            "discard local wiki edits, then retry."
        ),
    }


def _get_git_dir(wiki: Path) -> Path | None:
    dot_git = wiki / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            text = dot_git.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                target = text[len("gitdir:") :].strip()
                p = Path(target)
                if not p.is_absolute():
                    p = (wiki / p).resolve()
                return p if p.exists() else None
        except OSError:
            pass
    return None


def _is_behind_remote(wiki: Path, repo_name: str, branch: str) -> bool:
    remote_ref = WIKI_REMOTE_REF
    if not ref_exists(remote_ref, cwd=wiki):
        return False
    path = f"{repo_name}/{branch}/"
    result = run_git(
        ["rev-list", "--count", f"HEAD..{remote_ref}", "--", path],
        cwd=wiki,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return False
    remote_ahead = int(result.stdout.strip() or "0")
    if remote_ahead == 0:
        return False
    result2 = run_git(
        ["rev-list", "--count", f"{remote_ref}..HEAD", "--", path],
        cwd=wiki,
        check=False,
        timeout=30,
    )
    local_ahead = int(result2.stdout.strip() or "0") if result2.returncode == 0 else 0
    return local_ahead == 0


def _is_ahead_of_remote(wiki: Path, repo_name: str, branch: str) -> bool:
    remote_ref = WIKI_REMOTE_REF
    if not ref_exists(remote_ref, cwd=wiki):
        return False
    path = f"{repo_name}/{branch}/"
    result = run_git(
        ["rev-list", "--count", f"{remote_ref}..HEAD", "--", path],
        cwd=wiki,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return False
    local_ahead = int(result.stdout.strip() or "0")
    if local_ahead == 0:
        return False
    result2 = run_git(
        ["rev-list", "--count", f"HEAD..{remote_ref}", "--", path],
        cwd=wiki,
        check=False,
        timeout=30,
    )
    if result2.returncode != 0:
        return False
    remote_ahead = int(result2.stdout.strip() or "0")
    return remote_ahead == 0


def _is_diverged(wiki: Path, repo_name: str, branch: str) -> bool:
    remote_ref = WIKI_REMOTE_REF
    if not ref_exists(remote_ref, cwd=wiki):
        return False
    path = f"{repo_name}/{branch}/"
    result_local = run_git(
        ["rev-list", "--count", f"{remote_ref}..HEAD", "--", path],
        cwd=wiki,
        check=False,
        timeout=30,
    )
    result_remote = run_git(
        ["rev-list", "--count", f"HEAD..{remote_ref}", "--", path],
        cwd=wiki,
        check=False,
        timeout=30,
    )
    if result_local.returncode != 0 or result_remote.returncode != 0:
        # rev-list couldn't compute counts (e.g. shallow-graft edge cases).
        # Probe with a no-commit test merge, but classify by whether git
        # actually STARTED a merge: MERGE_HEAD present means a real non-ff
        # merge or conflict → diverged. A rc-0 "Already up to date" leaves no
        # MERGE_HEAD and must NOT be read as diverged (that was a false
        # "diverged" for perfectly in-sync wikis).
        run_git(
            ["merge", "--no-commit", "--no-ff", remote_ref],
            cwd=wiki,
            check=False,
            timeout=30,
        )
        diverged = _has_merge_in_progress(wiki)
        _abort_merge_safe(wiki)
        return diverged
    local_ahead = int(result_local.stdout.strip() or "0")
    remote_ahead = int(result_remote.stdout.strip() or "0")
    return local_ahead > 0 and remote_ahead > 0


def _has_index_lock(wiki: Path) -> bool:
    git_dir = _get_git_dir(wiki)
    if git_dir is None:
        return False
    return (git_dir / "index.lock").exists()


def _has_rebase_in_progress(wiki: Path) -> bool:
    git_dir = _get_git_dir(wiki)
    if git_dir is None:
        return False
    if (git_dir / "REBASE_HEAD").exists():
        return True
    if (git_dir / "rebase-merge").is_dir():
        return True
    return bool((git_dir / "rebase-apply").is_dir())


def _has_cherry_pick_in_progress(wiki: Path) -> bool:
    git_dir = _get_git_dir(wiki)
    if git_dir is None:
        return False
    return (git_dir / "CHERRY_PICK_HEAD").exists()


def _has_revert_in_progress(wiki: Path) -> bool:
    git_dir = _get_git_dir(wiki)
    if git_dir is None:
        return False
    return (git_dir / "REVERT_HEAD").exists()


def _remote_exists(wiki: Path) -> bool:
    result = run_git(["remote", "get-url", "origin"], cwd=wiki, check=False, timeout=15)
    return result.returncode == 0 and bool(result.stdout.strip())


def _remote_is_reachable(wiki: Path) -> bool:
    ls_result = run_git(
        ["-c", "protocol.file.allow=always", "ls-remote", "origin", WIKI_REMOTE_BRANCH],
        cwd=wiki,
        check=False,
        timeout=30,
    )
    return ls_result.returncode == 0


def _is_detached_head(wiki: Path) -> bool:
    git_dir = _get_git_dir(wiki)
    if git_dir is None:
        return False
    head_file = git_dir / "HEAD"
    try:
        content = head_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return not content.startswith("ref: ")


def _submodule_is_valid(wiki: Path) -> bool:
    git_dir = _get_git_dir(wiki)
    if git_dir is None:
        return False
    failures = 0
    for _ in range(2):
        result = run_git(["status", "--porcelain"], cwd=wiki, check=False, timeout=15)
        if result.returncode != 0:
            failures += 1
    return failures < 2


def _sparse_checkout_current(wiki: Path, pattern: str) -> bool:
    git_dir = _get_git_dir(wiki)
    if git_dir is None:
        return False
    sparse_file = git_dir / "info" / "sparse-checkout"
    if not sparse_file.is_file():
        return False
    try:
        content = sparse_file.read_text(encoding="utf-8")
        return pattern in content
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Diagnosis check registry
# ---------------------------------------------------------------------------


@dataclass
class DiagnosisCheck:
    name: str
    blocking: bool
    needs_context: bool
    run: Callable[[Path, str, str], dict | None]


def _check_wiki_not_initialized(wiki: Path, _rn: str, _br: str) -> dict | None:
    if not wiki_is_initialized(wiki):
        return {
            "issue": "wiki_not_initialized",
            "description": "Wiki submodule is not checked out.",
            "resolutions": [
                {
                    "id": "wiki_not_initialized:pull",
                    "description": "Pull the wiki from remote to set it up.",
                    "destructive": False,
                }
            ],
        }
    return None


def _check_corrupted_submodule(wiki: Path, _rn: str, _br: str) -> dict | None:
    if not _submodule_is_valid(wiki):
        return {
            "issue": "corrupted_submodule",
            "description": (
                "Wiki submodule appears corrupted (git operations fail consistently). "
                "The local wiki directory needs to be reinitialized."
            ),
            "resolutions": [
                {
                    "id": "corrupted_submodule:reinitialize",
                    "description": (
                        "Delete the local wiki directory and reinitialize it from "
                        "the remote. Local-only changes will be lost."
                    ),
                    "destructive": True,
                }
            ],
        }
    return None


def _check_index_locked(wiki: Path, _rn: str, _br: str) -> dict | None:
    if _has_index_lock(wiki):
        return {
            "issue": "index_locked",
            "description": (
                "A git lock file exists, likely from a previously interrupted operation. "
                "This prevents any wiki operations."
            ),
            "resolutions": [
                {
                    "id": "index_locked:remove_lock",
                    "description": "Remove the stale lock file so wiki operations can proceed.",
                    "destructive": False,
                }
            ],
        }
    return None


def _check_wiki_content_corrupt(wiki: Path, repo_name: str, branch: str) -> dict | None:
    if not repo_name or not branch:
        return None
    repo_wiki_path = wiki / repo_name / branch
    if not repo_wiki_path.exists():
        return None
    corrupted_files = []
    md_files = list(repo_wiki_path.rglob("*.md"))
    for f in md_files[:10]:
        try:
            f.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            corrupted_files.append(str(f.relative_to(repo_wiki_path)))
        except Exception:  # noqa: BLE001, S110
            pass
    if corrupted_files:
        return {
            "issue": "wiki_content_corrupt",
            "description": (
                f"Some wiki files contain non-UTF-8 content that will cause tools to crash. "
                f"Affected files: {', '.join(corrupted_files[:5])}"
                + (f" (and {len(corrupted_files) - 5} more)" if len(corrupted_files) > 5 else "")
            ),
            "corrupted_files": corrupted_files,
            "resolutions": [
                {
                    "id": "wiki_content_corrupt:sanitize",
                    "description": (
                        "Re-read corrupted files with error recovery and re-write them as clean UTF-8. "
                        "Non-UTF-8 bytes will be replaced with � (U+FFFD)."
                    ),
                    "destructive": False,
                }
            ],
        }
    return None


def _check_merge_conflict(wiki: Path, _rn: str, _br: str) -> dict | None:
    if not _has_merge_in_progress(wiki):
        return None
    unmerged = _has_unmerged_files(wiki)
    return {
        "issue": "merge_conflict",
        "description": f"A merge is in progress with {len(unmerged)} conflicted file(s).",
        "conflicted_files": unmerged,
        "resolutions": [
            {
                "id": "merge_conflict:keep_local",
                "description": "Keep your local version of all conflicted files, "
                "discard the remote changes, and complete the merge.",
                "destructive": False,
            },
            {
                "id": "merge_conflict:keep_remote",
                "description": "Keep the remote version of all conflicted files, "
                "discard your local changes, and complete the merge.",
                "destructive": True,
            },
            {
                "id": "merge_conflict:abort_merge",
                "description": "Cancel the merge entirely and return to the state "
                "before the merge started.",
                "destructive": False,
            },
        ],
    }


def _check_rebase_in_progress(wiki: Path, _rn: str, _br: str) -> dict | None:
    if not _has_rebase_in_progress(wiki):
        return None
    return {
        "issue": "rebase_in_progress",
        "description": "A rebase is in progress and needs to be completed or aborted.",
        "resolutions": [
            {
                "id": "rebase_in_progress:continue_rebase",
                "description": "Stage all resolved files and continue the rebase. "
                "Use this after resolving conflicts.",
                "destructive": False,
            },
            {
                "id": "rebase_in_progress:abort_rebase",
                "description": "Cancel the rebase and return to the state "
                "before the rebase started.",
                "destructive": False,
            },
            {
                "id": "rebase_in_progress:skip_and_continue",
                "description": "Skip the current conflicting commit and continue "
                "the rebase with the remaining commits.",
                "destructive": False,
            },
        ],
    }


def _check_cherry_pick_in_progress(wiki: Path, _rn: str, _br: str) -> dict | None:
    if not _has_cherry_pick_in_progress(wiki):
        return None
    return {
        "issue": "cherry_pick_in_progress",
        "description": "A cherry-pick is in progress and needs to be completed or aborted.",
        "resolutions": [
            {
                "id": "cherry_pick_in_progress:continue_cherry_pick",
                "description": "Stage all resolved files and continue the cherry-pick. "
                "Use this after resolving conflicts.",
                "destructive": False,
            },
            {
                "id": "cherry_pick_in_progress:abort_cherry_pick",
                "description": "Cancel the cherry-pick and return to the state "
                "before the cherry-pick started.",
                "destructive": False,
            },
        ],
    }


def _check_revert_in_progress(wiki: Path, _rn: str, _br: str) -> dict | None:
    if not _has_revert_in_progress(wiki):
        return None
    return {
        "issue": "revert_in_progress",
        "description": "A revert is in progress and needs to be completed or aborted.",
        "resolutions": [
            {
                "id": "revert_in_progress:continue_revert",
                "description": "Stage all resolved files and continue the revert. "
                "Use this after resolving conflicts.",
                "destructive": False,
            },
            {
                "id": "revert_in_progress:abort_revert",
                "description": "Cancel the revert and return to the state "
                "before the revert started.",
                "destructive": False,
            },
        ],
    }


def _check_missing_remote(wiki: Path, _rn: str, _br: str) -> dict | None:
    if not _remote_exists(wiki):
        remote_url = os.environ.get("WIKI_MCP_REMOTE_URL", "")
        return {
            "issue": "missing_remote",
            "description": (
                "The 'origin' remote is not configured or has no URL. "
                "Wiki cannot sync without a remote."
            ),
            "resolutions": [
                {
                    "id": "missing_remote:reconfigure_remote",
                    "description": (
                        "Configure the remote URL from WIKI_MCP_REMOTE_URL environment variable."
                    ),
                    "destructive": False,
                }
            ],
            "_remote_url_set": bool(remote_url),
        }
    return None


def _check_remote_unreachable(wiki: Path, _rn: str, _br: str) -> dict | None:
    if not _remote_is_reachable(wiki):
        return {
            "issue": "remote_unreachable",
            "description": (
                "The wiki remote is not reachable. This may be due to "
                "network issues, authentication failures, or an incorrect URL."
            ),
            "resolutions": [
                {
                    "id": "remote_unreachable:retry_fetch",
                    "description": (
                        "Retry fetching from the remote without shallow clone "
                        "limits. This may succeed where a shallow fetch failed."
                    ),
                    "destructive": False,
                },
                {
                    "id": "remote_unreachable:check_credentials",
                    "description": (
                        "Verify your git credentials and network connectivity. "
                        "The remote URL is configured but unreachable."
                    ),
                    "destructive": False,
                },
            ],
        }
    return None


def _check_detached_head(wiki: Path, _rn: str, _br: str) -> dict | None:
    if _is_detached_head(wiki):
        return {
            "issue": "detached_head",
            "description": (
                "Wiki is in a detached HEAD state. Operations should be "
                "performed on the wiki branch."
            ),
            "resolutions": [
                {
                    "id": "detached_head:checkout_wiki_branch",
                    "description": "Checkout the wiki branch to resume normal operations.",
                    "destructive": False,
                }
            ],
        }
    return None


def _check_sync_state(wiki: Path, repo_name: str, branch: str) -> dict | None:
    run_git(
        ["-c", "protocol.file.allow=always", "fetch", "origin", WIKI_REMOTE_BRANCH, "--deepen=50"],
        cwd=wiki,
        check=False,
    )
    if _is_behind_remote(wiki, repo_name, branch):
        return {
            "issue": "behind_remote",
            "description": (
                "Your local wiki is behind the remote. Pull the remote changes "
                "before pushing new changes."
            ),
            "resolutions": [
                {
                    "id": "behind_remote:pull_and_merge",
                    "description": (
                        "Fetch and merge remote changes into your local wiki. "
                        "Preserves your local changes and incorporates remote updates."
                    ),
                    "destructive": False,
                },
                {
                    "id": "behind_remote:reset_to_remote",
                    "description": (
                        "Reset your local wiki to match the remote. "
                        "Your local-only changes will be lost."
                    ),
                    "destructive": True,
                },
            ],
        }
    if _is_ahead_of_remote(wiki, repo_name, branch):
        return {
            "issue": "ahead_of_remote",
            "description": ("Your local wiki has commits that haven't been pushed to the remote."),
            "resolutions": [
                {
                    "id": "ahead_of_remote:push",
                    "description": (
                        "Fetch, merge any remote changes, and push your local "
                        "commits to the remote."
                    ),
                    "destructive": False,
                },
                {
                    "id": "ahead_of_remote:stash_pull_merge",
                    "description": (
                        "Stash your local changes, pull remote updates, "
                        "then re-apply your stashed changes."
                    ),
                    "destructive": False,
                },
                {
                    "id": "ahead_of_remote:discard_local",
                    "description": (
                        "Reset your local wiki to match the remote. "
                        "Your local-only changes will be lost."
                    ),
                    "destructive": True,
                },
            ],
        }
    if _is_diverged(wiki, repo_name, branch):
        return {
            "issue": "diverged",
            "description": (
                "Your local wiki and the remote have diverged. Both have changes the other doesn't."
            ),
            "resolutions": [
                {
                    "id": "diverged:merge",
                    "description": (
                        "Merge remote changes into your local wiki. "
                        "Preserves local changes for this repo's files; "
                        "accepts remote content for other repos' files. "
                        "log.md entries from both sides are combined."
                    ),
                    "destructive": False,
                },
                {
                    "id": "diverged:force_push",
                    "description": (
                        "Overwrite the remote with your local wiki. "
                        "Remote-only changes will be lost."
                    ),
                    "destructive": True,
                },
                {
                    "id": "diverged:reset_to_remote",
                    "description": (
                        "Reset your local wiki to match the remote. "
                        "Your local-only changes will be lost."
                    ),
                    "destructive": True,
                },
            ],
        }
    return None


def _check_sparse_checkout_stale(wiki: Path, repo_name: str, branch: str) -> dict | None:
    if not repo_name or not branch:
        return None
    sparse_pattern = f"{repo_name}/{branch}"
    if not _sparse_checkout_current(wiki, sparse_pattern):
        return {
            "issue": "sparse_checkout_stale",
            "description": (
                f"Wiki sparse-checkout pattern doesn't include "
                f"'{sparse_pattern}'. Files may be missing from the working tree."
            ),
            "resolutions": [
                {
                    "id": "sparse_checkout_stale:refresh",
                    "description": (
                        "Re-apply the sparse-checkout pattern to restore "
                        f"the '{sparse_pattern}' folder."
                    ),
                    "destructive": False,
                }
            ],
        }
    return None


def _check_dirty_worktree(wiki: Path, _rn: str, _br: str) -> dict | None:
    if not is_dirty(wiki):
        return None
    result = run_git(
        ["status", "--porcelain"],
        cwd=wiki,
        check=False,
        timeout=30,
    )
    changed_files = [
        line[3:].strip() for line in result.stdout.strip().splitlines() if line.strip()
    ]
    return {
        "issue": "dirty_worktree",
        "description": f"Wiki has {len(changed_files)} uncommitted change(s).",
        "changed_files": changed_files[:20],
        "resolutions": [
            {
                "id": "dirty_worktree:commit_and_push",
                "description": "Commit all pending wiki changes and push them to the remote.",
                "destructive": False,
            },
            {
                "id": "dirty_worktree:discard_changes",
                "description": (
                    "Discard all uncommitted wiki changes and restore "
                    "files to their last committed state."
                ),
                "destructive": True,
            },
        ],
    }


_DIAGNOSIS_CHECKS: list[DiagnosisCheck] = [
    DiagnosisCheck(
        "wiki_not_initialized", blocking=True, needs_context=False, run=_check_wiki_not_initialized
    ),
    DiagnosisCheck(
        "corrupted_submodule", blocking=True, needs_context=False, run=_check_corrupted_submodule
    ),
    DiagnosisCheck("index_locked", blocking=False, needs_context=False, run=_check_index_locked),
    DiagnosisCheck(
        "wiki_content_corrupt", blocking=False, needs_context=True, run=_check_wiki_content_corrupt
    ),
    DiagnosisCheck("merge_conflict", blocking=True, needs_context=False, run=_check_merge_conflict),
    DiagnosisCheck(
        "rebase_in_progress", blocking=True, needs_context=False, run=_check_rebase_in_progress
    ),
    DiagnosisCheck(
        "cherry_pick_in_progress",
        blocking=True,
        needs_context=False,
        run=_check_cherry_pick_in_progress,
    ),
    DiagnosisCheck(
        "revert_in_progress", blocking=True, needs_context=False, run=_check_revert_in_progress
    ),
    DiagnosisCheck("missing_remote", blocking=True, needs_context=False, run=_check_missing_remote),
    DiagnosisCheck(
        "remote_unreachable", blocking=True, needs_context=False, run=_check_remote_unreachable
    ),
    DiagnosisCheck("detached_head", blocking=False, needs_context=False, run=_check_detached_head),
    DiagnosisCheck("sync_state", blocking=False, needs_context=False, run=_check_sync_state),
    DiagnosisCheck(
        "sparse_checkout_stale",
        blocking=False,
        needs_context=True,
        run=_check_sparse_checkout_stale,
    ),
    DiagnosisCheck(
        "dirty_worktree", blocking=False, needs_context=False, run=_check_dirty_worktree
    ),
]


def _diagnose(wiki: Path, repo_name: str = "", branch: str = "") -> list[dict]:
    issues: list[dict] = []
    has_remote_url = bool(os.environ.get("WIKI_MCP_REMOTE_URL", ""))

    for check in _DIAGNOSIS_CHECKS:
        if check.needs_context and (not repo_name or not branch):
            continue
        if check.name == "missing_remote" and not has_remote_url:
            result = check.run(wiki, repo_name, branch)
            if result is not None:
                issues.append(result)
                return issues
        if check.name == "remote_unreachable" and not has_remote_url:
            continue
        result = check.run(wiki, repo_name, branch)
        if result is not None:
            result.pop("_remote_url_set", None)
            issues.append(result)
            if check.blocking:
                return issues

    if not issues:
        issues.append(
            {
                "issue": "none",
                "description": "No issues detected. Wiki is healthy.",
                "resolutions": [],
            }
        )

    return issues


# ---------------------------------------------------------------------------
# Action handler registry
# ---------------------------------------------------------------------------

# Recursion guard for corrupted_submodule:reinitialize
_reinit_depth = 0

_ACTION_HANDLERS: dict[str, Callable[[Path, str, str], dict]] = {}


def _action(id: str):
    def register(fn):
        _ACTION_HANDLERS[id] = fn
        return fn

    return register


@_action("merge_conflict:keep_local")
def _handle_merge_conflict_keep_local(wiki: Path, repo_name: str, branch: str) -> dict:

    if not _has_merge_in_progress(wiki):
        return {"status": "error", "error": "No merge in progress."}
    unmerged = _has_unmerged_files(wiki)
    for f in unmerged:
        if f.endswith("/log.md"):
            _merge_log_file(wiki, f)
        else:
            run_git(["checkout", "--ours", "--", f], cwd=wiki, check=False)
        run_git(["add", "--sparse", "--", f], cwd=wiki, check=False)
    commit_result = run_git(
        ["commit", *_no_verify_flag(), "--no-edit"],
        cwd=wiki,
        check=False,
    )
    if commit_result.returncode != 0:
        _abort_merge_safe(wiki)
        combined = f"{commit_result.stdout}\n{commit_result.stderr}".strip()
        return {
            "status": "partial",
            "action": "merge_conflict:keep_local",
            "message": (
                "Kept local version of conflicted files but the merge commit failed. "
                "The merge has been aborted to leave the wiki in a clean state. "
                "Common cause: git user.email/user.name is not configured. "
                "Call resolve_wiki_issue again to retry or choose a different strategy."
            ),
            "commit_stderr": combined,
        }
    return {
        "status": "resolved",
        "action": "merge_conflict:keep_local",
        "message": f"Kept local version of {len(unmerged)} file(s) and completed the merge.",
    }


@_action("merge_conflict:keep_remote")
def _handle_merge_conflict_keep_remote(wiki: Path, repo_name: str, branch: str) -> dict:

    if not _has_merge_in_progress(wiki):
        return {"status": "error", "error": "No merge in progress."}
    unmerged = _has_unmerged_files(wiki)
    for f in unmerged:
        run_git(["checkout", "--theirs", "--", f], cwd=wiki, check=False)
        run_git(["add", "--sparse", "--", f], cwd=wiki, check=False)
    commit_result = run_git(
        ["commit", *_no_verify_flag(), "--no-edit"],
        cwd=wiki,
        check=False,
    )
    if commit_result.returncode != 0:
        _abort_merge_safe(wiki)
        combined = f"{commit_result.stdout}\n{commit_result.stderr}".strip()
        return {
            "status": "partial",
            "action": "merge_conflict:keep_remote",
            "message": (
                "Kept remote version of conflicted files but the merge commit failed. "
                "The merge has been aborted to leave the wiki in a clean state. "
                "Common cause: git user.email/user.name is not configured. "
                "Call resolve_wiki_issue again to retry or choose a different strategy."
            ),
            "commit_stderr": combined,
        }
    return {
        "status": "resolved",
        "action": "merge_conflict:keep_remote",
        "message": f"Kept remote version of {len(unmerged)} file(s) and completed the merge.",
    }


@_action("merge_conflict:abort_merge")
def _handle_merge_conflict_abort_merge(wiki: Path, repo_name: str, branch: str) -> dict:

    if not _has_merge_in_progress(wiki):
        return {"status": "error", "error": "No merge in progress."}
    _abort_merge_safe(wiki)
    return {
        "status": "resolved",
        "action": "merge_conflict:abort_merge",
        "message": "Merge aborted. Wiki is back to pre-merge state.",
    }


@_action("dirty_worktree:commit_and_push")
def _handle_dirty_worktree_commit_and_push(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(["add", "--sparse", "-A"], cwd=wiki, check=False)
    commit_result = run_git(
        ["commit", *_no_verify_flag(), "-m", "wiki: commit pending changes"],
        cwd=wiki,
        check=False,
    )
    if commit_result.returncode != 0:
        combined = f"{commit_result.stdout}\n{commit_result.stderr}".strip()
        return {
            "status": "partial",
            "action": "dirty_worktree:commit_and_push",
            "message": (
                "Failed to commit pending wiki changes. "
                "Common cause: git user.email/user.name is not configured. "
                "Run resolve_wiki_issue again to diagnose."
            ),
            "commit_stderr": combined,
        }
    push = run_git(
        ["push", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
        cwd=wiki,
        check=False,
    )
    if push.returncode != 0:
        return {
            "status": "partial",
            "action": "dirty_worktree:commit_and_push",
            "message": "Changes committed but push failed. Run resolve_wiki_issue again to diagnose.",
        }
    return {
        "status": "resolved",
        "action": "dirty_worktree:commit_and_push",
        "message": "Committed and pushed all pending wiki changes.",
    }


@_action("dirty_worktree:discard_changes")
def _handle_dirty_worktree_discard_changes(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(["checkout", "--", "."], cwd=wiki, check=False)
    run_git(["clean", "-fd"], cwd=wiki, check=False)
    return {
        "status": "resolved",
        "action": "dirty_worktree:discard_changes",
        "message": "Discarded all uncommitted wiki changes.",
    }


@_action("diverged:merge")
def _handle_diverged_merge(wiki: Path, repo_name: str, branch: str) -> dict:

    resp = merge_and_autoresolve(wiki, repo_name, "diverged:merge")
    if resp is not None:
        return resp
    push = run_git(
        ["push", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
        cwd=wiki,
        check=False,
    )
    return {
        "status": "resolved",
        "action": "diverged:merge",
        "message": "Merged remote changes and pushed."
        if push.returncode == 0
        else "Merged remote changes. Push failed — retry push_wiki.",
    }


@_action("diverged:force_push")
def _handle_diverged_force_push(wiki: Path, repo_name: str, branch: str) -> dict:

    push = run_git(
        ["push", "--force-with-lease", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
        cwd=wiki,
        check=False,
    )
    if push.returncode != 0:
        return {
            "status": "error",
            "action": "diverged:force_push",
            "error": (
                f"Force push refused: remote has new content we don't know about. "
                f"This protects concurrent wiki changes from other agents. "
                f"Run resolve_wiki_issue with action='diverged:merge' to merge first, "
                f"then push normally. "
                f"Original error: {push.stderr.strip()}"
            ),
        }
    return {
        "status": "resolved",
        "action": "diverged:force_push",
        "message": (
            "Force-pushed local wiki to remote (with lease check). "
            "Remote now matches your local state. "
            "The lease check ensures no concurrent changes were overwritten."
        ),
    }


@_action("diverged:reset_to_remote")
def _handle_diverged_reset_to_remote(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(
        ["reset", "--hard", WIKI_REMOTE_REF],
        cwd=wiki,
        check=False,
    )
    return {
        "status": "resolved",
        "action": "diverged:reset_to_remote",
        "message": "Reset local wiki to match remote. Local-only changes have been discarded.",
    }


@_action("index_locked:remove_lock")
def _handle_index_locked_remove_lock(wiki: Path, repo_name: str, branch: str) -> dict:

    git_dir = wiki / ".git"
    if git_dir.is_file():
        text = git_dir.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            git_dir = (wiki / text.split(":", 1)[1].strip()).resolve()
    lock = git_dir / "index.lock"
    if lock.exists():
        lock.unlink()
    return {
        "status": "resolved",
        "action": "index_locked:remove_lock",
        "message": "Removed stale lock file. Wiki operations should work now.",
    }


@_action("wiki_content_corrupt:sanitize")
def _handle_wiki_content_corrupt_sanitize(wiki: Path, repo_name: str, branch: str) -> dict:

    if not repo_name or not branch:
        return {
            "status": "error",
            "error": "repo_name and branch are required for this action.",
        }
    repo_wiki_path = wiki / repo_name / branch
    if not repo_wiki_path.exists():
        return {
            "status": "error",
            "error": f"Wiki path {repo_wiki_path} does not exist.",
        }

    sanitized = []
    for f in repo_wiki_path.rglob("*.md"):
        try:
            f.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            content = f.read_text(encoding="utf-8", errors="replace")
            f.write_text(content, encoding="utf-8")
            sanitized.append(str(f.relative_to(repo_wiki_path)))
        except Exception:  # noqa: BLE001, S110
            pass

    if sanitized:
        return {
            "status": "resolved",
            "action": "wiki_content_corrupt:sanitize",
            "message": f"Sanitized {len(sanitized)} file(s) with encoding errors.",
            "sanitized_files": sanitized,
        }
    return {
        "status": "resolved",
        "action": "wiki_content_corrupt:sanitize",
        "message": "No corrupted files found.",
    }


@_action("behind_remote:pull_and_merge")
def _handle_behind_remote_pull_and_merge(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(
        ["-c", "protocol.file.allow=always", "fetch", "origin", WIKI_REMOTE_BRANCH],
        cwd=wiki,
        check=False,
    )
    resp = merge_and_autoresolve(wiki, repo_name, "behind_remote:pull_and_merge")
    if resp is not None:
        return resp
    return {
        "status": "resolved",
        "action": "behind_remote:pull_and_merge",
        "message": "Pulled and merged remote changes into local wiki.",
    }


@_action("behind_remote:reset_to_remote")
def _handle_behind_remote_reset_to_remote(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(
        ["reset", "--hard", WIKI_REMOTE_REF],
        cwd=wiki,
        check=False,
    )
    return {
        "status": "resolved",
        "action": "behind_remote:reset_to_remote",
        "message": "Reset local wiki to match remote. Local-only changes have been discarded.",
    }


@_action("ahead_of_remote:push")
def _handle_ahead_of_remote_push(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(
        ["-c", "protocol.file.allow=always", "fetch", "origin", WIKI_REMOTE_BRANCH, "--deepen=10"],
        cwd=wiki,
        check=False,
    )
    if ref_exists(WIKI_REMOTE_REF, cwd=wiki):
        resp = merge_and_autoresolve(wiki, repo_name, "ahead_of_remote:push")
        if resp is not None:
            return resp
    push = run_git(
        ["push", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
        cwd=wiki,
        check=False,
    )
    if push.returncode != 0:
        return {
            "status": "partial",
            "action": "ahead_of_remote:push",
            "message": f"Push failed: {push.stderr.strip()}. Run resolve_wiki_issue again.",
        }
    return {
        "status": "resolved",
        "action": "ahead_of_remote:push",
        "message": "Synced with remote and pushed local changes.",
    }


@_action("ahead_of_remote:stash_pull_merge")
def _handle_ahead_of_remote_stash_pull_merge(wiki: Path, repo_name: str, branch: str) -> dict:

    stash = run_git(["stash", "push", "-m", "wiki: pre-push stash"], cwd=wiki, check=False)
    if stash.returncode != 0:
        push = run_git(
            ["push", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
            cwd=wiki,
            check=False,
        )
        if push.returncode != 0:
            return {
                "status": "error",
                "action": "ahead_of_remote:stash_pull_merge",
                "error": f"Push failed: {push.stderr.strip()}",
            }
        return {
            "status": "resolved",
            "action": "ahead_of_remote:stash_pull_merge",
            "message": "No changes to stash. Pushed local changes directly.",
        }
    run_git(
        ["-c", "protocol.file.allow=always", "fetch", "origin", WIKI_REMOTE_BRANCH, "--deepen=10"],
        cwd=wiki,
        check=False,
    )
    if ref_exists(WIKI_REMOTE_REF, cwd=wiki):
        run_git(
            ["merge", "--ff-only", WIKI_REMOTE_REF],
            cwd=wiki,
            check=False,
        )
    pop = run_git(["stash", "pop"], cwd=wiki, check=False)
    if pop.returncode != 0:
        run_git(["checkout", "--", "."], cwd=wiki, check=False)
        run_git(["stash", "apply"], cwd=wiki, check=False)
        return {
            "status": "partial",
            "action": "ahead_of_remote:stash_pull_merge",
            "message": (
                "Stash pop produced conflicts. Your changes have been "
                "re-applied from stash (stash entry preserved). "
                "Resolve conflicts and commit, then push."
            ),
        }
    push = run_git(
        ["push", "origin", f"HEAD:refs/heads/{WIKI_REMOTE_BRANCH}"],
        cwd=wiki,
        check=False,
    )
    return {
        "status": "resolved" if push.returncode == 0 else "partial",
        "action": "ahead_of_remote:stash_pull_merge",
        "message": "Stashed changes, pulled remote, re-applied stash, and pushed."
        if push.returncode == 0
        else f"Stash applied but push failed: {push.stderr.strip()}",
    }


@_action("ahead_of_remote:discard_local")
def _handle_ahead_of_remote_discard_local(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(
        ["reset", "--hard", WIKI_REMOTE_REF],
        cwd=wiki,
        check=False,
    )
    return {
        "status": "resolved",
        "action": "ahead_of_remote:discard_local",
        "message": "Reset local wiki to match remote. Local-only changes have been discarded.",
    }


@_action("rebase_in_progress:continue_rebase")
def _handle_rebase_in_progress_continue_rebase(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(["add", "-A"], cwd=wiki, check=False)
    cont = run_git(
        ["rebase", "--continue"], cwd=wiki, check=False, env_extra={"GIT_EDITOR": "true"}
    )
    return {
        "status": "resolved" if cont.returncode == 0 else "partial",
        "action": "rebase_in_progress:continue_rebase",
        "message": "Continued rebase."
        if cont.returncode == 0
        else f"Rebase continue failed: {cont.stderr.strip()}",
    }


@_action("rebase_in_progress:abort_rebase")
def _handle_rebase_in_progress_abort_rebase(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(["rebase", "--abort"], cwd=wiki, check=False)
    return {
        "status": "resolved",
        "action": "rebase_in_progress:abort_rebase",
        "message": "Rebase aborted. Wiki is back to pre-rebase state.",
    }


@_action("rebase_in_progress:skip_and_continue")
def _handle_rebase_in_progress_skip_and_continue(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(["rebase", "--skip"], cwd=wiki, check=False)
    return {
        "status": "resolved",
        "action": "rebase_in_progress:skip_and_continue",
        "message": "Skipped current commit and continued rebase.",
    }


@_action("cherry_pick_in_progress:continue_cherry_pick")
def _handle_cherry_pick_in_progress_continue_cherry_pick(
    wiki: Path, repo_name: str, branch: str
) -> dict:

    run_git(["add", "-A"], cwd=wiki, check=False)
    cont = run_git(
        ["cherry-pick", "--continue"], cwd=wiki, check=False, env_extra={"GIT_EDITOR": "true"}
    )
    return {
        "status": "resolved" if cont.returncode == 0 else "partial",
        "action": "cherry_pick_in_progress:continue_cherry_pick",
        "message": "Continued cherry-pick."
        if cont.returncode == 0
        else f"Cherry-pick continue failed: {cont.stderr.strip()}",
    }


@_action("cherry_pick_in_progress:abort_cherry_pick")
def _handle_cherry_pick_in_progress_abort_cherry_pick(
    wiki: Path, repo_name: str, branch: str
) -> dict:

    run_git(["cherry-pick", "--abort"], cwd=wiki, check=False)
    return {
        "status": "resolved",
        "action": "cherry_pick_in_progress:abort_cherry_pick",
        "message": "Cherry-pick aborted. Wiki is back to pre-cherry-pick state.",
    }


@_action("revert_in_progress:continue_revert")
def _handle_revert_in_progress_continue_revert(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(["add", "-A"], cwd=wiki, check=False)
    cont = run_git(
        ["revert", "--continue"], cwd=wiki, check=False, env_extra={"GIT_EDITOR": "true"}
    )
    return {
        "status": "resolved" if cont.returncode == 0 else "partial",
        "action": "revert_in_progress:continue_revert",
        "message": "Continued revert."
        if cont.returncode == 0
        else f"Revert continue failed: {cont.stderr.strip()}",
    }


@_action("revert_in_progress:abort_revert")
def _handle_revert_in_progress_abort_revert(wiki: Path, repo_name: str, branch: str) -> dict:

    run_git(["revert", "--abort"], cwd=wiki, check=False)
    return {
        "status": "resolved",
        "action": "revert_in_progress:abort_revert",
        "message": "Revert aborted. Wiki is back to pre-revert state.",
    }


@_action("missing_remote:reconfigure_remote")
def _handle_missing_remote_reconfigure_remote(wiki: Path, repo_name: str, branch: str) -> dict:

    remote_url = os.environ.get("WIKI_MCP_REMOTE_URL", "")
    if not remote_url:
        return {
            "status": "error",
            "action": "missing_remote:reconfigure_remote",
            "error": (
                "WIKI_MCP_REMOTE_URL is not set. Cannot reconfigure remote "
                "without a URL. Set the environment variable and retry."
            ),
        }
    result = run_git(
        ["remote", "set-url", "origin", remote_url],
        cwd=wiki,
        check=False,
    )
    if result.returncode != 0:
        add = run_git(
            ["remote", "add", "origin", remote_url],
            cwd=wiki,
            check=False,
        )
        if add.returncode != 0:
            return {
                "status": "error",
                "action": "missing_remote:reconfigure_remote",
                "error": f"Failed to configure remote: {add.stderr.strip()}",
            }
    return {
        "status": "resolved",
        "action": "missing_remote:reconfigure_remote",
        "message": "Remote 'origin' configured with URL from WIKI_MCP_REMOTE_URL.",
    }


@_action("remote_unreachable:retry_fetch")
def _handle_remote_unreachable_retry_fetch(wiki: Path, repo_name: str, branch: str) -> dict:

    ls = run_git(
        ["-c", "protocol.file.allow=always", "ls-remote", "origin", WIKI_REMOTE_BRANCH],
        cwd=wiki,
        check=False,
        timeout=30,
    )
    if ls.returncode != 0:
        return {
            "status": "error",
            "action": "remote_unreachable:retry_fetch",
            "error": f"Remote still unreachable (ls-remote failed): {ls.stderr.strip()}",
        }
    fetch = run_git(
        ["-c", "protocol.file.allow=always", "fetch", "origin", WIKI_REMOTE_BRANCH],
        cwd=wiki,
        check=False,
    )
    if fetch.returncode != 0:
        return {
            "status": "error",
            "action": "remote_unreachable:retry_fetch",
            "error": f"Full fetch failed: {fetch.stderr.strip()}",
        }
    return {
        "status": "resolved",
        "action": "remote_unreachable:retry_fetch",
        "message": "Successfully fetched from remote (full fetch, no depth limit).",
    }


@_action("remote_unreachable:check_credentials")
def _handle_remote_unreachable_check_credentials(wiki: Path, repo_name: str, branch: str) -> dict:

    return {
        "status": "info",
        "action": "remote_unreachable:check_credentials",
        "message": (
            "Please verify:\n"
            "1. Your git credentials are configured (git credential fill)\n"
            "2. The remote URL is correct (git remote -v)\n"
            "3. Network connectivity to the remote host\n"
            "4. If using SSH, your SSH key is available"
        ),
    }


@_action("detached_head:checkout_wiki_branch")
def _handle_detached_head_checkout_wiki_branch(wiki: Path, repo_name: str, branch: str) -> dict:

    result = run_git(
        ["checkout", "-B", WIKI_REMOTE_BRANCH],
        cwd=wiki,
        check=False,
    )
    return {
        "status": "resolved" if result.returncode == 0 else "error",
        "action": "detached_head:checkout_wiki_branch",
        "message": f"Checked out '{WIKI_REMOTE_BRANCH}' branch."
        if result.returncode == 0
        else f"Checkout failed: {result.stderr.strip()}",
    }


@_action("corrupted_submodule:reinitialize")
def _handle_corrupted_submodule_reinitialize(wiki: Path, repo_name: str, branch: str) -> dict:

    global _reinit_depth
    if _reinit_depth > 0:
        return {
            "status": "error",
            "action": "corrupted_submodule:reinitialize",
            "error": (
                "Reinitialize is already in progress — "
                "the wiki submodule is corrupted and cannot be "
                "repaired automatically."
            ),
        }
    _reinit_depth += 1
    try:
        shutil.rmtree(wiki, ignore_errors=True)
    except OSError as e:
        _reinit_depth -= 1
        return {
            "status": "error",
            "action": "corrupted_submodule:reinitialize",
            "error": f"Failed to remove corrupted wiki directory: {e}",
        }
    from tools.pull import pull

    result = pull(repo_name=repo_name or None, branch=branch or None, repo_path=str(wiki.parent))
    _reinit_depth -= 1
    return result


@_action("sparse_checkout_stale:refresh")
def _handle_sparse_checkout_stale_refresh(wiki: Path, repo_name: str, branch: str) -> dict:

    if not repo_name or not branch:
        return {
            "status": "error",
            "action": "sparse_checkout_stale:refresh",
            "error": "Could not determine repo name or branch for sparse-checkout refresh.",
        }
    pattern = f"{repo_name}/{branch}"
    if not set_sparse_checkout_cone(wiki, [pattern]):
        run_git(["sparse-checkout", "set", pattern], cwd=wiki, check=False)
    run_git(["read-tree", "-mu", "HEAD"], cwd=wiki, check=False)
    run_git(["checkout", "--force", "HEAD"], cwd=wiki, check=False)
    from utils.wiki_index import WikiIndex

    WikiIndex._cache.clear()
    return {
        "status": "resolved",
        "action": "sparse_checkout_stale:refresh",
        "message": f"Refreshed sparse-checkout for '{pattern}'. Working tree synced.",
    }


@_action("wiki_not_initialized:pull")
def _handle_wiki_not_initialized_pull(wiki: Path, repo_name: str, branch: str) -> dict:

    from tools.pull import pull

    return pull(repo_name=repo_name or None, branch=branch or None, repo_path=str(wiki.parent))


def _execute(wiki: Path, action: str, repo_name: str = "", branch: str = "") -> dict:
    handler = _ACTION_HANDLERS.get(action)
    if handler is None:
        return {"status": "error", "error": f"Unknown action: {action!r}"}
    return handler(wiki, repo_name, branch)


_reinit_depth = 0


class ResolveBuilder:
    """Builds and executes a resolve workflow in discrete stages.

    Two modes:
    - Diagnose (no action): resolves context, runs diagnosis, returns issues.
    - Execute (with action): resolves context, executes the resolution action.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._wiki: Path = Path()
        self._repo_name: str = ""
        self._branch: str = ""
        self._action: str | None = None
        self._accumulated: dict[str, Any] = {}

    def for_wiki(
        self,
        repo_path: str | None,
        repo_name: str | None,
        branch: str | None,
    ) -> ResolveBuilder:
        from config import repo_root as _default_repo_root
        from utils.git import derive_branch, derive_repo_name

        root = Path(repo_path).resolve() if repo_path else _default_repo_root()
        self._wiki = root / "wiki"
        # Derive from the code repo when not supplied so a direct caller (not
        # going through server._resolve_context) still gets a scoped context.
        # Without this, repo_name/branch are empty and the sync-state check's
        # "{repo}/{branch}/" pathspec is invalid → rev-list fails → the
        # divergence probe falls back to a test-merge and misfires.
        self._repo_name = repo_name or derive_repo_name(root)
        self._branch = branch or derive_branch(root)
        return self

    # ── stage 1: resolve context ──────────────────────────────────

    def _resolve_context(self) -> tuple[bool, dict | None]:
        return True, None

    # ── stage 2a: diagnose ────────────────────────────────────────

    def _diagnose_wiki(self) -> tuple[bool, dict | None]:
        log.info("resolve_wiki_issue: diagnosing %s", self._wiki)
        issues = _diagnose(self._wiki, repo_name=self._repo_name, branch=self._branch)
        self._accumulated = {
            "status": "diagnosis",
            "wiki_path": str(self._wiki),
            "issues": issues,
        }
        return True, None

    # ── stage 2b: execute action ───────────────────────────────────

    def _execute_action(self) -> tuple[bool, dict | None]:
        log.info("resolve_wiki_issue: executing %s", self._action or "")
        assert self._action is not None
        result = _execute(self._wiki, self._action, repo_name=self._repo_name, branch=self._branch)
        from utils.wiki_index import WikiIndex

        WikiIndex._cache.clear()
        return False, result  # Short-circuit: result is the final response

    # ── orchestration ─────────────────────────────────────────────

    def execute(self, action: str | None = None) -> dict:
        self._action = action
        self._accumulated = {}

        stages: list = [self._resolve_context]
        if action:
            stages.append(self._execute_action)
        else:
            stages.append(self._diagnose_wiki)

        for stage in stages:
            ok, result = stage()
            if not ok:
                assert result is not None, f"stage {stage.__name__} returned (False, None)"
                return result
            if result is not None:
                self._accumulated.update(result)

        return self.to_result()

    def to_result(self) -> dict:
        return dict(self._accumulated)


# ── backward-compatible entry point ────────────────────────────────────


def resolve(
    action: str | None = None,
    repo_path: str | None = None,
    repo_name: str | None = None,
    branch: str | None = None,
) -> dict:
    return ResolveBuilder().for_wiki(repo_path, repo_name, branch).execute(action)
