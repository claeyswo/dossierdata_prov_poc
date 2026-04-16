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
│  → generated (schema versioning) → relations → validators    │
│  → tombstone shape → persistence → handler → side effects    │
│  → tasks → finalization                                      │
│                                                              │
│  Route modules under routes/:                                │
│  activities, dossiers, entities, files, access, prov         │
│                                                              │
│  ✓ Single activity endpoint (PUT, idempotent)                │
│  ✓ Batch endpoint (atomic multi-activity)                    │
│  ✓ Authorization (direct, scoped, entity-derived)            │
│  ✓ Workflow validation (requirements, forbidden, statuses)   │
│  ✓ Activity-level relations opt-in + plugin validators       │
│  ✓ Schema versioning (per-activity new + allowed versions)   │
│  ✓ Disjoint invariant (no overlap between used + generated)  │
│  ✓ Tombstone redaction (NULL content, row survives)          │
│  ✓ Side effects (recursive, wasInformedBy chain)             │
│  ✓ Status derivation (computed_status on activity rows)      │
│  ✓ Task system (4 types, entities with full PROV)            │
│  ✓ Worker (polls for due tasks, executes atomically)         │
│  ✓ Access control (dossier_access entity)                    │
│  ✓ PROV-JSON export                                          │
│  ✓ Interactive graph visualizations (timeline + columns)     │
│  ✓ Search integration hooks (Elasticsearch)                  │
│  ✓ Plugin interface                                          │
│                                                              │
│  No business logic. No domain-specific code.                 │
└──────────────────────────────────────────────────────────────┘
```

### Plugin internals (under `dossier_toelatingen_repo/`)

```
✓ workflow.yaml (activities, entities, roles, rules)
✓ entities.py (Pydantic models — typed entity access)
✓ handlers/ (system activity logic, conditional tasks)
✓ relation_validators/ (activity-level relation semantics)
✓ validators/ (custom business rules)
✓ tasks/ (type 2 recorded task handlers)
✓ pre_commit_hooks (synchronous validation/side effects, can veto an activity)
✓ post_activity_hook (search index updates, advisory, exceptions swallowed)
✓ search route (/dossiers/toelatingen/search)
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `PUT` | `/dossiers/{id}/activities/{id}/{type}` | Execute a typed activity |
| `PUT` | `/dossiers/{id}/activities/{id}` | Execute a generic activity |
| `PUT` | `/dossiers/{id}/activities` | Execute batch activities atomically |
| `GET` | `/dossiers/{id}` | Get dossier detail (filtered by access) |
| `GET` | `/dossiers` | List dossiers (stub) |
| `GET` | `/dossiers/toelatingen/search` | Workflow-specific search (ES stub) |
| `GET` | `/dossiers/{id}/entities/{type}` | All versions of an entity type |
| `GET` | `/dossiers/{id}/entities/{type}/{eid}` | All versions of a logical entity |
| `GET` | `/dossiers/{id}/entities/{type}/{eid}/{vid}` | Single entity version |
| `POST` | `/files/upload/request` | Mint a signed upload URL for the file service |
| `GET` | `/dossiers/{id}/prov` | PROV-JSON export |
| `GET` | `/dossiers/{id}/prov/graph/timeline` | Timeline visualization |
| `GET` | `/dossiers/{id}/prov/graph/columns` | Column layout visualization |
| `GET` | `/dossiers/{id}/archive` | PDF/A-3b archive (self-contained, with embedded PROV-JSON and bijlagen) |
| `GET` | `/health` | Liveness probe (always 200 if process is up) |
| `GET` | `/health/ready` | Readiness probe (checks DB connection, 503 if down) |

Graph query parameters: `?include_system_activities=true`, `?include_tasks=true`

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
# Create a dossier with dienAanvraagIn
curl -X PUT http://localhost:8000/dossiers/{dossier_id}/activities/{activity_id}/dienAanvraagIn \
  -H "Content-Type: application/json" \
  -H "X-POC-User: jan.aanvrager" \
  -d '{
    "workflow": "toelatingen",
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
    ]
  }'
