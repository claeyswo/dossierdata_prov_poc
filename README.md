# Dossier API — PROV-based Activity-Driven Dossier Management

A W3C PROV-based, activity-driven dossier management API built with FastAPI. Every state change is an activity, every piece of data is a versioned entity, and the full provenance graph is queryable and visualizable.

## Quick Start

### Prerequisites

PostgreSQL 16 or newer running on `127.0.0.1:5432`. The app uses native `UUID` and `JSONB` columns, so SQLite is not supported. To set up a local Postgres for development:

```bash
# Install (Debian/Ubuntu)
apt install postgresql postgresql-contrib
pg_ctlcluster 16 main start

# Create the dossier role and database (as the postgres OS user)
su -c "psql -c \"CREATE USER dossier WITH PASSWORD 'dossier';\"" postgres
su -c "psql -c \"CREATE DATABASE dossiers OWNER dossier;\"" postgres
su -c "psql -d dossiers -c \"GRANT ALL ON SCHEMA public TO dossier;\"" postgres

# For dev convenience, set host auth on 127.0.0.1 to trust so the
# app doesn't need a password in the connection string. Edit
# /etc/postgresql/16/main/pg_hba.conf and change the `host all all
# 127.0.0.1/32` and `::1/128` lines to use `trust` instead of
# `scram-sha-256`, then `pg_ctlcluster 16 main reload`. In
# production, use a real password or client certificates instead.
```

### Install and run

```bash
# Install all five projects in editable mode (one-time setup)
pip install -e dossier_common_repo/ -e file_service_repo/ -e dossier_engine_repo/ \
            -e dossier_toelatingen_repo/ -e dossier_app_repo/

# Run the dossier API (launch cwd does not matter — the engine's
# only filesystem path, file_service.storage_root, resolves against
# the config file's own directory).
# On first launch, the startup hook runs Alembic migrations to
# create the schema. On subsequent launches it applies any pending
# migrations automatically.
uvicorn dossier_app.main:app --reload --port 8000

# Run the file service (separate process, separate port)
uvicorn file_service.app:app --reload --port 8001

# Run the test flows
bash test_requests.sh

# Process scheduled tasks (worker reads the config installed with dossier_app)
python -m dossier_engine.worker --once

# Open Swagger docs
open http://localhost:8000/docs
```

## Database & Schema Management

The database schema is managed by **Alembic** migrations. The migration
files live under `dossier_engine_repo/alembic/versions/`.

### How it works

On API startup, the app runs `alembic upgrade head` via subprocess. This:
- Creates all tables from scratch on a fresh database
- Applies any pending migrations on an existing database
- Is idempotent — running it twice does nothing

The worker does NOT run migrations. It only connects to the database
and expects the schema to already exist. Always start the API before
the worker (or run `alembic upgrade head` manually first).

### Migrations are append-only

**Never edit a migration file that has ever been applied to a
deployed environment.** Alembic tracks which revisions have run
by hash in the `alembic_version` table. It does not compare
content. If you mutate the body of an existing migration, the
mutated form runs on fresh installs while older deployments —
which already recorded the pre-mutation revision as "done" — will
never re-run it. The result is two deployments with the same
revision marker but divergent schemas, and the drift is invisible
until something queries a column that exists in one install but
not the other.

If the schema needs to change, add a *new* migration file with a
new revision ID. The `scripts/check_migrations_append_only.py`
pre-commit guard enforces this: it rejects any diff that modifies
or deletes an existing file under `alembic/versions/`.

Before the first production deploy, this rule is relaxed — you
can consolidate or refactor migrations freely because no
`alembic_version` row exists yet in any deployment. After the
first deploy, it's frozen. Treat that transition as a commitment
to the current migration history.

### Manual migration commands

```bash
cd dossier_engine_repo/

# Apply all pending migrations
alembic upgrade head

# Check current version
alembic current

# Show migration history
alembic history

# Generate a new migration after changing models.py
alembic revision --autogenerate -m "add_foo_column"

# Downgrade one step (use with caution)
alembic downgrade -1
```

### Environment variable

The database URL can be overridden via `DOSSIER_DB_URL`:

```bash
export DOSSIER_DB_URL="postgresql+asyncpg://user:pass@prod-host:5432/dossiers"
alembic upgrade head
```

If not set, alembic reads the URL from `alembic.ini` (which defaults
to the local development database).

### Current migration

| Revision | Description |
|---|---|
| `9d887db892c9` | Initial schema — 7 tables, all indexes, partial JSONB indexes for worker poll |

## Architecture

Five sibling projects, each with its own `pyproject.toml`, installable independently:

```
dossier_common       → stdlib-only shared utilities (signing tokens).
                       No sibling deps. At the bottom of the graph.

file_service         → Standalone FastAPI service for upload/download.
                       Verifies signed tokens minted by the engine.
                       Depends on: dossier-common.

dossier_engine       → PROV-based activity-driven framework library.
                       Plugin-agnostic — loads workflow plugins from
                       config.yaml at startup, knows nothing about them
                       at source level. Depends on: dossier-common.

dossier_toelatingen  → Workflow plugin library (heritage permits).
                       Declares activities, entities, handlers, validators,
                       relation validators, tasks, search hook. Depends
                       on: dossier-engine.

dossier_app          → Deployment project. Pins engine + plugin +
                       file_service + common at strict versions, wires
                       them via config.yaml, exposes the uvicorn entry
                       point at dossier_app.main:app.
```

Dependency graph (arrows point at runtime imports, libraries pin
compatible ranges `>=0.1.0,<0.2.0`, the app pins strict `==0.1.0`):

```
              dossier_common
                   ↑  ↑
         ┌─────────┘  └─────────┐
         │                      │
   file_service           dossier_engine
         ↑                      ↑
         │                      │
         │              dossier_toelatingen
         │                      ↑
         └──────┬───────────────┘
                │
            dossier_app
```

### Engine internals (under `dossier_engine_repo/`)

```
┌──────────────────────────────────────────────────────────────┐
│  Request pipeline — phase modules under engine/pipeline/:    │
│  preconditions → authorization → used → disjoint invariant   │
│  → generated (schema versioning) → relations (process-       │
│  control + domain, IRI expansion) → validators → tombstone   │
│  shape → persistence (incl. domain relation add/remove)      │
│  → handler → side effects → tasks → finalization             │
│                                                              │
│  Route modules under routes/:                                │
│  activities, dossiers, entities, files, access, prov,        │
│  reference (reference data + field validation)               │
│  _activity_visibility (shared visibility logic)              │
│                                                              │
│  ✓ Single activity endpoint (PUT, idempotent)                │
│  ✓ Batch endpoint (atomic multi-activity)                    │
│  ✓ Workflow-scoped + workflow-agnostic URL families           │
│  ✓ Authorization (direct, scoped, entity-derived)            │
│  ✓ Workflow validation (requirements, forbidden, deadlines)   │
│  ✓ Process-control + domain relations (unified API)          │
│  ✓ Per-operation relation validators (add/remove split)      │
│  ✓ IRI expansion for domain relation refs                    │
│  ✓ Schema versioning (per-activity new + allowed versions)   │
│  ✓ Disjoint invariant (no overlap between used + generated)  │
│  ✓ Tombstone redaction (NULL content, row survives)          │
│  ✓ Side effects (recursive, wasInformedBy chain)             │
│  ✓ Status derivation (computed_status on activity rows)      │
│  ✓ Task system (4 types, entities with full PROV)            │
│  ✓ Worker (polls for due tasks, executes atomically)         │
│  ✓ Access control (dossier_access entity)                    │
│  ✓ Activity visibility (own/related/all/combined modes)      │
│  ✓ PROV-JSON export                                          │
│  ✓ Interactive graph visualizations (timeline + columns)     │
│  ✓ Search integration hooks (Elasticsearch)                  │
│  ✓ Reference data endpoints (in-memory, sub-ms)              │
│  ✓ Field validation endpoints (plugin-registered)            │
│  ✓ Audit log (NDJSON → Wazuh SIEM)                           │
│  ✓ Plugin interface                                          │
│                                                              │
│  No business logic. No domain-specific code.                 │
└──────────────────────────────────────────────────────────────┘
```

### Plugin internals (under `dossier_toelatingen_repo/`)

```
✓ workflow.yaml (activities, entities, roles, rules, reference_data, relation_types)
✓ entities.py (Pydantic models — typed entity access)
✓ handlers/ (system activity logic, conditional tasks)
✓ relation_validators/ (activity-level relation semantics)
✓ field_validators.py (lightweight validation between activities)
✓ validators/ (custom business rules)
✓ tasks/ (type 2 recorded task handlers)
✓ pre_commit_hooks (synchronous validation/side effects, can veto an activity)
✓ post_activity_hook (search index updates, advisory, exceptions swallowed)
✓ search route (/{workflow}/dossiers)
```

## API Endpoints

The API has two URL families: **workflow-scoped** routes (the workflow name is in the URL, no DB lookup needed to resolve the plugin) and **workflow-agnostic** routes (only a dossier UUID is needed; the engine resolves the workflow internally).

### Workflow-scoped

| Method | Path | Description |
|---|---|---|
| `PUT` | `/{workflow}/dossiers/{id}/activities/{id}/{type}` | Execute a typed activity |
| `PUT` | `/{workflow}/dossiers/{id}/activities/{id}` | Execute a generic activity |
| `PUT` | `/{workflow}/dossiers/{id}/activities` | Execute batch activities atomically |
| `GET` | `/{workflow}/dossiers` | Search workflow-specific index (fuzzy onderwerp, exact filters; ACL-filtered) |
| `GET` | `/{workflow}/reference` | All reference data lists (sub-ms, in-memory) |
| `GET` | `/{workflow}/reference/{list_name}` | Single reference data list |
| `GET` | `/{workflow}/validate` | List available field validators |
| `POST` | `/{workflow}/validate/{validator_name}` | Run a field-level validator |
| `POST` | `/{workflow}/admin/search/recreate` | Drop + recreate workflow index (admin access) |
| `POST` | `/{workflow}/admin/search/reindex` | Re-index workflow dossiers into workflow index (admin access) |
| `POST` | `/{workflow}/admin/search/reindex-all` | Re-index workflow dossiers into workflow + common indices (admin access) |

### Workflow-agnostic

| Method | Path | Description |
|---|---|---|
| `GET` | `/dossiers` | Search common index (fuzzy onderwerp, exact workflow; ACL-filtered) |
| `GET` | `/dossiers/{id}` | Get dossier detail (entities, activities, domain relations, filtered by access) |
| `PUT` | `/dossiers/{id}/activities/{id}` | Execute a generic activity |
| `PUT` | `/dossiers/{id}/activities` | Execute batch activities atomically |
| `GET` | `/dossiers/{id}/entities/{type}` | All versions of an entity type |
| `GET` | `/dossiers/{id}/entities/{type}/{eid}` | All versions of a logical entity |
| `GET` | `/dossiers/{id}/entities/{type}/{eid}/{vid}` | Single entity version |
| `GET` | `/dossiers/{id}/prov` | PROV-JSON export (audit access) |
| `GET` | `/dossiers/{id}/prov/graph/timeline` | Timeline — user view (dossier access) |
| `GET` | `/dossiers/{id}/prov/graph/columns` | Column layout — full record (audit access) |
| `GET` | `/dossiers/{id}/archive` | PDF/A-3b archive — full record (audit access) |
| `POST` | `/admin/search/common/recreate` | Drop + recreate common index (admin access) |
| `POST` | `/admin/search/common/reindex` | Re-index every dossier into common (admin access) |
| `POST` | `/files/upload/request` | Mint a signed upload URL for the file service |
| `GET` | `/health` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (checks DB) |

Entity endpoint paths match the canonical IRI structure (`https://id.erfgoed.net/dossiers/{id}/entities/{type}/{eid}/{vid}`), so domain relation `from_ref` / `to_ref` values are directly resolvable as API URLs.

