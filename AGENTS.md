# AGENTS.md

Instructions for AI agents working in this repo. Read before editing.

## What this is
Self-hosted web app that manages the *arr media stack (Sonarr, Radarr, Lidarr, Readarr, downloaders, Plex/Jellyfin, Prowlarr, Cleanuparr) with natural-language commands and an agentic chat loop (pydantic-ai) that can diagnose failed downloads end to end.

## Where things live
- HTTP clients, one module per service, all shaped by `src/arrmate/clients/base.py`; the *arr family shares `src/arrmate/clients/base_arr.py`
- Service registry (metadata, capabilities, probe wiring): `src/arrmate/clients/discovery.py`
- Agent package (model factory, deps, tools, playbooks, chat SSE, thread store): `src/arrmate/agent/`
- Natural-language command pipeline: `src/arrmate/core/` (parser, intent engine, executor)
- Web UI (HTMX + Alpine): `src/arrmate/interfaces/web/`, routes split by domain under `routes/` with shared state in `routes/_shared.py`
- Auth (SQLite user db, sessions, Plex SSO, rate limits): `src/arrmate/auth/`
- The `Makefile` is the single canonical interface for all checks; CI and pre-commit both call it.

## Stack
- Python 3.11+ (dev on 3.14), managed with `uv`
- FastAPI + Jinja2 + HTMX; pydantic-ai for the agent loop; httpx everywhere outbound

## Commands (Makefile is SSoT)
- `make install` uv sync plus install pre-commit hooks
- `make quality` the full gate: format-check, lint, typecheck, imports, dead-code, unused-deps, security, test, build
- `make run` start the web app on :8000
- `make docker-build` / `make docker-run` local container build and run

## Before considering work complete
1. Run `make quality`.
2. Fix all failures.
3. Do not weaken or remove quality checks to make them pass.
4. Do not add entries to the mypy technical-debt ledger in `pyproject.toml`; shrink it.

## Code style
- Modern static type hints for Python: `dict` and `list` (not `Dict`/`List`), `X | None` (not `Union`/`Optional`). Type hints required on function/method signatures and arguments, not local variables.
- Imports only at the top of a module, never inside functions. Absolute imports rooted at `arrmate`, no parent-relative imports.
- No bare `except`. Catch only the exceptions actually expected (`httpx.HTTPError`, `sqlite3.Error`, `ValueError`, ...).
- JSON responses from services are dynamically shaped; the client layer documents the expected shape in signatures and this is the only place `Any` crosses a boundary.
- Comments only to explain the non-obvious why, never the what. Default to zero.
- Descriptive, explicit variable names. Keep solutions short and simple.
- snake_case everywhere; no magic strings repeated 3+ times (promote to constants or StrEnum).

## Conventions
- Web routes live in `src/arrmate/interfaces/web/routes/<domain>.py`; shared helpers and singletons (templates, parser/engine/executor, client factories) live in `routes/_shared.py`. URLs are stable; internal moves must not change paths.
- Role policy: `user` reads and submits requests, `power_user`/`admin` execute destructive actions. The blocked-action set is `USER_BLOCKED_ACTIONS` in `src/arrmate/core/models.py`; add to it, never redefine it locally.
- Agent tools enforce `ctx.deps.require_write(...)` for mutations and wrap results with the `_safe` boundary so errors reach the model as text.
- Clients are short-lived: construct, act, `close()` in a `finally`/context manager. Never cache clients across requests.
- Secrets (API keys, passwords) are masked in UI responses and never logged.

## Hygiene
Never commit `.env`, `*.db`, `data/`, or `services.json` contents. `.gitignore` covers them. Webhooks and outbound HTTP only go to user-configured URLs.
