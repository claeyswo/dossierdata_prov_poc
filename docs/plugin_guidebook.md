# Plugin Guidebook

A workflow plugin tells the Dossier engine what a specific kind of dossier looks like and how it evolves. The engine handles persistence, PROV recording, the worker loop, authentication, and search infrastructure; your plugin provides the domain logic.

This guidebook has three parts:

- **Part 1 — Tutorial.** Build a plugin from nothing, adding features as you need them. Read this front-to-back the first time.
- **Part 2 — Reference.** Every Plugin field, every ActivityContext method, every YAML key. Scan-oriented; use it after the tutorial makes sense.
- **Part 3 — Template.** `dossiertype_template.md` — an annotated, copy-pastable skeleton showing every feature in its canonical YAML form. Paste into a new plugin and fill in.

Related docs: `docs/pipeline_architecture.md` (engine internals — what happens between your handler returning and the 200 going out), `docs/prov_conformance.md` (PROV-O compliance story).

---

# Part 1 — Tutorial

## What a plugin is

A plugin is a Python package with two files: `workflow.yaml` and `plugin.py`. The YAML declares the shape of your workflow (entity types, activities, their ordering and permissions); the Python provides callables the engine invokes at well-defined points (handlers, validators, task functions). The engine loads your plugin at startup, validates its shape, and routes requests to it based on the URL's workflow-name prefix.

You don't need a database migration, a FastAPI app, or a worker process. The engine provides all of that. Your plugin ships domain logic; the engine ships infrastructure.

## Your first plugin in 15 minutes

We'll build a minimal permit-request workflow with one entity type (`oe:aanvraag`), one activity (`dienAanvraagIn`), and no handler — the default "store what the client sent" behaviour is enough to start.

### Step 1: Define your entities

Create `my_plugin/entities.py`:

```python
from pydantic import BaseModel

class Aanvraag(BaseModel):
    aanvrager_naam: str
    onderwerp: str
```

This is a regular Pydantic model. The engine validates incoming request payloads against it, stores the validated dict, and returns typed instances from `context.get_typed("oe:aanvraag")` in handler code.

### Step 2: Define your workflow

Create `my_plugin/workflow.yaml`:

```yaml
name: "my_plugin"

entity_types:
  - type: "oe:aanvraag"
    model: "my_plugin.entities.Aanvraag"
    cardinality: "single"

activities:
  - name: "dienAanvraagIn"
    label: "Dien aanvraag in"
    description: "Indienen van een nieuwe aanvraag"
    can_create_dossier: true
    generates:
      - "oe:aanvraag"
    status: "in_behandeling"
```

`can_create_dossier: true` means this activity can create a new dossier out of thin air — it doesn't require a pre-existing one. `status` sets the dossier's status after the activity succeeds. `generates` lists the entity types this activity produces; with no `handler:` declared, the engine uses the default handler which stores the client-supplied content as-is.

### Step 3: Register the plugin

Create `my_plugin/__init__.py`:

```python
from pathlib import Path
import yaml

from dossier_engine.plugin import Plugin, build_entity_registries_from_workflow


def create_plugin() -> Plugin:
    workflow_path = Path(__file__).parent / "workflow.yaml"
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

`build_entity_registries_from_workflow` walks the YAML's `entity_types:` block, imports each declared `model:` dotted path, and assembles the two registries the `Plugin` dataclass expects. Entity models are the only registry the engine populates from dotted paths in YAML — all other plugin registries (handlers, validators, task functions, etc.) are built by your Python code and passed to the `Plugin` constructor. See Obs 95 in the review for the asymmetry.

### Step 4: Add to config and run

In the engine's `config.yaml`:

```yaml
plugins:
  - my_plugin
```

Start the server (`python -m uvicorn dossier_engine.app:create_app --factory`). Submit an aanvraag:

```bash
curl -X POST http://localhost:8000/dossiers/my_plugin/activities/dienAanvraagIn \
  -H "Content-Type: application/json" \
  -u alice:pwd \
  -d '{
    "generated": [
      {"entity": "oe:aanvraag/new@new",
       "content": {"aanvrager_naam": "Jan", "onderwerp": "Dakwerken"}}
    ]
  }'
```

You get back the created dossier's UUID and the activity record. That's a working plugin.

## Adding complexity gradually

The 15-minute plugin covers one entity, one activity, default handler. Real workflows need more — decision logic, validation, deadlines, search, access control. Each is a separate opt-in. Add them one at a time; none are required.

The rest of Part 1 walks through the features roughly in the order a real plugin grows into them. Feel free to skip ahead to what you need.

### Handlers — custom logic on activities

When the default "store what the client sent" isn't enough — you need to compute derived fields, validate cross-entity invariants, or choose the next status based on content — declare a handler:

```yaml
activities:
  - name: "neemBeslissing"
    handler: "my_plugin.handlers.handle_beslissing"
    used:
      - "oe:aanvraag"
    generates:
      - "oe:beslissing"
```

```python
# my_plugin/handlers/__init__.py
from dossier_engine.engine.context import HandlerResult
from dossier_engine.engine.errors import ActivityError

async def handle_beslissing(context, content):
    aanvraag = context.get_typed("oe:aanvraag")
    if not aanvraag:
        raise ActivityError(422, "No aanvraag found in used entities")

    # Derived field: compute a reference from the aanvraag
    content["reference"] = f"BES-{aanvraag.aanvrager_naam[:3].upper()}"

    # Status branch based on content
    status = "goedgekeurd" if content["beslissing"] == "positief" else "afgewezen"

    return HandlerResult(content=content, status=status)
```

The YAML references the handler by its **fully-qualified dotted path** —
`my_plugin.handlers.handle_beslissing`. The engine resolves the path at plugin
load time and populates `plugin.handlers` from the workflow YAML (Obs 95 /
Round 28). Plugin authors don't hand-build short-name dicts anymore — keeping
a function module-level is all the "registration" needed. A typo in the path
fails at plugin startup with a clear error naming the activity and YAML field.

**Handler signature:** `async def handler(context: ActivityContext, content: dict) -> HandlerResult`. The `content` dict is the client-supplied payload from the request's `generated` block (already validated against the Pydantic model). `context` is the activity context — the Part 2 reference has the full method list.

**When to write a handler:** when the activity does anything other than "accept the content, store it, set a static status." If you need to read other entities, compute anything, or branch on content, you need a handler.

**When not to write a handler:** pure create-and-store activities. A `dienAanvraagIn` that just records the client's aanvraag doesn't need a handler — omit it and the engine handles the store.

### Split-style hooks — `status_resolver` and `task_builders`

Handlers can return `content + status + tasks` all at once, but as a handler grows, those three concerns don't always fit together cleanly. Split-style lets you split them off:

```yaml
activities:
  - name: "neemBeslissing"
    handler: "my_plugin.handlers.handle_beslissing"
    status_resolver: "my_plugin.handlers.resolve_beslissing_status"
    task_builders:
      - "my_plugin.handlers.build_trekAanvraag_task"
      - "my_plugin.handlers.build_appeal_notification_task"
```

```python
async def resolve_beslissing_status(context):
    beslissing = context.get_typed("oe:beslissing")
    return "goedgekeurd" if beslissing.beslissing == "positief" else "afgewezen"

async def build_trekAanvraag_task(context):
    # Returns a single task dict, or None to skip
    return {
        "kind": "recorded",
        "function": "trek_aanvraag_in",
        "scheduled_for": "+60d",
    }
```

**Constraint:** if an activity declares a `status_resolver`, its `handler` must not return `status`. Same for `task_builders` and `tasks`. The engine raises at plugin load if both sources set the same field — "who decides X" is always unambiguous.

**When to split:** when the handler's content logic and status logic don't share state, or when the same status/task logic applies to multiple activities. Status resolvers are natural for "branch on entity content"; task builders are natural for "schedule these three tasks, each with their own conditions."

### Side effects and conditional execution

Side effects trigger *another* activity (in the same dossier) as a consequence of the current one succeeding. Example: when a beslissing is taken, automatically emit a `sendNotification` activity:

```yaml
activities:
  - name: "neemBeslissing"
    side_effects:
      - activity: "sendNotification"
        condition:
          entity_type: "oe:beslissing"
          field: "content.beslissing"
          value: "positief"
```

Side effects fire only if the condition matches. Two gate forms:

**Dict form** (reads at a glance in YAML):

```yaml
condition:
  entity_type: "oe:beslissing"
  field: "content.beslissing"
  value: "positief"
