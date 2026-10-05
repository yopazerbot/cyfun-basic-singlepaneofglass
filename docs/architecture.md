# Architecture

## Components

```
browser ──TLS──> caddy (reverse proxy, HSTS, certificates)
                   │ http, internal network only
                   ▼
                 app (FastAPI + uvicorn, one worker)
                   ├── Jinja2 server-rendered HTML, htmx for two partial updates
                   ├── SQLite (WAL) in /data/cyfun.sqlite3
                   ├── /data/evidence/<random>.<ext>          uploaded evidence
                   ├── /data/connector_snapshots/<key>/*.json  raw connector output
                   └── APScheduler thread: connector runs every CONNECTOR_SYNC_HOURS
                          │ outbound HTTPS only
                          ▼
            Microsoft Graph · GitHub API · Railway GraphQL · Cloudflare API
                          ▲
   login.microsoftonline.com (OIDC discovery, authorization, token, JWKS)
```

Two containers, one named volume for data, one for Caddy state. No database server, no message queue, no front-end build.

## Why these choices

| Decision | Reason |
|---|---|
| Python, FastAPI, Jinja2 | One language for scoring, exports, connectors and web; server-rendered pages keep the browser side trivial and the CSP strict (no inline scripts, no third-party assets). |
| SQLite | Single organisation, a few users, small data. No credentials, no network listener, trivial backup (online backup API). |
| htmx, vendored | Live recalculation of the risk matrix without a JavaScript build. Pinned file, served from the application origin. |
| APScheduler in-process | Connector runs are small; a separate worker would add a container for nothing. |
| Entra ID OIDC, manual flow | About 150 lines with joserfc for token validation (RS256 only); no session middleware with signed cookies; server-side sessions can be revoked. |
| Local accounts with scrypt | Standard-library hashing, no extra dependency; lets the application run before single sign-on exists and gives an external auditor an account without a tenant. Off switch in one variable. |
| XML-level workbook filling | The CCB workbook is protected and carries a chart, defined names and conditional formatting; rewriting it with a spreadsheet library drops parts of it. Editing only the value cells keeps it byte-identical elsewhere. |
| Caddy | Automatic TLS (internal CA or ACME), few moving parts, sensible defaults. |

## Data model

Single organisation (row id 1). Tables: organisation, user, login_state, session, score, snapshot, risk_assessment, risk_item, asset, document, evidence, action, connector_run, check_result, activity. See `app/cyfun/models.py`. JSON columns hold lists of requirement identifiers and connector details.

Framework content is not in the database. `basic_2025.json`, `important_2025.json`, `essential_2025.json` and `risk_model.json` are generated from the CCB workbooks by `scripts/build_framework.py` and `scripts/build_risk_sectors.py`; `goals_2025.json` is extracted from the CCB booklets by `scripts/build_goals.py`; `guidance_basic_2025.json` holds guidance summaries written for this project. Each level file carries its thresholds, N/A rules and the cell layout of its workbook (columns, completion date cell, summary sheet). The organisation's target level selects which file is loaded. A new CCB tool version means regenerating the JSON and, if rows moved, nothing else: the export locates cells by sheet and row from the JSON and validates the uploaded workbook against it.

## Request flow

1. `SecurityMiddleware`: rate limit on `/auth/*`, request size limit and Origin check for state-changing methods, security headers on every response.
2. Router dependency `require_user` loads the session from the cookie hash; 401 becomes a redirect to `/auth/login?next=...`; an account that still has to change its password is redirected to `/auth/password`. `require_admin` enforces the role on every write.
3. Handlers use a SQLAlchemy session per request, write an `activity` row for each change, and redirect with a flash message (`?msg=` or `?err=` plus an HMAC signature `s`; unsigned messages are not shown).

## Connector flow

`scheduler.run_connector` creates a `connector_run`, calls `Connector.sync()`, upserts assets by `(source, external_id)`, retires assets no longer seen, stores `check_result` rows, writes the raw snapshot to disk, marks the run `ok` or `error`. A manual run is queued on the same scheduler so two runs of one connector never overlap. Connector credentials are read from settings; nothing is stored in the database.

## Exports

* `export_xlsx.fill_workbook`: validates the uploaded workbook (sheet names, requirement text per row), writes F/G/L cells and Introduction!T27, evaluates the workbook's own formulas (AVERAGE, SUM, COUNT, IF, OR, references, ranges, shared formulas) to refresh cached values, sets `fullCalcOnLoad`.
* `audit_pack.build_pack`: assembles the ZIP described in docs/audit-verification.md.

## Adding a connector

Create `app/cyfun/connectors/<name>.py` with a `Connector` subclass: `key`, `name`, `description`, `env_vars`, `configured()`, `sync()` returning `SyncResult(inventory, checks, raw)`. Keep evaluation logic in pure functions (see `evaluate_*` in the existing connectors) so it can be unit tested without network access. Register the class in `connectors/__init__.py`, add settings fields in `config.py` and document the credentials in docs/connectors.md.
