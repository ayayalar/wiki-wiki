# wiki-mcp-server

An MCP server that maintains a persistent, LLM-managed wiki for any code repo.
The wiki lives as a nested git clone at `<repo>/wiki/` pointing at a shared
"all repos" wiki repo (AzDo or any git host), and each developer's machine
sparse-checks-out only their repo's folder.

Search across wiki pages is powered by [qmd](https://pypi.org/project/qmd/) —
a local hybrid (BM25 + vector + LLM rerank) search engine for markdown.

## Tools exposed

| Tool          | When to call                            |
|---------------|-----------------------------------------|
| `pull_wiki`   | Session start (auto-bootstraps the nested wiki clone AND scaffolds CLAUDE.md/index.md/log.md on first ever pull) |
| `init_wiki`   | Manual re-scaffold after deletion — usually unnecessary, pull_wiki handles it |
| `query_wiki`  | Find pages relevant to a topic          |
| `fetch_wiki`  | Load a specific page into context       |
| `ingest_wiki` | After a code commit you want absorbed   |
| `lint_wiki`   | Periodic audit of wiki health           |
| `push_wiki`   | After updating wiki pages               |
| `wiki_usage`  | Show estimated current-session input/output token usage |
| `reset_wiki`  | Recovery: dry-run by default, `force=True` to apply |
| `delete_wiki` | Delete a wiki branch from the remote (refuses main/master) |

`wiki_usage` returns estimated tokens (character-based heuristic), not exact model-billed tokens.

- `ingest_wiki` supports optional scoped ingest via `paths` (repo-relative glob/dir list) **or** `topic` (path keyword); they are mutually exclusive, and omitting both preserves full ingest behavior.

## Installation

```bash
git clone <this repo>
cd wiki-mcp-server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then register `server.py` with your MCP-capable agent (Claude Code, Cursor,
etc.). Configure the agent to launch the server with **cwd set to the code
repo root**. Example MCP config snippet:

```json
{
  "mcpServers": {
    "wiki": {
      "command": "/path/to/wiki-mcp-server/.venv/bin/python",
      "args": ["/path/to/wiki-mcp-server/server.py"],
      "cwd": "/path/to/your-code-repo",
      "env": {
        "WIKI_MCP_REMOTE_URL": "https://dev.azure.com/your-org/your-project/_git/my-org-wiki"
      }
    }
  }
}
```

The first `pull_wiki()` call will bootstrap `wiki/` as a nested clone (if not
already present), configure sparse-checkout to this repo's folder, and append
`wiki/` to the code repo's local `.git/info/exclude` so it stays untracked.
`pull_wiki()` never commits to the code repo and never creates or updates
`.gitmodules`.

### Private remote auth (PAT)

For private HTTPS remotes, configure a PAT in the MCP server env:

```json
{
  "mcpServers": {
    "wiki": {
      "env": {
        "WIKI_MCP_REMOTE_URL": "https://github.com/your-org/your-wiki.git",
        "WIKI_MCP_REMOTE_PAT": "<token>",
        "WIKI_MCP_REMOTE_USERNAME": "x-access-token"
      }
    }
  }
}
```

- `WIKI_MCP_REMOTE_PAT` enables non-interactive auth for HTTPS git operations.
- `WIKI_MCP_REMOTE_USERNAME` is optional. Default is `x-access-token` (recommended for GitHub and service identities).
- PAT auth is applied process-locally via git config env vars, so the token is not written to `.gitmodules` or git config files.
- PAT auth is ignored for `ssh://` and local `file://` remotes.

## Wiki repo URL configuration

The shared wiki repo URL is read from a single env var:
`WIKI_MCP_REMOTE_URL`. Set it in the MCP server's `env` block in your client
config (alongside `WIKI_MCP_REPO_ROOT`). On first `pull_wiki()`, each
developer clone bootstraps its own nested `wiki/` clone from that URL.
Nothing is committed to the code repo, and no `.gitmodules` entry is created.

Back-compat: repos already wired with a committed wiki submodule continue to
work unchanged. They are detected and supported as-is. Migration to the nested
clone model is opt-in via `reset_wiki(force=True)` followed by `pull_wiki()`.

If `WIKI_MCP_REMOTE_URL` isn't set and the wiki clone isn't already
present, `pull_wiki()` returns `status: needs_setup` with an
instruction to add the env var to the MCP client config.

## Local-only testing (no AzDo required)

The server works end-to-end against a local bare repo as the wiki remote:

```bash
# 1. Throwaway wiki remote
rm -rf /tmp/my-org-wiki.git /tmp/repo-a
git init --bare -b main /tmp/my-org-wiki.git

# 1a. Seed main with one commit
git clone /tmp/my-org-wiki.git /tmp/_boot
cd /tmp/_boot && git checkout -b main && git commit --allow-empty -m init && git push -u origin main
cd / && rm -rf /tmp/_boot

# 2. Throwaway code repo
git init -b main /tmp/repo-a
cd /tmp/repo-a
mkdir backend frontend infra
touch backend/.gitkeep frontend/.gitkeep infra/.gitkeep
git add . && git commit -m init

# 3. Launch your agent with cwd=/tmp/repo-a and WIKI_MCP_REMOTE_URL set.
#    pull_wiki() will auto-bootstrap the nested wiki clone silently.
export WIKI_MCP_REMOTE_URL=/tmp/my-org-wiki.git
```

`get_repo_name()` derives the repo name from `git remote get-url origin`. If
no remote is set, it falls back to the directory basename. Override at any
time with `WIKI_MCP_REPO_NAME`.

## Environment variables

| Variable                | Purpose                                                  |
|-------------------------|----------------------------------------------------------|
| `WIKI_MCP_REPO_ROOT`    | Skip repo-root discovery; use this path                  |
| `WIKI_MCP_REPO_NAME`    | Override the repo/folder/collection name                 |
| `WIKI_MCP_REMOTE_URL`   | Wiki repo URL for first-time auto-bootstrap of the nested wiki clone |
| `WIKI_MCP_REMOTE_PAT`   | Optional PAT for non-interactive HTTPS auth to the wiki remote |
| `WIKI_MCP_REMOTE_USERNAME` | Optional username for PAT auth (default: `x-access-token`) |
| `WIKI_MCP_VERIFY`       | `"1"` to run pre-commit hooks on the server's auto-commits (default: skipped) |
| `WIKI_MCP_GIT_TIMEOUT`  | Per-git-command timeout in seconds (default 300; `"0"` disables) |
| `XDG_CACHE_HOME`        | Cache root for qmd DB and the server log (default `~/.cache`) |

## Troubleshooting

### A tool feels slow or hung — what's it doing?

Every git command the server runs is logged to
`$XDG_CACHE_HOME/wiki-mcp/server.log` (`~/.cache/wiki-mcp/server.log`
on most systems) with timestamps and elapsed time. Tail it from a
separate terminal to watch live:

```bash
tail -F ~/.cache/wiki-mcp/server.log
```

Typical entry:

```
2026-05-18 09:14:02 START git clone --depth 1 ... wiki (cwd=/path/to/repo)
2026-05-18 09:14:47 done git clone --depth 1 ... wiki in 45.31s rc=0
```

If a single command sits in `START` for tens of seconds (or longer)
without a matching `done`, that command is your bottleneck.

### A git command timed out (`subprocess.TimeoutExpired` in the log)

The server applies a 300-second default timeout per git command so
hangs surface as errors instead of freezing the agent. If a real
operation legitimately needs more time (e.g. a huge first-time clone
over a slow link), raise it in your MCP client config:

```jsonc
"env": {
  "WIKI_MCP_GIT_TIMEOUT": "1800"   // 30 minutes; "0" disables entirely
}
```

If a *trivial* git command is timing out (`status`, `branch`,
`rev-parse`), the problem is environmental — usually Windows
Defender real-time protection scanning `git.exe` or the repo. See
the next entry.

### `pull_wiki` hangs for minutes on a large code repo

Two distinct causes, each with a different fix:

**A. Pre-commit hooks on the wiki repo's auto-commits.**
Auto-commits created by this server in the nested wiki clone can fire
pre-commit hooks (lint, typecheck, tests). On large repos, that can
take many minutes.

Opt out (only for our auto-commits — your own commits keep running hooks):

```jsonc
// MCP client config, in the wiki server's env block
// Hooks are skipped by default. Set WIKI_MCP_VERIFY=1 if you need them.
"env": {
  "WIKI_MCP_REMOTE_URL": "..."
}
```

Affected commits include `chore: scaffold wiki for <repo>` (first
scaffold only) and `wiki: update <repo> for <branch>` (every push)
inside the nested wiki clone. Your own development commits in the code
repo still run hooks as normal.

**B. Windows Defender (or other EDR) scanning git on first invocation.**
On Windows machines with aggressive AV, the very first `git` subprocess
of a session can take **many minutes** while Defender scans `git.exe`
and the repo. The server already avoids spawning git for trivial
metadata reads (branch lookup, origin URL, and wiki bootstrap checks use
direct file IO), so the first actual `git` subprocess is usually
`git ls-remote` against your wiki remote
— a real network operation. If even that one is slow, exclude git
and your repo from Defender:

```powershell
Add-MpPreference -ExclusionProcess "git.exe"
Add-MpPreference -ExclusionPath "D:\src\rover\tokenization-service"
```

Also check: is your code repo on a mapped network drive, OneDrive
folder, or VirtualBox/WSL share? All three trash git performance on
Windows. Moving the repo to a plain local SSD path typically yields a
10-100x speedup.

### `git status` shows `wiki/` in the code repo

In the nested-clone model, `pull_wiki()` appends `wiki/` to the code
repo's local `.git/info/exclude`, so `wiki/` should stay untracked in
that clone.

If `wiki/` still appears, check whether it was previously staged or
tracked in your local branch, and verify `.git/info/exclude` contains
`wiki/`.

Back-compat note: if your repo is a legacy submodule-based setup, gitlink
pointer churn can still appear by design.

### `pull_wiki` returns `bootstrap_failed`

The URL in `WIKI_MCP_REMOTE_URL` does not point at a reachable bare git
repo. Verify the URL works manually:

```bash
git ls-remote <your-wiki-url>
```

For local testing, the bare repo must actually exist:

```bash
git init --bare -b main /path/to/wiki-remote.git
git clone /path/to/wiki-remote.git /tmp/_seed
cd /tmp/_seed && git commit --allow-empty -m init && git push -u origin main
cd / && rm -rf /tmp/_seed
```

### Tools return `wiki_not_initialized`

`wiki/` exists but isn't an initialized nested clone (no `wiki/.git`). The
guard fires here to prevent silent damage — without it, `git -C wiki
<cmd>` walks up and operates on the parent **code** repo, which would
commit wiki content as ordinary files in the wrong place. Call
`pull_wiki()` first. If it returns `bootstrap_failed`, fix
`WIKI_MCP_REMOTE_URL` per above.

### `pull_wiki` returns `wiki_dir_blocked` or HEAD has a polluting wiki commit

Use `reset_wiki()` — it handles both states automatically.

1. **Dry-run first:** call `reset_wiki()` (no argument). It returns a
   `would_do` plan listing detected actions:
   - `move_orphan_wiki_dir`: moves `wiki/` to `.wiki.backup-<timestamp>/`.
   - `undo_polluting_commit`: `git reset HEAD~1` to drop a HEAD commit
     that *only* contains `wiki/<repo>/*` files.
2. **Apply:** call `reset_wiki(force=True)`. Then call `pull_wiki()` to
   bootstrap cleanly.

Hard safety stops — reset will refuse to:
- Touch a healthy wiki checkout (`wiki/.git` exists).
- Undo a HEAD commit that mixes wiki files with other changes.
- Undo a commit that's already reachable from `origin/<branch>` (would
  require a force-push to recover).

