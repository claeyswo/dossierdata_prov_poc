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
    model: "my_workflow.entities.Aanvraag"

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

### Split-style hooks — status_resolver and task_builders

A handler can return three things: content, status, tasks. For simple activities that's fine. For complex activities — especially those where status depends on multiple entities, or where several different tasks get scheduled conditionally — the handler becomes a Swiss army knife and the YAML tells you nothing about what side effects the activity has.

You can optionally split those concerns across dedicated functions declared in the YAML:

```yaml
- name: "tekenBeslissing"
  # No handler needed — this activity doesn't compute entity content
  # beyond what the client submits.
  status_resolver: "resolve_beslissing_status"
  task_builders:
    - "schedule_trekAanvraag_if_onvolledig"
    - "send_ontvangstbevestiging"
```

Each function has a single responsibility:

```python
async def resolve_beslissing_status(ctx: ActivityContext) -> str | None:
    """Map the latest handtekening + beslissing to a status string."""
    handtekening = ctx.get_typed("oe:handtekening")
    beslissing = ctx.get_typed("oe:beslissing")
    if not handtekening:
        return "beslissing_te_tekenen"
    if beslissing and beslissing.beslissing == "goedgekeurd":
        return "toelating_verleend"
    # ...
    return None  # leave unchanged


async def schedule_trekAanvraag_if_onvolledig(
    ctx: ActivityContext,
) -> list[dict]:
    """Return a task dict when beslissing is onvolledig, else []."""
    beslissing = ctx.get_typed("oe:beslissing")
    if not beslissing or beslissing.beslissing != "onvolledig":
        return []
    return [{
        "kind": "scheduled_activity",
        "target_activity": "trekAanvraagIn",
        "scheduled_for": f"+{ctx.constants.aanvraag_deadline_days}d",
        "cancel_if_activities": ["vervolledigAanvraag"],
        "anchor_type": "oe:aanvraag",
    }]


STATUS_RESOLVERS = {"resolve_beslissing_status": resolve_beslissing_status}
TASK_BUILDERS = {
    "schedule_trekAanvraag_if_onvolledig": schedule_trekAanvraag_if_onvolledig,
}
```

Register alongside handlers:

```python
Plugin(
    ...,
    handlers=HANDLERS,
    status_resolvers=STATUS_RESOLVERS,
    task_builders=TASK_BUILDERS,
)
```

#### When to use which style

The two styles coexist permanently — legacy handlers keep working and new activities can use either. The decision is about readability and testability, not correctness.

**Stick with a handler when:**
- The activity is simple (one short function, one concern)
- Content / status / task decisions are tightly coupled — branching on the same condition to produce all three
- You have one focused test that exercises the whole activity

**Split when:**
- The handler has grown past ~50 lines
- Multiple different tasks are scheduled from one activity
- A task_builder is reusable across activities (declare the builder once, reference from both YAML entries)
- A domain reviewer would benefit from seeing "this activity schedules X and Y" in the YAML without opening Python

#### The "exactly one source" rule

An activity that declares `status_resolver` must NOT also have a handler returning `status`. Same for `task_builders` + handler `tasks`. The engine raises `ActivityError(500)` with a clear message at activity execution time if it finds both:

```
Activity 'tekenBeslissing' declares status_resolver 'resolve_beslissing_status'
but its handler also returned status='toelating_verleend'. Remove one — the
same activity cannot have status come from both sources.
```

This keeps "who decides X" unambiguous per activity. Mixing styles within one activity is always a bug (usually a half-finished migration); the engine catches it loudly instead of silently picking a winner.

### Side effects and conditional execution

Side effects are activities that fire automatically after a parent activity succeeds. They're composable (one side effect can declare its own `side_effects:`) and attributed to the system user, so they're recorded as their own activity rows in PROV.

```yaml
- name: "dienAanvraagIn"
  side_effects:
    - activity: "duidVerantwoordelijkeOrganisatieAan"
    - activity: "setSystemFields"
```

Each entry may carry a conditional gate in one of two forms, mutually exclusive per entry:

```yaml
# Dict form: field equality against a dossier entity.
side_effects:
  - activity: "publishToPortal"
    condition:
      entity_type: "oe:beslissing"
      field: "content.beslissing"
      value: "goedgekeurd"

# Function form: a named predicate in Python.
side_effects:
  - activity: "publishToPortal"
    condition_fn: "should_publish"
```

