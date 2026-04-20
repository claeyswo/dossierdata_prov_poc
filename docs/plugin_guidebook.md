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
PUT /mijn_workflow/dossiers/{id}/activities/{id}/oe:dienAanvraagIn
PUT /mijn_workflow/dossiers/{id}/activities/{id}/oe:behandelAanvraag
GET /dossiers/{id}
GET /dossiers/{id}/prov
```

Note the `oe:` prefix in the activity URL. Type-like path segments
are always qualified — entity types (`/entities/oe:aanvraag/...`)
and activity types (`/activities/{id}/oe:dienAanvraagIn`) follow
the same convention. If you declare a bare name (`dienAanvraagIn`)
in your workflow YAML, the engine automatically qualifies it to
the default prefix. Your client can also use the generic endpoint
`PUT /{workflow}/dossiers/{id}/activities/{id}` with a bare name
in the body's `type` field — the engine qualifies on the server
side.

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
    # Fire immediately after the activity completes — no scheduled_for.
    - kind: "recorded"
      function: "send_ontvangstbevestiging"

    # Fire 20 days later — relative offset.
    - kind: "recorded"
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

#### Scheduling: when does a task run?

The `scheduled_for` field accepts two forms:

**Relative offset** — resolved against the activity's start time.

```yaml
scheduled_for: "+20d"    # 20 days from now
scheduled_for: "+2h"     # 2 hours from now
scheduled_for: "+45m"    # 45 minutes
scheduled_for: "+3w"     # 3 weeks
```

Units: `m` (minutes), `h` (hours), `d` (days), `w` (weeks). The `+` prefix is required — it's what tells the parser "this is an offset, not a date". Negative offsets and unknown units are rejected at activity execution time.

**Absolute ISO 8601** — for fixed wall-clock times.

```yaml
scheduled_for: "2026-12-31T23:59:59Z"
scheduled_for: "2026-05-01T12:00:00+02:00"
```

Omit `scheduled_for` entirely for tasks that should run immediately after the activity completes.

#### Dynamic deadlines (from entity content or config)

YAML can't read entity fields — there's no `{{ aanvraag.deadline }}` template syntax, deliberately. For anything where the deadline depends on entity content or runtime config, compute the ISO string in a handler and return it in `HandlerResult.tasks`:

```python
from datetime import datetime, timezone, timedelta

async def handle_beslissing(context, content):
    # Deadline from plugin config — 30 days by default, overrideable
    # via env var or workflow.yaml.
    deadline_days = context.constants.aanvraag_deadline_days

    deadline = (
        datetime.now(timezone.utc) + timedelta(days=deadline_days)
    ).isoformat()

    task_dict = {
        "kind": "scheduled_activity",
        "target_activity": "trekAanvraagIn",
        "scheduled_for": deadline,     # full ISO string
        "cancel_if_activities": ["vervolledigAanvraag"],
        "anchor_type": "oe:aanvraag",
    }
    return HandlerResult(tasks=[task_dict])
```

Handlers have full access to `context.get_typed("oe:aanvraag")` to read entity content, `context.constants` for configured values, and `context.dossier_id` for anything else needing lookup. The rule of thumb: **if the deadline depends on something that varies, compute in a handler. If it's always "N days after this activity", use the YAML offset.**

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

### Using external ontologies

Your workflow doesn't have to invent its own vocabulary for everything. You can adopt types from standard ontologies like FOAF (people), Dublin Core (documents), Schema.org (general-purpose), or PROV (provenance itself). This makes your PROV graph interoperable — other systems that understand these vocabularies can consume your data directly.

Declare external prefixes in your `workflow.yaml`:

```yaml
namespaces:
  foaf: "http://xmlns.com/foaf/0.1/"
  dcterms: "http://purl.org/dc/terms/"
  schema: "http://schema.org/"
```

Then use them anywhere a qualified type goes — entity types, relations, activity generates/used:

```yaml
entity_types:
  - type: "oe:aanvraag"          # your own ontology
    cardinality: multiple
    schema: "my_workflow.entities.Aanvraag"

  - type: "foaf:Person"           # adopted from FOAF
    cardinality: multiple
    schema: "my_workflow.entities.Person"

  - type: "dcterms:BibliographicResource"   # adopted from Dublin Core
    cardinality: multiple
    schema: "my_workflow.entities.Document"

relations:
  - type: "oe:betreft"
    kind: domain
  - type: "dcterms:isPartOf"      # Dublin Core relation
    kind: domain
    from_types: [entity]
    to_types: [entity]
