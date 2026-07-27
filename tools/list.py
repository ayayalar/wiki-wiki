"""list: enumerate remote wiki repositories from the shared wiki remote."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import wiki_path as _default_wiki_path
from utils.git import run_git, wiki_is_initialized
from utils.wiki import read_remote_url, wiki_not_initialized_response


class ListBuilder:
    """Builds and executes a list workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._wiki: Path = Path()
        self._pattern: str | None = None
        self._repo_names: list[str] = []
        self._remote_url: str | None = None
        self._accumulated: dict[str, Any] = {}

    def for_params(
        self,
        pattern: str | None = None,
        repo_path: str | None = None,
    ) -> ListBuilder:
        self._wiki = (Path(repo_path).resolve() / "wiki") if repo_path else _default_wiki_path()
        self._pattern = pattern
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        if not wiki_is_initialized(self._wiki):
            return False, wiki_not_initialized_response(self._wiki)
        return True, None

    # ── stage 2: read remote URL ──────────────────────────────────

    def _read_remote_url(self) -> tuple[bool, dict | None]:
        self._remote_url = read_remote_url()
        if not self._remote_url:
            return False, {
                "status": "error",
                "message": (
                    "WIKI_MCP_REMOTE_URL is not configured. Set it in your MCP "
                    "client config to the shared wiki repo URL."
                ),
            }
        return True, None

    # ── stage 3: fetch remote ─────────────────────────────────────

    def _fetch_remote(self) -> tuple[bool, dict | None]:
        remote = self._remote_url
        assert remote is not None
        fetch_result = run_git(
            [
                "-c",
                "protocol.file.allow=always",
                "fetch",
                remote,
                "+refs/heads/wiki:refs/remotes/origin/wiki",
                "--depth",
                "1",
            ],
            cwd=self._wiki,
            check=False,
        )
        if fetch_result.returncode != 0:
            return False, {
                "status": "error",
                "message": f"git fetch failed: {(fetch_result.stderr or '').strip()}",
            }
        return True, None

    # ── stage 4: list repo names ──────────────────────────────────

    def _list_repo_names(self) -> tuple[bool, dict | None]:
        ls_result = run_git(
            ["ls-tree", "--name-only", "origin/wiki"],
            cwd=self._wiki,
            check=False,
        )
        if ls_result.returncode != 0:
            return False, {
                "status": "error",
                "message": f"git ls-tree failed: {(ls_result.stderr or '').strip()}",
            }

        self._repo_names = [line.strip() for line in ls_result.stdout.splitlines() if line.strip()]

        if self._pattern:
            pattern_lower = self._pattern.lower()
            self._repo_names = [r for r in self._repo_names if pattern_lower in r.lower()]

        return True, None

    # ── stage 5: list branches per repo ───────────────────────────

    def _list_branches(self) -> tuple[bool, dict | None]:
        repos: list[dict] = []
        for repo_name in self._repo_names:
            branches_result = run_git(
                ["ls-tree", "--name-only", f"origin/wiki:{repo_name}"],
                cwd=self._wiki,
                check=False,
            )
            branches: list[str] = []
            if branches_result.returncode == 0:
                branches = [
                    line.strip() for line in branches_result.stdout.splitlines() if line.strip()
                ]
            repos.append({"name": repo_name, "branches": branches})

        self._accumulated["repos"] = repos
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        self._accumulated = {}

        stages = [
            self._resolve_and_validate,
            self._read_remote_url,
            self._fetch_remote,
            self._list_repo_names,
            self._list_branches,
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
        repos = self._accumulated.get("repos", [])
        response: dict[str, Any] = dict(self._accumulated)
        response["pattern"] = self._pattern
        response["count"] = len(repos)
        response["summary"] = self._format_summary(repos)
        return response

    def _format_summary(self, repos: list[dict]) -> str:
        def _branches_display(branches: list[str]) -> str:
            if not branches:
                return "(none)"
            shown = branches[:5]
            result = ", ".join(shown)
            if len(branches) > 5:
                result += f" (and {len(branches) - 5} more)"
            return result

        table = "| # | Repo | Branches |\n|---|------|----------|\n"
        if not repos:
            table += "| (none) | |\n"
        else:
            for i, repo in enumerate(repos, 1):
                branches = _branches_display(repo.get("branches", []))
                table += f"| {i} | {repo['name']} | {branches} |\n"
        return table


# ── backward-compatible entry point ────────────────────────────────────


def list_remote_wikis(pattern: str | None = None, repo_path: str | None = None) -> dict:
    return ListBuilder().for_params(pattern, repo_path).execute()