**Access tiers:** the three endpoints marked "audit access" (`/prov`, `/prov/graph/columns`, `/archive`) require a role in `global_audit_access` (config.yaml) or the dossier's `audit_access` list. They return the complete unfiltered record — system activities, tasks, all entities — and are intended for auditors, compliance, and long-term preservation. The timeline endpoint (`/prov/graph/timeline`) uses ordinary dossier access and honors per-user filtering; it never shows system activities or tasks. No query-parameter toggles — behavior is determined by access tier.

### CORS

The API includes CORS middleware. By default all origins are allowed
(development mode). To restrict in production, add to `config.yaml`:

```yaml
cors:
  allowed_origins:
    - "https://app.example.be"
    - "https://admin.example.be"
```

## Request Format

```bash
# Create a dossier with dienAanvraagIn (workflow-scoped URL)
curl -X PUT http://localhost:8000/toelatingen/dossiers/{dossier_id}/activities/{activity_id}/dienAanvraagIn \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "used": [
      { "entity": "https://id.erfgoed.net/erfgoedobjecten/10001" }
    ],
    "generated": [
      {
        "entity": "oe:aanvraag/{entity_id}@{version_id}",
        "content": {
          "onderwerp": "Restauratie gevelbekleding",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "Brugge",
          "object": "https://id.erfgoed.net/erfgoedobjecten/10001"
        }
      }
    ],
    "relations": [
      {
        "type": "oe:betreft",
        "from": "oe:aanvraag/{entity_id}@{version_id}",
        "to": "https://id.erfgoed.net/erfgoedobjecten/10001"
      }
    ]
  }'
```

Key concepts:
- `workflow` — only needed on the generic endpoint for the first activity (creates the dossier). On workflow-scoped URLs (`/{workflow}/dossiers/...`) it's inferred from the URL.
- `used` — references to existing entities or external URIs (read-only)
- `generated` — new entities with content (use `derivedFrom` for revisions). External URIs in `generated` don't need a `content` field; the engine auto-creates the external row.
- `relations` — two kinds in one list:
  - **Process-control** (activity→entity): `{"type": "oe:neemtAkteVan", "entity": "oe:aanvraag/X@v3"}` — the type must be declared in the activity's YAML. Plugin validators enforce semantics.
  - **Domain** (entity→entity/URI): `{"type": "oe:betreft", "from": "oe:aanvraag/X@v1", "to": "https://..."}` — semantic links persisted in the `domain_relations` table. Shorthand refs (`oe:type/eid@vid`, `dossier:did`) are expanded to full IRIs before storage.
- `remove_relations` — supersede existing domain relations: `{"type": "oe:betreft", "from": "oe:aanvraag/X@v1", "to": "https://..."}`. The engine sets `superseded_by_activity_id` on the matched row. Only allowed when the activity declares `operations: [remove]` for that type.
- `informed_by` — optional, local UUID or cross-dossier URI
- All IDs are client-generated UUIDs — PUTs are fully idempotent

## Core Invariants

- **Disjoint used/generated** — an entity cannot appear in both the `used` and `generated` blocks of the same activity. This eliminates double edges in the PROV graph and is enforced in the engine between the `used` and `generated` phases. Built-in system activities (marked `built_in: True` in their activity definition) are exempt because they legitimately need to read and write the same row — for example, the tombstone activity lists the versions it's redacting in `used` and produces the nulled replacements in `generated`.
- **Idempotent PUTs** — activity IDs are client-generated. Replaying the exact same request returns the cached response body without re-executing. Replaying with different content against an existing ID is a 409.
- **Append-only with one exception** — activity rows and entity version rows are never updated. The only exception is tombstone: it NULLs the `content` column on tombstoned entity rows in place, leaves the row itself (plus `entity_id`, `schema_version`, timestamps, and `generated_by`), and stamps `tombstoned_by` with the tombstone activity ID. The row survives so the PROV graph keeps its shape; only the blob content is gone.
- **Schema versioning** — entity types can declare multiple Pydantic models under versioned keys. Activities declare `new_version` (the version stamped on fresh entities) and `allowed_versions` (the versions the activity will accept as revision parents). The engine rejects revisions of entities at disallowed versions with `422 unsupported_schema_version`.
- **Per-dossier write serialization** — every activity takes a `SELECT ... FOR UPDATE` lock on its dossier row as its first action (in `ensure_dossier`). Concurrent activities against the same dossier execute one after the other, not in parallel. Other dossiers remain fully parallel — the lock is row-level, not table-level. When the second-to-arrive request wakes up after the first commits, it reads fresh state; if its client-supplied `derivedFrom` is now stale (the first request already produced a newer version), the derivation-chain validator rejects with `422 invalid_derivation_chain`. This is the optimistic-concurrency mechanism — no client-side ETag headers, no retry loops, just Postgres row locks at the natural serialization boundary.

## Task System

Four types of tasks, all modeled as `system:task` entities with full PROV:

| Type | Kind | Description |
|---|---|---|
| 1 | `fire_and_forget` | Runs inline, no record |
| 2 | `recorded` | Worker executes function, completeTask records result |
| 3 | `scheduled_activity` | Worker executes an activity at a scheduled time |
| 4 | `cross_dossier_activity` | Worker executes an activity in another dossier |

Tasks can be defined statically in YAML or appended conditionally by handlers at runtime. Only one scheduled task of a given `target_activity` per dossier can be queued at a time — scheduling a second one supersedes the first (unless `allow_multiple: true`). Scheduled tasks declare `cancel_if_activities` to name activities whose execution should cancel the task before the worker picks it up.

### Worker

```bash
python -m dossier_engine.worker --once          # drain all due tasks and exit
python -m dossier_engine.worker                 # continuous polling (10s default)
python -m dossier_engine.worker --interval 5    # custom poll interval
python -m dossier_engine.worker --help          # full options
```

The worker is production-ready and safe to deploy redundantly. Every significant piece of its behavior went through a dedicated hardening pass — see `dossier_engine_repo/dossier_engine/worker.py` for source.

**Concurrency model.** Multiple workers can run against the same database. The poll query is `SELECT ... LIMIT 5 FOR UPDATE OF entities SKIP LOCKED` — each worker tries to claim up to 5 candidate tasks, Postgres locks them for the duration of the claiming transaction, and any other worker's concurrent claim attempts skip over locked rows entirely. The lock persists from the claim through the task execution to the commit, so no two workers ever execute the same task version. When one worker commits (success, cancellation, retry, or dead-letter), the lock releases and the next worker's next claim sees the updated status. No leader election, no leases, no external coordination — Postgres handles it.

**Claim-lock-execute loop.** The worker has two nested loops: an inner drain loop that claims one task, executes it, and commits in a single transaction, repeating until the claim query returns nothing; and an outer sleep loop that waits for the next poll interval (or a SIGTERM). `--once` mode runs one inner drain and exits.

**Retry policy.** When a task execution raises an exception, the failure is caught and routed through `_record_failure`, which writes a new task version in a fresh transaction with updated retry bookkeeping:

- `attempt_count` is incremented.
- If `attempt_count >= max_attempts` (default 3), the new version has `status = "dead_letter"` — terminal, never picked up by the worker again.
- Otherwise, the new version stays in `status = "scheduled"` but gains a `next_attempt_at` field set to `now + base_delay_seconds * 2**(attempt_count - 1) * (1 + jitter)` where jitter is uniform in `[-0.1, 0.1]`. The default `base_delay_seconds` is 60, so failures retry at roughly 60s, 120s, 240s for attempts 1, 2, 3. The jitter prevents thundering-herd retries.
- Error telemetry is handled in two layers. Log records go to the Python `logging` system via `logger.warning(..., exc_info=...)` (retries) and `logger.error(..., exc_info=...)` (dead-letter), both with structured `extra` fields (`task_id`, `task_entity_id`, `dossier_id`, `function`, `kind`, `attempt_count`, `max_attempts`). On top of that, the worker directly calls `sentry_sdk.capture_exception(...)` at three specific decision points — retry, dead-letter, and worker-loop crash — with explicit fingerprints that control how events group into Sentry issues. See the [Worker → Sentry](#worker--sentry) section below for the fingerprinting contract. The task content itself does NOT carry error text — only operational state (`attempt_count`, `last_attempt_at`, `next_attempt_at`) that the poll loop needs for retry decisions.

Tasks can override `max_attempts` and `base_delay_seconds` in their content to opt into a different retry shape — e.g. a cross-dossier task that calls a flaky external service might use `max_attempts=5, base_delay_seconds=30` for faster, more aggressive retry. All retry state goes through the same `complete_task → execute_activity → systemAction` pathway as happy-path completions, so every write is validated, every post-activity hook fires, and the full PROV graph is preserved.

**Dead-letter handling.** Dead-lettered tasks stay in the database with `status = "dead_letter"` and are invisible to the poll query (which filters on `status = 'scheduled'`). They require operator intervention. Use the worker's `--requeue-dead-letters` CLI flag:

```bash
# Requeue every dead-lettered task, all dossiers
python -m dossier_engine.worker --requeue-dead-letters

# Requeue only dead letters in one dossier
python -m dossier_engine.worker --requeue-dead-letters --dossier=<uuid>

# Requeue one specific task by its logical entity_id
python -m dossier_engine.worker --requeue-dead-letters --task=<entity_uuid>

# Combine filters — requeue a specific task in a specific dossier
python -m dossier_engine.worker --requeue-dead-letters --dossier=<uuid> --task=<entity_uuid>
```

The requeue writes a fresh revision of each dead-lettered task with `status = "scheduled"`, `attempt_count = 0`, and `next_attempt_at = null`. The original `scheduled_for` is preserved as the historical record of when the task was first queued, and `last_attempt_at` is preserved so operators can still see when the task last tried. All other task content (function name, target_activity, etc.) is carried forward unchanged.

Each dossier's requeue is audited as a single `systemAction` activity that generates N task revisions plus one `system:note` explaining the scope and listing the requeued task entity_ids. Running `--requeue-dead-letters` once with 50 dead letters spread across 10 dossiers produces 10 audit entries, one per dossier, each listing the tasks requeued in that dossier. The systemAction is a first-class activity in the PROV graph — an auditor walking the dossier's history sees exactly when each requeue happened and which tasks were affected.

The requeue command runs as a one-shot and exits — it does NOT start a drain cycle. If you want to immediately execute the requeued tasks, run `python -m dossier_engine.worker --once` afterward (or wait for the next normal poll cycle to pick them up).

**Graceful shutdown.** `SIGTERM` and `SIGINT` set an `asyncio.Event`. The outer sleep loop uses `asyncio.wait_for(shutdown.wait(), timeout=poll_interval)` as its "sleep", so the signal unblocks the sleep immediately. The inner drain loop checks the event at the top of each iteration, so a signal mid-drain finishes the in-flight task cleanly (its transaction runs to completion — we never interrupt a task mid-transaction) and then exits before starting the next one. Container orchestrators that send SIGTERM and wait a few seconds before killing the process will see the worker exit cleanly with `Worker stopped` in the log.

**Observability log lines** (all under logger `dossier.worker`). ERROR-level entries include `exc_info` when they report an exception, so the Sentry logging integration captures them as full events with stack traces:

| Line | Level | When |
|---|---|---|
| `Worker started. Poll interval: Ns. Once: True/False` | INFO | startup |
| `Task X: processing kind=Y function=Z` | INFO | beginning of each task execution |
| `Task X: recorded task 'Y' completed` | INFO | successful recorded task |
| `Task X: scheduled activity Y executed` | INFO | successful scheduled_activity |
| `Task X: cross-dossier activity Y in Z` | INFO | successful cross_dossier |
| `Drain cycle: processed N tasks` | INFO | end of each drain pass, only when N > 0 |
| `Task X: attempt K/M failed, retry at T` | WARNING | transient failure with retry scheduled; carries `exc_info` + structured `extra` |
| `Task X: attempt K/M failed, moving to dead_letter` | ERROR | failure with retries exhausted; carries `exc_info` + structured `extra` |
| `Task X execution failed: E` | ERROR | raw exception trace before retry decision |
| `requeue_dead_letters: dossier X — requeued N task(s)` | INFO | per-dossier requeue completion |
| `requeue_dead_letters: done, N task(s) requeued total` | INFO | end of a requeue run |
| `Worker received signal N, shutting down gracefully` | INFO | SIGTERM/SIGINT arrived |
| `Worker stopped` | INFO | clean exit |

The failure log lines carry structured `extra` fields (`task_id`, `task_entity_id`, `dossier_id`, `function`, `kind`, `attempt_count`, `max_attempts`) that Sentry maps to event tags. That makes Sentry queries like `task_id:<uuid>` or `dossier_id:<uuid> attempt_count:>1` work out of the box, so an operator investigating a failing task can pull the full attempt history without having to cross-reference logs manually.

A simple prometheus-style scrape can be built on top of these by counting `recorded task '*' completed` lines for throughput and `moving to dead_letter` lines for failure-budget alerts. Real metrics (gauge for queue depth, histogram for task duration) are a separate Level 2 concern and not yet implemented.

**Inspecting the queue from psql**. These are latest-version-aware queries — they ignore superseded task revisions and only show each logical task's current state:

```sql
-- Backlog depth by status (latest version per logical task)
WITH latest AS (
  SELECT DISTINCT ON (entity_id) *
  FROM entities
  WHERE type = 'system:task'
  ORDER BY entity_id, created_at DESC
)
SELECT content->>'status' AS status, COUNT(*)
FROM latest
GROUP BY 1
ORDER BY 2 DESC;

-- Dead-lettered tasks (error details live in Sentry, not here)
WITH latest AS (
  SELECT DISTINCT ON (entity_id) *
  FROM entities
  WHERE type = 'system:task'
  ORDER BY entity_id, created_at DESC
)
SELECT entity_id,
       content->>'function'        AS fn,
       content->>'attempt_count'   AS attempts,
       content->>'last_attempt_at' AS last_tried,
       dossier_id
FROM latest
WHERE content->>'status' = 'dead_letter'
ORDER BY content->>'last_attempt_at' DESC;

-- Tasks waiting on retry delay
WITH latest AS (
  SELECT DISTINCT ON (entity_id) *
  FROM entities
  WHERE type = 'system:task'
  ORDER BY entity_id, created_at DESC
)
SELECT entity_id,
       content->>'function'        AS fn,
       content->>'attempt_count'   AS attempts,
       content->>'next_attempt_at' AS retry_at
FROM latest
WHERE content->>'status' = 'scheduled'
  AND content ? 'next_attempt_at'
  AND (content->>'next_attempt_at')::timestamptz > NOW()
ORDER BY (content->>'next_attempt_at')::timestamptz;
```

For the full attempt history of a specific task (who failed, when, what error), query Sentry by `task_id:<entity_id>`. The database only stores operational state, not error details.

**Running multiple workers.** On Postgres with `FOR UPDATE OF entities SKIP LOCKED`, concurrent workers are safe by construction. Run as many as your task throughput requires. A sensible starting point is one worker per CPU core for CPU-bound task functions, or a few workers per core for IO-bound ones. All workers connect to the same Postgres instance; no other coordination is needed.

### Worker → Sentry

The worker emits Sentry events explicitly at three decision points, with fingerprints chosen so operators get one issue per logical problem — not one per log line.

```
┌──────────────────────────────────────────────────────────────────┐
│ Event                          │ Level     │ Fingerprint         │
├────────────────────────────────┼───────────┼─────────────────────┤
│ Task retry failure             │ warning   │ ["worker.task.      │
│                                │           │   retry",           │
│                                │           │   <function>]       │
├────────────────────────────────┼───────────┼─────────────────────┤
│ Task escalated to dead_letter  │ error     │ ["worker.task.      │
│                                │           │   dead_letter",     │
│                                │           │   <function>,       │
│                                │           │   <task_entity_id>] │
├────────────────────────────────┼───────────┼─────────────────────┤
│ Worker loop itself crashed     │ fatal     │ ["worker.loop.      │
│                                │           │   crash"]           │
└──────────────────────────────────────────────────────────────────┘
```

**Retries** collapse by task function. All SMTP flakes from `send_ontvangstbevestiging` go into one issue whose event count tells you how much SMTP is flaking. Operators see a trend, not a flood.

**Dead-letters** are per-task. Each dead-lettered task is its own Sentry issue, tagged with task id, function, dossier id. Operators investigate, fix, then requeue via `--requeue-dead-letters --task=<uuid>`. Resolving the Sentry issue goes hand-in-hand with requeueing the task.

**Worker-loop crashes** collapse into one. If the poll loop itself throws — Postgres goes away, async runtime explodes — you get one issue, not N (one per failed poll cycle).

Log records still ride along. The `LoggingIntegration` is configured with `event_level=None` so `logger.info(...)`, `logger.warning(...)`, `logger.error(...)` become Sentry **breadcrumbs** (context threaded onto the next explicit event), not standalone events. Every Sentry event carries the last ~100 log records as a trail, which means an investigator clicking through an event sees what the worker was doing right before the failure.

**Configuration.** The worker calls `init_sentry()` at startup. It reads `SENTRY_DSN` from the environment; if unset (or `sentry_sdk` isn't installed), all Sentry calls are silent no-ops. Deployments wire it via:

```bash
export SENTRY_DSN='https://xxx@yyy.ingest.sentry.io/zzz'
export SENTRY_ENVIRONMENT='prod'   # optional
export SENTRY_RELEASE='v1.2.3'     # optional
python -m dossier_engine.worker
```

**Per-event tags** attached to every Sentry event: `task_id`, `task_entity_id`, `dossier_id`, `task_function`, `task_attempt`, `task_phase` (one of `retry` / `dead_letter`). `max_attempts` is attached as extra context. This makes Sentry queries like `task_function:move_bijlagen_to_permanent AND task_phase:dead_letter` work out of the box.

## Plugin Extension Points

A workflow plugin can hook into the activity pipeline at two distinct points. They look superficially similar but have very different contracts — pick deliberately.

### `pre_commit_hooks` (strict, can veto)

A list of callables that run after persistence, side effects, and task scheduling, but before the `cached_status` / `eligible_activities` projection and before the transaction commits. Exceptions raised from a hook **propagate out of the pipeline and roll the activity back**. Hooks run in declaration order; the first raise stops subsequent hooks.

Use for: synchronous validation and mandatory side effects that must succeed or the activity is invalid. Examples: PKI signature verification against an external service before recording that a document was signed; reserving an external ID in another system; enforcing cross-entity invariants that can't be expressed statically in the workflow YAML.

Signature:

```python
async def my_hook(
    *, repo, dossier_id, plugin, activity_def,
    generated_items, used_rows, user,
) -> None:
    # Inspect state. Raise ActivityError(code, message) to reject.
    ...
```

Registration on the `Plugin` dataclass:

```python
plugin = Plugin(
    ...,
    pre_commit_hooks=[my_hook, another_hook],
)
```

### `post_activity_hook` (advisory, best-effort)

A single callable invoked after the cached status is computed. Exceptions are logged as warnings and **swallowed** — a failing hook never fails the activity. Runs inside the same transaction, so writes the hook performs are atomic with the activity, but a crash in the hook doesn't undo the activity itself.

Use for: search index updates, cache invalidation, metrics emission, outbound notifications where "best-effort" is acceptable. Examples: pushing a denormalized document to Elasticsearch, nudging a downstream webhook subscriber.

Signature:

```python
async def my_hook(
    repo, dossier_id, activity_type, status, entities,
) -> None:
    ...
```

### Choosing between them

The question to ask: **if this hook fails, should the user see their activity succeeded or failed?**

* "The activity must not proceed if this step can't complete" → `pre_commit_hooks`
* "The activity succeeded; any downstream propagation is a separate concern" → `post_activity_hook`

A flaky search index should not block users from submitting forms. A failed mandatory signature verification should.

## Domain Relations

Separate from PROV (which handles provenance — who did what when), domain relations capture semantic links between things: an aanvraag *concerns* an erfgoedobject, a beslissing *falls under* a legal article, one dossier *is related to* another.

### Two kinds of relation, one API field

The `relations` list on an activity request carries both:

- **Process-control** (`oe:neemtAkteVan`): activity→entity, stored in `activity_relations`. The activity is one end of the relation.
- **Domain** (`oe:betreft`, `oe:valtOnder`, `oe:gerelateerd_aan`): entity→entity/URI, stored in `domain_relations`. Neither end is the activity; the activity is the *provenance* of the relation.

The engine distinguishes them by the `kind` field in the workflow YAML and by the request shape (`entity` for process-control, `from`+`to` for domain).

### Ref formats and IRI expansion

Domain relation endpoints accept shorthand refs for convenience. The engine expands them to full IRIs before storage via `expand_ref()` in `prov_iris.py`:

| Shorthand | Expands to | Use case |
|---|---|---|
| `oe:type/eid@vid` | `https://id.erfgoed.net/dossiers/{current}/entities/oe:type/eid/vid` | Local entity |
| `dossier:did/oe:type/eid@vid` | `https://id.erfgoed.net/dossiers/did/entities/oe:type/eid/vid` | Cross-dossier entity |
| `dossier:did` | `https://id.erfgoed.net/dossiers/did/` | Dossier itself |
| `https://...` | unchanged | External URI |

`classify_ref()` determines the kind (entity, dossier, external_uri) from both shorthand and expanded forms.

### Operations control

Each activity declares which domain relation types it can add and/or remove:

```yaml
- name: dienAanvraagIn
  relations:
    - type: "oe:betreft"
      kind: domain
      operations: [add]           # can establish, can't remove

- name: bewerkRelaties
  relations:
    - type: "oe:betreft"
      kind: domain
      operations: [add, remove]   # can do both
```

The engine rejects `remove_relations` entries for types where the activity only declares `[add]`.

### Per-operation validators

Relation validators can be split by operation:

```yaml
relations:
  - type: "oe:betreft"
    kind: domain
    validators:
      add: "validate_betreft_target"      # checks URI resolves
      remove: "validate_betreft_removable" # checks no beslissing depends on it
```

Falls back to a single `validator:` string or the plugin-level `relation_validators[type]` if no per-operation split is declared.

### Storage

The `domain_relations` table stores every relation (active and superseded):

| Column | Description |
|---|---|
| `relation_type` | e.g. `oe:betreft` |
| `from_ref` | Full IRI of the source |
| `to_ref` | Full IRI of the target |
| `created_by_activity_id` | Which activity established this relation |
| `superseded_by_activity_id` | Which activity removed it (NULL if active) |
| `superseded_at` | When it was removed (NULL if active) |

Active relations: `WHERE superseded_at IS NULL`. Full history is preserved — superseded rows stay for audit.

### GET response

`GET /dossiers/{id}` includes active domain relations:

```json
{
  "domainRelations": [
    {
      "type": "oe:betreft",
      "from": "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1",
      "to": "https://id.erfgoed.net/erfgoedobjecten/10001",
      "createdBy": "a1000000-...",
      "createdAt": "2026-04-17T10:00:00Z"
    }
  ]
}
```

## Reference Data & Validation

### Static reference data

Workflow plugins declare reference lists in their `workflow.yaml`:

```yaml
reference_data:
  bijlagetypes:
    - key: "foto"
      label: "Foto"
    - key: "detailplan"
      label: "Detailplan"
  gemeenten:
    - key: "brugge"
      label: "Brugge"
      nis_code: "31005"
```

Served from in-memory plugin config — no DB hit, sub-millisecond response:

```
GET /toelatingen/reference              → all lists in one call
GET /toelatingen/reference/bijlagetypes → single list
```

Frontend caches aggressively; data only changes on deployment.

### Field-level validation

Plugins register lightweight async validators for checks that need server-side logic but shouldn't wait until activity submission:

```python
# In the plugin
FIELD_VALIDATORS = {
    "erfgoedobject": validate_erfgoedobject,
    "handeling": validate_handeling,
}
plugin = Plugin(..., field_validators=FIELD_VALIDATORS)
```

Called between activities via POST:

```
POST /toelatingen/validate/erfgoedobject
{"uri": "https://id.erfgoed.net/erfgoedobjecten/10001"}
→ {"valid": true, "label": "Stadhuis Brugge", "type": "monument", "gemeente": "Brugge"}

POST /toelatingen/validate/handeling
{"erfgoedobject_uri": "https://...", "handeling": "sloop_deel"}
→ {"valid": false, "error": "Handeling 'sloop_deel' is niet toegelaten voor type 'landschap'..."}

GET /toelatingen/validate
→ {"validators": ["erfgoedobject", "handeling"]}
```

No DB writes, no PROV records, no transaction — pure validation functions. The frontend calls these on blur/change for instant feedback.

## Search (Elasticsearch)

Dossier listing and search go through Elasticsearch, not Postgres. Two indices:

- **`dossiers-common`** — one doc per dossier, every workflow. Fields: `dossier_id`, `workflow`, `onderwerp`, `__acl__`. Served at `GET /dossiers` with fuzzy match on `onderwerp` and exact filter on `workflow`.
- **`dossiers-toelatingen`** — one doc per toelatingen dossier. Fields: `dossier_id`, `onderwerp`, `gemeente`, `beslissing`, `__acl__`. Served at `GET /toelatingen/dossiers` with fuzzy match on `onderwerp` and exact filters on `gemeente` and `beslissing`. Each workflow plugin owns its own specific index the same way.

Both indices receive upserts after every activity via each plugin's `post_activity_hook`. When Elasticsearch is not configured, the hooks are silent no-ops (the activity still commits) and the search endpoints return empty results with an explanatory `reason` field — no Postgres fallback, deliberately. An index with no data is a configuration problem, not something to paper over.

### ACL filtering

Every indexed doc carries a flat `__acl__` list — role names and agent UUIDs concatenated from three sources:

1. Per-dossier `access` entries in the `oe:dossier_access` entity (roles and agents)
2. Per-dossier `audit_access` list on the same entity
3. Global roles from `config.yaml`'s `global_access` block

Every search query AND's in a `terms` filter that checks `__acl__` against `user.roles ∪ {user.id}`. Users see only dossiers they could also open directly via `GET /dossiers/{id}`. Global-access users (e.g. `beheerder`) see everything because their role is in every doc's `__acl__`.

### Enabling Elasticsearch

Set two environment variables before starting the app:

```bash
export DOSSIER_ES_URL="https://your-cluster.example.be:9200"
export DOSSIER_ES_API_KEY="encoded-api-key-from-your-cluster"

# Optional: disable for self-signed dev clusters
export DOSSIER_ES_VERIFY_CERTS=false
```

The API key is used verbatim in the `Authorization: ApiKey <key>` header. Generate one in Kibana (Stack Management → Security → API keys) with permissions to read/write both indices. The URL and key should only come from env vars — never commit them to `config.yaml`.

Add at least one role to `global_admin_access` in `config.yaml` so the admin endpoints are reachable:

```yaml
global_admin_access:
  - "beheerder"
```

After setting env vars and restarting the app, create the indices and populate them:

```bash
# Create the dossiers-common index (drops first if it exists)
curl -X POST http://localhost:8000/admin/search/common/recreate \
  -H "X-POC-User: claeyswo"

# Populate it from Postgres
curl -X POST http://localhost:8000/admin/search/common/reindex \
  -H "X-POC-User: claeyswo"

# Same for the toelatingen-specific index
curl -X POST http://localhost:8000/toelatingen/admin/search/recreate \
  -H "X-POC-User: claeyswo"
curl -X POST http://localhost:8000/toelatingen/admin/search/reindex \
  -H "X-POC-User: claeyswo"

# Convenience: toelatingen + common in one walk (both indices get
# fresh docs for every toelatingen dossier)
curl -X POST http://localhost:8000/toelatingen/admin/search/reindex-all \
  -H "X-POC-User: claeyswo"
```

From then on, each activity's `post_activity_hook` keeps both indices in sync — no further manual reindexing needed unless the mapping changes.

### Admin endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/search/common/recreate` | Drop + recreate common index |
| `POST` | `/admin/search/common/reindex` | Re-index every dossier into common |
| `POST` | `/toelatingen/admin/search/recreate` | Drop + recreate toelatingen index |
| `POST` | `/toelatingen/admin/search/reindex` | Re-index toelatingen dossiers (toelatingen index only) |
| `POST` | `/toelatingen/admin/search/reindex-all` | Re-index toelatingen dossiers into both indices |

All gated on `global_admin_access` (strict role check, default-deny on misconfiguration).

### Three access tiers

The platform now has three orthogonal role tiers in `config.yaml`:

- **`global_access`** — day-to-day business views; controls dossier/entity visibility via `view`/`activity_view`. Also contributes to `__acl__` so these users find dossiers in search.
- **`global_audit_access`** — full-record views (`/prov`, `/prov/graph/columns`, `/archive`). Role-only.
- **`global_admin_access`** — destructive operations (recreate/reindex). Role-only. Default-deny if empty.

They don't imply each other — a `beheerder` that appears in all three lists gets all three sets of powers explicitly. A role granted audit doesn't automatically get admin or vice versa.

## Activity Visibility

The `activity_view` setting on access entries controls which activities a user sees in the dossier timeline. Four input forms, all normalised to an `ActivityViewMode` dataclass by `parse_activity_view()`:

| Form | Meaning |
|---|---|
| `"all"` | Every activity visible |
| `"own"` | Only activities where the user is the PROV agent |
| `["dienAanvraagIn", "neemBeslissing"]` | Only these activity types |
| `{"mode": "own", "include": ["neemBeslissing"]}` | Combined: own activities plus named types regardless of agent |

The combined dict form is useful for aanvragers who should see their own actions plus all decisions, even if someone else made them. The shared module `routes/_activity_visibility.py` provides `parse_activity_view()` and `is_activity_visible()`, used by both the dossier-detail and PROV endpoints via callback-based lookups (no code duplication).

> **Round 31:** The `"related"` mode (activities touching visible entities, plus the user's own) was removed. It wasn't used in production and had confusing semantics. Legacy `"related"` values in dossier_access entities fall through to a deny-safe default at read time; Pydantic rejects `"related"` at write time.

### Audit-level access

Some endpoints expose the complete, unfiltered provenance record — all activities (including system ones), all tasks, all entities regardless of per-user filtering. These are the auditor, compliance, and long-term-preservation views:

- `GET /dossiers/{id}/prov` — PROV-JSON export
- `GET /dossiers/{id}/prov/graph/columns` — full-record column visualization
- `GET /dossiers/{id}/archive` — PDF/A-3b archive

Access to these is gated by `check_audit_access` rather than the ordinary dossier access check. A user with ordinary dossier access does **not** automatically get audit-level views; they need an explicit role grant. Two sources, both role-based (no per-agent grants):

- `global_audit_access` in `config.yaml` — applies to every dossier.
- `audit_access` list on the dossier's `oe:dossier_access` entity — per-dossier roles (e.g. a signing authority for this specific application).

Default-deny: no match → 403 and an audit trail entry (`dossier.audit_denied`) for compliance investigations. The timeline endpoint (`/prov/graph/timeline`) is unaffected — it's the day-to-day user view and uses ordinary dossier access with per-user filtering.

## Dossier Archive (PDF/A-3b)

The `GET /dossiers/{id}/archive` endpoint produces a self-contained PDF/A-3b archive suitable for long-term (30+ year) retention. It's an audit-level endpoint — see the [Audit-level access](#audit-level-access) section for role requirements. The archive contains:

- **Cover page** — dossier metadata, workflow, status, creation date, all involved agents with their canonical URIs, and a full activity timeline.
- **Provenance timeline** — a static SVG rendered server-side (pure Python, no D3 or browser required) showing activity columns and entity version markers.
- **Entity version history** — every version of every entity type (external, domain, and system entities) with full JSON content, derivation references, timestamps, and tombstone markers.
- **Embedded `prov.json`** — the complete W3C PROV-JSON export as a PDF/A-3 attachment for machine extraction.
- **Embedded bijlagen** — all file attachments referenced by any entity's `bijlagen` array, embedded as PDF/A-3 attachments alongside `prov.json`.

Embedded files are accessible through the attachments panel of any PDF/A-3-aware viewer (Evince: sidebar paperclip; Okular: Tools → Embedded Files; Adobe Reader: left panel → paperclip). The XMP metadata declares `pdfaid:part=3` and `pdfaid:conformance=B`.

For strict PDF/A validation (veraPDF), an ICC output intent profile must be added — fpdf2 does not generate one natively. In production, post-process with Ghostscript:

```bash
gs -dPDFA=3 -dBATCH -dNOPAUSE -dNOOUTERSAVE \
   -sColorConversionStrategy=RGB -sDEVICE=pdfwrite \
   -sPDFACompatibilityPolicy=1 \
   -sOutputFile=archive_pdfa.pdf archive.pdf
```

## Audit Log

Separate from the PROV graph (which records successful state transitions) and from Sentry (which captures exceptions), the audit log is an append-only record of **who did what to what, when** — including reads, denials, and exports. It answers compliance questions like "who looked up applicant X's dossier?" or "who exported data between dates Y and Z?"

The PROV graph already covers the "what changed" half of any audit story — every activity has an actor, a timestamp, and the set of entities it touched. What PROV doesn't cover is read events (nothing was modified, so nothing is persisted), authorisation denials (the activity never ran, so no activity row), and data exports (the PDF/A archive isn't a PROV activity). The audit log fills those gaps.

### Design

The engine writes newline-delimited JSON (NDJSON) to a local file. A Wazuh agent on the same host tails the file and forwards events to the SIEM. The application does not talk to Wazuh over the network — it writes a file, Wazuh reads the file. This matters for two reasons: if Wazuh is down, the application keeps running (writes still succeed, the agent catches up later), and if the application crashes, the file is already on disk (no buffered-in-memory events lost).

One complete JSON object per physical line, `\n`-terminated. Rotation is handled by Python's `RotatingFileHandler`, not by `logrotate` — the Wazuh agent tails by inode, so the handler's rename-on-rotation doesn't lose events across the boundary.

### Event shape

```json
{
  "event_action": "dossier.exported",
  "actor": {"id": "claeyswo", "name": "Claeys Wouter"},
  "target": {"type": "Dossier", "id": "d1000000-0000-0000-0000-000000000001"},
  "outcome": "allowed",
  "dossier_id": "d1000000-0000-0000-0000-000000000001",
  "extra": {"export_format": "pdfa3", "bytes_sent": 125478},
  "@timestamp": "2026-04-16T10:51:08.583947+00:00"
}
```

Fields:

- `event_action` — namespaced verb, a small stable vocabulary (see table below). SIEM alert rules key on exact strings, so don't rename without coordinating with the SIEM team. The JSON key is `event_action` (not `action`) to avoid a collision with Wazuh's reserved static field names: `user`, `srcip`, `dstip`, `srcport`, `dstport`, `protocol`, **`action`**, `id`, `url`, `data`, `extra_data`, `status`, `system_name`. Any one of those as a top-level key in JSON audit events would fail to match in Wazuh rules because the JSON decoder won't place them into the dynamic-field table.
- `actor.id` / `actor.name` — who did it. `actor.id` is the stable identifier (usually a username or user UUID); `actor.name` is for human-readable reports. Worker-driven events use `"system"` / `"Systeem"`.
- `target.type` / `target.id` — what was acted on. Usually `Dossier` + dossier UUID.
- `outcome` — exactly one of `allowed` / `denied` / `error`.
- `dossier_id` — always the containing dossier's UUID when the event is dossier-scoped; denormalised so SIEM queries filter without joining on target.
- `reason` — free-text explanation, only present for `denied` / `error`.
- `extra` — action-specific structured fields. Examples: `export_format` for exports, `bytes_sent` for file downloads, `query` for searches. Nested under `extra` rather than at top level, so keys named `status` / `id` / etc. don't hit the reserved-name collision either.
- `@timestamp` — ISO-8601 UTC with microseconds. Produced by the application, not the Wazuh agent, so a delayed write doesn't get misattributed to a later bucket.

### Action vocabulary

| Action | Emitted when | Typical outcome | Status |
|---|---|---|---|
| `dossier.created` | Entry-point activity (an activity with `can_create_dossier: true`, e.g. `dienAanvraagIn`) commits successfully | `allowed` | wired |
| `dossier.updated` | Any subsequent activity commits successfully | `allowed` | wired |
| `dossier.read` | `GET /dossiers/{id}` serves a response | `allowed` | wired |
| `dossier.exported` | `GET /dossiers/{id}/archive` produces a PDF/A | `allowed` | wired |
| `dossier.denied` | Authorization refused an action (read access check or write-side `ActivityError(403)`) | `denied` | wired |
| `dossier.searched` | Search query executes | `allowed` | reserved — not yet emitted; the current `/dossiers` list endpoint is a stub, real search belongs to future workflow-specific endpoints |
| `dossier.file_accessed` | Bijlage download served | `allowed` | reserved — not yet emitted; downloads are handled by the `file_service`, which would need its own audit emission |

Non-authorization errors (validation failures, business-rule violations, 422/400 responses) are deliberately *not* in the vocabulary. Those belong in the application log and Sentry, not the SIEM audit trail — security teams care about *what actors did*, not *which form fields failed validation*. If a pattern of validation failures turns out to be security-relevant later (e.g. someone probing for working field combinations), Sentry's rate-and-pattern tooling is the right place to notice.

Worker task execution is similarly not in the vocabulary. Task lifecycle is internal to the engine (retries, dead-lettering) and belongs in the operational monitoring stream (Sentry + metrics), not the audit trail. Every worker-driven action that has security meaning already produces an audit event via the standard `dossier.created` / `dossier.updated` path — because the worker uses the same `execute_activity` code path as user-initiated writes.

### Enabling in the application

Audit emission is off by default — the `dossier.audit` logger has no handler, and `emit_audit()` is a silent no-op. This keeps dev and test environments clean. To enable, add an `audit:` block to `config.yaml`:

```yaml
audit:
  log_path: "/var/log/dossier/audit.json"
  max_bytes: 104857600       # 100 MB per file (optional; this is the default)
  backup_count: 10           # keep 10 rotated files, ~1 GB ceiling (optional)
```

On startup, the engine logs one INFO line confirming configuration:

```
INFO  dossier  Audit log configured: /var/log/dossier/audit.json (rotation: 10 × 104857600 bytes)
```

If the configured path's directory doesn't exist, the engine logs a warning and continues without audit (emissions become silent no-ops). This is deliberate — the SIEM is out of scope for application dependencies, and we never want a missing audit directory to fail the app. The ops team is responsible for provisioning the directory.

### Host setup (for the ops team)

**Directory:**

```bash
sudo mkdir -p /var/log/dossier
sudo chown dossier-app:wazuh /var/log/dossier
sudo chmod 2750 /var/log/dossier
```

Group `wazuh` is the group the `wazuh-agent` process runs under; it gets read access via the group bit so it can tail the file. The `2` in `2750` is the setgid bit — new files inherit the group so rotated files stay readable by the agent. The `dossier-app` user is whatever user uvicorn runs as; it gets write.

**Wazuh agent** — add the following block to the agent's `ossec.conf` (typically at `/var/ossec/etc/ossec.conf` inside the `<ossec_config>` root) and restart the agent:

```xml
<localfile>
  <log_format>json</log_format>
  <location>/var/log/dossier/audit.json</location>
  <label key="@source">dossier_engine</label>
  <label key="component">audit</label>
</localfile>
```

The two `<label>` blocks stamp every event with `@source=dossier_engine` and `component=audit` so the SIEM team can filter cleanly in the Wazuh dashboard. A dashboard KQL query like `@source: dossier_engine AND component: audit AND event_action: dossier.exported` (or `rule.groups: audit` once custom rules are in place) returns every export event across all hosts running the engine.

Restart the agent to pick up the new block:

```bash
sudo systemctl restart wazuh-agent
```

Verify the agent sees the file:

```bash
sudo tail -n 5 /var/ossec/logs/ossec.log | grep -i "audit.json"
# Expected: "Analyzing file: '/var/log/dossier/audit.json'."
```

**Custom rules and retention** — the Wazuh server-side rules and retention policy (e.g. "keep audit events for 7 years") are the SIEM team's responsibility, not the application's. Without rules, events reach the manager but are dropped from the alert pipeline (Wazuh indexes only events that trigger a rule) — so the SIEM team needs to write at least a baseline set of rules for the `@source: dossier_engine` stream before anything appears in the dashboard. A starter rule block is in the [Testing locally](#testing-locally) section below; copy, adapt severities to your alert philosophy, drop into `/var/ossec/etc/rules/local_rules.xml`, and restart the manager.

Richer rules (alert on unusual export rates, cross-correlate `dossier.denied` with same actor, etc.) are out of scope for this README — they belong in the SIEM team's rulebook.

### Testing locally

Two levels of local test, from cheapest to most realistic:

**Level 1 — just the file, no Wazuh at all.** Enable the audit block in `config.yaml` with `log_path: "/tmp/dossier_audit/audit.json"`, create the directory (`mkdir -p /tmp/dossier_audit`), start the app, drive a request, and inspect the file with `tail -F /tmp/dossier_audit/audit.json | jq .`. This verifies the application side end-to-end: are events being emitted at the right moments, with the right actor, target, and outcome? Every line should be a complete single-line JSON object that `jq` parses without complaint. 90% of audit bugs are caught at this level.

**Level 2 — local Wazuh agent, no manager.** Install only the agent and point it at your audit file, then use the `wazuh-logtest` replay tool to confirm the agent correctly parses each event shape through its decoder chain. The agent doesn't need to actually forward anywhere; `wazuh-logtest` runs the decoder pipeline locally and prints what would have been sent. On Ubuntu/Debian:

```bash
curl -sO https://packages.wazuh.com/4.x/apt/pool/main/w/wazuh-agent/wazuh-agent_4.7.0-1_amd64.deb
sudo WAZUH_MANAGER="localhost" dpkg -i wazuh-agent_*.deb
sudo systemctl enable --now wazuh-agent
```

Add the `<localfile>` block from the "Host setup" section above to `/var/ossec/etc/ossec.conf`, create `/var/log/dossier/` with the permissions shown there, and restart the agent. Then:

```bash
sudo /var/ossec/bin/wazuh-logtest
# Paste a single NDJSON line from your audit file.
# Output shows the JSON decoding, extracted fields, and rule matches.
```

**Level 3 — full Wazuh stack.** Only necessary when you want to see events in the dashboard, build visualizations, or test alert rules. The single-node Docker compose is the fastest path:

```bash
git clone https://github.com/wazuh/wazuh-docker.git
cd wazuh-docker/single-node
docker compose -f generate-indexer-certs.yml run --rm generator
docker compose up -d
```

Takes a couple of minutes and a few GB of RAM. Dashboard on `https://localhost:443`, default login `admin` / `SecretPassword` (change immediately). After pointing the agent's `WAZUH_MANAGER` at `127.0.0.1` and restarting it, your audit events reach the manager — but **they will not appear in the dashboard yet**. Two things still need to happen:

1. **Custom rules on the manager.** Wazuh only indexes events that match a rule; unmatched events are silently dropped from the alert pipeline. Drop the rule block below into `/var/ossec/etc/rules/local_rules.xml` on the manager and run `sudo systemctl restart wazuh-manager`:

    ```xml
    <group name="dossier,audit">

      <rule id="100201" level="3">
        <decoded_as>json</decoded_as>
        <field name="component">audit</field>
        <field name="event_action">dossier.read</field>
        <description>Dossier read by $(actor.name) on $(target.id)</description>
      </rule>

      <rule id="100202" level="5">
        <decoded_as>json</decoded_as>
        <field name="component">audit</field>
        <field name="event_action">dossier.exported</field>
        <description>Dossier exported by $(actor.name): $(target.id)</description>
      </rule>

      <rule id="100203" level="8">
        <decoded_as>json</decoded_as>
        <field name="component">audit</field>
        <field name="event_action">dossier.denied</field>
        <description>Dossier access denied: $(actor.name) on $(target.id)</description>
      </rule>

      <rule id="100205" level="3">
        <decoded_as>json</decoded_as>
        <field name="component">audit</field>
        <field name="event_action">dossier.created</field>
        <description>Dossier created by $(actor.name): $(target.id)</description>
      </rule>

      <rule id="100206" level="3">
        <decoded_as>json</decoded_as>
        <field name="component">audit</field>
        <field name="event_action">dossier.updated</field>
        <description>Dossier updated by $(actor.name): $(target.id)</description>
      </rule>

    </group>
    ```

    Each rule is self-contained. Two `<field>` selectors per rule gate the match: `component: audit` (injected by the agent's `<label>` block) identifies the stream, and `event_action` picks the specific event type within it.

    **Don't match on `@source` — it doesn't work reliably.** The `<label key="@source">dossier_engine</label>` value appears in decoded output and in alert JSON, but Wazuh's `<field>` selector can't reliably match on field names starting with `@`. Rules using `<field name="@source">dossier_engine</field>` appear to work for some events but mysteriously fail for others. Match on `component` (plain name) instead. This was verified via `wazuh-logtest` on 4.14.2: identical events where `<field name="@source">` fails, `<field name="component">` matches.

    **Why `event_action` and not `action`?** Wazuh reserves 13 static-field names: `user`, `srcip`, `dstip`, `srcport`, `dstport`, `protocol`, **`action`**, `id`, `url`, `data`, `extra_data`, `status`, `system_name`. Using any of those as top-level JSON keys either fails to match in `<field name="...">` rules, or (for `action` specifically) causes a fatal `Field 'action' is static` rule-load error that prevents `local_rules.xml` from loading at all. The app emits `event_action` to sidestep this.

    **Flat rules, not parent/child.** The more elegant pattern — one parent rule at level 2 matching the stream, child rules via `<if_sid>` matching specific actions — fires inconsistently in practice: identical-shape events chain through for some `event_action` values and silently bypass the parent for others. Flattening each rule into its own complete match spec avoids the issue at the cost of a few repeated lines. Individual child levels are starting points — tune to your SIEM team's alert philosophy. Keep them at level ≥ 3 so they pass Wazuh's default `log_alert_level` threshold.

    To verify a rule matches against a real production event, grab one from `/var/ossec/logs/archives/archives.json` (with archives enabled via `<logall_json>yes</logall_json>` in the manager's `<global>` block), extract the `full_log` string, and pipe it through `wazuh-logtest`:

    ```bash
    cat > /tmp/test_event.json <<'EOF'
    {"event_action":"dossier.denied","actor":{"id":"u1","name":"Test"},"target":{"type":"Dossier","id":"d1"},"outcome":"denied","dossier_id":"d1","reason":"no access","@timestamp":"2026-04-16T12:00:00.000000+00:00","@source":"dossier_engine","component":"audit"}
    EOF
    cat /tmp/test_event.json | sudo /var/ossec/bin/wazuh-logtest
    ```

    Phase 3 should show `Rule id: '100203' (level 8)`. Turn archives off afterwards (`<logall_json>no</logall_json>`) — they double indexer storage use.

2. **Find the events in the right dashboard view.** On Wazuh 4.9 and later, the left-hand sidebar has **Explore** (the data-query section) rather than the legacy "Modules" entry. Expand Explore → **Discover** (also shown as "Threat Hunting" or "Events" depending on minor version) to see your audit alerts. Select the `wazuh-alerts-*` index pattern at the top-left, widen the time picker to "Last 24 hours," then filter by `rule.groups: audit` to isolate the dossier-engine stream, or by `event_action: dossier.exported` to narrow further.

**Seeing nothing in Threat Hunting?** Three layers to check, in order:

- `sudo tail /var/ossec/logs/ossec.log | grep audit.json` on the **agent host** — confirms the agent is reading the file. You want a line like `Analyzing file: '/var/log/dossier/audit.json'`.
- `sudo tail /var/ossec/logs/archives/archives.json` on the **manager** (requires `<logall_json>yes</logall_json>` in the manager's `ossec.conf` `<global>` block) — confirms events are reaching the manager at all, regardless of rules.
- Only if both of those check out, then it's a rule-matching or indexing issue — check that `wazuh-alerts-*` index exists in Stack Management → Index Patterns, and that your rule IDs don't collide with another file in `/var/ossec/etc/rules/`.

A common pitfall during dev: `> /var/log/dossier/audit.json` truncates the file, which leaves the agent's saved read-position past the new end-of-file. The agent then stops reading until you restart it (`sudo systemctl restart wazuh-agent`). This only happens during manual truncation — the application's `RotatingFileHandler` does proper rotation, which the agent handles correctly.

### Rotation and disk use

At 100 MB × 10 files = 1 GB maximum disk footprint. At a typical load of ~1 KB per event and ~10 events per dossier interaction (create + a few reads + maybe an export), one MB holds roughly 1000 interactions, so a 100 MB file holds ~100 000 interactions. A single rotated file should cover weeks in normal operation.

Rotation is triggered by size, not time. If volume spikes (e.g. a bulk-processing job), rotations accelerate; retention of *all* events ultimately depends on Wazuh consuming from the file faster than it rotates, so the 1 GB ceiling is a soft backpressure signal rather than a hard retention guarantee. Monitoring for rotation frequency in Wazuh lets you catch anomalies before the oldest rotated file gets dropped.

### PII and minimisation

Audit events use UUIDs as identifiers (`actor.id`, `dossier.id`, `target.id`), not applicant names or national register numbers. This keeps the audit log one join away from the PII — queries that need human-readable context dereference through the database at query time, not at write time. The benefit is that subject access requests and data-deletion requests don't require touching the audit log at all; the join target changes, the audit events don't.

`actor.name` is a display-name (typically from JWT claims), not sensitive personal data — it's always a name *the actor chose to present*. If your JWT contains sensitive name data, scrub it before it reaches `emit_audit()`.

### Observability

The audit logger is deliberately isolated from the rest of the logging tree: `propagate = False` on `dossier.audit`. This prevents audit events from bubbling up to the root logger and ending up in stderr, Sentry, or your general log aggregator (which would each have the wrong retention policy and, in Sentry's case, the wrong trust boundary — you don't want audit events triggering alerts or being visible to general developers).

Failures to write to the audit file are swallowed (so the user request never fails because the disk filled), but the underlying `RotatingFileHandler` logs its own warning via the root logger when it can't write, which your standard log pipeline will surface.

## Data Migrations

The engine includes a framework for one-shot data migrations that operate on existing entity content. Migrations are executed as PROV activities (one `systemAction` per dossier) so the transformation itself is recorded in the provenance graph. The framework is idempotent: once applied to a dossier, a `system:note` entity with the migration UUID is created, and re-running skips that dossier.

Each migration is a `DataMigration` instance declaring the target type, a transform function, and optional filter/workflow scoping. Because the disjoint invariant forbids listing the same entity in both `used` and `generated`, migrations only `generate` new versions with a `derivedFrom` reference to the prior version — no `used` items.

### Writing a migration

Add new migrations to the END of the `MIGRATIONS` list in `dossier_toelatingen_repo/dossier_toelatingen/data_migrations/__init__.py`. Never reorder or remove existing entries.

```python
from dossier_engine.migrations import DataMigration

def _add_classificatie(content: dict) -> dict | None:
    if "classificatie" in content:
        return None  # already has the field, no-op
    return {**content, "classificatie": None, "urgentie": None}

MIGRATIONS = [
    DataMigration(
        id="f47ac10b-58cc-4372-a567-0e02b2c3d479",  # new UUID per migration
        message="Add classificatie and urgentie to aanvraag (v1→v2 backfill)",
        target_type="oe:aanvraag",
        transform=_add_classificatie,
        workflow="toelatingen",
    ),
]
```

### Running migrations

```bash
# Dry run — shows what would change, writes nothing
python -m dossier_toelatingen.data_migrations --dry-run

# Apply pending migrations
python -m dossier_toelatingen.data_migrations

# Custom config path
python -m dossier_toelatingen.data_migrations --config path/to/config.yaml
```

Each migration runs in its own per-dossier transaction, so a failure on one dossier does not roll back others. The summary output reports applied/skipped/errors per migration.

## Test Flows

The test script (`test_requests.sh`) creates 9 dossiers:

- **D1 — Brugge, RRN aanvrager.** `dienAanvraagIn` → `neemBeslissing(onvolledig)` → `vervolledigAanvraag` → `neemBeslissing(goedgekeurd)`. Exercises the happy path with direct decisions and file upload/download URL injection.

- **D2 — Gent, KBO aanvrager, separate signer.** `dienAanvraagIn` → `doeVoorstelBeslissing(onvolledig)` → `tekenBeslissing` (signs) → `vervolledigAanvraag` → `bewerkAanvraag` → `doeVoorstelBeslissing(goedgekeurd)` → `tekenBeslissing` (declines) → `doeVoorstelBeslissing(goedgekeurd)` → `tekenBeslissing` (signs). Exercises the proposal-and-sign flow, decline/retry, and the task-cancellation end-to-end flow verified by D7.

- **D3 — Batch auto-resolve.** `dienAanvraagIn` → BATCH[`bewerkAanvraag` + `doeVoorstelBeslissing`]. The second activity in the batch auto-resolves the revised aanvraag from the first activity's generated entities via the repo flush between batch steps.

- **D4 — Batch explicit ref.** Same shape as D3 but with an explicit `used` reference between the two batched activities, exercising the non-auto-resolve path.

- **D5 — Derivation rules (negative tests).** Six negative cases covering missing derivation chain, stale derivedFrom pointers, cross-entity derivation, unknown parent versions, and two flavors of the disjoint-invariant check: listing the same logical entity in both `used` and `generated` fails with `422 used_generated_overlap`, and the same rule applies symmetrically to external URIs — an external URI that appears in both blocks of the same activity is rejected with the same `kind: "external"` overlap payload. See `invariants.enforce_used_generated_disjoint` and `test_invariants.py::test_external_overlap_by_uri` for the authoritative behaviour.

- **D6 — Stale used + `oe:neemtAkteVan`.** Positive and negative paths for acknowledging newer versions of an entity the activity chose not to revise. Uses `doeVoorstelBeslissing` (a read-only activity that inspects the aanvraag) as the test vehicle because stale-used semantics only apply to activities that inspect an entity they don't themselves revise.

- **D7 — Task cancellation.** Verifies that D2's `trekAanvraagIn` scheduled task (scheduled when `doeVoorstelBeslissing` returned `onvolledig`) gets cancelled by `vervolledigAanvraag` — the end-to-end cancel-on-activity-fires flow.

- **D8 — Schema versioning.** Exercises per-activity `new_version` / `allowed_versions` declarations. A v1 aanvraag is revised to v2 by an activity that declares `allowed_versions: [v1, v2]`. A subsequent activity that declares `allowed_versions: [v2]` is then tried against the v1 parent and rejected with `422 unsupported_schema_version`.

- **D9 — Tombstone.** Full shape check: the tombstone activity accepts one or more versions of a single logical entity in `used`, nulls their content, stamps `tombstoned_by`, and produces a generated replacement row. GETs on a tombstoned version 301-redirect to the live replacement. Re-tombstoning a later version is allowed.

- **D10 — Exception grants.** End-to-end legally-audited bypass lifecycle: a beheerder grants a `system:exception` for `oe:trekAanvraagIn` (blocked by status rule in this dossier), the read-side eligibility computation surfaces the activity in `allowedActivities` with an `exempted_by_exception` field naming the exception's version_id (so frontends can render "via exception" badging or confirmation prompts before consuming the single-use bypass), the aanvrager then runs the blocked activity and succeeds via the bypass, the engine auto-injects a `consumeException` side-effect that revises the exception to `status: consumed`, the beheerder re-grants via a revision of the same logical entity (same `entity_id`), and finally retracts it — ending with `status: cancelled`. Exercises `check_exceptions`, used-list injection, side-effect auto-injection, per-activity uniqueness, activity-field immutability across revisions, and bidirectional read/write consistency (eligibility surfacing matches bypass execution). The whole exception mechanism (model, three activities, validator, two handlers, bypass phase, eligibility surfacing) lives in the engine — toelatingen opts in via a 3-line `exceptions:` block in its workflow YAML.

Expected result: **36 OKs, 0 failures.**

### Running the full test suite from scratch

Both the dossier API (port 8000) and the file service (port 8001) must be up, and the database must be empty. The test suite is order-dependent on fresh state (fixed entity/activity UUIDs per dossier). The procedure is:

```bash
# 1. Kill any surviving uvicorn processes
pkill -9 -f uvicorn
sleep 1

# 2. Wipe the database (Postgres schema) and the file storage.
#    On the next API launch, Alembic will recreate all tables from
#    the initial migration. The file storage lives next to the config
#    file inside the dossier_app package.
psql -h 127.0.0.1 -U dossier dossiers \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO dossier;"
rm -rf /home/claude/toelatingen/dossier_app_repo/dossier_app/file_storage

# 3. Launch both services. Launch cwd doesn't matter because every
#    project is pip-installed (editable). /tmp is a convenient
#    neutral cwd that avoids the repo-root namespace-package
#    collision (see TROUBLESHOOTING.md).
cd /tmp
setsid python3 -m uvicorn dossier_app.main:app --port 8000 \
  </dev/null >/tmp/dossier.log 2>&1 &
setsid python3 -m uvicorn file_service.app:app --port 8001 \
  </dev/null >/tmp/files.log 2>&1 &
sleep 6   # allow time for Alembic migrations to run on first start

# 4. Confirm both services are alive and the DB is ready
curl -s http://localhost:8000/health        # {"status":"ok"}
curl -s http://localhost:8000/health/ready   # {"status":"ready"}
curl -s http://localhost:8001/health         # {"status":"ok"}

# 5. Run the suite
bash /home/claude/toelatingen/test_requests.sh > /tmp/test_run.log 2>&1
grep -c "OK:" /tmp/test_run.log       # should print: 25
```

Environment troubleshooting notes (deleted-inode gotchas, process-group kills inside agentic tool wrappers, `python-multipart` prerequisite) live in `TROUBLESHOOTING.md`.

## POC Users

| Username | Name | Roles |
|---|---|---|
| `claeyswo` | Wouter Claeys | beheerder |
| `jan.aanvrager` | Jan Peeters | RRN 85010100123 |
| `firma.acme` | ACME BV | KBO 0123456789 |
| `marie.brugge` | Marie Vandenbroeck | behandelaar, beslisser, gemeente Brugge |
| `benjamma` | Matthias Benjamins | behandelaar, gemeente OE |
| `sophie.tekent` | Sophie Marchand | beslisser, behandelaar, gemeente OE |

## Frontend

A small Vue 3 frontend (`dossier_frontend/`) showcases the activity workflow end-to-end against a running backend. It's a POC, not production — no real authentication, no i18n framework, no file upload — but it covers the main user flows and demonstrates how to consume the engine's API.

### What it demonstrates

1. **Role-aware access control.** Switch between four POC profiles (beheerder, two kinds of aanvrager, behandelaar) from the top-right badge. The dashboard, the list of "next actions" on a dossier, and the availability of the archive-export button all change with the active profile. The backend enforces access regardless; the frontend just reads what the backend returns.
2. **Activity-driven workflow.** The dossier detail view presents the current dossier state (status pill, content entities, chronological activity timeline) alongside a panel of activities the active user can execute next. Clicking one opens its form inline — no separate edit pages — reinforcing the "everything is an activity" design.
3. **The main flows in the toelatingen workflow.** `dienAanvraagIn` (new-application form for aanvragers), `bewerkAanvraag` (behandelaar edits, producing a new entity version with `derivedFrom`), `vervolledigAanvraag` (aanvrager completes after an "onvolledig" beslissing — same shape as bewerk, different role and required status), `neemBeslissing` (generates `oe:beslissing` + `oe:handtekening` atomically). The archive export as a PDF/A download is available on the detail view for beheerders.

### Design direction

Restrained, institutional, editorial. Source Serif 4 display paired with IBM Plex Sans for UI; single warm-ochre accent (evoking heritage metalwork rather than startup purple); rectangular corners and hairline 1px rules throughout; subtle SVG paper-noise background for depth. The palette, typography, and component density were chosen to match civil-service document handling rather than generic SaaS aesthetics.

### Run it locally

Backend first (both services):

```bash
# Terminal 1: file service
cd file_service_repo && python3 -m uvicorn file_service.app:app --port 8001

# Terminal 2: dossier app
cd toelatingen && python3 -m uvicorn dossier_app.main:app --port 8000
```

Then frontend:

```bash
cd dossier_frontend
npm install
npm run dev
```

Vite serves on `http://localhost:5173` and proxies `/api/*` to `http://localhost:8000` (see `vite.config.js`) so the frontend can make same-origin calls without CORS configuration. For a production build:

```bash
npm run build      # output in dist/
npm run preview    # serve the build locally
```

### Known limitations

- **No file upload.** The `dienAanvraagIn` form submits with an empty `bijlagen` array. The `neemBeslissing` form generates a placeholder `file_id` for the required `brief` field, which passes backend validation (the `FileId` type is a tagged string; the engine doesn't check the file's existence against file_service), but the `brief_download_url` that the engine injects into the GET response will 404 because no actual PDF was uploaded. Real file handling would require implementing the signed-upload-URL dance from `routes/files.py`.
- **No PROV timeline visualization.** The engine already exposes an interactive provenance graph at `GET /dossiers/{id}/prov` (HTML/SVG), rendered by a Jinja template in `dossier_engine/templates/prov_timeline.html`. The frontend could embed or link to that page rather than reimplementing it — the activity timeline in the detail view is a simpler text version sufficient for most review work.
- **Search is stubbed.** The backend's `GET /dossiers` is a basic listing, not a real search endpoint. Production deployments would add workflow-specific endpoints that query Elasticsearch (see the `post_activity_hook` pattern).
- **"Login" is just a user picker.** The current profile persists to `localStorage`, and every API call sends `X-POC-User: <username>`. The backend's `POCAuthMiddleware` looks up user definitions from `workflow.yaml`. This is the same auth model the backend tests use and is not suitable for production — real deployments would swap in JWT auth at the middleware layer.

### Structure

```
dossier_frontend/
├── index.html
├── package.json           # Vue 3, Vue Router, Pinia, Tailwind, Vite
├── vite.config.js         # /api proxy to :8000
├── tailwind.config.js     # custom palette (ink/paper/brass + status)
├── postcss.config.js
└── src/
    ├── main.js
    ├── App.vue            # top bar + router view + footer
    ├── router.js          # 4 routes, auth guard
    ├── api.js             # fetch wrapper, X-POC-User injection
    ├── styles.css         # Tailwind + base typography + component primitives
    ├── stores/
    │   └── auth.js        # current POC user, persisted to localStorage
    ├── views/
    │   ├── LoginView.vue         # profile picker
    │   ├── DashboardView.vue     # list of accessible dossiers
    │   ├── DossierDetailView.vue # two-column detail + inline activity forms
    │   └── NewAanvraagView.vue   # dienAanvraagIn form
    └── components/
        ├── UserBadge.vue              # top-right user dropdown
        ├── StatusPill.vue             # coloured status label
        ├── ActivityCard.vue           # timeline entry
        ├── EntityDisplay.vue          # generic entity content renderer
        ├── ReviseAanvraagForm.vue     # shared form for bewerkAanvraag + vervolledigAanvraag
        └── NeemBeslissingForm.vue     # decision form (goedgekeurd / afgekeurd / onvolledig)
```

### Demo script

A five-minute walk through the full workflow:

1. Open the app, pick **Jan Peeters** (aanvrager). Dashboard is empty; click "Nieuwe aanvraag".
2. Fill in the form — leave **Brugge** as the gemeente so Marie can later take the decision. Submit.
3. You land on the new dossier's detail view. Status: *ingediend*. The aanvraag content is rendered; the activity timeline shows the `dienAanvraagIn` plus any system-triggered side-effects (`duidVerantwoordelijkeOrganisatieAan`, `setSystemFields`).
4. Via the user-badge dropdown, switch to **Marie Vandenbroeck** (behandelaar Brugge). The dashboard now shows the new dossier in her workbench. Open it.
5. The "Volgende stap" panel now offers *Bewerk aanvraag* and *Neem beslissing*. Click *Neem beslissing*, pick **Onvolledig**, confirm. The dossier transitions to *aanvraag_onvolledig*.
6. Switch back to **Jan**. Open the dossier — a new action, *Vervolledig aanvraag*, is now available. Click it, edit the onderwerp to add the requested details, and submit. The status returns to *ingediend*.
7. Switch back to **Marie**. *Neem beslissing* is available again. This time pick **Goedgekeurd**. The dossier transitions to *Verleend*.
8. Switch to **Wouter Claeys** (beheerder). Open the dossier. The archive-export button appears in the header. Click it — a PDF/A archive of the entire dossier downloads, with the full PROV trail embedded as a PROV-JSON attachment.

This sequence exercises the onvolledig → vervolledig → goedgekeurd loop, which demonstrates the core value of the PROV model: the final archive contains the *entire* correspondence, including the original submission, the incomplete decision, Jan's revision, and the final goedgekeuring — all cryptographically linked via `derivedFrom`.

## Key Design Decisions

- **Activity-driven** — single endpoint pattern `PUT /{workflow}/dossiers/{id}/activities/{id}/{type}`, all state changes are activities.
- **Two URL families** — workflow-scoped routes (`/{workflow}/dossiers/...`) for activities, search, reference data, and validation; workflow-agnostic routes (`/dossiers/{id}/...`) for reads, PROV, archive, and entity access. A dossier's IRI (`https://id.erfgoed.net/dossiers/{id}`) is its identity; the workflow is an implementation detail.
- **W3C PROV** is the provenance model — every entity version has a `wasGeneratedBy` activity, every revision has a `wasDerivedFrom` chain. PROV handles *who did what when*; domain relations handle *what relates to what*.
- **Domain relations are separate from PROV** — semantic links (`oe:betreft`, `oe:valtOnder`, `oe:gerelateerd_aan`) live in the `domain_relations` table, not in the PROV graph. Both process-control and domain relations come through the same `relations` field on the activity request; the engine routes by `kind` in the YAML.
- **All IDs client-generated** — PUTs are idempotent, safe for retry.
- **Entity ref format**: `prefix:type/entity_id@version_id` in the API; expanded to full IRIs (`https://id.erfgoed.net/dossiers/{did}/entities/...`) before storage in domain relations.
- **Append-only with redaction** — activity and entity rows are immutable except for tombstone, which NULLs content in place and leaves everything else (so the PROV graph keeps its shape). Domain relations are superseded (not deleted) — the old row stays with `superseded_by_activity_id` for audit.
- **Tasks are entities** — `system:task` with version lifecycle (scheduled → completed/cancelled).
- **Typed entity access** — handlers use `context.get_singleton_typed("oe:type")` for Pydantic model instances on singleton entity types. For multi-cardinality types, use `context.get_entities_latest` to get the full list.
- **Search delegated to Elasticsearch** — plugin provides `post_activity_hook` and search routes.
- **External entities persisted** — external URIs stored as entities with type `"external"`, full PROV trail.
- **Reference data is YAML-declared, served from memory** — no DB hit, sub-millisecond. Field validators are plugin-registered async callables, called between activities for fast feedback.
- **Pipeline phases are small and documented** — every phase function in `engine/pipeline/` declares its Reads/Writes contract, which makes individual phases unit-testable against fixture `ActivityState` objects without needing the full HTTP stack.
- **Two-tier access** — `check_dossier_access` gates business views (filtered per user); `check_audit_access` gates full-record views (`/prov`, `/prov/graph/columns`, `/archive`). A role in `global_audit_access` or the dossier's `audit_access` list is required for audit views; ordinary dossier access is not enough.
- **Workflow constants typed and env-overridable** — each plugin declares a Pydantic `BaseSettings` subclass (`context.constants.x` from handlers, `plugin.constants.x` from hooks). Precedence: env vars > `workflow.yaml` > class defaults. Secrets go only via env vars. See `dossier_toelatingen/constants.py`.
- **Qualified activity names** — activity names in YAML and URLs are prefixed with the plugin's IRI prefix (`oe:dienAanvraagIn`), so cross-plugin workflows can coexist without collision. The engine normalizes bare names and matches by local name where appropriate for backward-friendly plugin code.
- **Namespace registry** — plugins declare the vocabularies they use (`namespaces:` block in workflow.yaml). Built-in prefixes (`prov`, `xsd`, `rdf`, `rdfs`) are always available; unknown prefixes fail at load time rather than producing bogus IRIs at runtime.
- **Split-style activity hooks (opt-in)** — alongside the classic handler-returns-everything style, activities can declare `status_resolver: "name"` and/or `task_builders: [...]` in YAML to route those concerns through dedicated single-responsibility functions. Makes workflow side effects visible in the YAML without opening Python. Engine enforces "exactly one source per concern" — declaring a split hook forbids the handler from also returning that field. See `docs/plugin_guidebook.md` "Split-style hooks" for decision criteria.

## Adding a New Workflow

1. Create a new plugin package (copy `dossier_toelatingen_repo/` as template)
2. Define entities, `workflow.yaml` (including `reference_data:` and `relation_types:`), handlers, validators, field_validators, tasks, relation_validators
3. Add to `config.yaml`:
   ```yaml
   plugins:
     - dossier_toelatingen
     - dossier_inzageaanvragen
   ```
4. Restart — new routes appear automatically:
   - `/{workflow}/dossiers/...` — workflow-scoped activity, search, reference, validation endpoints
   - Typed activity endpoints per client-callable activity
   - Reference data served from the workflow's YAML
   - Field validators registered by the plugin

See `dossiertype_template.md` for the complete workflow definition reference.
