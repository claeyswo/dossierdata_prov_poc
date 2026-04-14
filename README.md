# Dossier API — PROV-based Activity-Driven Dossier Management

A W3C PROV-based, activity-driven dossier management API built with FastAPI. Every state change is an activity, every piece of data is a versioned entity, and the full provenance graph is queryable and visualizable.

## Quick Start

```bash
# Install dependencies
pip install -e .

# Run the API
cd gov_dossier_app
uvicorn main:app --reload

# Run the test flows
bash test_requests.sh

# Process scheduled tasks
python -m gov_dossier_engine.worker --once --config gov_dossier_app/config.yaml

# Open Swagger docs
open http://localhost:8000/docs
```

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  gov_dossier_engine (framework)                              │
│                                                              │
│  ✓ Single activity endpoint (PUT, idempotent)                │
│  ✓ Batch endpoint (atomic multi-activity)                    │
│  ✓ Authorization (direct, scoped, entity-derived)            │
│  ✓ Workflow validation (requirements, forbidden, statuses)   │
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
├──────────────────────────────────────────────────────────────┤
│  gov_dossier_toelatingen (plugin)                            │
│                                                              │
│  ✓ workflow.yaml (activities, entities, roles, rules)        │
│  ✓ entities.py (Pydantic models — typed entity access)       │
│  ✓ handlers/ (system activity logic, conditional tasks)      │
│  ✓ validators/ (custom business rules)                       │
│  ✓ tasks/ (type 2 recorded task handlers)                    │
│  ✓ post_activity_hook (search index updates)                 │
│  ✓ search route (/dossiers/toelatingen/search)               │
├──────────────────────────────────────────────────────────────┤
│  gov_dossier_app (deployment)                                │
│                                                              │
│  config.yaml + main.py                                       │
└──────────────────────────────────────────────────────────────┘
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
- `generated` — new entities with content (use `derivedFrom` for revisions)
- `informed_by` — optional, local UUID or cross-dossier URI
- All IDs are client-generated UUIDs — PUTs are fully idempotent

## Task System

Four types of tasks, all modeled as `system:task` entities with full PROV:

| Type | Kind | Description |
|---|---|---|
| 1 | `fire_and_forget` | Runs inline, no record |
| 2 | `recorded` | Worker executes function, completeTask records result |
| 3 | `scheduled_activity` | Worker executes an activity at a scheduled time |
| 4 | `cross_dossier_activity` | Worker executes an activity in another dossier |

Tasks can be defined statically in YAML or appended conditionally by handlers at runtime.

### Worker

```bash
python -m gov_dossier_engine.worker --once          # process all due tasks and exit
python -m gov_dossier_engine.worker                  # continuous polling (10s)
python -m gov_dossier_engine.worker --interval 5     # custom interval
```

## Test Flows

The test script (`test_requests.sh`) creates 4 dossiers:

**D1 — Brugge, RRN aanvrager:** dienAanvraagIn → neemBeslissing(onvolledig) → vervolledigAanvraag → neemBeslissing(goedgekeurd)

**D2 — Gent, KBO aanvrager, separate signer:** dienAanvraagIn → doeVoorstelBeslissing(onvolledig) → tekenBeslissing(sophie signs) → vervolledigAanvraag → bewerkAanvraag → doeVoorstelBeslissing(goedgekeurd) → tekenBeslissing(sophie declines) → doeVoorstelBeslissing(goedgekeurd) → tekenBeslissing(sophie signs)

**D3 — Batch auto-resolve:** dienAanvraagIn → BATCH[bewerkAanvraag + doeVoorstelBeslissing]

**D4 — Batch explicit ref:** dienAanvraagIn → BATCH[bewerkAanvraag + doeVoorstelBeslissing with explicit used ref]

**D5 — Derivation rule checks:** 5 negative cases for missing/stale/unknown/cross-entity derivation.

**D6 — Stale used references + `oe:neemtAkteVan`:** positive and negative acknowledgement paths.

**D7 — Anchor mechanism:** verifies that D2's `trekAanvraagIn` scheduled task is anchored to the aanvraag and cancelled by `vervolledigAanvraag`.

Expected result: **11 OKs, 0 failures.**

### Running the full test suite from scratch

Both the dossier API (port 8000) and the file service (port 8001) must be up, and the database must be empty. The test suite is order-dependent on fresh state (fixed entity/activity UUIDs per dossier). Use this procedure:

**Prerequisites** (check once per environment):

```bash
python3 -c "import multipart" 2>/dev/null || pip install python-multipart
```

The file service uses `multipart/form-data` uploads and will refuse to start without `python-multipart`, failing with `RuntimeError: Form data requires "python-multipart"` in `/tmp/files.log`. The base `pyproject.toml` deps don't include it, so a fresh environment needs this once.