```

**What the engine does with this:**

1. At plugin load, validates that every qualified type references a declared prefix. Typo `foa:Person` instead of `foaf:Person`? You get a clear error at startup, not a runtime surprise.
2. PROV-JSON exports include a full `prefixes` block with all your declarations, so downstream consumers can expand `foaf:Person` → `http://xmlns.com/foaf/0.1/Person`.
3. Reference refs like `foaf:Person/e1@v1` parse correctly. Entity IRIs look like `https://{platform}/dossiers/{did}/entities/foaf:Person/{eid}/{vid}` — self-describing.

**Important distinction:** using `foaf:Person` as an entity type means your engine *stores* `foaf:Person` instances (you own the data, you manage the versioning, the engine persists them). It doesn't mean you link to FOAF instances that live elsewhere — those are external URIs (`{"entity": "http://example.org/agents/bob"}`).

Built-in prefixes (`prov`, `xsd`, `rdf`, `rdfs`) are always available; you don't need to declare them. Your plugin's own prefix (`oe` by default) is registered from `config.yaml`'s `iri_base.ontology`.

### Workflow constants and environment variables

Your plugin will have constants — deadline durations, decision thresholds, feature flags, external service URLs, API keys. Hardcoding them in handler code is fine for a prototype, but they need a proper home: some change per deployment (dev vs prod URLs), some are secrets (never commit), some are domain-level tuning you want operators to adjust without code changes.

The engine gives you a typed `constants` slot on every plugin. Declare a Pydantic `BaseSettings` class:

```python
# my_workflow/constants.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class MyWorkflowConstants(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOSSIER_MY_WORKFLOW_",
        case_sensitive=False,
        frozen=True,
    )

    # Domain constants
    aanvraag_deadline_days: int = 30
    max_attachments: int = 20

    # Environment-specific
    external_api_url: str = "http://localhost:9200"
    external_api_key: str | None = None

    # Feature flags
    auto_approve_enabled: bool = False
```

Wire it into `create_plugin()`:

```python
def create_plugin() -> Plugin:
    workflow = yaml.safe_load(open("workflow.yaml"))
    yaml_constants = (workflow.get("constants") or {}).get("values", {}) or {}
    constants = MyWorkflowConstants(**yaml_constants)
    return Plugin(..., constants=constants)
```

Access it anywhere in your plugin code:

```python
# In a handler
async def handle_beslissing(context, content):
    deadline_days = context.constants.aanvraag_deadline_days
    ...

# In a hook or route factory
async def update_search_index(repo, dossier_id, ...):
    url = plugin.constants.external_api_url
    ...
```

**Precedence (highest wins):**

1. **Environment variables** — `DOSSIER_MY_WORKFLOW_AANVRAAG_DEADLINE_DAYS=60` — operator's escape hatch for per-deployment tuning and the only acceptable place for secrets.
2. **`workflow.yaml`** — `constants.values` block — committable domain-level tuning.
3. **Class defaults** — the values in your Pydantic class.

```yaml
# workflow.yaml
constants:
  values:
    aanvraag_deadline_days: 45   # overrides class default of 30
    # external_api_key: never put secrets here — use env vars
```

**What goes where:**

- **Code defaults**: sensible values that let a bare install work
- **`workflow.yaml`**: domain decisions the plugin author makes (deadlines, thresholds, feature flag defaults)
- **Environment variables**: deployment-specific values (URLs, timeouts, feature flag overrides) and ALL secrets

Because the class is `frozen=True`, nothing can mutate the constants after plugin load — they're immutable for the lifetime of the process.

## What you don't need to think about

The engine handles these automatically — you declare them in YAML or not at all:

- **PROV provenance** — every activity, entity version, and derivation chain is recorded
- **Versioning** — `derivedFrom` chains are computed from `used` + `generated` declarations
- **Idempotency** — client-generated UUIDs make PUTs safe to retry
- **Access control** — the `oe:dossier_access` entity (managed by a handler) controls who sees what. Audit-level views (`/prov`, `/prov/graph/columns`, `/archive`) require a separate `global_audit_access` role list in `config.yaml` or an `audit_access` list on the dossier — these endpoints expose the full unfiltered record (system activities, tasks, all entities) and should be restricted to auditors and compliance roles.
- **Archiving** — `GET /dossiers/{id}/archive` produces a PDF/A-3b with full provenance and embedded files (audit-level access required)
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
| `constants` | `BaseSettings` | Typed workflow constants (env vars + YAML) |

Most plugins use only `entity_models`, `handlers`, and perhaps `task_handlers`. Everything else is opt-in for when you need it.