```

Resolves the entity of the given type (which must be in the current activity's `used` or `generated` block), reads the field path, compares for equality.

**Function form** (anything the dict form can't express):

```yaml
condition_fn: "my_plugin.handlers.is_publication_not_frozen"
```

```python
async def is_publication_not_frozen(ctx):
    return not ctx.constants.publication_freeze_active
```

Register by placing the function at module level and referencing its
fully-qualified dotted path from YAML — same pattern as handlers.

Use for date comparisons, value-in-set checks, boolean combinations, anything that isn't `field == value`. Both forms receive the same `ActivityContext` your handlers see.

**Condition and condition_fn are mutually exclusive** per side-effect entry. Load-time validator rejects entries that set both.

### Reference data — static lists for the frontend

Many workflows have fixed lists the frontend needs (provinces, decision types, categories). Declare them inline rather than building a separate API:

```yaml
reference_data:
  beslissing_types:
    - id: "positief"
      label: "Positieve beslissing"
    - id: "negatief"
      label: "Negatieve beslissing"
    - id: "voorwaardelijk"
      label: "Voorwaardelijk positief"
```

The engine exposes these via `GET /workflows/{workflow_name}/reference_data/{key}`. No Python code needed — the data lives in YAML and the engine serves it.

### Field validators — instant feedback between activities

Sometimes a form field needs server-side validation before the user submits: "does this external identifier resolve?", "is this date within the allowed range?". Register a field validator:

```python
from dossier_engine.plugin import FieldValidator
from pydantic import BaseModel

class ErfgoedobjectRequest(BaseModel):
    uri: str

class ErfgoedobjectResponse(BaseModel):
    ok: bool
    label: str | None = None

async def validate_erfgoedobject(payload: dict) -> dict:
    uri = payload["uri"]
    # ... call external service, resolve, etc.
    return {"ok": True, "label": "Some label"}

# Module-level FieldValidator binding. The workflow YAML references this
# by dotted path (``my_plugin.field_validators.erfgoedobject``) in the
# top-level ``field_validators:`` block; the engine resolves it at plugin
# load time.
erfgoedobject = FieldValidator(
    fn=validate_erfgoedobject,
    request_model=ErfgoedobjectRequest,
    response_model=ErfgoedobjectResponse,
    summary="Valideer erfgoedobject URI",
    description="Controleer of de URI verwijst naar een gekend erfgoedobject.",
)
```

In `workflow.yaml`, register the URL key → dotted path mapping:

```yaml
field_validators:
  erfgoedobject: "my_plugin.field_validators.erfgoedobject"
```

`field_validators:` is the one registry whose keys are NOT dotted paths — the
key (`erfgoedobject` here) ends up in the HTTP URL
`POST /{workflow}/validate/{key}`, so it stays a user-facing short string.
The *value* is the dotted path the engine resolves.

The frontend calls `POST /{workflow}/validate/erfgoedobject` with a JSON body; the engine validates against `request_model`, invokes the function, validates the return against `response_model`, and serves the result with proper OpenAPI documentation.

**Plain-callable form** (legacy): you can also point the dotted path at a bare async function without request/response models. Typed `FieldValidator` is strongly preferred — the OpenAPI schemas let the frontend code-gen typed clients.

### Relations — linking entities to other entities or external URIs

Process-control relations annotate a single activity (e.g. "this activity acknowledges a newer version of the used entity"). Domain relations are entity-to-entity (or entity-to-external-URI) edges that persist in the PROV graph.

Declare every relation type **once at workflow level** with a mandatory `kind`:

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

Activities reference types by name. Activity-level declarations cannot override `kind`, `from_types`, `to_types`, or `description` — those live at workflow level only:

```yaml
activities:
  - name: "bewerkRelaties"
    relations:
      - type: "oe:betreft"
        operations: ["add", "remove"]
        validators:
          add: "my_plugin.relation_validators.validate_betreft_target"
          remove: "my_plugin.relation_validators.validate_betreft_removable"

      - type: "oe:neemtAkteVan"
        validator: "my_plugin.relation_validators.validate_neemt_akte_van"
```

This is the post-Bug-78 contract (Round 26). Load-time validation rejects misaligned YAML with a clear error. See Part 2 → *Relations reference* for the full rules.

### Tasks — scheduling future work

Tasks are work that happens alongside or after an activity — notifications, deadline reminders, auto-withdraw timers, cross-dossier coordination. There are four kinds, distinguished by *when* they execute and *how much* the engine records.

1. **`fire_and_forget`** — runs inline during the activity pipeline. Fast, ephemeral, no record. Errors are swallowed. Good for "send a notification, but don't fail the activity if the notifier is down."
2. **`recorded`** — picked up by the worker after the activity commits. A `system:task` entity is created; the function runs, success/failure is recorded. Good for notifications and deadline reminders that need audit trails.
3. **`scheduled_activity`** — picked up by the worker. When it fires, the engine emits *this specific activity* in the dossier automatically, as though a user triggered it. No plugin function involved. Good for deadline-driven state transitions ("if no decision in 60 days, auto-withdraw").
4. **`cross_dossier_activity`** — picked up by the worker. The plugin's task function returns a `TaskResult(target_dossier_id=..., content=...)` and the worker executes the target activity there, with PROV linking the two dossiers.

Kinds 2-4 all produce `system:task` entities and survive server restarts. Kind 1 does not.

#### Fire-and-forget tasks

```yaml
activities:
  - name: "dienAanvraagIn"
    tasks:
      - kind: "fire_and_forget"
        function: "my_plugin.tasks.send_ontvangstbevestiging_email"
```

```python
async def send_ontvangstbevestiging_email(ctx):
    # Send email; if SMTP is down, log and return.
    # The activity has already succeeded by the time this runs;
    # failure here doesn't roll anything back.
    ...
```

Task function signature: `async def fn(ctx: ActivityContext) -> None`. Exceptions are caught and logged; they don't affect the activity's transaction. No worker involvement — the function runs synchronously during the pipeline's task-processing phase, right after persistence.

**When to use:** side effects where best-effort is good enough and a record is overhead you don't need. If the work needs an audit trail or retry on failure, use `recorded` instead.

#### Recorded tasks

```yaml
activities:
  - name: "dienAanvraagIn"
    tasks:
      - kind: "recorded"
        function: "my_plugin.tasks.send_ontvangstbevestiging"
      - kind: "recorded"
        function: "my_plugin.tasks.check_behandeltermijn"
        scheduled_for: "+20d"
```

```python
async def send_ontvangstbevestiging(ctx):
    # ctx is an ActivityContext. Send email, generate PDF, etc.
    pass
```

Task function signature: `async def fn(ctx: ActivityContext) -> None`. Return value is ignored — the worker only cares whether the function raised. Success or failure is recorded as a new version of the task entity, so "did this task run and did it succeed" is queryable after the fact.

**When to use:** any background work that needs a record. Deadline reminders, batch emails that should retry on failure, emit-to-external-system. If errors should trigger retries or alerts, this is the kind you want.

#### Scheduled_activity tasks

```yaml
activities:
  - name: "dienAanvraagIn"
    tasks:
      - kind: "scheduled_activity"
        target_activity: "trekAanvraagIn"
        scheduled_for: "+60d"
        cancel_if_activities: ["vervolledigAanvraag", "neemBeslissing"]
```

Fields:

- `target_activity` — the activity the engine will emit when the task fires.
- `scheduled_for` — when.
- `cancel_if_activities` — list of activity types that, if any of them runs before the scheduled time, cancel this task. Prevents the "aanvraag was completed, but the 60-day auto-withdraw still fired" footgun.

No Python function needed — the engine handles everything. The target activity must exist in the same workflow.

**Building scheduled_activity tasks from a handler** — when the schedule depends on computed data (entity content, runtime config), return the task from your handler instead of declaring in YAML:

```python
from datetime import datetime, timezone, timedelta
from dossier_engine.engine.context import HandlerResult

async def handle_dienAanvraagIn(context, content):
    deadline_days = context.constants.aanvraag_deadline_days
    deadline = (
        datetime.now(timezone.utc) + timedelta(days=deadline_days)
    ).isoformat()

    return HandlerResult(
        content=content,
        tasks=[{
            "kind": "scheduled_activity",
            "target_activity": "trekAanvraagIn",
            "scheduled_for": deadline,
            "cancel_if_activities": ["vervolledigAanvraag"],
        }],
    )
