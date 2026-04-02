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
