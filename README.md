# Dossier API — Toelatingen beschermd erfgoed

PROV-based, activity-driven dossier management API built with FastAPI.

## Quick Start

```bash
# Install dependencies
pip install -e .

# Run the API
uvicorn main:app --reload

# Open Swagger docs
open http://localhost:8000/docs
```

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  gov_dossier_engine (framework)                              │
│                                                              │
│  ✓ Generic activity handler (one endpoint for everything)    │
│  ✓ Authorization engine (direct, scoped, entity-derived)     │
│  ✓ Workflow validation (requirements, forbidden, statuses)   │
│  ✓ Side effect execution (wasInformedBy chain)               │
│  ✓ Status derivation (from activity history, never stored)   │
│  ✓ Route generator (typed docs per workflow)                 │
│  ✓ Plugin interface                                          │
│                                                              │
│  No business logic. No domain-specific code.                 │
├──────────────────────────────────────────────────────────────┤
│  gov_dossier_toelatingen (plugin)                            │
│                                                              │
│  ✓ workflow.yaml (activities, entities, roles, rules)        │
│  ✓ entities.py (Pydantic models)                             │
│  ✓ handlers/ (system activity logic)                         │
│  ✓ validators/ (custom business rules)                       │
│  ✓ tasks/ (async task handlers)                              │
├──────────────────────────────────────────────────────────────┤
│  gov_dossier_app (deployment)                                │
│                                                              │
│  config.yaml + main.py                                       │
└──────────────────────────────────────────────────────────────┘
```

## API Endpoints

### Generic
- `PUT /dossiers/{dossier_id}/activities/{activity_id}` — Execute any activity
- `GET /dossiers/{dossier_id}` — Get dossier details (status, entities, history)
- `GET /dossiers` — List dossiers

### Per-workflow (typed, for docs)
- `PUT /dossiers/{id}/activities/{id}/dienAanvraagIn` — Dien aanvraag in
- `PUT /dossiers/{id}/activities/{id}/bewerkAanvraag` — Bewerk aanvraag
- `PUT /dossiers/{id}/activities/{id}/vervolledigAanvraag` — Vervolledig aanvraag
- `PUT /dossiers/{id}/activities/{id}/doeVoorstelBeslissing` — Doe voorstel beslissing
- `PUT /dossiers/{id}/activities/{id}/tekenBeslissing` — Teken beslissing

All typed routes call the same generic handler internally.

## Example Flow

### 1. Submit an application

```bash
curl -X PUT http://localhost:8000/dossiers/11111111-1111-1111-1111-111111111111/activities/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d '{
    "type": "dienAanvraagIn",
    "workflow": "toelatingen",
    "role": "oe:aanvrager",
    "used": [
      {
        "entity": "oe:aanvraag/22222222-2222-2222-2222-222222222222@33333333-3333-3333-3333-333333333333",
        "content": {
          "onderwerp": "Restauratie gevelbekleding",
          "handeling": "renovatie",
          "aanvrager": { "rrn": "85010100123" },
          "gemeente": "antwerpen",
          "object": "https://id.erfgoed.net/erfgoedobjecten/12345"
        }
      },
      {
        "entity": "https://id.erfgoed.net/erfgoedobjecten/12345"
      }
    ]
  }'
```

### 2. Check dossier status

```bash
curl http://localhost:8000/dossiers/11111111-1111-1111-1111-111111111111 \
  -H "X-POC-User: claeyswo"
```

### 3. Propose a decision

```bash
curl -X PUT http://localhost:8000/dossiers/11111111-1111-1111-1111-111111111111/activities/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d '{
    "type": "doeVoorstelBeslissing",
    "role": "oe:behandelaar",
    "used": [
      {
        "entity": "oe:beslissing/44444444-4444-4444-4444-444444444444@55555555-5555-5555-5555-555555555555",
        "content": {
          "beslissing": "goedgekeurd",
          "datum": "2026-03-26T10:00:00Z",
          "object": "https://id.erfgoed.net/erfgoedobjecten/12345",
          "brief": "https://dms.example.com/brieven/brief-001"
        }
      }
    ]
  }'
```

### 4. Sign the decision

```bash
curl -X PUT http://localhost:8000/dossiers/11111111-1111-1111-1111-111111111111/activities/cccccccc-cccc-cccc-cccc-cccccccccccc \
  -H "Content-Type: application/json" \
  -H "X-POC-User: claeyswo" \
  -d '{
    "type": "tekenBeslissing",
    "role": "oe:ondertekenaar",
    "used": [
      {
        "entity": "oe:handtekening/66666666-6666-6666-6666-666666666666@77777777-7777-7777-7777-777777777777",
        "content": {
          "getekend": true
        }
      }
    ]
  }'
```

## Key Design Decisions

- **All IDs client-generated** — PUTs are idempotent, safe for retry
- **Entity ref format**: `prefix/id@version` (e.g. `oe:aanvraag/uuid@uuid`)
- **Status derived**, never stored — query over activity history
- **Side effects are proper PROV activities** with `wasInformedBy` links
- **Append-only tables** — no UPDATEs, no DELETEs, full audit trail
- **Content stored as JSONB**, validated by Pydantic on write
- **Functional roles ≠ technical roles** — mapped per activity in authorization rules

## POC Authentication

Pass `X-POC-User` header with a username from `poc_users` in workflow.yaml.

Current users:
- `claeyswo` — beheerder role (can do everything)

Add more users in `gov_dossier_toelatingen/workflow.yaml` under `poc_users`.

## Switching to PostgreSQL

1. Install asyncpg: `pip install asyncpg`
2. Update `gov_dossier_app/config.yaml`:
   ```yaml
   database:
     url: "postgresql+asyncpg://user:pass@localhost:5432/dossiers"
   ```
3. Restart the API

## Adding a New Workflow

1. Create a new plugin package (e.g. `gov_dossier_vergunningen/`)
2. Define entities, workflow.yaml, handlers
3. Add to `config.yaml`:
   ```yaml
   plugins:
     - gov_dossier_toelatingen
     - gov_dossier_vergunningen
   ```
4. Restart — new routes appear in Swagger automatically
