"""init: scaffold a fresh wiki under <wiki>/<repo_name>/<branch>/ for cold-start repos."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import repo_root as _default_repo_root
from utils.git import get_current_branch, get_repo_name, wiki_is_initialized
from utils.wiki import check_params, scaffold, wiki_not_initialized_response, detect_domains_from_repo
from utils.git import _no_verify_flag, run_git


def scaffold_repo_wiki_if_empty(
    repo_wiki_path: Path, repo_name: str, wiki_root: Path | None = None,
    code_root: Path | None = None,
) -> dict | None:
    existing = list(repo_wiki_path.rglob("*.md")) if repo_wiki_path.exists() else []
    if existing:
        return None

    _code_root = code_root if code_root is not None else _default_repo_root()
    domains = detect_domains_from_repo(_code_root)
    created = scaffold(repo_wiki_path, repo_name)

    _wiki_root = wiki_root if wiki_root is not None else repo_wiki_path.parent
    git_add_path = repo_wiki_path.relative_to(_wiki_root).as_posix()
    run_git(["add", git_add_path], cwd=_wiki_root, check=False)
    run_git(
        ["commit", *_no_verify_flag(), "-m", f"chore: scaffold wiki for {git_add_path}"],
        cwd=_wiki_root,
        check=False,
    )

    return {
        "scaffolded": created,
        "domains": domains,
    }


class InitBuilder:
    """Builds and executes an init workflow in discrete stages.

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
    ) -> InitBuilder:
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

    # ── stage 2: check existing ───────────────────────────────────

    def _check_existing(self) -> tuple[bool, dict | None]:
        scaffold_result = scaffold_repo_wiki_if_empty(
            self._repo_wiki_path, self._repo_name,
            wiki_root=self._wiki, code_root=self._code_root,
        )
        if scaffold_result is None:
            existing = list(self._repo_wiki_path.rglob("*.md"))
            return False, {
                "error": "Wiki already initialized. Use ingest() instead.",
                "repo": self._repo_name,
                "branch": self._branch,
                "wiki_path": str(self._repo_wiki_path),
                "existing_files": [str(p.relative_to(self._repo_wiki_path)) for p in existing],
            }
        self._accumulated.update(scaffold_result)
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        self._accumulated = {}

        stages = [
            self._resolve_and_validate,
            self._check_existing,
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
        response["mode"] = "bootstrap"
        response["structure"] = "hierarchical"
        response["repo"] = self._repo_name
        response["wiki_path"] = str(self._repo_wiki_path)
        response["instruction"] = (
            f"Build a hierarchical wiki under {self._repo_wiki_path}/ following CLAUDE.md. "
            f"{Path('CLAUDE.md').name}, index.md, and log.md are already created. "
            "For each domain, create domain/index.md plus all relevant page types "
            "(architecture.md, modules/*.md, patterns.md, gotchas.md). "
            "Use Read/Glob/lint_wiki to explore the repo structure before writing. "
            "Pages must be DEEP and SPECIFIC to this codebase — see the Content quality "
            "standard in CLAUDE.md. Include: component interactions, data flows, design "
            "decisions with rationale, public interfaces, config/env vars, failure modes, "
            "non-obvious implementation details. No generic filler. "
            "When done, call push()."
        )
        return response


# ── backward-compatible entry point ────────────────────────────────────


def init(repo_name: str | None = None, branch: str | None = None, repo_path: str | None = None) -> dict:
    return (
        InitBuilder()
        .for_repo_branch(repo_name, branch, repo_path)
        .execute()
    )
