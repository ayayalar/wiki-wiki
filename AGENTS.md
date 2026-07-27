# wiki-mcp-server — AGENTS.md

## What this is

A Python 3.11+ MCP server (FastMCP) that maintains an LLM-managed wiki per code repo. The wiki lives as a nested git clone at `<repo>/wiki/` pointing to a shared bare git repo. Each repo+branch gets its own folder under sparse-checkout.

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -e ".[dev]"
```

## Commands

| Task | Command |
|------|---------|
| Lint | `ruff check .` |
| Format | `ruff format .` |
| All tests | `.venv/Scripts/python -m pytest` (~6 min) |
| Single test | `.venv/Scripts/python -m pytest tests/test_resolve.py::test_name -v` |

`ruff line-length = 100` (configured in `pyproject.toml`).

## Architecture

- **`server.py`** — FastMCP entry point. One `@mcp.tool()` per wiki operation.
- **`tools/`** — One file per tool (`pull.py`, `push.py`, `ingest.py`, `resolve.py`, etc.).
- **`utils/git.py`** — All git subprocess wrappers. Serializes git commands per working directory with `threading.Lock` to prevent `index.lock` contention. Strips `VSCODE_GIT_*`, `GIT_ASKPASS`, `SSH_ASKPASS`, `GIT_CONFIG_*` from env to avoid blocking in headless contexts. Passes `-c protocol.file.allow=always` for local file:// URLs.
- **`utils/wiki_index.py`** — In-memory inverted index search cache (LRU, max 32 entries), invalidated on push/pull.
- **`config.py`** — Repo-root discovery (walks up for `.gitmodules` or `.git`; in the nested-clone model this is typically the code repo's own `.git`), wiki path resolution.
- **`templates/`** — Scaffold templates: `CLAUDE.md`, `index.md`, `log.md`.

## Key design facts

- **Wiki is a nested git clone** at `<repo>/wiki/` (not a submodule). All git operations run inside that nested clone, never the parent code repo.
- **Sparse-checkout** limits disk to `{repo_name}/{branch}/` per session. Branch switches auto-refresh sparse-checkout in `_resolve_context`.
- **Shared remote branch** — all repos share a single `wiki` branch in the remote. Content is namespaced by `{repo_name}/{code_branch}/`.
- **`WIKI_MCP_REMOTE_URL`** env var is required for first-time bootstrap of each developer clone. Bootstrap does not commit to the code repo and does not create/update `.gitmodules`; `wiki/` is excluded locally via `.git/info/exclude`.
- **Back-compat** — legacy submodule-based wiki setups are still detected and supported; migration is opt-in via `reset_wiki(force=True)` then `pull_wiki`.
- **Pre-commit hooks are skipped by default** on auto-commits. Set `WIKI_MCP_VERIFY=1` to enable them.
- **`WIKI_MCP_GIT_TIMEOUT`** controls per-git-command timeout (default 60s based on code). Set to `"0"` to disable. The README says 300s but the code defaults to 60s.
- **`protocol.file.allow=always`** is passed to all git commands that interact with the remote, enabling local `file://` URLs.

## Tool workflow

Call order for a typical wiki session:
1. **`pull_wiki`** — session start. Auto-bootstraps the nested wiki clone, resolves merge conflicts, fetches/merges remote, scaffolds CLAUDE.md/index.md/log.md on first pull.
2. **`ingest_wiki`** — after code changes. Returns code diff + wiki index. Pre-syncs wiki if behind/diverged.
3. **`fetch_wiki` / `query_wiki`** — read or search wiki pages.
4. **`push_wiki`** — after updating pages. **Two-step flow enforced by ingest instructions**:
   - Step A: `push_wiki()` (no arguments) — returns preview of changed files. **STOP** and show the preview to the user. Wait for approval.
   - Step B: `push_wiki(confirm=True)` — ONLY after user approves, commit and push.
   Never call `confirm=True` without showing the preview first. Auto-syncs with remote before committing (prevents TOCTOU).
5. **`resolve_wiki_issue`** — diagnose (no action) returns issues with resolution options. Execute with `action="issue:resolution"`.

## Testing

All tests are integration tests against real git in temp directories. Conftest provides:
- `bare_wiki` — fresh bare git repo with orphan `wiki` branch
- `code_repo` — fresh code repo with `src/app.py`
- Fixtures set `WIKI_MCP_REMOTE_URL` and clear all caches (`config._wiki_path_cache`, `config._repo_root_cache`, `git_mod._repo_name_cache`, `WikiIndex.invalidate_all`)

Tests are slow (~6 min) because each test rebuilds git repos.

## Windows quirks

- First `git.exe` call per session hangs while Defender scans. `utils/git.py` pre-warms with background `git --version` thread at import time.
- All subprocess calls use `CREATE_NO_WINDOW` and `CREATE_NEW_PROCESS_GROUP` flags.
- `ingest.py` and `wiki_index.py` use temp files for `git cat-file --batch` stdin/stdout on Windows (pipe inheritance causes hangs).
- `run_git` uses temp files for stdout/stderr on Windows instead of `subprocess.PIPE` (grandchild processes inherit pipe handles and `communicate()` never gets EOF).
- Paths are normalized from MSYS/Git Bash format (`/d/foo` → `D:/foo`) in `_normalize_path` in `server.py`.

## CVE-2025-48384

`utils/git.py` checks git version against CVE-2025-48384 (config CRLF parsing flaw in submodules). Returns advisory warning if unpatched — does not block operation. Patched versions: v2.43.7, v2.44.4, v2.45.4, v2.46.4, v2.47.3, v2.48.2, v2.49.1, v2.50.1+.