```

Key concepts:
- `workflow` — only needed for the first activity (creates the dossier)
- `used` — references to existing entities or external URIs (read-only)
- `generated` — new entities with content (use `derivedFrom` for revisions). External URIs in `generated` don't need a `content` field; the engine auto-creates the external row.
- `relations` — generic activity→entity links under a named type (e.g. `oe:neemtAkteVan`). The type must be declared in the activity's YAML relations block, and a plugin validator enforces the semantics.
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

Tasks can be defined statically in YAML or appended conditionally by handlers at runtime. Scheduled tasks can be anchored to an entity via `anchor_entity_id`/`anchor_type`, which lets the engine locate the anchored entity even when the triggering activity didn't directly touch it — crucial when `cancel_if_activities` cancels a task based on a later activity that doesn't share the original scope.

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

The requeue writes a fresh revision of each dead-lettered task with `status = "scheduled"`, `attempt_count = 0`, and `next_attempt_at = null`. The original `scheduled_for` is preserved as the historical record of when the task was first queued, and `last_attempt_at` is preserved so operators can still see when the task last tried. All other task content (function name, anchor, target_activity, etc.) is carried forward unchanged.

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

## Dossier Archive (PDF/A-3b)

The `GET /dossiers/{id}/archive` endpoint produces a self-contained PDF/A-3b archive suitable for long-term (30+ year) retention. The archive contains:

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

- **D2 — Gent, KBO aanvrager, separate signer.** `dienAanvraagIn` → `doeVoorstelBeslissing(onvolledig)` → `tekenBeslissing` (signs) → `vervolledigAanvraag` → `bewerkAanvraag` → `doeVoorstelBeslissing(goedgekeurd)` → `tekenBeslissing` (declines) → `doeVoorstelBeslissing(goedgekeurd)` → `tekenBeslissing` (signs). Exercises the proposal-and-sign flow, decline/retry, and the anchor mechanism used by D7.

- **D3 — Batch auto-resolve.** `dienAanvraagIn` → BATCH[`bewerkAanvraag` + `doeVoorstelBeslissing`]. The second activity in the batch auto-resolves the revised aanvraag from the first activity's generated entities via the repo flush between batch steps.

- **D4 — Batch explicit ref.** Same shape as D3 but with an explicit `used` reference between the two batched activities, exercising the non-auto-resolve path.

- **D5 — Derivation rules (negative tests).** Five negative cases covering missing derivation chain, stale derivedFrom pointers, cross-entity derivation, unknown parent versions, and the disjoint-invariant check (an attempt to list the same entity in both `used` and `generated` fails with `422 used_generated_overlap`). Also a positive test for external URIs appearing in both `used` and `generated` in the same activity, which is allowed because externals are not PROV entities in the disjoint sense.

- **D6 — Stale used + `oe:neemtAkteVan`.** Positive and negative paths for acknowledging newer versions of an entity the activity chose not to revise. Uses `doeVoorstelBeslissing` (a read-only activity anchored to the aanvraag) as the test vehicle because stale-used semantics only apply to activities that inspect an entity they don't themselves revise.

- **D7 — Anchor mechanism.** Verifies that D2's `trekAanvraagIn` scheduled task is anchored to the aanvraag and gets cancelled by `vervolledigAanvraag` even though `vervolledigAanvraag` doesn't list the task in its used block.

- **D8 — Schema versioning.** Exercises per-activity `new_version` / `allowed_versions` declarations. A v1 aanvraag is revised to v2 by an activity that declares `allowed_versions: [v1, v2]`. A subsequent activity that declares `allowed_versions: [v2]` is then tried against the v1 parent and rejected with `422 unsupported_schema_version`.

- **D9 — Tombstone.** Full shape check: the tombstone activity accepts one or more versions of a single logical entity in `used`, nulls their content, stamps `tombstoned_by`, and produces a generated replacement row. GETs on a tombstoned version 301-redirect to the live replacement. Re-tombstoning a later version is allowed.

Expected result: **25 OKs, 0 failures.**

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

## Key Design Decisions

- **Activity-driven** — single endpoint pattern `PUT /dossiers/{id}/activities/{id}`, all state changes are activities.
- **W3C PROV** is the data model — no separate audit log, the PROV graph IS the system state.
- **All IDs client-generated** — PUTs are idempotent, safe for retry.
- **Entity ref format**: `prefix:type/entity_id@version_id`.
- **Append-only with redaction** — activity and entity rows are immutable except for tombstone, which NULLs content in place and leaves everything else (so the PROV graph keeps its shape).
- **Tasks are entities** — `system:task` with version lifecycle (scheduled → completed/cancelled).
- **Typed entity access** — handlers use `context.get_singleton_typed("oe:type")` for Pydantic model instances on singleton entity types. For multi-cardinality types, use `context.get_entities_latest` to get the full list.
- **Search delegated to Elasticsearch** — plugin provides `post_activity_hook` and search routes.
- **External entities persisted** — external URIs stored as entities with type `"external"`, full PROV trail.
- **Pipeline phases are small and documented** — every phase function in `engine/pipeline/` declares its Reads/Writes contract, which makes individual phases unit-testable against fixture `ActivityState` objects without needing the full HTTP stack.

## Adding a New Workflow

1. Create a new plugin package (copy `dossier_toelatingen_repo/` as template)
2. Define entities, workflow.yaml, handlers, validators, tasks, relation_validators
3. Add to `config.yaml`:
   ```yaml
   plugins:
     - dossier_toelatingen
     - dossier_vergunningen
   ```
4. Restart — new routes and search endpoints appear automatically

See `dossiertype_template.md` for the complete workflow definition reference.