```

Handler-built tasks have the same shape as YAML-declared tasks. The difference is who chooses the parameters.

When the deadline is simply a fixed offset from a datetime already on an entity (e.g. *"30 days after the aanvraag was registered"*), you don't need a handler — the `{from_entity, field, offset}` dict form of `scheduled_for` covers that directly in YAML (see *Scheduling formats* below). Reach for a handler only when the logic involves runtime config (as above), computation across more than one entity, or Python-level arithmetic the DSL doesn't express.

#### Cross_dossier_activity tasks

```yaml
activities:
  - name: "publiceerBeslissing"
    tasks:
      - kind: "cross_dossier_activity"
        function: "my_plugin.tasks.create_subsidy_notice"
        target_activity: "ontvangBesluit"
        scheduled_for: "+0"
```

```python
from dossier_engine.engine.context import TaskResult

async def create_subsidy_notice(ctx) -> TaskResult:
    beslissing = await ctx.get_singleton_typed("oe:beslissing")
    return TaskResult(
        target_dossier_id=str(beslissing.linked_subsidy_dossier_id),
        content={"reference": beslissing.reference, "decision": beslissing.beslissing},
    )
```

The worker:
1. Calls your function to get the `TaskResult` (which dossier + what content).
2. Looks up the target dossier's plugin (may be a different workflow).
3. Executes the target activity in that dossier, with the source dossier's URI recorded in `used` (as `urn:dossier:{source_id}`) and `informed_by` pointing at the source activity.
4. Marks the source-side task completed with a PROV link to the target activity.

**When to use cross_dossier_activity:** when action in one dossier should trigger action in another (subsidy-decision → notice-creation in a separate subsidy-register dossier). Keeps each dossier's PROV graph clean while preserving the inter-dossier link.

#### Scheduling formats

`scheduled_for` accepts four forms:

- **Relative offset** — `+Nd` / `+Nh` / `+Nm` / `+Nw` (days, hours, minutes, weeks). The sign is required and can be `+` or `-`; it's what tells the parser this is an offset rather than a malformed date. Resolved against the activity's start time. Negative offsets resolve to a time in the past, which the worker picks up on its next poll — useful for "fire immediately" semantics or for deadlines that have already elapsed.
- **Absolute ISO 8601** — `"2026-12-31T23:59:59Z"`, `"2026-05-01T12:00:00+02:00"`.
- **Entity field reference** — a dict `{from_entity, field}` reading a datetime from an entity this activity uses or generates. The field must contain an ISO 8601 datetime string, a date-only string (`"2026-05-01"` → midnight UTC), or a Python `datetime` (for handler-built tasks that insert one directly). Uses the same `from_entity`/`field` idiom you already know from authorization scopes and finalization status mappings. Raises 500 at scheduling time if the entity isn't in the activity's `used`/`generated` block, or if the field is null/missing/unparseable.
- **Entity field + offset** — the same dict with an additional signed offset. The killer use case for reminders: *"fire 7 days before the permit expires"* is `{from_entity: ..., field: expires_at, offset: "-7d"}`. The offset uses the same signed-relative grammar (`+20d` / `-7d`).
- **Omit** — the task runs immediately after the activity completes.

Shape of the entity-field form:

```yaml
scheduled_for:
  from_entity: "oe:aanvraag"
  field: "content.registered_at"   # or "registered_at" — leading "content." is optional
  offset: "+7d"                    # optional; signed (+ or -)
```

For schedules depending on more than one entity, or needing arithmetic the DSL doesn't cover, compute `scheduled_for` inside a handler and return a pre-formatted ISO string.

#### Supersession — what happens when you schedule the same task twice

By default, scheduling a new task with the same `target_activity` (or `function`) supersedes any existing scheduled task in the same dossier. The prior task's content is rewritten to `status: superseded` so the worker skips it. Only one scheduled instance of a given target per dossier is ever on the worker's queue at a time. This makes the "update the deadline" pattern work naturally — just call the scheduling activity again and the new deadline wins.

Set `allow_multiple: true` on the task declaration to opt out — then multiple scheduled tasks with the same shape coexist. Most plugins don't need this; the default is usually what you want.

`allow_multiple` only governs scheduling. Cancellation (via `cancel_if_activities`) still fires on every matching task regardless — a task being allowed to coexist with others doesn't change whether the event it's waiting on has fired.

### Workflow rules — `requirements` and `forbidden`

Every activity can declare what must have happened (or must *not* have happened) in the dossier before it's allowed to run. Example: you can't `neemBeslissing` before `dienAanvraagIn`, and you can't `trekAanvraagIn` after `neemBeslissing`:

```yaml
activities:
  - name: "neemBeslissing"
    requirements:
      activities: ["dienAanvraagIn"]
      statuses: ["in_behandeling"]
      entities: ["oe:aanvraag"]
    forbidden:
      activities: ["trekAanvraagIn"]
      statuses: ["ingetrokken", "afgewezen"]
```

Each sub-key is optional; any combination works.

- `activities:` — these activities must (for requirements) / must not (for forbidden) have a completed instance in the dossier.
- `statuses:` — the dossier's current cached status must match one of these (for requirements) / must match none of these (for forbidden).
- `entities:` — (requirements only) at least one entity of each listed type must exist. `forbidden` has no `entities:` key — the engine only checks forbidden `activities` and `statuses`. Model "this type must not exist" with a status check instead, or raise in a handler.

The engine checks `requirements` before running the activity and raises 422 on failure. `forbidden` is the inverse — the activity is rejected if any listed item *does* apply. The failing requirement is named in the error so the client knows what they're missing.

**When to use:** any time the activity has a precondition that isn't purely about input validation. If the activity's validity depends on the dossier's history, use `requirements`. If it depends on the dossier's history *not* containing certain events, use `forbidden`.

#### Time-based rules — `not_before` and `not_after`

Activities can declare temporal windows inside the same `requirements` / `forbidden` blocks:

- `requirements.not_before` — the earliest wall-clock moment the activity becomes legal. Before that, the activity is blocked with a "not yet available" error.
- `forbidden.not_after` — the deadline past which the activity is no longer legal. After that, it's blocked with a "deadline has passed" error.

Both accept the same three value shapes:

```yaml
activities:
  - name: "objectionWindow"
    # Absolute ISO 8601 — known fixed deadline.
    requirements:
      not_before: "2026-01-01T00:00:00Z"
    forbidden:
      not_after: "2026-12-31T23:59:59Z"
```

```yaml
activities:
  - name: "renewPermit"
    # Entity field reference — deadline comes from a singleton.
    forbidden:
      not_after:
        from_entity: "oe:permit"
        field: "expires_at"
```

```yaml
activities:
  - name: "sendExpiryReminder"
    # Entity field + signed offset — the reminder idiom.
    # "fire 7 days before permit expiry"
    forbidden:
      not_after:
        from_entity: "oe:permit"
        field: "expires_at"
        offset: "-7d"
```

**Singletons only** for the entity-field forms. Multi-cardinality types don't work — "which instance's deadline applies?" has no unambiguous answer. The plugin validator rejects non-singleton references at startup; the runtime resolver also defends against them. If you need per-instance deadlines, compute them in a handler and return a pre-formatted ISO string via `scheduled_for` (for tasks) or gate the activity via a custom status check.

**Relative offsets from "now" are not supported** on deadlines. `"+20d"` at check time has no fixed anchor — the deadline would slide every time the check ran. Use an absolute ISO string or anchor to an entity field instead.

**Anchor missing = rule inactive.** When a dict-form rule points at a singleton type the dossier doesn't have yet, the resolver returns `None` and the rule is treated as not firing. Combine with `requirements.entities: [oe:permit]` to gate the activity behind the anchor's existence:

```yaml
activities:
  - name: "renewPermit"
    requirements:
      entities: ["oe:permit"]         # activity requires a permit to exist…
    forbidden:
      not_after:                      # …and can only run before it expires.
        from_entity: "oe:permit"
        field: "expires_at"