If reset refuses your case, the situation needs a manual decision —
fix it with regular git commands.

After `reset_wiki` succeeds, your previous wiki content is preserved
in the `.wiki.backup-<timestamp>/` directory at the repo root. Once
`pull_wiki` and `init_wiki` rebuild the proper wiki structure,
copy content back in:

```bash
cp -r <code-repo>/.wiki.backup-<timestamp>/<repo>/* <code-repo>/wiki/<repo>/
# then call push_wiki() to commit to the real wiki remote
```

## qmd integration

Each code repo's wiki is a separate qmd collection, stored in a per-collection
SQLite DB at `$XDG_CACHE_HOME/wiki-mcp/<repo>/db.sqlite`. `push_wiki()` drops
and recreates this DB to keep search results aligned with the wiki tree.
First indexing downloads a 600M embedding model (~30 sec) and caches it under
the HuggingFace cache directory.

## Wiki repo bootstrap

In your shared wiki repo (e.g. `my-org-wiki`), each code repo gets its own
top-level folder:

```
my-org-wiki/
  repo-a/
    CLAUDE.md          # written by init_wiki()
    index.md
    log.md
    backend/...
  repo-b/
    ...
```

`init_wiki()` writes `CLAUDE.md` (the wiki schema), an empty `index.md`, and
an empty `log.md`. The agent fills in the domain content.

