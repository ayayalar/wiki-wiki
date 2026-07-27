"""ingest: return the diff of code changes the agent should fold into the wiki.

We diff between the origin default branch base and HEAD. The origin default
branch is resolved from the `refs/remotes/origin/HEAD` symbolic ref (set
automatically by `git clone`). If no origin default branch is available
(e.g. on the base branch itself or a fresh repo), we fall back to
`git diff HEAD` (working tree + staged).

When the wiki for the current branch is empty (fresh scaffold, reset, etc.),
we still compute the diff against the origin base and do a **targeted ingest**
— only creating wiki pages for files that differ from the origin base. Full
codebase ingest (directory structure + key files) only happens when there's
no origin base branch to compare against.

Performance notes
-----------------
Full ingest (empty wiki) now runs only **two** git subprocesses:
  1. ``git ls-tree -r HEAD --name-only``  - shared between directory
     summary and key-file matching.
  2. ``git cat-file --batch``             - reads ALL matched key files
     in a single call (replaces the old Nx``git show`` loop).

This reduces subprocess overhead from ~22-32 calls down to 2 on large
repos, cutting full-ingest time from 6-16 s to under 2 s.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from config import repo_root as _default_repo_root
from utils.git import get_current_branch, get_merge_base, get_origin_default_branch, get_repo_name, ref_exists, run_git, wiki_is_initialized
from utils.wiki import get_log_tail, wiki_not_initialized_response, check_params

# Exclude wiki submodule and .gitmodules from all diffs
_EXCLUDE = ["--", ".", ":(exclude)wiki", ":(exclude).gitmodules"]

# Max diff size in chars before we truncate
_MAX_DIFF_CHARS = 80_000

# Max total chars for key file contents in full ingest
_MAX_KEY_FILES_CHARS = 40_000

# Max chars per individual key file
_MAX_SINGLE_FILE_CHARS = 3000

# Max chars for the wiki index snippet in responses
_MAX_INDEX_CHARS = 2000

# Overall response size cap (excluding instruction text).
# If exceeded, key_files and index are stripped to keep the
# response within model context limits.
_MAX_RESPONSE_CHARS = 120_000

# Patterns for key architectural files (case-insensitive)
_KEY_FILE_PATTERNS: list[str] = [
    r"README\.md$",
    r"Program\.cs$",
    r"Startup\.cs$",
    r"appsettings\.json$",
    r"appsettings\.Development\.json$",
    r"\.csproj$",
    r"package\.json$",
    r"tsconfig\.json$",
    r"app\.(py|ts|js)$",
    r"main\.(py|ts|js|go)$",
    r"index\.(ts|js)$",
    r"Dockerfile$",
    r"docker-compose\.ya?ml$",
    r"\.env\.example$",
    r"requirements\.txt$",
    r"pyproject\.toml$",
    r"go\.mod$",
    r"Cargo\.toml$",
    r"pom\.xml$",
    r"build\.gradle$",
]

# Pre-compiled regexes (P3 fix - avoid recompiling on every call)
_COMPILED_KEY_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _KEY_FILE_PATTERNS]

# Windows subprocess flags - match utils/git.py conventions
_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# P4 fix: _wiki_is_empty checks for actual domain entries, not placeholder text
# ---------------------------------------------------------------------------

def _wiki_is_empty(index_path: Path) -> bool:
    """True if the wiki index doesn't exist or has no agent-written domain entries.

    The scaffold template contains a placeholder ``## [Domain](domain/index.md)``
    with an uppercase "D". Agent-written entries use lowercase domain names like
    ``## [src](src/index.md)``. We detect this by checking for ``## [`` followed
    by a lowercase letter, which is the canonical pattern agents produce.

    Also checks for the scaffold placeholder text as a fallback, so the check
    remains robust against template changes.

    Additionally checks for domain subdirectories with their own index.md files —
    if they exist, the wiki is not truly empty even if the root index.md is
    malformed or missing domain entries.
    """
    if not index_path.exists():
        return True
    # Domain subdirectories with index.md indicate existing wiki content,
    # even if the root index.md is malformed or missing entries
    parent = index_path.parent
    if parent.exists():
        for entry in parent.iterdir():
            if entry.is_dir() and (entry / "index.md").exists():
                return False
    try:
        with open(index_path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return True
    # Scaffold placeholder contains this exact phrase
    if "will be added by the agent" in head:
        return True
    # Agent-written entries: ## [lowercase_name](path)
    return not re.search(r"##\s+\[[a-z]", head)


# ---------------------------------------------------------------------------
# P1 fix: shared file-list helper (replaces duplicate ls-tree calls)
# ---------------------------------------------------------------------------

def _get_all_files(code_root: Path) -> list[str]:
    """Return a deduplicated list of all tracked files in the repo.

    Shared between ``_directory_summary`` and ``_match_key_files`` so
    ``git ls-tree -r HEAD`` is invoked exactly once per ingest call.
    """
    result = run_git(
        ["ls-tree", "-r", "HEAD", "--name-only"],
        cwd=code_root, check=False,
    )
    return [
        f for f in result.stdout.strip().splitlines()
        if f and not f.startswith("wiki/") and f != ".gitmodules"
    ]


# ---------------------------------------------------------------------------
# _directory_summary - now accepts a pre-fetched file list
# ---------------------------------------------------------------------------

def _directory_summary(all_files: list[str]) -> str:
    """Return a compact directory structure with file counts per folder."""
    # Group by top-level directory, showing 2nd-level subdirs
    groups: dict[str, dict[str, int]] = {}
    root_files: list[str] = []
    for f in all_files:
        parts = f.split("/")
        if len(parts) == 1:
            root_files.append(f)
        else:
            top = parts[0]
            sub = parts[1] if len(parts) > 2 else "(files)"
            groups.setdefault(top, {})
            groups[top][sub] = groups[top].get(sub, 0) + 1

    lines = [f"Total: {len(all_files)} files\n"]
    if root_files:
        lines.append(f"(root): {', '.join(root_files)}")
    for folder in sorted(groups):
        subs = groups[folder]
        total = sum(subs.values())
        sub_parts = [f"{s} ({c})" for s, c in sorted(subs.items())]
        lines.append(f"{folder}/ [{total} files]: {', '.join(sub_parts[:15])}")
        if len(sub_parts) > 15:
            lines.append(f"  ... and {len(sub_parts) - 15} more subdirs")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Key-file matching - now accepts a pre-fetched file list
# ---------------------------------------------------------------------------

def _match_key_files(all_files: list[str]) -> list[str]:
    """Return paths of files matching key architectural patterns.

    Uses pre-compiled regexes (_COMPILED_KEY_PATTERNS) to avoid
    per-call recompilation.
    """
    matched: list[str] = []
    for f in all_files:
        basename = f.rsplit("/", 1)[-1]
        if any(rx.search(basename) for rx in _COMPILED_KEY_PATTERNS):
            matched.append(f)
    return sorted(matched)

# ---------------------------------------------------------------------------
# P2 fix: batch-read key files via git cat-file --batch (single subprocess)
# ---------------------------------------------------------------------------

def _read_files_batch(code_root: Path, filepaths: list[str]) -> dict[str, str]:
    """Read multiple files from HEAD in a single git subprocess.

    Returns {filepath: content} for each readable file.  Skips files
    that are missing, binary, or exceed ``_MAX_SINGLE_FILE_CHARS``.
    """
    if not filepaths:
        return {}

    refs = [f"HEAD:{fp}" for fp in filepaths]

    # --- Windows: use temp files to avoid pipe-handle inheritance hangs ---
    if sys.platform == "win32":
        import shutil
        tmpdir = tempfile.mkdtemp(prefix="wiki_ingest_")
        in_path = os.path.join(tmpdir, "in")
        out_path = os.path.join(tmpdir, "out")
        try:
            with open(in_path, "w", encoding="utf-8") as f:
                for ref in refs:
                    f.write(ref + "\n")
            with open(in_path, "r", encoding="utf-8") as in_f, \
                 open(out_path, "wb") as out_f:
                proc = subprocess.Popen(
                    ["git", "cat-file", "--batch"],
                    cwd=str(code_root),
                    stdin=in_f,
                    stdout=out_f,
                    stderr=subprocess.DEVNULL,
                    creationflags=_CREATIONFLAGS,
                )
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                    return {}
            with open(out_path, "rb") as f:
                data = f.read()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        stdin_data = ("\n".join(refs) + "\n").encode("utf-8")
        proc = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=str(code_root),
            input=stdin_data,
            capture_output=True,
            timeout=30,
            creationflags=_CREATIONFLAGS,
        )
        if proc.returncode != 0:
            return {}
        data = proc.stdout

    # Parse binary output: "<sha> blob <size>\n<content>\n" per entry
    files: dict[str, str] = {}
    offset = 0
    for filepath in filepaths:
        try:
            nl = data.index(b"\n", offset)
            header = data[offset:nl].decode("utf-8", errors="replace")
            offset = nl + 1
            if "missing" in header:
                continue
            parts = header.split()
            if len(parts) < 3:
                break
            size = int(parts[2])
            raw = data[offset:offset + size]
            offset += size + 1  # skip trailing newline
            # M2 fix: skip binary files
            if b"\x00" in raw:
                continue
            content = raw.decode("utf-8", errors="replace")
            if not content.strip():
                continue
            if len(content) > _MAX_SINGLE_FILE_CHARS:
                content = content[:_MAX_SINGLE_FILE_CHARS] + "\n... (truncated)"
            files[filepath] = content
        except (ValueError, IndexError):
            break

    return files

# ---------------------------------------------------------------------------
# _collect_key_files - orchestrator (no subprocesses of its own)
# ---------------------------------------------------------------------------

def _collect_key_files(code_root: Path, all_files: list[str]) -> str:
    """Read key architectural files and return their contents inline.

    Accepts a pre-fetched file list to avoid a duplicate ``ls-tree`` call.
    Uses ``_read_files_batch`` to read all files in one subprocess.
    """
    matched = _match_key_files(all_files)
    if not matched:
        return "(no key files found)"

    file_contents = _read_files_batch(code_root, matched)

    sections: list[str] = []
    total_chars = 0
    for filepath in matched:  # already sorted by _match_key_files
        if total_chars >= _MAX_KEY_FILES_CHARS:
            sections.append(f"\n... (key files truncated at {_MAX_KEY_FILES_CHARS} chars)")
            break
        content = file_contents.get(filepath)
        if not content:
            continue
        sections.append(f"### {filepath}\n```\n{content}\n```")
        total_chars += len(content)

    return "\n\n".join(sections) if sections else "(no key files found)"


# ---------------------------------------------------------------------------
# _truncate_index: keep only the top of index.md (domain list)
# ---------------------------------------------------------------------------

def _truncate_index(text: str) -> str:
    """Return at most _MAX_INDEX_CHARS of the wiki index, truncating
    gracefully at a line boundary."""
    if len(text) <= _MAX_INDEX_CHARS:
        return text
    truncated = text[:_MAX_INDEX_CHARS]
    # Cut at the last newline to avoid mid-line truncation
    last_nl = truncated.rfind("\n")
    if last_nl > _MAX_INDEX_CHARS * 0.5:
        truncated = truncated[:last_nl]
    return truncated + f"\n... (index truncated, {len(text)} total chars)"


# ---------------------------------------------------------------------------
# _cap_response: enforce overall response size limit
# ---------------------------------------------------------------------------

def _cap_response(resp: dict) -> dict:
    """If the response body (excluding 'instruction') exceeds
    _MAX_RESPONSE_CHARS, strip 'key_files' and 'index' to shrink it."""
    # Estimate serialized size of non-instruction fields
    import json
    body = {k: v for k, v in resp.items() if k != "instruction"}
    size = len(json.dumps(body, ensure_ascii=False))

    if size <= _MAX_RESPONSE_CHARS:
        return resp

    # Strip key_files first (largest contributor in full ingest)
    if resp.get("key_files"):
        resp["key_files"] = f"(key_files suppressed: response size {size} exceeds {_MAX_RESPONSE_CHARS} cap)"
        resp.setdefault("_truncated", []).append("key_files")

    # Re-check; if still over, strip index too
    body = {k: v for k, v in resp.items() if k != "instruction"}
    size = len(json.dumps(body, ensure_ascii=False))
    if size > _MAX_RESPONSE_CHARS and resp.get("index"):
        resp["index"] = f"(index suppressed: response size exceeds {_MAX_RESPONSE_CHARS} cap)"
        resp.setdefault("_truncated", []).append("index")

    return resp


# ---------------------------------------------------------------------------
# M1 fix: _compute_diff warns when merge-base fallback is used
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _format_log_table: parse log.md entries into a markdown table
# ---------------------------------------------------------------------------

def _format_log_table(log_text: str) -> str:
    """Parse ``log.md`` entries and return a markdown table."""
    lines = log_text.strip().splitlines()
    entries: list[tuple[str, str, str]] = []
    for line in lines:
        line = line.strip()
        if line.startswith("## [") and "] " in line and " | " in line:
            rest = line[4:].strip()
            parts = rest.split("] ", 1)
            if len(parts) == 2:
                date = parts[0].strip()
                op_summary = parts[1].strip()
                op_parts = op_summary.split(" | ", 1)
                op = op_parts[0].strip()
                summary = op_parts[1].strip() if len(op_parts) > 1 else ""
                entries.append((date, op, summary))

    table = "| Date | Operation | Summary |\n|------|-----------|---------|\n"
    if not entries:
        table += "| (none) | |\n"
    else:
        for date, op, summary in entries:
            table += f"| {date} | {op} | {summary} |\n"
    return table


# ---------------------------------------------------------------------------


def _validate_scope(paths: list[str] | None, topic: str | None) -> str | None:
    """Validate mutually-exclusive scope params. Returns error message or None."""
    has_paths = paths is not None and len(paths) > 0
    has_topic = topic is not None and topic.strip() != ""

    if has_paths and has_topic:
        return "Provide either 'paths' or 'topic', not both."

    if has_topic:
        assert topic is not None
        if "\0" in topic:
            return "topic contains null bytes."
        if len(topic) > 200:
            return "topic is too long (max 200 chars)."

    if has_paths:
        from pathlib import PureWindowsPath

        assert paths is not None
        for p in paths:
            if not isinstance(p, str) or not p.strip():
                return "paths entries must be non-empty strings."
            if "\0" in p:
                return "paths entry contains null bytes."
            for flavor in (Path(p), PureWindowsPath(p)):
                if ".." in flavor.parts:
                    return f"paths entry {p!r} contains '..' — traversal not allowed."
                if flavor.is_absolute() or flavor.drive or flavor.root:
                    return f"paths entry {p!r} must be repo-relative (no absolute/drive/root)."

    return None


def _resolve_scope(all_files: list[str], paths: list[str] | None, topic: str | None) -> list[str]:
    """Return subset of all_files matching scope; empty scope returns all files."""
    import fnmatch

    has_paths = paths is not None and len(paths) > 0
    has_topic = topic is not None and topic.strip() != ""

    if not has_paths and not has_topic:
        return all_files

    if has_paths:
        assert paths is not None
        normalized = [p.replace("\\", "/").strip().rstrip("/") for p in paths]
        out: list[str] = []
        for file_path in all_files:
            for pattern in normalized:
                if (
                    file_path == pattern
                    or file_path.startswith(pattern + "/")
                    or fnmatch.fnmatchcase(file_path, pattern)
                    or fnmatch.fnmatchcase(file_path, pattern + "/*")
                ):
                    out.append(file_path)
                    break
        return out

    assert topic is not None
    token = topic.strip().lower()
    seps = str.maketrans("/_-.", "    ")
    out: list[str] = []
    for file_path in all_files:
        lowered = file_path.lower()
        if token in lowered or token in lowered.translate(seps).split():
            out.append(file_path)
    return out


# ---------------------------------------------------------------------------


def _compute_diff(code_root: Path, scope_pathspec: list[str] | None = None) -> tuple[str, str]:
    """Returns (diff_text, diff_spec).

    Resolution order:
      1. origin default branch merge-base..HEAD
      2. origin default branch HEAD~..HEAD  (fallback when merge-base fails)
      3. HEAD~..HEAD              (no origin default branch available)
      4. HEAD (working tree)      (single-commit repo)

    The origin default branch is resolved from ``refs/remotes/origin/HEAD``
    (set automatically by ``git clone``). When the origin default branch
    exists but merge-base resolution fails (e.g. unrelated histories),
    we fall back to ``HEAD~..HEAD`` and annotate the diff_spec so the
    agent knows the scope is narrow.
    """
    pathspec = _EXCLUDE
    if scope_pathspec:
        normalized = [p.replace("\\", "/") for p in scope_pathspec if p and p.strip()]
        if len(normalized) > 400:
            reduced: set[str] = set()
            for entry in normalized:
                parts = entry.split("/")
                if len(parts) >= 2:
                    reduced.add(f"{parts[0]}/{parts[1]}")
                else:
                    reduced.add(parts[0])
            normalized = sorted(reduced)
        pathspec = ["--", *normalized, ":(exclude)wiki", ":(exclude).gitmodules"]

    origin_base = get_origin_default_branch(code_root)
    if origin_base and ref_exists(origin_base, cwd=code_root):
        base = get_merge_base(origin_base, "HEAD", cwd=code_root)
        if base:
            result = run_git(["diff", f"{base}..HEAD"] + pathspec, cwd=code_root, check=False)
            return result.stdout, f"{base[:8]}..HEAD"
        # origin base exists but merge-base failed - fall back with warning
        if ref_exists("HEAD~", cwd=code_root):
            result = run_git(["diff", "HEAD~..HEAD"] + pathspec, cwd=code_root, check=False)
            return result.stdout, f"HEAD~..HEAD (merge-base with {origin_base} unavailable)"
    if ref_exists("HEAD~", cwd=code_root):
        result = run_git(["diff", "HEAD~..HEAD"] + pathspec, cwd=code_root, check=False)
        return result.stdout, "HEAD~..HEAD"
    result = run_git(["diff", "HEAD"] + pathspec, cwd=code_root, check=False)
    return result.stdout, "HEAD"

# ---------------------------------------------------------------------------
# IngestBuilder
# ---------------------------------------------------------------------------


class IngestBuilder:
    """Builds and executes an ingest workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._repo_name: str = ""
        self._branch: str = ""
        self._code_root: Path = Path()
        self._wiki: Path = Path()
        self._repo_wiki_path: Path = Path()
        self._index_path: Path = Path()
        self._log_path: Path = Path()
        self._wiki_race_warning: bool = False
        self._paths: list[str] | None = None
        self._topic: str | None = None
        self._scope_active: bool = False
        self._accumulated: dict[str, Any] = {}

    # ── configuration ─────────────────────────────────────────────

    def for_repo_branch(
        self,
        repo_name: str | None = None,
        branch: str | None = None,
        repo_path: str | None = None,
        paths: list[str] | None = None,
        topic: str | None = None,
    ) -> IngestBuilder:
        """Resolve paths, repo name, and branch."""
        self._code_root = Path(repo_path).resolve() if repo_path else _default_repo_root()
        self._wiki = self._code_root / "wiki"
        self._repo_name = repo_name or get_repo_name()
        self._branch = branch or get_current_branch()
        self._repo_wiki_path = self._wiki / self._repo_name / self._branch
        self._index_path = self._repo_wiki_path / "index.md"
        self._log_path = self._repo_wiki_path / "log.md"
        self._paths = paths
        self._topic = topic
        self._scope_active = bool(paths) or bool(topic and topic.strip())
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        """Validate wiki is initialized and params are safe (C1 fix)."""
        if not wiki_is_initialized(self._wiki):
            return False, wiki_not_initialized_response(self._wiki)
        err = check_params(self._repo_name, self._branch)
        if err:
            return False, err

        scope_err = _validate_scope(self._paths, self._topic)
        if scope_err:
            return False, {"status": "invalid_params", "error": scope_err}

        return True, None

    def _scope_meta(self, matched_count: int) -> dict:
        return {
            "paths": self._paths or [],
            "topic": self._topic or "",
            "matched_files": matched_count,
        }

    def _scope_desc(self) -> str:
        if self._paths:
            return "paths: " + ", ".join(self._paths)
        if self._topic and self._topic.strip():
            return f"topic: {self._topic.strip()}"
        return ""

    def _no_files_matched_scope_response(self, all_files: list[str]) -> dict:
        tops = sorted({f.split("/", 1)[0] for f in all_files if "/" in f})
        return {
            "status": "no_files_matched_scope",
            "repo": self._repo_name,
            "wiki_path": str(self._repo_wiki_path),
            "scope": self._scope_meta(0),
            "available_top_level_dirs": tops,
            "instruction": (
                f"No tracked files matched the requested scope ({self._scope_desc()}). "
                "Widen or correct the scope and retry. Available top-level directories: "
                + ", ".join(tops)
            ),
        }

    def _no_changes_within_scope_response(self, diff_spec: str) -> dict:
        return {
            "status": "no changes",
            "diff": "",
            "diff_spec": f"{diff_spec} (within scope)",
            "repo": self._repo_name,
            "wiki_path": str(self._repo_wiki_path),
            "scope": self._scope_meta(0),
            "index": _truncate_index(
                self._index_path.read_text(encoding="utf-8", errors="replace")
                if self._index_path.exists()
                else ""
            ),
            "log_tail": get_log_tail(self._log_path, 5),
            "log_summary": _format_log_table(get_log_tail(self._log_path, 5)),
            "instruction": (
                f"No code changes within the requested scope ({self._scope_desc()}). "
                f"The wiki lives at {self._repo_wiki_path}. Nothing to update for this scope."
            ),
        }

    # ── stage 2: pre-ingest sync ─────────────────────────────────

    def _pre_ingest_sync(self) -> tuple[bool, dict | None]:
        """Pull from remote if behind or diverged. May retry once."""
        from tools.resolve import _is_behind_remote, _is_diverged
        from tools.pull import pull as _pull_impl

        if not _is_behind_remote(self._wiki, self._repo_name, self._branch) and not _is_diverged(self._wiki, self._repo_name, self._branch):
            return True, None

        pull_result = _pull_impl(
            repo_name=self._repo_name, branch=self._branch, repo_path=str(self._code_root)
        )
        if pull_result.get("status") in ("merge_conflict", "stash_conflict", "merge_in_progress"):
            return False, {
                "status": "wiki_sync_conflict",
                "resolve_action": pull_result.get("resolve_action"),
                "message": (
                    "Wiki has remote changes with conflicts. "
                    "Call resolve_wiki_issue() to resolve, then retry ingest_wiki()."
                ),
            }

        # Recheck: after pull, verify we're actually synced.
        if _is_behind_remote(self._wiki, self._repo_name, self._branch) or _is_diverged(self._wiki, self._repo_name, self._branch):
            pull_result2 = _pull_impl(
                repo_name=self._repo_name, branch=self._branch, repo_path=str(self._code_root)
            )
            if pull_result2.get("status") in ("merge_conflict", "stash_conflict", "merge_in_progress"):
                return False, {
                    "status": "wiki_sync_conflict",
                    "resolve_action": pull_result2.get("resolve_action"),
                    "message": (
                        "Wiki still has remote changes with conflicts after retry. "
                        "Call resolve_wiki_issue() to resolve, then retry ingest_wiki()."
                    ),
                }
            if _is_behind_remote(self._wiki, self._repo_name, self._branch) or _is_diverged(self._wiki, self._repo_name, self._branch):
                self._wiki_race_warning = True

        return True, None

    # ── stage 3: invalidate cache ─────────────────────────────────

    def _invalidate_cache(self) -> tuple[bool, dict | None]:
        """Clear the in-memory search index for this repo+branch."""
        from utils.wiki_index import WikiIndex
        WikiIndex.invalidate(self._repo_name, self._branch)
        return True, None

    # ── stage 4: compute result ───────────────────────────────────

    def _compute_result(self) -> tuple[bool, dict | None]:
        """Core branching: detect wiki state and build appropriate response."""
        empty_wiki = _wiki_is_empty(self._index_path)

        if empty_wiki:
            response = self._build_empty_wiki_response()
        else:
            response = self._build_incremental_response()

        if self._wiki_race_warning:
            response["wiki_sync_warning"] = (
                "Wiki may be behind remote (concurrent push detected). "
                "Consider calling pull_wiki() before updating pages."
            )
        return True, _cap_response(response)

    # ── empty wiki response helpers ──────────────────────────────

    def _build_empty_wiki_response(self) -> dict:
        """Build response when wiki is empty (targeted create or full create)."""
        origin_base = get_origin_default_branch(self._code_root)
        if origin_base and ref_exists(origin_base, cwd=self._code_root):
            return self._build_targeted_create_response(origin_base)
        return self._build_full_create_response()

    def _build_targeted_create_response(self, origin_base: str) -> dict:
        """Targeted ingest: only document files that differ from origin base."""
        base_branch_name = origin_base.split("/", 1)[1]

        all_files = _get_all_files(self._code_root)
        scoped = _resolve_scope(all_files, self._paths, self._topic)
        if self._scope_active and not scoped:
            return self._no_files_matched_scope_response(all_files)

        scope_pathspec = scoped if self._scope_active else None
        diff, diff_spec = _compute_diff(self._code_root, scope_pathspec=scope_pathspec)

        if not diff.strip():
            if self._scope_active:
                return self._no_changes_within_scope_response(diff_spec)
            return self._build_full_create_response()

        if len(diff) > _MAX_DIFF_CHARS:
            truncated = diff[:_MAX_DIFF_CHARS]
            diff = truncated + f"\n\n... (diff truncated at {_MAX_DIFF_CHARS} chars, {len(diff)} total)"

        base_wiki_path = self._wiki / self._repo_name / base_branch_name
        base_index_path = base_wiki_path / "index.md"
        base_index = _truncate_index(
            base_index_path.read_text(encoding="utf-8", errors="replace")
            if base_index_path.exists() else ""
        )

        scoped_prefix = ""
        if self._scope_active:
            scoped_prefix = f"SCOPED INGEST — document ONLY this area ({self._scope_desc()}). "

        response: dict[str, Any] = {
            "status": "action_required",
            "action": "create_wiki_pages_for_diff",
            "wiki_state": "targeted_create",
            "diff": diff,
            "diff_spec": diff_spec,
            "repo": self._repo_name,
            "wiki_path": str(self._repo_wiki_path),
            "index": base_index,
            "log_tail": get_log_tail(self._log_path, 5),
            "log_summary": _format_log_table(get_log_tail(self._log_path, 5)),
            "instruction": (
                f"{scoped_prefix}"
                f"TARGETED WIKI CREATION: The current branch has no wiki pages, but there is a base branch wiki to build from. "
                f"The wiki lives at {self._repo_wiki_path}. "
                f"Use the base branch index below as the existing wiki structure. "
                f"ONLY create pages for files that appear in the diff — do NOT recreate pages for domains that already exist in the base branch. "
                "REQUIRED STEPS: "
                "1. Read the diff to identify which files changed and which domains are affected. "
                "2. For each changed file: create a wiki page describing its purpose, structure, and changes. "
                f"3. Write {self._repo_wiki_path}/index.md with ## entries linking to each domain page. "
                f"4. Write {self._repo_wiki_path}/log.md: '## [YYYY-MM-DD] ingest | Targeted ingest for branch changes'. "
                "5. Call push_wiki() WITHOUT confirm=True (no arguments) — this returns a preview of changed files. "
                "   STOP. Present the preview to the user and wait for their approval. "
                "6. ONLY after the user explicitly approves, call push_wiki(confirm=True) to commit and push. "
                "   Do NOT skip step 6 or call confirm=True without user approval."
            ),
        }
        if self._scope_active:
            response["scope"] = self._scope_meta(len(scoped))
        return response

    def _build_full_create_response(self) -> dict:
        """Full ingest: document entire codebase from directory structure + key files."""
        all_files = _get_all_files(self._code_root)
        scoped = _resolve_scope(all_files, self._paths, self._topic)

        if self._scope_active and not scoped:
            return self._no_files_matched_scope_response(all_files)

        dir_summary = _directory_summary(scoped)
        key_files = _collect_key_files(self._code_root, scoped)

        scoped_prefix = ""
        if self._scope_active:
            scoped_prefix = f"SCOPED INGEST — document ONLY this area ({self._scope_desc()}). "

        response: dict[str, Any] = {
            "status": "action_required",
            "action": "create_wiki_pages",
            "wiki_state": "full_create",
            "diff_spec": "(full codebase)",
            "repo": self._repo_name,
            "wiki_path": str(self._repo_wiki_path),
            "directory_structure": dir_summary,
            "key_files": key_files,
            "index": "",
            "log_tail": get_log_tail(self._log_path, 5),
            "log_summary": _format_log_table(get_log_tail(self._log_path, 5)),
            "instruction": (
                f"{scoped_prefix}"
                f"CLEAN-SLATE WIKI CREATION: No wiki content exists and no diff is available. "
                f"Build the wiki from the directory_structure and key_files provided. "
                f"The wiki lives at {self._repo_wiki_path}. "
                f"DO NOT read additional source files — derive everything from directory_structure and key_files. "
                f"Produce a HIERARCHICAL wiki with real content pages, not just indexes. REQUIRED STEPS: "
                f"1. Identify the major domains in {self._repo_name} from directory_structure. "
                "2. For EACH domain, create a folder and write CONTENT PAGES with dense, specific detail "
                "derived from the provided material (do NOT write shallow summaries): "
                "'architecture.md' (overall design, tech stack, key components and how they connect, primary data flow, "
                "notable design decisions), 'modules/<name>.md' (one page per major module: responsibility, key "
                "functions/classes, config/env vars, interactions), and 'patterns.md' / 'gotchas.md' where the provided "
                "material supports them. Only create pages you can actually populate from directory_structure + key_files. "
                "3. For EACH domain, write '<domain>/index.md' as a PAGE CATALOG listing its pages: "
                "one line per page as '- [page](page.md) — one-line summary'. "
                f"4. Write {self._repo_wiki_path}/index.md as the top index: one entry per domain as "
                "'## [domain](domain/index.md) — one-line summary'. "
                f"5. Write {self._repo_wiki_path}/log.md: '## [YYYY-MM-DD] ingest | Initial full ingest'. "
                "6. Call push_wiki() WITHOUT confirm=True (no arguments) — this returns a preview of changed files. "
                "   STOP. Present the preview to the user and wait for their approval. "
                "7. ONLY after the user explicitly approves, call push_wiki(confirm=True) to commit and push. "
                "   Do NOT skip step 7 or call confirm=True without user approval."
            ),
        }
        if self._scope_active:
            response["scope"] = self._scope_meta(len(scoped))
        return response

    # ── incremental response helpers ──────────────────────────────

    def _build_incremental_response(self) -> dict:
        """Build response for incremental ingest (wiki exists, compute diff)."""
        scoped: list[str] | None = None
        if self._scope_active:
            all_files = _get_all_files(self._code_root)
            scoped = _resolve_scope(all_files, self._paths, self._topic)
            if not scoped:
                return self._no_files_matched_scope_response(all_files)

        diff, diff_spec = _compute_diff(
            self._code_root,
            scope_pathspec=scoped if self._scope_active else None,
        )

        if not diff.strip():
            if self._scope_active:
                return self._no_changes_within_scope_response(diff_spec)
            return {
                "status": "no changes",
                "diff": "",
                "diff_spec": diff_spec,
                "repo": self._repo_name,
                "wiki_path": str(self._repo_wiki_path),
                "index": _truncate_index(
                    self._index_path.read_text(encoding="utf-8", errors="replace")
                    if self._index_path.exists() else ""
                ),
                    "log_tail": get_log_tail(self._log_path, 5),
                    "log_summary": _format_log_table(get_log_tail(self._log_path, 5)),
                    "instruction": (
                        f"No code changes detected since last ingest (diff_spec: {diff_spec}). "
                    f"The wiki lives at {self._repo_wiki_path}. "
                    "If you need to update existing wiki content (e.g. fix stale documentation), "
                    "use fetch_wiki() to load pages, edit them directly, then call push_wiki()."
                ),
            }

        if len(diff) > _MAX_DIFF_CHARS:
            truncated = diff[:_MAX_DIFF_CHARS]
            diff = truncated + f"\n\n... (diff truncated at {_MAX_DIFF_CHARS} chars, {len(diff)} total)"

        scoped_prefix = ""
        if self._scope_active:
            scoped_prefix = f"SCOPED INGEST — document ONLY this area ({self._scope_desc()}). "

        response: dict[str, Any] = {
            "status": "action_required",
            "action": "update_wiki_pages_for_diff",
            "wiki_state": "incremental_update",
            "diff": diff,
            "diff_spec": diff_spec,
            "repo": self._repo_name,
            "wiki_path": str(self._repo_wiki_path),
            "index": _truncate_index(
                self._index_path.read_text(encoding="utf-8", errors="replace")
                if self._index_path.exists() else ""
            ),
                "log_tail": get_log_tail(self._log_path, 5),
                "log_summary": _format_log_table(get_log_tail(self._log_path, 5)),
                "instruction": (
                    f"{scoped_prefix}"
                    f"INCREMENTAL WIKI UPDATE: Code has changed and you must update wiki pages. "
                f"The wiki lives at {self._repo_wiki_path}. "
                f"REQUIRED STEPS: "
                f"1. Read the diff to identify which domains in {self._repo_name} are affected. "
                "2. Call fetch_wiki() on each affected domain's index.md. "
                "3. Call fetch_wiki() on each specific page that needs updating. "
                "4. Rewrite those pages with DEEP, SPECIFIC content: component interactions, data flows, "
                "   design decisions with rationale, non-obvious details, config/env vars, failure modes. "
                "   If a module page doesn't exist, create it. "
                "5. Update domain index.md if pages were added or removed. "
                "6. Append to log.md: '## [YYYY-MM-DD] ingest | <summary of what changed>'. "
                "7. Call push_wiki() WITHOUT confirm=True (no arguments) — this returns a preview of changed files. "
                "   STOP. Present the preview to the user and wait for their approval. "
                "8. ONLY after the user explicitly approves, call push_wiki(confirm=True) to commit and push. "
                "   Do NOT skip step 8 or call confirm=True without user approval."
            ),
        }
        if self._scope_active:
            response["scope"] = self._scope_meta(len(scoped or []))
        return response

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        """Run all stages in order, short-circuiting on the first error."""
        self._accumulated = {}

        stages = [
            self._resolve_and_validate,
            self._pre_ingest_sync,
            self._invalidate_cache,
            self._compute_result,
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
        """Return the final response assembled by _compute_result."""
        return dict(self._accumulated)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest(
    repo_name: str | None = None,
    branch: str | None = None,
    repo_path: str | None = None,
    paths: list[str] | None = None,
    topic: str | None = None,
) -> dict:
    return (
        IngestBuilder()
        .for_repo_branch(repo_name, branch, repo_path, paths=paths, topic=topic)
        .execute()
    )