```

**Eligible-activities response.** When an activity declares a deadline rule and is currently eligible, the resolved ISO deadline is included in the allowed-activities response as a flat `not_before` / `not_after` field:

```json
{
  "type": "renewPermit",
  "label": "Renew Permit",
  "not_after": "2026-12-31T00:00:00+00:00"
}
```

Frontends can use it for "expires in 3 days" countdowns or disabled-but-visible hints. Fields are only present when the declaration resolves successfully — missing (for singleton-missing cases) rather than null.

**Cache staleness.** The `eligible_activities` cache on the dossier row is invalidated on every activity execution, not on wall-clock passage. That means an activity whose `not_after` just ticked over stays in the cached list until something else runs in the dossier. The **execution path always does a fresh check** — if a user clicks the stale-but-now-expired activity, the engine returns 422 from the full `validate_workflow_rules` call. Stale list is a display concern; correctness is never stale.

### Access control — roles and authorization

Who can run which activity? Two complementary mechanisms:

**`allowed_roles`** — flat list of roles that can run the activity:

```yaml
activities:
  - name: "neemBeslissing"
    allowed_roles: ["behandelaar", "beheerder"]
    default_role: "behandelaar"
```

Simplest form. If the request-making user has any role in the list, the activity is allowed.

**`authorization`** — full authorization block with access level + role-matching shapes:

```yaml
activities:
  - name: "publiekeZoekopdracht"
    authorization:
      access: "everyone"     # no authentication required

  - name: "bekijkEigenDossier"
    authorization:
      access: "authenticated"   # default — any logged-in user

  - name: "behandelAanvraag"
    authorization:
      access: "roles"
      roles:
        # Shape 1: direct match — the user has this exact role.
        - role: "beheerder"

        # Shape 2: scoped match — the role string is composed at runtime
        # from a base role + a value resolved from an entity field.
        # User must have "gemeente-toevoeger:1234" if the aanvraag's
        # gemeente field is "1234".
        - role: "gemeente-toevoeger"
          scope:
            from_entity: "oe:aanvraag"
            field: "content.gemeente"

        # Shape 3: entity-derived — the entity field value IS the role.
        # User must have a role matching the aanvrager's RRN.
        - from_entity: "oe:aanvraag"
          field: "content.aanvrager.rrn"
```

`access` picks the gate level:
- `"everyone"` — public endpoint, no auth at all.
- `"authenticated"` (default if omitted) — any logged-in user.
- `"roles"` — check against the `roles:` list below, using the three shapes.

The three role shapes under `access: roles` let you express "this role has global scope," "this role is scoped to an attribute value," and "this user is this specific aanvrager." The engine tries each role entry in turn; the user must satisfy at least one for the activity to proceed. Denial reasons name every entry that didn't match, so debugging is straightforward.

**`can_create_dossier`** — allows the activity to run without a pre-existing dossier. Set to `true` on your entry activity (the one that creates the dossier). All other activities require the dossier's existence.

**`default_role`** — used when the same user has multiple roles that could authorize the activity. The engine records the default role on the resulting activity's association row, so audit-time "who did this with what role" is deterministic.

### Tombstoning — GDPR-style redaction

The engine provides a built-in `oe:tombstone` activity that redacts entity content while preserving the PROV graph structure. Workflows opt in by declaring which roles are allowed to trigger it:

```yaml
tombstone:
  allowed_roles: ["beheerder"]
```

The engine auto-registers a `tombstone` activity in your workflow when the block is declared. No Python code needed; no YAML activity entry needed beyond the top-level `tombstone:` block. Tombstoning sets every entity's `content` to null, records a tombstone activity in the PROV graph, and marks the dossier tombstoned (a flag the frontend uses to render differently).

Leave the `tombstone:` block out and the activity isn't registered — users can't tombstone dossiers of this workflow at all. The role list is workflow-specific; there's no engine-wide default.

### Schema versioning — entity content that changes shape over time

When an entity's Pydantic model grows a new field or changes a field's type, you can't just edit the existing model — old rows in the database still have the old shape. Schema versioning lets multiple versions of a model coexist:

```python
class AanvraagV1(BaseModel):
    aanvrager_naam: str
    onderwerp: str

class AanvraagV2(BaseModel):
    aanvrager_naam: str
    onderwerp: str
    gemeente: str  # new required field
```

```yaml
entity_types:
  - type: "oe:aanvraag"
    model: "my_plugin.entities.AanvraagV1"  # legacy/default
    schemas:
      v1: "my_plugin.entities.AanvraagV1"
      v2: "my_plugin.entities.AanvraagV2"
```

Activities declare which versions they accept and which they produce:

```yaml
activities:
  - name: "dienAanvraagIn"
    entities:
      oe:aanvraag:
        new_version: "v2"

  - name: "bewerkAanvraag"
    entities:
      oe:aanvraag:
        allowed_versions: ["v1", "v2"]
        new_version: "v2"
```

- `new_version:` — the schema version the activity writes. Activities that generate this entity produce rows stamped `schema_version = "v2"`.
- `allowed_versions:` — the schema versions this activity accepts as input. If the used entity has a different version, the engine rejects 422 before your handler runs.

Reading is version-aware too. `context.get_typed("oe:aanvraag")` consults each row's stored `schema_version` and returns an instance of the matching model class — `AanvraagV1` for legacy rows, `AanvraagV2` for new ones. Your handler can branch on `isinstance(aanvraag, AanvraagV2)` to handle both shapes during migration.

**Load-time validation**: every `new_version:` / `allowed_versions:` reference is checked against the declared `schemas:` block. Typos fail the plugin at startup, not silently at runtime.

**Legacy compatibility**: rows with `schema_version = NULL` fall back to the top-level `model:` field. Plugins that don't version anything can leave `schemas:` empty entirely.

### Workflow constants and environment variables

Workflow constants are typed config: deadline durations, feature flags, external service URLs, API keys. Three precedence layers, highest wins:

1. **Environment variables** (operator escape hatch, secrets).
2. **`constants.values` block in workflow.yaml** (plugin author's domain-level tuning).
3. **Pydantic class defaults**.

```python
# my_plugin/constants.py
from pydantic_settings import BaseSettings

class ToelatingenConstants(BaseSettings):
    aanvraag_deadline_days: int = 30
    publication_freeze_active: bool = False
    external_erfgoedobjecten_url: str = "https://id.erfgoed.net"

    model_config = {"env_prefix": "TOELATINGEN_"}
```

```yaml
constants:
  values:
    aanvraag_deadline_days: 60    # overrides the class default
    publication_freeze_active: false
```

```python
# my_plugin/__init__.py
from .constants import ToelatingenConstants

def create_plugin() -> Plugin:
    # ...load workflow as before...
    yaml_constants = (workflow.get("constants") or {}).get("values", {}) or {}
    constants = ToelatingenConstants(**yaml_constants)
    return Plugin(..., constants=constants)
```

```bash
# Operator overrides domain-level value
TOELATINGEN_AANVRAAG_DEADLINE_DAYS=90 ./run_server.sh
```

Access in handlers via `context.constants.aanvraag_deadline_days`. Access in hooks and factories (which don't have a context) via `plugin.constants.aanvraag_deadline_days`. Returns `None` if the plugin didn't declare a constants class; accessing attributes on `None` raises `AttributeError`, so the clearer pattern is "declare an empty class rather than leave undeclared."

### Search — Elasticsearch integration

The platform has a two-tier index model: an engine-owned `dossiers-common` index (one doc per dossier across all workflows) and an optional workflow-specific index (`dossiers-{workflow}`) that you own.

Three plugin hooks wire into it:

**1. `post_activity_hook` — incremental indexing.** Runs after every activity completes. Upserts both indexes. No-op when `DOSSIER_ES_URL` is empty (so dev doesn't require Elasticsearch).

```python
async def update_index(repo, dossier_id, activity_type, status, entities):
    # Build the per-dossier doc, upsert to your workflow index, upsert to common.
    ...

Plugin(..., post_activity_hook=update_index)
```

**2. `search_route_factory` — custom search endpoints.** Registers routes like `/dossiers/{workflow}/search`:

```python
def register_search_routes(app, get_user):
    @app.get("/dossiers/my_plugin/search")
    async def search(q: str, user = Depends(get_user)):
        # ACL-aware query against your workflow index
        ...

Plugin(..., search_route_factory=register_search_routes)
```

**3. `build_common_doc_for_dossier` — bulk reindex builder.** Called by the engine's admin reindex endpoint when it walks every dossier to rebuild the common index from Postgres. **Strongly recommended** — without it, the engine falls back to a bare doc (empty onderwerp, global-access-roles-only ACL), which makes every non-global user invisible from search until the next per-activity upsert.

```python
from dossier_engine.search.common_index import build_common_doc