**Dict form — `condition: {entity_type, field, value}`.** The engine looks up the named entity (in the trigger's scope, falling back to dossier-wide singleton lookup), reads the field via dot-notation, and compares to `value`. All three keys are required. Shape is validated at plugin load — typos like `from_entity:` (borrowed from status-rule syntax) or `mapping:` fail with an explicit error pointing at the right shape.

**Function form — `condition_fn: "name"`.** References a predicate registered on `plugin.side_effect_conditions`. The function receives an `ActivityContext` scoped to the triggering activity (so it can read the trigger's used and generated entities via `ctx.get_typed(...)`) and returns `bool`. Use this for any gate that isn't simple equality — counts, date comparisons, boolean combinations, config lookups.

```python
async def should_publish(ctx: ActivityContext) -> bool:
    beslissing = ctx.get_typed("oe:beslissing")
    if not beslissing or beslissing.beslissing != "goedgekeurd":
        return False
    # Business rule: don't publish during freeze windows.
    return not ctx.constants.publication_freeze_active

SIDE_EFFECT_CONDITIONS = {"should_publish": should_publish}

Plugin(..., side_effect_conditions=SIDE_EFFECT_CONDITIONS)
```

Both the YAML name and the registered function names are cross-checked at plugin load. An unknown `condition_fn` name fails fast with a list of registered names. Declaring both `condition:` and `condition_fn:` on the same entry also fails at load — pick one.

**Choosing between the two forms.** Use the dict form when the gate is a single field equality — it's readable inline and one less indirection. Use `condition_fn:` as soon as the gate involves anything else. Don't grow the dict shape with `value_in`, `value_not`, boolean combinators — that's a DSL creep path. `condition_fn` is the escape hatch; use it.

#### When to use `condition:` / `condition_fn:` vs an empty-result handler

Since a handler can also just return `HandlerResult()` with nothing, there's a choice. They're **not equivalent** — they produce different PROV graphs:

| Approach | Activity row written? | Visible in PROV | Meaning |
|---|---|---|---|
| `condition:` or `condition_fn:` blocks execution | No | No trace | "This activity doesn't apply here." |
| Handler returns empty | Yes | Activity appears, produced nothing | "Activity ran, chose not to produce output." |

Two concrete examples that cut opposite ways:

**`setSystemFields` once per dossier.** You want this activity to run on `dienAanvraagIn` but not again on `bewerkAanvraag` edits. Gating with `condition: {entity_type: "oe:system_fields", field: ..., value: ...}` means PROV shows it ran once, cleanly. Using an empty handler on re-edits would show `setSystemFields` firing on every edit and producing nothing — noise in the audit trail.

**`publishToPortal` with a legitimate "decline to publish".** The parent activity took a decision; publishing was considered, but policy says don't publish in this case. If you gate with a condition, the audit record silently lacks any mention that publication was considered. An empty-result handler preserves the trace: "publication was attempted, produced no output." That's the truthful record.

Rule of thumb: use a condition gate when the side effect genuinely **doesn't apply** (re-runs, wrong kind of entity, out-of-scope state). Use an empty-result handler when the side effect **applies but decided not to produce anything** (a reviewed case that needs to be visible in audit).

#### Why side effects have a condition mechanism at all

For tasks, we rejected YAML conditions in favor of `task_builders: ["fn_name"]` — full Python power, no DSL. For side effects we kept a gating mechanism. The reasons are different:

- **Tasks can be built programmatically.** A `task_builder` function decides whether to schedule and the return list can be empty. The task entity either exists or it doesn't — there's no "task activity ran and did nothing" residue in PROV.
- **Side effects cannot.** A handler cannot decide "don't run this activity at all" — the activity row is created before the handler is invoked. Only a YAML-declared gate can actually prevent the activity from leaving a PROV trace.

So the condition mechanism is the *only* way to express "this side effect shouldn't apply here" without leaving a PROV residue. The dict form handles the common case; `condition_fn:` covers everything else without inviting a DSL.

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

The platform ships with a two-tier index model: a **common index** (`dossiers-common`, engine-owned, one doc per dossier across all workflows) and an optional **workflow-specific index** (e.g. `dossiers-toelatingen`, plugin-owned, fields tuned to that workflow's entity shape). A plugin wires into search via three hooks.

**1. `post_activity_hook` — incremental indexing.** Runs after every activity completes. Upserts both the plugin's own index and the engine's common index. Silent no-op when `DOSSIER_ES_URL` is empty.

```python
async def update_index(repo, dossier_id, activity_type, status, entities):
    # Build a common-index doc (for /dossiers?workflow=...)
    # and a workflow-specific doc, then push both to ES.
    pass
```

**2. `search_route_factory` — the plugin's search endpoint.** Registers `GET /{workflow}/dossiers` with fuzzy/exact filters over the workflow-specific index. Always AND's in `build_acl_filter(user)` so users only see what they're allowed to see.

```python
def register_search(app, get_user):
    @app.get("/mijn_workflow/dossiers")
    async def search(...):
        # Query the workflow-specific ES index, ACL-filtered.
        pass
```

**3. `build_common_doc_for_dossier` — bulk reindex builder.** Optional, but **strongly recommended**. Called by the engine's `POST /admin/search/common/reindex` endpoint when it walks every dossier to rebuild the common index from Postgres. Without this, the engine falls back to a minimal doc (`onderwerp=""`, `__acl__` with only global-access roles) for your workflow's dossiers — which makes every non-global user invisible from search until the next per-activity upsert rewrites the doc. A bulk reindex shortly after a mapping change or a fresh cluster is exactly when you need this most, so plugins should implement it.

```python
async def build_common_doc_for_dossier(repo, dossier_id):
    """Produce the common-index doc for one of this plugin's
    dossiers. Called by engine-level reindex."""
    from dossier_engine.search.common_index import build_common_doc

    aanvraag = await repo.get_singleton_entity(dossier_id, "oe:aanvraag")
    access = await repo.get_singleton_entity(dossier_id, "oe:dossier_access")
    if aanvraag is None and access is None:
        return None  # counts as "skipped"

    onderwerp = (aanvraag.content or {}).get("onderwerp") if aanvraag else None
    return build_common_doc(
        dossier_id=dossier_id,
        workflow="mijn_workflow",
        onderwerp=onderwerp,
        access_entity_content=access.content if access else None,
    )

Plugin(...,
    post_activity_hook=update_index,
    search_route_factory=register_search,
    build_common_doc_for_dossier=build_common_doc_for_dossier,
)
```

The engine provides `build_common_doc(...)` to assemble the standard shape — plugin code only supplies onderwerp and the access entity's content (so ACL is derived consistently across plugins).

### Activity names — qualified vs bare

Activity names in `workflow.yaml` can be written bare (`dienAanvraagIn`) or qualified with the plugin's prefix (`oe:dienAanvraagIn`). At plugin registration time, the engine walks the workflow dict and qualifies any bare name it finds — including cross-references:

- `activities[*].name`
- `activities[*].requirements.activities`
- `activities[*].forbidden.activities`
- `activities[*].side_effects[*].activity`
- `activities[*].tasks[*].target_activity` and `cancel_if_activities`

After registration, every activity name in the plugin object is qualified. **Downstream code should compare by qualified name only.** This is why the DB stores qualified activity types, URLs route on qualified names (`/toelatingen/dossiers/{id}/activities/{aid}/oe:dienAanvraagIn`), and the frontend keys lookups on qualified types.

**What this means for plugin authors:** write whichever form feels natural — the engine normalizes. But be aware that if you add a new YAML shape that cross-references an activity name (e.g. `my_new_feature.activities`), you need to extend `_normalize_plugin_activity_names` to qualify entries in that shape too. Otherwise bare names silently pass through and downstream filters (like `system_activity_types` in the columns PROV graph) will miss.

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
    model: "my_workflow.entities.Aanvraag"

  - type: "foaf:Person"           # adopted from FOAF
    cardinality: multiple
    model: "my_workflow.entities.Person"

  - type: "dcterms:BibliographicResource"   # adopted from Dublin Core
    cardinality: multiple
    model: "my_workflow.entities.Document"

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
| `status_resolvers` | `dict[str, Callable]` | Status resolver name → async function (split-style) |
| `task_builders` | `dict[str, Callable]` | Task builder name → async function (split-style) |
| `validators` | `dict[str, Callable]` | Validator name → async function |
| `relation_validators` | `dict[str, Callable]` | Validator name → async function. Dict **keys are validator names**, NOT relation type names — the engine rejects collisions at plugin load time. See "Declaring relations" and "Relation-validator keying" below. |
| `field_validators` | `dict[str, Callable]` | Validator name → async function |
| `task_handlers` | `dict[str, Callable]` | Task function name → async function |
| `pre_commit_hooks` | `list[Callable]` | Strict hooks (can veto activity) |
| `post_activity_hook` | `Callable` | Advisory hook (errors swallowed) |
| `search_route_factory` | `Callable` | Registers search endpoints |
| `build_common_doc_for_dossier` | `Callable` | Builds per-dossier doc for engine-level common-index reindex |
| `constants` | `BaseSettings` | Typed workflow constants (env vars + YAML) |

Most plugins use only `entity_models`, `handlers`, and perhaps `task_handlers`. Everything else is opt-in for when you need it.

### Declaring relations

Relation types are declared **once at workflow level** with mandatory `kind`. Activities reference them by type name and may add an `operations:` list and a validator. The engine rejects misaligned declarations at plugin load — see "Load-time validation" below.

**Workflow level** (the single source of truth):

```yaml
relations:
  - type: "oe:neemtAkteVan"
    kind: "process_control"
    description: "Acknowledge a newer version the activity chose not to act on."

  - type: "oe:betreft"
    kind: "domain"
    from_types: ["entity"]
    to_types: ["external_uri"]
    description: "Link an entity to the external object it concerns."
```

Required fields: `type`, `kind` (must be `"domain"` or `"process_control"`). Optional: `from_types`, `to_types` (domain only — omit both to accept any ref type), `description`.

**Activity level** (reference by name; no redeclaration):

```yaml
activities:
  - name: "bewerkRelaties"
    relations:
      - type: "oe:betreft"
        operations: ["add", "remove"]
        validators:
          add: "validate_betreft_target"
          remove: "validate_betreft_removable"

      - type: "oe:neemtAkteVan"
        validator: "validate_neemt_akte_van"
```

Allowed fields: `type` (required, must match a workflow-level declaration), `operations` (optional), and one of `validator:` (single-string) or `validators:` (dict with BOTH `add` and `remove`) — never both. The fields `kind`, `from_types`, `to_types`, `description` are **forbidden at activity level** — they live at workflow level only.

**Kind semantics:**
- `process_control` relations are activity→entity annotations (`{entity: "ref"}`). Stateless; no remove operation. `validators:` dict form and `operations: [remove]` are forbidden on process_control activity declarations.
- `domain` relations are entity→entity edges (`{from: "ref", to: "ref"}`). Support both add and remove. `from_types`/`to_types` at workflow level constrain the ref shape of each side.

**Dispatch is kind-driven.** The engine resolves `kind` from the workflow-level declaration and dispatches the request accordingly. If the request's shape doesn't match the declared kind (e.g. client sends `{entity: ...}` on a `kind: domain` relation), the engine returns **422** with an error identifying the declared kind and the expected fields. The `kind:` field is load-bearing, not documentation — don't get it wrong and expect the engine to guess from the payload.

### Relation-validator keying

`plugin.relation_validators` is a `dict[str, Callable]` mapping **validator name → async function**. The dict keys are validator *names* — they must not match any declared relation type name (the engine's load-time `validate_relation_validator_registrations` rejects collisions explicitly).

Activity-level YAML references validators by name in one of two forms:

**Style 1 — per-operation validators (domain relations only):**

```yaml
relations:
  - type: "oe:betreft"
    operations: ["add", "remove"]
    validators:
      add: "validate_betreft_target"
      remove: "validate_betreft_removable"
```

`validators:` is a dict with both `add` and `remove` keys required — partial dicts are rejected at load time. Use when add and remove need different logic (add checks the target exists; remove checks no downstream dependency). This form is forbidden on `kind: process_control` relations (they have no remove semantic).

**Style 2 — single validator string:**

```yaml
relations:
  - type: "oe:neemtAkteVan"
    validator: "validate_neemt_akte_van"
```

Single `validator:` key fires for all operations. Works for both kinds. Use when one function handles both add/remove (rare for domain) or when the relation is process_control (where `validator:` is the only legal form).

**Resolution order** (from `engine/pipeline/relations.py::_resolve_validator`):
1. Activity-level `validators.{add,remove}` → plugin named-validator lookup.
2. Activity-level `validator:` string → plugin named-validator lookup.
3. None (validation skipped for this relation).

**What's gone:** the previous "Style 3" — plugin-level fallback that looked up `plugin.relation_validators[<relation_type>]` when no activity-level validator was declared — was removed. It silently ran for activities that didn't opt in, made the dispatch order non-obvious, and (with the dict key matching the type name) invited accidental cross-activity coupling. If an activity wants a validator, it declares one explicitly.

### Load-time validation

At plugin load, the engine runs two validators in sequence:
- `validate_relation_declarations(workflow)` — shape-checks every workflow-level and activity-level relation declaration. Fails fast with ValueError on missing kind, invalid kind, domain-only fields used on process_control, activity-level kind/from_types/to_types/description, unknown keys, partial `validators:` dicts, and `validators:` dict or `operations: [remove]` on process_control.
- `validate_relation_validator_registrations(plugin)` — cross-checks `plugin.relation_validators` dict keys against declared relation type names; rejects collisions.

A plugin that violates the contract fails to load at startup rather than producing silent misdispatches at runtime. If your plugin currently works but produces a ValueError after upgrading past Bug 78, the error message names the rule broken and the offending declaration — fix the YAML and reload.
