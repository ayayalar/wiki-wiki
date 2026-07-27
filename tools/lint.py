"""lint: return everything the agent needs to audit wiki health."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import repo_root as _default_repo_root
from utils.git import get_current_branch, get_repo_name, run_git, wiki_is_initialized
from utils.wiki import check_params, wiki_not_initialized_response


class LintBuilder:
    """Builds and executes a lint workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._wiki: Path = Path()
        self._code_root: Path = Path()
        self._repo_name: str = ""
        self._branch: str = ""
        self._repo_wiki_path: Path = Path()
        self._accumulated: dict[str, Any] = {}

    def for_repo_branch(
        self,
        repo_name: str | None,
        branch: str | None,
        repo_path: str | None,
    ) -> LintBuilder:
        self._code_root = Path(repo_path).resolve() if repo_path else _default_repo_root()
        self._wiki = self._code_root / "wiki"
        self._repo_name = repo_name or get_repo_name()
        self._branch = branch or get_current_branch()
        self._repo_wiki_path = self._wiki / self._repo_name / self._branch
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        if not wiki_is_initialized(self._wiki):
            return False, wiki_not_initialized_response(self._wiki)
        err = check_params(self._repo_name, self._branch)
        if err:
            return False, err
        return True, None

    # ── stage 2: gather wiki state ────────────────────────────────

    def _gather_wiki_state(self) -> tuple[bool, dict | None]:
        domain_indexes: dict[str, str] = {}
        wiki_file_paths: list[str] = []
        if self._repo_wiki_path.exists():
            for f in sorted(self._repo_wiki_path.rglob("index.md")):
                rel = str(f.relative_to(self._repo_wiki_path))
                domain_indexes[rel] = f.read_text(encoding="utf-8", errors="replace")
            wiki_file_paths = [
                str(f.relative_to(self._repo_wiki_path))
                for f in sorted(self._repo_wiki_path.rglob("*.md"))
            ]

        self._accumulated["domain_indexes"] = domain_indexes
        self._accumulated["wiki_file_paths"] = wiki_file_paths
        return True, None

    # ── stage 3: gather repo tree ─────────────────────────────────

    def _gather_repo_tree(self) -> tuple[bool, dict | None]:
        tree_result = run_git(["ls-files"], cwd=self._code_root, check=False)
        dirs: set[str] = set()
        for line in tree_result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.replace("\\", "/").split("/")
            for depth in range(1, min(len(parts), 3)):
                dirs.add("/".join(parts[:depth]))
        self._accumulated["repo_dirs"] = sorted(dirs)
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        self._accumulated = {}

        stages = [
            self._resolve_and_validate,
            self._gather_wiki_state,
            self._gather_repo_tree,
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
        response["repo"] = self._repo_name
        response["wiki_path"] = str(self._repo_wiki_path)
        response["summary"] = self._format_summary()
        response["instruction"] = (
            "Audit wiki health: "
            "1. Use fetch() to load pages that may be stale. "
            "2. Check stale references to removed code. "
            "3. Identify repo modules with no wiki page. "
            "4. Flag contradictions between pages. "
            "5. Check broken cross-links. "
            "6. Append findings to log.md: '## [YYYY-MM-DD] lint | <findings>'."
        )
        return response

    def _format_summary(self) -> str:
        wiki_files = self._accumulated.get("wiki_file_paths", [])
        indexes = self._accumulated.get("domain_indexes", {})
        repo_dirs = self._accumulated.get("repo_dirs", [])

        index_count = len(indexes)
        page_count = len(wiki_files) - index_count
        page_count = max(page_count, 0)

        parts: list[str] = []
        parts.append("| Metric | Value |")
        parts.append("|--------|-------|")
        parts.append(f"| Repo | {self._repo_name} |")
        parts.append(f"| Branch | {self._branch} |")
        parts.append(f"| Wiki Pages | {len(wiki_files)} |")
        parts.append(f"| Index Files | {index_count} |")
        parts.append(f"| Page Files | {page_count} |")
        parts.append(f"| Code Directories | {len(repo_dirs)} |")

        if wiki_files:
            parts.append("")
            parts.append("| # | Page | Type |")
            parts.append("|---|------|------|")
            for i, path in enumerate(wiki_files, 1):
                ptype = "index" if path.endswith("/index.md") or path == "index.md" else "page"
                parts.append(f"| {i} | {path} | {ptype} |")

        return "\n".join(parts)


# ── backward-compatible entry point ────────────────────────────────────


def lint(
    repo_name: str | None = None, branch: str | None = None, repo_path: str | None = None
) -> dict:
    return LintBuilder().for_repo_branch(repo_name, branch, repo_path).execute()