async def build_common_doc_for_dossier(repo, dossier_id):
    access = await repo.get_singleton_entity(dossier_id, "oe:dossier_access")
    aanvraag = await repo.get_singleton_entity(dossier_id, "oe:aanvraag")
    return build_common_doc(
        dossier_id=dossier_id,
        workflow_name="my_plugin",
        onderwerp=aanvraag.content.get("onderwerp") if aanvraag else "",
        access_content=access.content if access else None,
    )

Plugin(..., build_common_doc_for_dossier=build_common_doc_for_dossier)
```

The engine provides `build_common_doc(...)` to assemble the standard doc shape — plugin code only supplies onderwerp and the access entity's content, so ACL derivation stays consistent across plugins.

### Pre-commit hooks — strict validation that can roll back

Most of the time, `post_activity_hook` is the right shape: fire-and-forget side effects whose failure shouldn't roll back the activity. But sometimes you need the inverse — validation or side-effect work that *must* succeed or the activity should fail.

```python
async def verify_pki_signature(*, repo, dossier_id, plugin, activity_def,
                               generated_items, used_rows, user):
    for item in generated_items:
        if item.get("type") == "oe:beslissing":
            signature = item["content"].get("signature")
            if not verify_signature(signature, user.certificate):
                raise ActivityError(422, "PKI signature invalid")

Plugin(..., pre_commit_hooks=[verify_pki_signature])
```

Pre-commit hooks run after persistence but *before* transaction commit. Unlike `post_activity_hook`, exceptions are NOT swallowed — they propagate and roll back the whole activity. Use for synchronous validation or side effects that must succeed or the activity should be rejected:

- PKI signature checks
- External ID reservations (reserve a number sequence, fail the activity if the external system rejects)
- Mandatory file service operations (move uploads to permanent storage; if it fails, don't commit the activity that referenced them)

Hooks run in registered order. First raise wins — subsequent hooks don't run. Raise `ActivityError` for structured HTTP responses; any other exception becomes a 500.

### Activity names — qualified vs bare

Activity names in YAML can be bare (`dienAanvraagIn`) or qualified (`oe:dienAanvraagIn`). The engine normalizes everything to qualified form at plugin load — the workflow's default prefix (from the `namespaces:` block, or `oe` if unset) prepends the bare name.

```yaml
namespaces:
  default: "https://id.erfgoed.net/oe#"
  prefixes:
    oe: "https://id.erfgoed.net/oe#"
```

Internally the engine always sees `oe:dienAanvraagIn`. API requests can use either form; the URL router normalizes.

**When it matters:** if two plugins use the same bare activity name, they need distinct prefixes to keep them separated in the PROV graph. For a single-plugin deployment, the default `oe:` is fine and you can write bare names everywhere in YAML.

### Using external ontologies

PROV entities and relations use IRIs — full `https://` URLs identify types across systems. The `namespaces:` block lets you register prefix shortcuts:

```yaml
namespaces:
  default: "https://id.erfgoed.net/oe#"
  prefixes:
    oe: "https://id.erfgoed.net/oe#"
    prov: "http://www.w3.org/ns/prov#"
    dcterms: "http://purl.org/dc/terms/"
```

Then in your YAML and code, write `dcterms:created` instead of the full URL. The engine expands prefixes on input, preserves them on display.

## What you don't need to think about

The engine handles, without any plugin code:

- **HTTP layer** — routing, JSON deserialization, Pydantic validation of request bodies against your entity models, error handling, OpenAPI generation.
- **Persistence** — the PROV graph (activity / entity / used / generated / association rows), dossier status, schema versioning, transaction boundaries.
- **Worker loop** — task polling, claiming, execution, retry, cross-dossier coordination. Your task functions are just async callables.
- **Authentication** — POC auth via HTTP Basic (dev/test), real auth via whatever the deployment wires in. Your plugin receives `User` objects; you don't parse headers.
- **Authorization mechanics** — the three role-matching shapes, the scope resolution, the caching. Your plugin declares rules in YAML; the engine evaluates them.
- **Search infrastructure** — the common index, admin endpoints, ACL derivation. You write the workflow-specific per-dossier doc builder; the engine handles the rest.
- **Audit logging** — SIEM-worthy events for access denials, failed authorizations, tombstone activities. Your plugin just does its thing.
- **Pipeline phases** — the ordered sequence of validation, handler invocation, relations processing, persistence, projection. You plug into specific points; you don't orchestrate.

If you find yourself needing to know about any of the above in detail, it's probably for debugging — `docs/pipeline_architecture.md` has the engine internals.

---

# Part 2 — Reference

Part 2 is scan-oriented: exhaustive tables with minimal narrative, cross-linked back to the tutorial sections that teach each feature. Skim for what you need; read the tutorial for the "why" and "when."

## The Plugin dataclass

The `Plugin` dataclass is the runtime registration object. Your `create_plugin()` constructs one and returns it; the engine stores it in the `PluginRegistry` under the workflow's name.

Source: `dossier_engine/plugin.py`.

### Required fields

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Workflow name, matches the YAML's top-level `name:`. Used as URL prefix in API routes (`/dossiers/{name}/...`). |
| `workflow` | `dict` | Parsed workflow.yaml. The engine inspects this at load (validators) and runtime (dispatch, permission checks). |
| `entity_models` | `dict[str, type[BaseModel]]` | Entity type name → Pydantic model. Populated by `build_entity_registries_from_workflow` from YAML `entity_types[*].model:` dotted paths. |

### Optional fields — registries of named callables

Each is a `dict[name, callable]`. YAML references them by name; the engine resolves via dict lookup. Keys are names your YAML chooses; values are async functions.

| Field | Type | What YAML declares |
|---|---|---|
| `handlers` | `dict[str, Callable]` | `activity.handler: "name"` |
| `validators` | `dict[str, Callable]` | `activity.validators: ["name", ...]` — standalone cross-entity validators |
| `task_handlers` | `dict[str, Callable]` | `task.function: "name"` for `recorded` and `cross_dossier_activity` kinds |
| `status_resolvers` | `dict[str, Callable]` | `activity.status_resolver: "name"` |
| `task_builders` | `dict[str, Callable]` | `activity.task_builders: ["name", ...]` |
| `side_effect_conditions` | `dict[str, Callable]` | `side_effect.condition_fn: "name"` |
| `relation_validators` | `dict[str, Callable]` | `activity.relations[*].validator: "name"` or `validators: {add: "name", remove: "name"}`. **Keys must not collide with declared relation type names** — Bug 78 load-time validator rejects at startup. |
| `field_validators` | `dict[str, Callable \| FieldValidator]` | Exposed at `POST /{workflow}/validate/{name}`. `FieldValidator` wrapper adds request/response Pydantic models for OpenAPI. |

### Optional fields — single callables and lists

| Field | Type | Description |
|---|---|---|
| `post_activity_hook` | `Callable \| None` | Advisory post-commit hook. Exceptions swallowed. Signature: `async def hook(repo, dossier_id, activity_type, status, entities) -> None` |
| `pre_commit_hooks` | `list[Callable]` | Strict pre-commit hooks. Exceptions propagate and roll back. Run in registered order; first raise wins. Signature: `async def hook(*, repo, dossier_id, plugin, activity_def, generated_items, used_rows, user) -> None` |
| `search_route_factory` | `Callable \| None` | Called during route registration. Signature: `def factory(app, get_user) -> None` |
| `build_common_doc_for_dossier` | `Callable \| None` | Per-dossier common-index doc builder. Signature: `async def build(repo, dossier_id) -> dict \| None`. Returning `None` skips this dossier in reindex. |

### Optional fields — other

| Field | Type | Description |
|---|---|---|
| `entity_schemas` | `dict[tuple[str, str], type[BaseModel]]` | `(type, version) → model` for schema-versioned entities. Populated by `build_entity_registries_from_workflow` from YAML `entity_types[*].schemas:`. |
| `constants` | `BaseSettings \| None` | Typed workflow constants. Populated at plugin load by `create_plugin()` using the class named in `constants.class:` in YAML. `None` if the plugin doesn't declare a constants class. |

### Plugin methods