```bash
# 1. Kill any surviving uvicorn processes (important — see note below)
pkill -9 -f uvicorn
sleep 1
ps -ef | grep uvicorn | grep -v grep   # must be empty

# 2. Wipe the database and file storage
rm -f  /home/claude/toelatingen/dossiers.db*
rm -rf /home/claude/toelatingen/file_storage

# 3. Launch both services, fully detached from the calling shell.
#    The `</dev/null` redirect is REQUIRED — without it, the child
#    inherits the parent's stdin and may be killed when the parent exits,
#    leaving zombie processes holding a deleted-inode database.
cd /home/claude/toelatingen
setsid python3 -m uvicorn main:app --port 8000 \
  </dev/null >/tmp/dossier.log 2>&1 &
setsid python3 -m uvicorn gov_file_service.app:app --port 8001 \
  </dev/null >/tmp/files.log 2>&1 &
sleep 4

# 4. Confirm both services are alive before running tests
curl -s -o /dev/null -w "dossier:%{http_code}\n" http://localhost:8000/dossiers
curl -s -o /dev/null -w "files:%{http_code}\n"   http://localhost:8001/health
# Expected: dossier:401 (auth challenge = alive), files:200 (health endpoint).
# Anything returning 000 means that service isn't actually listening — check
# /tmp/dossier.log or /tmp/files.log for the crash reason before proceeding.
# The dossier API has no /health endpoint, so 401 on /dossiers is the
# canonical liveness signal — it proves auth middleware is wired up.

# 5. Run the suite
bash /home/claude/toelatingen/test_requests.sh > /tmp/test_run.log 2>&1
grep -c "OK:" /tmp/test_run.log       # should print: 11
grep -cE "Traceback|AssertionError" /tmp/test_run.log  # should print: 0
```

**If you're running inside an agentic tool environment** (Claude Code, a notebook runner, or anything that wraps bash cells with a timeout and kills the cell's process group on return): don't bundle the `setsid` launches, the `sleep 4`, and the `curl` liveness check into a single cell. Split them into three: (1) kill+wipe, (2) launch the two services, (3) verify with `curl`. Even with `</dev/null` and `setsid`, some wrappers hang waiting on the backgrounded children to exit before returning, and the cell times out. When that happens the tool kills the cell's process group and takes the services down with it, leaving you with an empty on-disk `dossiers.db` and the symptoms described below.

**The deleted-inode gotcha:** if you `rm` the database while a uvicorn process is still running, the server holds the unlinked inode via an open fd and keeps serving the old state, while a new empty `dossiers.db` file appears on disk. Subsequent test runs then see an impossible mix of "empty DB on disk" and "fully-populated state via the API," with frozen timestamps and replayed idempotency responses. Always `pkill` **before** `rm`, and confirm `ps` is clean before restarting. You can verify the live process is pointing at the current file (not a deleted one) with:

```bash
PID=$(pgrep -f "uvicorn main:app")
ls -la /proc/$PID/fd/ | grep "\.db"
# Bad:  .../dossiers.db (deleted)
# Good: .../dossiers.db
```

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

- **Activity-driven** — single endpoint pattern `PUT /dossiers/{id}/activities/{id}`, all state changes are activities
- **W3C PROV** is the data model — no separate audit log, the PROV graph IS the system state
- **All IDs client-generated** — PUTs are idempotent, safe for retry
- **Entity ref format**: `prefix:type/entity_id@version_id`
- **Append-only** — no UPDATEs, no DELETEs, full audit trail
- **Tasks are entities** — `system:task` with version lifecycle (scheduled → completed/cancelled)
- **Typed entity access** — handlers use `context.get_typed("oe:type")` for Pydantic model instances
- **Search delegated to Elasticsearch** — plugin provides `post_activity_hook` and search routes
- **External entities persisted** — external URIs stored as entities with type `"external"`, full PROV trail

## Switching to PostgreSQL

1. Install asyncpg: `pip install asyncpg`
2. Update `gov_dossier_app/config.yaml`:
   ```yaml
   database:
     url: "postgresql+asyncpg://user:pass@localhost:5432/dossiers"
   ```
3. Restart the API

## Adding a New Workflow

1. Create a new plugin package (copy `gov_dossier_toelatingen/` as template)
2. Define entities, workflow.yaml, handlers, validators, tasks
3. Add to `config.yaml`:
   ```yaml
   plugins:
     - gov_dossier_toelatingen
     - gov_dossier_vergunningen
   ```
4. Restart — new routes and search endpoints appear automatically

See `dossiertype_template.md` for the complete workflow definition reference.
