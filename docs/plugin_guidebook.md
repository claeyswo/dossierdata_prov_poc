# Plugin Guidebook

*A practical guide for building a new workflow plugin. Covers only the 10% you'll actually use. For the full engine internals, see [Pipeline Architecture](pipeline_architecture.md).*

## What a plugin is

A plugin is a Python package that defines a workflow — the activities, entities, statuses, roles, and business rules for one type of dossier. The engine handles everything generic (PROV, persistence, authorization, task scheduling, archiving). Your plugin handles everything specific to your domain.

The toelatingen plugin manages building permits for protected heritage. The inzageaanvragen plugin will manage public-records access requests. Both use the same engine, but their workflows are completely different.

## Your first plugin in 15 minutes

A minimal plugin has three files:

```
my_workflow_repo/
├── my_workflow/
│   ├── __init__.py      ← plugin registration
│   ├── workflow.yaml     ← activities, entities, statuses
│   └── entities.py       ← Pydantic models for entity content
└── setup.py
```

### Step 1: Define your entities

Entities are the data your dossier manages. Each entity type has a Pydantic model that validates its content.

```python
# entities.py
from pydantic import BaseModel
from typing import Optional

class Aanvraag(BaseModel):
    onderwerp: str
    aanvrager_naam: str
    gemeente: str
    beschrijving: Optional[str] = None
```

### Step 2: Define your workflow

The workflow YAML is the heart of your plugin. It declares what can happen, in what order, and who's allowed to do it.

```yaml
# workflow.yaml
name: "mijn_workflow"
description: "Mijn eerste workflow"
version: "0.1"

roles:
  - name: "oe:aanvrager"
    description: "Persoon die de aanvraag indient"
  - name: "oe:behandelaar"
    description: "Persoon die de aanvraag behandelt"

entity_types:
  - type: "oe:aanvraag"
    cardinality: "single"     # one per dossier
    schema: "my_workflow.entities.Aanvraag"

activities:

  - name: "dienAanvraagIn"
    label: "Dien aanvraag in"
    can_create_dossier: true
    allowed_roles: ["oe:aanvrager"]
    default_role: "oe:aanvrager"
    authorization:
      access: "authenticated"
    generates:
      - "oe:aanvraag"
    status: "ingediend"

  - name: "behandelAanvraag"
    label: "Behandel aanvraag"
    allowed_roles: ["oe:behandelaar"]
    default_role: "oe:behandelaar"
    authorization:
      access: "roles"
      roles:
        - role: "behandelaar"
    requirements:
      statuses: ["ingediend"]
    used:
      - type: "oe:aanvraag"
        auto_resolve: "latest"
    generates:
      - "oe:aanvraag"
    status: "behandeld"
```

That's a working workflow. Two activities: one creates a dossier with an aanvraag, the other revises it. The engine handles PROV recording, versioning (`derivedFrom` chains), access control, and archiving automatically.

### Step 3: Register the plugin

```python
# __init__.py
import os
import yaml
from dossier_engine.plugin import Plugin, build_entity_registries_from_workflow

def create_plugin() -> Plugin:
    workflow_path = os.path.join(os.path.dirname(__file__), "workflow.yaml")
    with open(workflow_path) as f:
        workflow = yaml.safe_load(f)

    entity_models, entity_schemas = build_entity_registries_from_workflow(workflow)

    return Plugin(
        name=workflow["name"],
        workflow=workflow,
        entity_models=entity_models,
        entity_schemas=entity_schemas,
    )
```

### Step 4: Add to config and run

```yaml
# config.yaml
plugins:
  - my_workflow
```

Start the API. Your endpoints appear automatically:

```
PUT /mijn_workflow/dossiers/{id}/activities/{id}/dienAanvraagIn
PUT /mijn_workflow/dossiers/{id}/activities/{id}/behandelAanvraag
GET /dossiers/{id}
GET /dossiers/{id}/prov
```

## Adding complexity gradually

The minimal plugin above works but doesn't use most of the engine's features. Here's how to add them one at a time, when you need them.

### Handlers — custom logic on activities

When an activity needs to do more than just store entities (compute derived values, set access rules, conditionally schedule tasks), you write a handler.

```python
# handlers.py
from dossier_engine.engine.context import ActivityContext, HandlerResult

async def handle_beslissing(ctx: ActivityContext, content: dict) -> HandlerResult:
    """Decide the dossier status based on the beslissing content."""
    beslissing = content.get("beslissing", "")
    if beslissing == "goedgekeurd":
        return HandlerResult(status="goedgekeurd")
    elif beslissing == "afgekeurd":
        return HandlerResult(status="afgekeurd")
    else:
        return HandlerResult(status="onvolledig")

HANDLERS = {
    "handle_beslissing": handle_beslissing,
}
```

Wire it in the YAML:

```yaml
- name: "neemBeslissing"
  handler: "handle_beslissing"
  generates: ["oe:beslissing"]
  # status is set by the handler, not the YAML
```

And register on the plugin:

```python
Plugin(..., handlers=HANDLERS)
```

### Reference data — static lists for the frontend

Dropdowns, type lists, municipality codes — anything the frontend needs to render forms. Declared in the YAML, served from memory, sub-millisecond.

```yaml
reference_data:
  gemeenten:
    - key: "brugge"
      label: "Brugge"
      nis_code: "31005"
    - key: "gent"
      label: "Gent"
      nis_code: "44021"
  documenttypes:
    - key: "beslissing"
      label: "Beslissing"
    - key: "advies"
      label: "Advies"
```

Available at:

```
GET /mijn_workflow/reference              → all lists
GET /mijn_workflow/reference/gemeenten    → one list
```

### Field validators — instant feedback between activities

When the frontend needs to validate a field value against server-side data (does this URI exist? is this combination valid?), register a field validator.

```python
# field_validators.py
async def validate_gemeente(body: dict) -> dict:
    gemeente = body.get("gemeente", "")
    if gemeente not in KNOWN_GEMEENTEN:
        return {"valid": False, "error": f"Onbekende gemeente: {gemeente}"}
    return {"valid": True, "label": KNOWN_GEMEENTEN[gemeente]}

FIELD_VALIDATORS = {
    "gemeente": validate_gemeente,
}
```

Register on the plugin:

```python
Plugin(..., field_validators=FIELD_VALIDATORS)
```

Available at:

```
POST /mijn_workflow/validate/gemeente
{"gemeente": "brugge"}
→ {"valid": true, "label": "Brugge"}
```

### Domain relations — linking entities to external objects

When your entities relate to things outside the dossier (erfgoedobjecten, legal articles, other dossiers), declare domain relation types.

```yaml
# Workflow-level declaration
relations:
  - type: "oe:betreft"
    kind: "domain"
    from_types: ["entity"]
    to_types: ["external_uri"]

# On the activity that can create the link
activities:
  - name: "dienAanvraagIn"
    relations:
      - type: "oe:betreft"
        kind: "domain"
        operations: [add]
```

The API caller includes the relation in the activity request:

```json
{
  "generated": [{"entity": "oe:aanvraag/e1@v1", "content": {...}}],
  "relations": [
    {
      "type": "oe:betreft",
      "from": "oe:aanvraag/e1@v1",
      "to": "https://id.erfgoed.net/erfgoedobjecten/10001"
    }
  ]
}
```

The shorthand refs (`oe:aanvraag/e1@v1`) are automatically expanded to full IRIs before storage. The relation appears in the dossier detail response under `domainRelations`.

To allow removing relations, add a dedicated activity:

```yaml
- name: "bewerkRelaties"
  label: "Bewerk relaties"
  generates: []
  status: null
  relations:
    - type: "oe:betreft"
      kind: "domain"
      operations: [add, remove]
```

### Tasks — scheduling future work

When an activity should trigger future work (send a notification, check a deadline, escalate after N days), declare a task.

```yaml
- name: "dienAanvraagIn"
  tasks:
    - kind: "recorded"
      function: "send_ontvangstbevestiging"
    - kind: "anchored"
      anchor_type: "oe:aanvraag"
      function: "check_behandeltermijn"
      scheduled_for: "+20d"
```

The task handler is an async function:

```python
async def send_ontvangstbevestiging(ctx):
    # In production: send email, generate PDF, etc.
    pass

TASK_HANDLERS = {
    "send_ontvangstbevestiging": send_ontvangstbevestiging,
}
```

Registered on the plugin:

```python
Plugin(..., task_handlers=TASK_HANDLERS)
```

### Search — Elasticsearch integration

For production search, register a `post_activity_hook` that pushes data to Elasticsearch after each activity, and a `search_route_factory` that registers the search endpoint.

```python
async def update_index(repo, dossier_id, activity_type, status, entities):
    # Push to Elasticsearch
    pass

def register_search(app, get_user):
    @app.get("/mijn_workflow/dossiers")
    async def search(...):
        # Query Elasticsearch
        pass

Plugin(...,
    post_activity_hook=update_index,
    search_route_factory=register_search,
)
```

## What you don't need to think about

The engine handles these automatically — you declare them in YAML or not at all:

- **PROV provenance** — every activity, entity version, and derivation chain is recorded
- **Versioning** — `derivedFrom` chains are computed from `used` + `generated` declarations
- **Idempotency** — client-generated UUIDs make PUTs safe to retry
- **Access control** — the `oe:dossier_access` entity (managed by a handler) controls who sees what
- **Archiving** — `GET /dossiers/{id}/archive` produces a PDF/A-3b with full provenance and embedded files
- **Tombstone redaction** — the built-in `tombstone` activity NULLs entity content for GDPR without breaking the PROV graph
- **Activity visibility** — access entries control which activities each user sees in the timeline
- **IRI generation** — entity IRIs follow the W3C PROV structure and match the API routes
- **Audit logging** — reads, writes, denials, and exports are logged to NDJSON for SIEM integration

## The complete plugin interface

Everything a plugin can register:

| Field | Type | Purpose |
|---|---|---|
| `name` | `str` | Workflow name (used in URLs) |
| `workflow` | `dict` | Parsed workflow.yaml |
| `entity_models` | `dict[str, BaseModel]` | Entity type → Pydantic model |
| `entity_schemas` | `dict[tuple, BaseModel]` | (type, version) → Pydantic model (optional) |
| `handlers` | `dict[str, Callable]` | Handler name → async function |
| `validators` | `dict[str, Callable]` | Validator name → async function |
| `relation_validators` | `dict[str, Callable]` | Relation type → async validator |
| `field_validators` | `dict[str, Callable]` | Validator name → async function |
| `task_handlers` | `dict[str, Callable]` | Task function name → async function |
| `pre_commit_hooks` | `list[Callable]` | Strict hooks (can veto activity) |
| `post_activity_hook` | `Callable` | Advisory hook (errors swallowed) |
| `search_route_factory` | `Callable` | Registers search endpoints |

Most plugins use only `entity_models`, `handlers`, and perhaps `task_handlers`. Everything else is opt-in for when you need it.