| Method | Signature | Description |
|---|---|---|
| `cardinality_of(entity_type)` | `str` | `"single"` or `"multiple"`. Workflow declarations win; falls back to engine defaults (`system:task`, `system:note`, `external` → `"multiple"`; `oe:dossier_access` → `"single"`). |
| `is_singleton(entity_type)` | `bool` | Shorthand for `cardinality_of(entity_type) == "single"`. |
| `resolve_schema(entity_type, schema_version)` | `type[BaseModel] \| None` | Route (type, version) → model class. Consults `entity_schemas` first; falls back to `entity_models` when `schema_version` is None. |
| `find_activity_def(activity_type)` | `dict \| None` | Look up an activity's YAML block by qualified or bare name. |

## ActivityContext

Passed as the first argument to handlers, task functions, status resolvers, task builders, and side-effect condition functions. Provides typed access to used entities plus a few helpers that hide the cardinality distinction.

Source: `dossier_engine/engine/context.py`.

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `repo` | `Repository` | Database repository for ad-hoc queries. Use when the helpers below don't cover your case. |
| `dossier_id` | `UUID` | The current dossier. |
| `user` | `User \| None` | **Executor identity** — who is running this code right now. For direct request handlers, the request-maker. For side effects and worker tasks, the system user. Use for "who is doing this?" |
| `triggering_user` | `User \| None` | **Attribution identity** — who is attributed with the activity that caused this context. For direct handlers, same as `user`. For side effects, the original request-maker. For worker tasks, the user attributed with the triggering activity. Use for audit events and denial reasons. |
| `triggering_activity_id` | `UUID \| None` | ID of the activity that triggered this context. Set for task handlers (the activity that scheduled the task). `None` for direct handlers (the context *is* the triggering activity). |
| `constants` | `BaseSettings \| None` | Shortcut to `plugin.constants`. |

The `user` / `triggering_user` split is documented in the Round 18 review notes and the `ActivityContext` class docstring. Executor and attribution matter most in side effects and worker tasks where they diverge.

### Methods — used-entity lookup

These consult the entities declared in the current activity's `used:` block (resolved into `ActivityContext._used_entities` before the handler runs).

| Method | Signature | Description |
|---|---|---|
| `get_used_entity(entity_type)` | `EntityRow \| None` | The `EntityRow` for a used entity, or `None`. Low-level — gives you access to version_id, generated_by, schema_version, content, tombstone flags. |
| `get_used_row(entity_type)` | `EntityRow \| None` | Alias for `get_used_entity`. Prefer this name when you need the version id (e.g. for seeding a lineage walk). |
| `get_typed(entity_type)` | `BaseModel \| None` | Used entity's content as a validated Pydantic instance. Routes via `plugin.resolve_schema` — returns the right version's model class automatically. `None` if the entity doesn't exist or is tombstoned. |

### Methods — dossier-level lookup (async)

These query the whole dossier, not just the current activity's `used` block.

| Method | Signature | Description |
|---|---|---|
| `get_singleton_entity(entity_type)` | `async → EntityRow \| None` | The singleton entity row of this type in the dossier. Raises `CardinalityError` if the type isn't declared singleton. |
| `get_singleton_typed(entity_type)` | `async → BaseModel \| None` | Same as `get_singleton_entity` but returns the content as a typed Pydantic instance (version-aware via `resolve_schema`). Raises `CardinalityError` on non-singleton types. |
| `get_entities_latest(entity_type)` | `async → list[EntityRow]` | Latest version of each logical entity of this type. Works for both singleton and multi-cardinality — singletons yield a 0- or 1-element list. |
| `has_activity(activity_type)` | `async → bool` | Has this activity type been completed in this dossier? Useful for conditional logic that doesn't fit workflow-rules (`requirements`/`forbidden`). |

### When to use which

- **Need the content of an entity the activity explicitly used?** `get_typed(type)` — the most common case.
- **Need version IDs, schema version, timestamps?** `get_used_row(type)` — returns the EntityRow.
- **Need a singleton that's not in `used`?** `get_singleton_typed(type)` — async, queries the dossier directly.
- **Iterating multi-cardinality entities?** `get_entities_latest(type)` — yields the latest version of each logical entity.
- **Branching on workflow history?** `has_activity(type)` — but prefer declaring it in `requirements`/`forbidden` if the rule is static.
- **Doing something the helpers don't cover?** `self.repo` gives you the full Repository API.

## HandlerResult

Return value from a plugin handler. Source: `dossier_engine/engine/context.py`.

```python
HandlerResult(
    content=None,          # dict | None — single-entity convenience shape
    generated=None,        # list[dict] | None — explicit list of entities to generate
    status=None,           # str | None — override the activity's YAML status
    tasks=None,            # list[dict] | None — task definitions to schedule
)
```

### Fields

| Field | Type | Description |
|---|---|---|
| `content` | `dict \| None` | Convenience: when the activity's YAML declares a single type in `generates:` and the handler just returns one dict of content, the engine infers the type. Equivalent to `generated=[{"type": None, "content": content}]`. |
| `generated` | `list[dict \| tuple] \| None` | Explicit list of entities to generate. Each item is either a `(type, content)` tuple (legacy) or a dict with `type`, `content`, optional `entity_id`, optional `derived_from`. **Multi-cardinality types must use the dict form** to specify which logical entity is being revised. |
| `status` | `str \| None` | Override the activity's YAML-declared status. If set and the activity also declares `status_resolver:`, the engine raises at load — "who decides the status" must be unambiguous. |
| `tasks` | `list[dict] \| None` | Task definitions to schedule. Same shape as activity-level `tasks:` YAML entries. Runtime equivalent of declaring tasks in YAML; use when scheduling depends on computed content. If the activity declares `task_builders:`, handler-returned `tasks` must be empty — same unambiguity rule. |

### Which shape to return when

- **Single generated entity, type inferred from YAML `generates[0]`:** return `HandlerResult(content={...})`.
- **Single generated entity, explicit type:** return `HandlerResult(generated=[{"type": "oe:x", "content": {...}}])`.
- **Multi-cardinality revision** (revising an existing logical entity, not creating a new one): `HandlerResult(generated=[{"type": "...", "entity_id": existing_id, "content": {...}}])`.
- **Multiple entities in one activity:** `HandlerResult(generated=[entity1, entity2])`.
- **No generated entities, just status or tasks:** `HandlerResult(status="...")` or `HandlerResult(tasks=[...])`.

## TaskResult

Return value from a `cross_dossier_activity` task function. Source: `dossier_engine/engine/context.py`.

```python
TaskResult(
    target_dossier_id="<uuid str>",   # required
    content=None,                     # dict | None — content for the target activity
)
```

| Field | Type | Description |
|---|---|---|
| `target_dossier_id` | `str` | UUID of the dossier where the target activity will run. Your function computes this from whatever logic the workflow needs. |
| `content` | `dict \| None` | Content for the target activity's first `generates[0]` entity. The worker wraps this in the request shape the target activity's pipeline expects. |

Cross-dossier tasks are the only kind that return a value — recorded and scheduled_activity tasks return `None` (the worker just checks for non-exceptional completion).

## Task kinds reference

Four kinds, distinguished by *where* they execute and *whether* they leave a record.

| Kind | Runs in | Entity created? | Errors | Where dispatched |
|---|---|---|---|---|
| `fire_and_forget` | Activity pipeline (inline) | No | Swallowed | `engine/pipeline/tasks.py::_fire_and_forget` |
| `recorded` | Worker (background) | Yes (`system:task`) | Marked failed, retried | `worker.py::_process_recorded` |
| `scheduled_activity` | Worker (background) | Yes (`system:task`) | Marked failed, retried | `worker.py::_process_scheduled_activity` |
| `cross_dossier_activity` | Worker (background) | Yes (`system:task`) | Marked failed, retried | `worker.py::_process_cross_dossier` |

Kinds 2-4 produce `system:task` entities and survive server restarts. Kind 1 doesn't — it runs once during the activity's pipeline and leaves nothing behind.

### `fire_and_forget`

**Shape:**

```yaml
- kind: "fire_and_forget"
  function: "my_plugin.tasks.fn_name"
```

No `scheduled_for` or `cancel_if_activities` — fire_and_forget runs once, inline, synchronously during the activity's task-processing phase.

**Function signature:** `async def fn(ctx: ActivityContext) -> None`.

**Semantics:** the engine calls the function immediately after persistence (but before `post_activity_hook`). Exceptions are caught and logged; they don't affect the transaction or propagate to the client. If the function is unregistered or the task dict has no `function:`, the task is silently skipped.

**Use for:** best-effort side work that should never block or fail the activity. Sending non-critical notifications, emitting metrics, triggering cache-warm requests on external services.

### `recorded`