## Branch strategy

Wiki branches mirror code branches 1:1. Each branch contains only the changes
scoped to the current repo's folder.

```
repo-a/main          ↔  my-org-wiki/main          (sparse: repo-a/ only)
repo-a/feature/auth  ↔  my-org-wiki/feature/auth  (sparse: repo-a/ only)
```

## AzDo automation (optional)

Two example pipelines are in [`examples/`](examples/):

- `wiki-repo-azure-pipelines.yml` — runs in the **wiki** repo. On any push to
  a non-`main` branch, opens a draft PR against `main` if none exists. This is
  what creates the wiki PR that the code repo's pipeline then merges.

- `code-repo-azure-pipelines.yml` — runs in each **code** repo. When a PR to
  `main` merges, finds the matching wiki PR (same source branch) and merges it
  via squash. Warns (does not fail) if no wiki PR is found.

The MCP server itself does not create PRs — that responsibility is delegated
to the wiki-repo pipeline.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                                # full suite (~6 min)
```

All tests are integration tests against real git in a temp directory.

## Project layout

```
wiki-mcp-server/
  server.py             # FastMCP entry point
  config.py             # repo-root discovery, WIKI_PATH
  tools/                # one file per tool
  utils/                # git, wiki, qmd helpers
  templates/            # CLAUDE.md and stub index/log
  tests/                # one integration test file per tool + e2e
  examples/             # AzDo pipeline templates
```
