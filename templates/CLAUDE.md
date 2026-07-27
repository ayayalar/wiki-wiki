# Wiki Schema

## Purpose
Persistent pre-reasoned knowledge base about this codebase.
Goal: reduce agent token usage by eliminating rediscovery from scratch.
Consumers: developers via agent, PO/PM/Architect via conversational app.

## Structure
Always hierarchical. Every domain has its own `index.md`.
Never use a flat structure regardless of repo size.

## `index.md` (top level)
Domain summaries only. One entry per domain:
`## [Domain](domain/index.md) — one-line summary`

## `domain/index.md`
Page catalog for that domain:
`- [page](page.md) — one-line summary`

## `log.md` format
Append-only. Each entry:
`## [YYYY-MM-DD] <operation> | <summary>`
Operations: init, ingest, lint, query

## Page types
- `architecture.md` — overall design, tech stack, key decisions
- `modules/*.md`    — one page per major module
- `patterns.md`     — conventions used throughout
- `gotchas.md`      — non-obvious things, traps, known issues

## Content quality standard
Pages must be **dense and specific** — not shallow summaries. Each page should
capture reasoning that would otherwise require re-reading source files.

**architecture.md must include:**
- Component diagram (ASCII or bullet-based) showing how pieces connect
- Data flow for the primary use case (request → processing → response)
- Key design decisions and *why* they were made (not just what)
- External dependencies and integration points
- Failure modes and how they're handled

**modules/*.md must include:**
- What the module owns (responsibility boundary)
- Public interface / API surface (key functions, classes, endpoints)
- Internal structure (major sub-components and their roles)
- Non-obvious implementation details (algorithms, state machines, caching)
- How it's configured and what env vars / config keys it reads
- How it interacts with other modules (dependencies, events, contracts)

**patterns.md must include:**
- Concrete named patterns actually used in this codebase (not generic advice)
- Brief code sketch or example showing the pattern in use
- Where and why the pattern is applied

**gotchas.md must include:**
- Specific traps, surprising behavior, or sharp edges that caused or could cause bugs
- Non-obvious constraints (ordering requirements, race conditions, size limits)
- Known workarounds for platform or library quirks

**Depth rule:** If a page could apply to any project without modification,
it is too generic. Every statement must be specific to this codebase.

## Navigation rule
Always read top `index.md` first.
Then domain `index.md`.
Then specific pages via `fetch_wiki()`.
Never load all pages at once.

## Ingest workflow
1. Read top `index.md` to understand domain structure
2. Identify which domain(s) the diff touches
3. Read affected domain `index.md` via `fetch_wiki()`
4. Fetch and update specific pages
5. Update domain `index.md` if pages added or removed
6. Update top `index.md` if domains added or removed
7. Append to `log.md`: `## [date] ingest | <summary>`

## Query workflow
1. Read top `index.md`
2. Use `query_wiki()` search results to identify relevant pages
3. Fetch specific pages only via `fetch_wiki()`
4. Never re-read raw source files if the wiki covers it

## Lint workflow
1. Read all domain indexes
2. Fetch pages that may be stale
3. Check against repo file tree
4. Flag: stale references, orphan pages, missing module pages,
          contradictions, broken cross-links
5. Append to `log.md`: `## [date] lint | <findings>`

## Confirmation rule
When a tool returns `"status": "pending_confirmation"`, **STOP** and show the
preview to the user. Wait for explicit approval before re-calling with
`confirm=True`. Never auto-confirm destructive actions (push, delete, reset).