**Shape:**

```yaml
- kind: "recorded"
  function: "my_plugin.tasks.fn_name"
  scheduled_for: "+20d"           # optional
```

**Function signature:** `async def fn(ctx: ActivityContext) -> None`.

**Semantics:** the worker invokes the function at the scheduled time. Function returns normally → task marked completed. Function raises → task marked failed, retries applied per worker policy. `cancel_if_activities` is supported but unusual for recorded tasks; most recorded tasks just fire.

**Use for:** notifications, deadline reminders, integration calls — anything that needs an audit trail or retry semantics. Compare with `fire_and_forget` above for inline best-effort work.

### `scheduled_activity`

**Shape:**

```yaml
- kind: "scheduled_activity"
  target_activity: "trekAanvraagIn"
  scheduled_for: "+60d"
  cancel_if_activities: ["vervolledigAanvraag"]   # optional — cancelling activities
```

**No function needed.** The engine runs the named activity automatically.

**Semantics:** at the scheduled time, if no activity in `cancel_if_activities` has run in the dossier since the task was created, the engine invokes `target_activity` as the system user. The target activity's `used` block is filled in by the engine's normal auto-resolve (trigger scope → singleton fallback). PROV records the scheduling activity as `wasInformedBy` of the resulting activity.

**Use for:** time-driven state transitions. The "auto-withdraw after 60 days of inactivity" pattern.

### `cross_dossier_activity`

**Shape:**

```yaml
- kind: "cross_dossier_activity"
  function: "my_plugin.tasks.fn_name"
  target_activity: "ontvangBesluit"
  scheduled_for: "+0"             # can be any scheduling shape
```

**Function signature:** `async def fn(ctx: ActivityContext) -> TaskResult`.

**Semantics:**

1. Worker calls the function to get a `TaskResult` (target dossier + content).
2. Looks up the target dossier's plugin (may be a different workflow).
3. Invokes `target_activity` in the target dossier as the system user, with `used=[{entity: "urn:dossier:<source_id>"}]` and `informed_by` pointing at the source task's triggering activity.
4. Marks the source-side task completed with a PROV link to the target activity.

**Use for:** inter-dossier effects — action in A triggers action in B. Keeps both PROV graphs clean while preserving the cross-dossier link.

### Cross-cutting fields (apply to `recorded`, `scheduled_activity`, `cross_dossier_activity`)

All three worker-backed kinds share a set of optional fields. `fire_and_forget` doesn't support any of them — it runs once, synchronously.

| Field | Type | Purpose |
|---|---|---|
| `scheduled_for` | `str` or `dict` | When to fire. See *Scheduling-format grammar* below. Omit for immediate. |
| `cancel_if_activities` | `list[str]` | Activity types that cancel this task if they run in the dossier before the scheduled time. |
| `allow_multiple` | `bool` | Default `false`. When `false`, scheduling a new task with the same `target_activity` (or `function`) in the same dossier supersedes any existing scheduled task — the prior's content is rewritten with `status: superseded`. When `true`, multiple scheduled tasks with the same shape coexist. `allow_multiple` only governs scheduling; cancellation still fires on every matching task regardless. |

**Supersession.** With `allow_multiple: false` (the default), the common "update the deadline" pattern works naturally — call the scheduling activity twice and the second call's task replaces the first. Only one scheduled instance of a given target per dossier is ever on the worker's queue at a time.

**Cancellation.** The canceling activity must be in the task's `cancel_if_activities` list and must run in the same dossier before the scheduled time. Prevents the "aanvraag was completed but the 60-day auto-withdraw still fired" footgun.

### Scheduling-format grammar

| Form | Example | Resolved against |
|---|---|---|
| Signed relative offset | `"+20d"`, `"-7d"`, `"+2h"`, `"+45m"`, `"+3w"` | Activity start time |
| Absolute ISO 8601 | `"2026-12-31T23:59:59Z"`, `"2026-05-01T12:00:00+02:00"` | — (literal) |
| Entity field reference | `{from_entity: "oe:aanvraag", field: "content.registered_at"}` | The field's datetime value |
| Entity field + offset | `{from_entity: "oe:aanvraag", field: "expires_at", offset: "-7d"}` | The field's datetime value shifted by the offset |
| Omitted | (no `scheduled_for` key) | Immediate — runs right after the activity |

Units: `m` minutes, `h` hours, `d` days, `w` weeks. A sign (`+` or `-`) is required on every relative offset — it's what tells the parser "this is an offset, not a date." Negative offsets are legal both at the top level (`"-7d"` = 7 days before activity start) and inside the entity field form (`offset: "-7d"` = 7 days before the field's value). Past-dated tasks are accepted; the worker picks them up on its next poll.

The entity referenced by the dict form must be in the activity's `used` or `generated` block — the resolver reads from `state.resolved_entities` at scheduling time. A missing entity, missing field, null value, or unparseable datetime string raises a 500 at activity execution so YAML authors get a clear error instead of a silently-wrong schedule.

## Workflow YAML schema

Every key the engine reads, organised by scope. Keys not listed here are ignored silently — if you typo a key name, the engine won't catch it. Match case exactly.

### Top-level keys

| Key | Required | Description |
|---|---|---|
| `name` | ✓ | Workflow name. Must match what `config.yaml` registered and what the `Plugin` constructor receives. |
| `activities` | ✓ | List of activity definitions. See *Activity-level keys* below. |
| `entity_types` | ✓ | List of entity type declarations. See *Entity type keys* below. |
| `relations` | — | List of workflow-level relation declarations. See *Relations reference* below. |
| `namespaces` | — | Namespace prefixes. Keys: `default` (default prefix for bare activity names), `prefixes` (dict of prefix → IRI). |
| `reference_data` | — | Dict of `{key: list-of-items}` — static data served via `GET /workflows/{name}/reference_data/{key}`. |
| `constants` | — | Typed workflow constants. Sub-key: `values` (dict of overrides, merged into the constants-class instantiation). No `class:` key here — the plugin's `create_plugin()` imports the BaseSettings class directly and passes `values` via `**kwargs`. |
| `tombstone` | — | Opts the workflow into the engine-provided tombstone activity. Sub-keys: `allowed_roles` (list of roles permitted to tombstone). |
| `poc_users` | — | List of user dicts for the POC auth middleware. Only used in dev/test; production auth ignores this block. |

### Entity type keys

Under `entity_types[*]`:

| Key | Required | Description |
|---|---|---|
| `type` | ✓ | Qualified type string, e.g. `"oe:aanvraag"`. |
| `model` | ✓ | Dotted Python path to the Pydantic model class. Engine imports at plugin load. Used as legacy/default when `schema_version` is NULL on a stored row. |
| `cardinality` | — | `"single"` or `"multiple"`. Default `"multiple"`. Singletons have at most one logical entity per dossier; multi-cardinality can have many. |
| `schemas` | — | Dict of `version → dotted path`. Each listed version's model class is registered in `entity_schemas` and consulted at read time via `resolve_schema`. |

### Activity-level keys

Under `activities[*]`:

| Key | Required | Description |
|---|---|---|
| `name` | ✓ | Activity name, qualified or bare. Engine normalizes to qualified at load. |
| `label` | — | Human-readable label for UIs. |
| `description` | — | Human-readable description. |
| `handler` | — | Name in `plugin.handlers`. Omit for default "store what's sent" behaviour. |
| `status_resolver` | — | Name in `plugin.status_resolvers`. Exclusive with handler-returned `status`. |
| `task_builders` | — | List of names in `plugin.task_builders`. Exclusive with handler-returned `tasks`. |
| `validators` | — | List of names in `plugin.validators`. Each runs after the handler; any raise rejects the activity. |
| `used` | — | List of entity types the activity consumes. Resolved from the request's `used:` block; made available in context. |
| `generates` | — | List of entity types the activity produces. The first type is the default for `HandlerResult(content=...)` inference. |
| `entities` | — | Per-type entity config. Dict of `type → {new_version, allowed_versions}` for schema-versioned entities. |
| `relations` | — | List of relation declarations (by name reference to workflow-level types). See *Relations reference*. |
| `status` | — | Literal string — the dossier status after this activity succeeds. |
| `tasks` | — | List of task declarations. See *Task kinds reference* above. |
| `side_effects` | — | List of activities to trigger automatically on success. Sub-keys per entry: `activity` (target), `condition` or `condition_fn` (mutually exclusive gate). |
| `requirements` | — | Preconditions. Sub-keys: `activities`, `entities`, `statuses` — all lists. See *Workflow rules* below. |
| `forbidden` | — | Negative preconditions. Same sub-keys as `requirements`. |
| `authorization` | — | Full authorization block with role-matching shapes. See *Access control reference* below. |
| `allowed_roles` | — | Simplified authorization — flat list of roles that can run the activity. Shorthand for `authorization.roles: [{role: X}, ...]`. |
| `default_role` | — | Default role to record on the activity's association row when the user has multiple eligible roles. |
| `can_create_dossier` | — | Boolean; default `false`. When `true`, the activity can run without a pre-existing dossier (creates one). Entry activities only. |
| `time` | — | Reserved — timestamp override for testing. Don't use in production YAML. |

