# Dossier API — PROV-based Activity-Driven Dossier Management

A W3C PROV-based, activity-driven dossier management API built with FastAPI. Every state change is an activity, every piece of data is a versioned entity, and the full provenance graph is queryable and visualizable.

## Quick Start

```bash
# Install all five projects in editable mode (one-time setup)
pip install -e dossier_common/ -e file_service/ -e dossier_engine/ \
            -e dossier_toelatingen/ -e dossier_app/

# Run the dossier API (launch cwd does not matter — config paths
# resolve against the config file's own directory)
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

### Engine internals (under `dossier_engine/`)

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

### Plugin internals (under `dossier_toelatingen/`)

```
✓ workflow.yaml (activities, entities, roles, rules)
✓ entities.py (Pydantic models — typed entity access)
✓ handlers/ (system activity logic, conditional tasks)
✓ relation_validators/ (activity-level relation semantics)
✓ validators/ (custom business rules)
✓ tasks/ (type 2 recorded task handlers)
✓ post_activity_hook (search index updates)
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

Graph query parameters: `?include_system_activities=true`, `?include_tasks=true`

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
python -m dossier_engine.worker --once          # process all due tasks and exit
python -m dossier_engine.worker                  # continuous polling (10s)
python -m dossier_engine.worker --interval 5     # custom interval
```

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

# 2. Wipe the database and file storage. Both live next to the
#    config file inside the dossier_app package, not at the repo
#    root — that's where the engine's config-relative path resolver
#    anchors them.
rm -f  /home/claude/toelatingen/dossier_app/dossier_app/dossiers.db*
rm -rf /home/claude/toelatingen/dossier_app/dossier_app/file_storage

# 3. Launch both services. Launch cwd doesn't matter because every
#    project is pip-installed (editable) and config paths resolve
#    against the config file's own directory. /tmp is a convenient
#    neutral cwd that avoids the repo-root namespace-package
#    collision (see TROUBLESHOOTING.md).
cd /tmp
setsid python3 -m uvicorn dossier_app.main:app --port 8000 \
  </dev/null >/tmp/dossier.log 2>&1 &
setsid python3 -m uvicorn file_service.app:app --port 8001 \
  </dev/null >/tmp/files.log 2>&1 &
sleep 4

# 4. Confirm both services are alive
curl -s -o /dev/null -w "dossier:%{http_code}\n" http://localhost:8000/dossiers
curl -s -o /dev/null -w "files:%{http_code}\n"   http://localhost:8001/health
# Expected: dossier:401, files:200

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

## Switching to PostgreSQL

1. Install asyncpg: `pip install asyncpg`
2. Update `dossier_app/config.yaml`:
   ```yaml
   database:
     url: "postgresql+asyncpg://user:pass@localhost:5432/dossiers"
   ```
3. Restart the API

## Adding a New Workflow

1. Create a new plugin package (copy `dossier_toelatingen/` as template)
2. Define entities, workflow.yaml, handlers, validators, tasks, relation_validators
3. Add to `config.yaml`:
   ```yaml
   plugins:
     - dossier_toelatingen
     - dossier_vergunningen
   ```
4. Restart — new routes and search endpoints appear automatically

See `dossiertype_template.md` for the complete workflow definition reference.
