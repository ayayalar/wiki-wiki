"""fetch: load a specific wiki page scoped to this repo's wiki folder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import wiki_path as _default_wiki_path
from utils.git import get_current_branch, get_repo_name, wiki_is_initialized
from utils.wiki import check_params, wiki_not_initialized_response


class FetchBuilder:
    """Builds and executes a fetch workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._wiki: Path = Path()
        self._repo_name: str = ""
        self._branch: str = ""
        self._path: str = ""
        self._repo_wiki_path: Path = Path()
        self._accumulated: dict[str, Any] = {}

    def for_repo_branch(
        self,
        path: str,
        repo_name: str | None,
        branch: str | None,
        repo_path: str | None,
    ) -> FetchBuilder:
        self._wiki = (Path(repo_path).resolve() / "wiki") if repo_path else _default_wiki_path()
        self._repo_name = repo_name or get_repo_name()
        self._branch = branch or get_current_branch()
        self._path = path
        self._repo_wiki_path = (self._wiki / self._repo_name / self._branch).resolve()
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        if not wiki_is_initialized(self._wiki):
            return False, wiki_not_initialized_response(self._wiki)
        err = check_params(self._repo_name, self._branch)
        if err:
            return False, err
        return True, None

    # ── stage 2: validate path ────────────────────────────────────

    def _validate_path(self) -> tuple[bool, dict | None]:
        p = Path(self._path)
        if ".." in p.parts or p.is_absolute():
            return False, {"status": "invalid_params", "error": "path contains '..' or is absolute — not allowed."}

        requested = (self._repo_wiki_path / p).resolve()
        try:
            requested.relative_to(self._repo_wiki_path)
        except ValueError:
            return False, {"error": f"Path escapes wiki folder: {self._path}"}

        if requested.suffix != ".md":
            return False, {"error": "Only markdown files can be fetched"}

        if not requested.is_file():
            return False, {"error": f"Page not found: {self._repo_name}/{self._branch}/{self._path}"}

        try:
            self._accumulated["content"] = requested.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, {"error": f"Could not read page {self._path}: {exc}"}
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        self._accumulated = {}

        stages = [
            self._resolve_and_validate,
            self._validate_path,
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
        response["path"] = self._path
        response["repo"] = self._repo_name
        response["branch"] = self._branch
        return response


# ── backward-compatible entry point ────────────────────────────────────


def fetch(path: str, repo_name: str | None = None, branch: str | None = None, repo_path: str | None = None) -> dict:
    return (
        FetchBuilder()
        .for_repo_branch(path, repo_name, branch, repo_path)
        .execute()
    )