### Relations reference

Post-Bug-78 contract (Round 26). Source: `dossier_engine/plugin.py::validate_relation_declarations`.

**Workflow-level** (under top-level `relations:`):

| Key | Required | Kind | Description |
|---|---|---|---|
| `type` | ✓ | Any | Qualified type string. |
| `kind` | ✓ | Any | `"domain"` or `"process_control"`. Drives runtime dispatch; dictates what request shape is expected. |
| `from_types` | — | Domain only | List of allowed source ref shapes: `"entity"`, `"external_uri"`, `"dossier"`. Omitting accepts any. Rejected on `process_control` at load. |
| `to_types` | — | Domain only | List of allowed target ref shapes. Same values as `from_types`. Omitting accepts any. Rejected on `process_control` at load. |
| `description` | — | Any | Free text. |

**Activity-level** (under `activities[*].relations:`):

| Key | Required | Description |
|---|---|---|
| `type` | ✓ | Must resolve to a workflow-level declaration. |
| `operations` | — | List: `["add"]`, `["remove"]`, or `["add", "remove"]`. `["remove"]` rejected on `process_control` types. |
| `validator` | — | Single name in `plugin.relation_validators`. Fires for all operations. Exclusive with `validators`. |
| `validators` | — | Dict `{add: "name", remove: "name"}` — both keys required. Rejected on `process_control` types. Exclusive with `validator`. |

**Forbidden at activity level:** `kind`, `from_types`, `to_types`, `description`. Declared workflow-level only.

**Forbidden combinations** (load-time rejection):

| Combination | Why |
|---|---|
| `kind: process_control` + `from_types:` at workflow level | from_types is domain-only (entity→entity shape) |
| `kind: process_control` + `to_types:` at workflow level | Same |
| `kind: process_control` + `validators:` dict at activity level | process_control has no remove operation |
| `kind: process_control` + `operations: [remove]` at activity level | Same |
| `validator:` + `validators:` at activity level | Mutually exclusive |
| `validators:` dict with keys other than `{add, remove}` at activity level | Partial dicts rejected; use `validator:` single-string instead |
| `relation_validators` dict key matching a declared type name | Would re-create Style-3 by-naming-convention. Rename the validator function. |

### Access control reference

**Simplified form** — `allowed_roles`:

```yaml
activities:
  - name: "X"
    allowed_roles: ["behandelaar", "beheerder"]
    default_role: "behandelaar"
```

The engine desugars to the equivalent `authorization.roles: [{role: "behandelaar"}, {role: "beheerder"}]`. Use when role strings are literal and scope-free.

**Full form** — `authorization.roles`. Each entry is one of three shapes:

| Shape | Example | Semantics |
|---|---|---|
| Direct | `{role: "beheerder"}` | User has this exact role in their roles list. |
| Scoped | `{role: "gemeente-toevoeger", scope: {from_entity: "oe:aanvraag", field: "content.gemeente"}}` | Engine resolves the entity's field value at request time; composes `{base}:{value}`; user must have that composed role. |
| Entity-derived | `{from_entity: "oe:aanvraag", field: "content.aanvrager.rrn"}` | Engine resolves the entity's field; the field value *is* the role the user must have. |

The engine tries entries in order. Any match → authorized. All fail → 403 with a denial reason listing each entry that didn't match.

**Scope resolution notes:**
- `from_entity` must be a singleton type in the dossier. Non-singleton types raise `CardinalityError` at evaluation.
- `field` is a dotted path (`content.gemeente`, `content.aanvrager.rrn`). `None` values at any step fail the match with a clear error.
- Scoped matching requires a dossier (both shapes read from the dossier's entities). `can_create_dossier` activities can only use direct-match.

### Workflow rules — `requirements` and `forbidden`

Evaluated before authorization. Source: `dossier_engine/engine/pipeline/authorization.py::validate_workflow_rules`.

**Sub-keys** (same for both blocks):

| Sub-key | Type | `requirements` semantic | `forbidden` semantic |
|---|---|---|---|
| `activities` | `list[str]` | Each must have completed at least once | None may have completed |
| `entities` | `list[str]` | At least one of each type must exist | None may exist |
| `statuses` | `list[str]` | Dossier's current status must be in the list | Dossier's current status must not be in the list |

Errors name the first failing check — "required activity 'X' not found," "forbidden status 'X' is current," etc. Fails with 422.

### Tombstone

```yaml
tombstone:
  allowed_roles: ["beheerder"]
```

Auto-registers `oe:tombstone` activity in the workflow's activity list at plugin load. Tombstone execution nulls every entity's content, stamps them tombstoned, and records a tombstone activity in the PROV graph.

Omit the block entirely to disable tombstoning for the workflow.

### Namespaces

```yaml
namespaces:
  default: "https://id.erfgoed.net/oe#"
  prefixes:
    oe: "https://id.erfgoed.net/oe#"
    prov: "http://www.w3.org/ns/prov#"
    dcterms: "http://purl.org/dc/terms/"
```

`default` is the prefix to qualify bare names against. `prefixes` declares the prefix → IRI map used for IRI expansion.

### Constants

```yaml
constants:
  values:
    deadline_days: 60
    feature_flag_x: true
```

```python
# my_plugin/__init__.py
from .constants import MyConstants

def create_plugin():
    yaml_constants = (workflow.get("constants") or {}).get("values", {}) or {}
    constants = MyConstants(**yaml_constants)
    return Plugin(..., constants=constants)
```

`values` is the only sub-key in YAML. The plugin's `create_plugin()` imports the `BaseSettings` class directly — there's no `class:` field naming a dotted path. Environment variables override `values` (per pydantic-settings standard resolution); `values` overrides class defaults.

### POC users

```yaml
poc_users:
  - id: "alice"
    username: "alice"
    password: "pwd"
    roles: ["behandelaar"]
    type: "person"
    name: "Alice Behandelaar"
    properties: {}
```

Dev/test only. Real deployments use their own auth middleware; this block is ignored.

## Engine-provided entity types

These types are registered automatically — don't declare them in `entity_types:`.

| Type | Cardinality | Purpose |
|---|---|---|
| `system:task` | multiple | Task entities produced by scheduling. One per logical task; versions track state transitions. |
| `system:note` | multiple | Free-form notes attached to a dossier. |
| `oe:dossier_access` | single | The per-dossier ACL entity; content drives search filtering and row-level authorization. |
| `external` | multiple | External URI references (bracketed dossiers, entities the workflow references but doesn't own). |

Your workflow can reference these types in `used:` blocks, in `entities:` configuration, in search builders, etc. Declaring them in `entity_types:` is redundant but not rejected.

## Engine-provided activities

Registered automatically when certain conditions hold. Don't declare these in `activities:`.

| Activity | When registered | Purpose |
|---|---|---|
| `systemAction` | Always | Initial bootstrap activity every dossier has. Used by the engine as the attachment point for cross-dossier references and to seed the PROV graph. |
| `oe:tombstone` | When workflow declares `tombstone:` block | GDPR-style redaction. |

## Glossary of error shapes

When you hit these, they're shouting from specific code paths:

| Exception | Where from | Meaning |
|---|---|---|
| `ValueError` at plugin load | `plugin.py` validators | YAML shape violation. Message names the exact rule. |
| `ActivityError(status, msg)` | Various pipeline phases | Structured HTTP error. `status` is 4xx or 5xx; `msg` goes to the client. |
| `CardinalityError` | `plugin.is_singleton` consumers | Called a singleton helper on a multi-cardinality type (or vice versa). |
| `LineageAmbiguous` | `lineage.find_related_entity` | PROV walk found multiple distinct candidates — structural data anomaly, needs triage. Round 25. |
