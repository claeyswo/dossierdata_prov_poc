# Dossier Type Definition Template

## Dossier Type

```yaml
name: ""                          # e.g. "subsidieaanvraag", "vergunningsaanvraag"
description: ""                   # human readable description
version: "1.0"                    # version of this workflow definition
```

---

## Roles

```yaml
# Functional roles in this workflow.
# These describe what someone DOES in the business process.
# They are used in PROV associations ("wasAssociatedWith ... role: behandelaar").
#
# How these map to technical roles in your auth system is
# defined per activity in the authorization section.
# There is NO naming convention between functional and technical roles.
#
# Activities define allowed_roles + default_role so the client
# doesn't need to specify a role in most cases.

roles:
  - name: ""                      # e.g. "oe:aanvrager", "oe:behandelaar", "oe:ondertekenaar"
    description: ""
```

---

## POC Users

```yaml
# Simulates what your auth framework provides in production.
#
# In production, the user object comes from your auth framework (JWT/OAuth/session):
#   user:
#     id: UUID
#     type: string              # "persoon", "medewerker", "systeem"
#     name: string
#     roles: [string]           # TECHNICAL roles from the auth system
#     properties:               # key-value pairs from your identity provider
#       some_field: "some_value"
#
# The roles array contains TECHNICAL role strings, not functional role names.
# The POC middleware looks users up by the X-POC-User header.

poc_users:

  - id: ""                        # UUID
    username: ""                  # used in X-POC-User header
    type: ""                      # "persoon", "medewerker", "systeem"
    name: ""                      # display name
    roles:
      - ""                        # TECHNICAL role strings from the auth system
    properties:                   # key-value pairs, must match what side effects reference
      field_name: "value"
```

---

## Entity Types

```yaml
# Entity types are defined as Pydantic models in Python code.
# The workflow YAML references them by module path.
#
# Entity types use a prefixed naming convention: "prefix:name"
# e.g. "oe:aanvraag", "gov:besluit", "sub:motivatie"
# The prefix IS the entity type everywhere — in the DB, in refs, in the YAML.

entity_types:
  - type: ""                      # e.g. "oe:aanvraag", "gov:motivatie"
    description: ""
    cardinality: ""               # "single" = one logical entity per dossier
                                  #   - auto_resolve returns the one entity
                                  #   - requirements check latest version
                                  # "multiple" = many logical entities per dossier
                                  #   - auto_resolve returns all of them
                                  #   - client specifies which one via derivedFrom for revisions
    revisable: true               # can new versions be created?
    model: ""                     # Python import path to the Pydantic model
                                  # e.g. "gov_dossier_subsidie.entities.Aanvraag"

  # Always include — managed by side effects, used for access control.
  # This model lives in the engine, not in the plugin.
  - type: "oe:dossier_access"
    description: "Bepaalt wie dit dossier kan zien en wat ze kunnen zien"
    cardinality: "single"
    revisable: true
    model: "gov_dossier_engine.entities.DossierAccess"
```

---

## Authorization

```yaml
authorization:
  # Gate: who is allowed to start a new dossier of this type?
  # Only checked when can_create_dossier activity is the first one.
  # Access types: "everyone", "authenticated", "roles"
  create_dossier:
    access: ""
    roles:                        # required if access is "roles"
      # Three technical role matching patterns:
      #
      # 1. Direct match: user must have this exact string in their roles
      - role: "some-exact-string"
      #
      # 2. Scoped: technical_role + ":" + value resolved from an entity.
      #    Only usable on existing dossiers, not for create_dossier.
      # - role: "gemeente-toevoeger"
      #   scope:
      #     from_entity: "oe:aanvraag"
      #     field: "content.gemeente"
      #   # → resolves to "gemeente-toevoeger:amsterdam"
      #
      # 3. Entity-derived: the entity field value IS the technical role string.
      #    Only usable on existing dossiers, not for create_dossier.
      # - from_entity: "oe:aanvraag"
      #   field: "content.toegewezen_rol"

  # View and list are always based on the dossier_access entity.
  # No configuration needed — the engine checks it automatically.
  # The dossier_access entity is managed by side effects.
```

---

## Activities

```yaml
# =======================================================================
# Core concepts:
#
# used      = things this activity reads/references (not modified)
#             Client sends entity refs or external URIs.
#             Server can auto-resolve latest version if client omits.
#
# generated = things this activity creates (new entities or revisions)
#             Client sends entity refs + content.
#             Optional derivedFrom for revisions.
#             Handler can also produce generated content (system activities).
#
# No overlap: an entity is either used OR generated, never both.
# This eliminates double edges in the PROV graph.
# =======================================================================

activities:

  - name: ""                      # e.g. "dienAanvraagIn"
    label: ""                     # human readable, e.g. "Dien aanvraag in"
    description: ""

    # --- Dossier Creation ---
    can_create_dossier: false     # true = this activity can start a new dossier

    # --- Client Callable ---
    # client_callable: false      # if false, only triggered by side_effects (system activity)
                                  # defaults to true if omitted

    # --- Functional Roles ---
    # What role(s) the agent plays when performing this activity.
    # Used in PROV associations. If the client omits the role, default_role is used.
    allowed_roles: ["oe:aanvrager"]     # list of allowed functional roles
    default_role: "oe:aanvrager"        # used when client omits role from request

    # --- Handler ---
    # Python function that computes the generated entity content.
    # If absent: content comes from the client's "generated" block.
    # If present: engine calls this function to produce the content.
    # Activities WITH handlers CAN be client_callable (e.g. neemBeslissing).
    #
    # handler: "determine_responsible_org"
    #
    # In the plugin:
    #   async def determine_responsible_org(context: ActivityContext, content) -> HandlerResult:
    #       aanvraag = context.get_used_entity("oe:aanvraag")
    #       return HandlerResult(content={"organisatie": "..."}, status="ingediend")

    # --- Authorization ---
    authorization:
      access: ""                  # "everyone", "authenticated", "roles"
      roles:
        # Pattern 1: Direct match
        - role: ""
        #
        # Pattern 2: Scoped match
        # - role: "gemeente-toevoeger"
        #   scope:
        #     from_entity: "oe:aanvraag"
        #     field: "content.gemeente"
        #
        # Pattern 3: Entity-derived match
        # - from_entity: "oe:aanvraag"
        #   field: "content.toegewezen_rol"

    # --- Workflow Rules ---
    requirements:
      activities:                 # which activities must have been completed
        - ""
      entities:                   # which entity types must exist (latest version)
        - ""
      statuses:                   # dossier must be in one of these statuses
        - ""

    forbidden:
      activities:                 # which activities must NOT have been completed
        - ""
      statuses:                   # dossier must NOT be in any of these statuses
        - ""

    # --- Used: references this activity reads ---
    # Only for existing entities (references) or external URIs.
    # No content here — content goes in the "generated" block.
    #
    # Resolution logic:
    #   1. Client sends ref         → engine validates it exists
    #   2. Client omits + auto_resolve → server resolves latest version
    #   3. Client omits + required  → 422 error
    #   4. Client omits + not required → not included
    used:
      # Local entity reference
      - type: ""                  # entity type, e.g. "oe:aanvraag"
        required: false           # is this reference required in the request?
        auto_resolve: "latest"    # if omitted by client, server resolves latest version
                                  # null = no auto-resolve
        description: ""           # shown in API docs

      # External entity (URI)
      # - type: "object"
      #   external: true          # engine only records the URI, no existence check
      #   required: false
      #   description: ""

    # --- Generates: entity types this activity can create ---
    # A list of entity type strings.
    # Client sends new entities in the "generated" block of the request.
    # Handler can also produce content (system activities).
    # Engine validates:
    #   - entity type is in this list
    #   - content passes Pydantic validation
    #   - derivedFrom points to an existing version (if provided)
    generates:
      - ""                        # e.g. "oe:aanvraag", "oe:beslissing"

    # --- Status ---
    # What status this activity sets on the dossier.
    # Can be:
    #   - a string: "ingediend"
    #   - null: no status change (handler may set it via HandlerResult.status)
    #   - a mapping: derive from entity content
    #     status:
    #       from_entity: "oe:besluit"
    #       field: "content.uitkomst"
    #       mapping:
    #         toegekend: "besluit_toegekend"
    #         afgewezen: "besluit_afgewezen"
    status: ""

    validators: []                # list of {name: "validator_fn_name"}
    side_effects: []              # list of {activity: "SystemActivityName"}

    # --- Tasks ---
    # Tasks are created as system:task entities with full PROV trail.
    # Four types:
    #
    # Type 1 — Fire-and-forget: runs inline, no record
    # Type 2 — Recorded: worker executes function, completeTask records result
    # Type 3 — Scheduled activity: worker executes an activity in the same dossier
    # Type 4 — Cross-dossier: worker executes an activity in another dossier
    #
    # Tasks are entities, not a separate table. The worker polls for
    # system:task entities with status "scheduled" and scheduled_for <= now.
    #
    # Lifecycle as entity versions:
    #   v1: scheduled (generated by the triggering activity)
    #   v2: completed | cancelled | superseded | failed (generated by completeTask or cancel logic)
    #
    # Cancel logic: when an activity in cancel_if_activities occurs, the engine
    # automatically creates a cancelled version of the task. Full provenance —
    # you can see which activity cancelled which task.
    #
    # allow_multiple: false (default) = creating a new task of the same target_activity
    # supersedes any existing scheduled one. true = multiple can coexist.

    tasks:

      # --- Type 1: Fire-and-forget ---
      # Runs inline during activity execution. No entity, no PROV.
      # If it fails, the activity still succeeds.
      - kind: "fire_and_forget"
        function: "send_notification_email"

      # --- Type 2: Recorded task ---
      # Worker picks it up, calls the function, creates a completed version.
      # PROV: activity → task_v1 (scheduled) → completeTask → task_v2 (completed)
      - kind: "recorded"
        function: "log_audit_event"

      # --- Type 3: Scheduled activity (same dossier) ---
      # Worker executes the target activity at the scheduled time.
      # scheduled_for can be a static ISO datetime or resolved from an entity field.
      # cancel_if_activities: list of activity types that cancel this task if they
      # occur after the task was created.
      # PROV: activity → task (scheduled) → target_activity (wasInformedBy original)
      #       → completeTask (wasInformedBy target_activity) → task (completed)
      - kind: "scheduled_activity"
        target_activity: "trekAanvraagIn"
        scheduled_for: "2026-05-01T00:00:00Z"    # or resolved dynamically
        cancel_if_activities: ["vervolledigAanvraag", "bewerkAanvraag"]
        allow_multiple: false

      # --- Type 4: Cross-dossier activity ---
      # Worker calls the function to determine the target dossier,
      # then executes an activity there. The source dossier is passed as
      # an external used entity. wasInformedBy links cross-dossier via URIs.
      # PROV in source: activity → task (scheduled) → completeTask (informed by target)
      # PROV in target: target_activity (informed by urn:dossier:source/activity/id,
      #                                  used urn:dossier:source)
      - kind: "cross_dossier_activity"
        function: "find_related_dossier"
        target_activity: "ontvangMelding"
        allow_multiple: true
```

---

## Task Functions

Task functions are defined in the plugin's `tasks/` module and registered in `TASK_HANDLERS`.

```python
# Type 1 and 2: receives ActivityContext
async def send_notification_email(context: ActivityContext):
    aanvraag = context.get_typed("oe:aanvraag")
    # ... send email ...

async def log_audit_event(context: ActivityContext):
    # ... log to external audit system ...
    pass

# Type 4: returns TaskResult with target dossier
from gov_dossier_engine.engine import TaskResult

async def find_related_dossier(context: ActivityContext):
    aanvraag = context.get_typed("oe:aanvraag")
    # ... determine target dossier ...
    return TaskResult(
        target_dossier_id="d5000000-...",
        content={"bericht": "Gerelateerd dossier is goedgekeurd"},
    )

TASK_HANDLERS = {
    "send_notification_email": send_notification_email,
    "log_audit_event": log_audit_event,
    "find_related_dossier": find_related_dossier,
}
```

Type 3 has no function — the worker just executes the target activity.

---

## Conditional Task Queueing from Handlers

Tasks defined in the YAML `tasks:` block always execute. For conditional tasks — where
the decision depends on entity content at runtime — handlers can append tasks dynamically.

`HandlerResult` accepts a `tasks` list alongside `generated` and `status`:

```python
async def neem_beslissing(context: ActivityContext, content: dict | None) -> HandlerResult:
    beslissing = context.get_typed("oe:beslissing")
    handtekening = context.get_typed("oe:handtekening")

    if not handtekening or not handtekening.getekend:
        return HandlerResult(status="klaar_voor_behandeling")

    if beslissing and beslissing.beslissing == "onvolledig":
        # Only schedule trekAanvraagIn when the decision is "onvolledig"
        from datetime import datetime, timezone, timedelta
        deadline = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

        return HandlerResult(
            status="aanvraag_onvolledig",
            tasks=[{
                "kind": "scheduled_activity",
                "target_activity": "trekAanvraagIn",
                "scheduled_for": deadline,
                "cancel_if_activities": ["vervolledigAanvraag"],
                "allow_multiple": False,
            }],
        )

    if beslissing and beslissing.beslissing == "goedgekeurd":
        return HandlerResult(status="toelating_verleend")

    return HandlerResult(status="toelating_geweigerd")
```

Handler tasks are merged with YAML tasks during step 15 of activity execution.
The engine processes them identically — creating `system:task` entities with full PROV.

This enables patterns like:
- Schedule a deadline only when a specific outcome occurs
- Queue cross-dossier notifications only under certain conditions
- Fire-and-forget an email only when a threshold is met

Handlers can also return multiple entities alongside tasks:

```python
return HandlerResult(
    generated=[
        ("oe:beslissing", {"beslissing": "goedgekeurd", "datum": "..."}),
        ("oe:handtekening", {"getekend": True}),
    ],
    status="toelating_verleend",
    tasks=[{
        "kind": "fire_and_forget",
        "function": "send_approval_notification",
    }],
)
```

---

## Worker

The worker processes due tasks. Runs as a separate process sharing the same DB.

```bash
# Process all due tasks once and exit
python -m gov_dossier_engine.worker --once

# Run continuously, polling every 10 seconds
python -m gov_dossier_engine.worker

# Custom interval and config
python -m gov_dossier_engine.worker --interval 5 --config gov_dossier_app/config.yaml
```

The worker:
1. Polls for `system:task` entities where `status == "scheduled"` and `scheduled_for <= now`
2. Keeps only the latest version of each logical task entity (handles superseded)
3. Checks if `cancel_if_activities` have occurred since the task was created
4. Executes per type (inline function / execute_activity / cross-dossier)
5. Creates a `completeTask` activity with the result
6. All within one DB transaction — if anything fails, everything rolls back
7. On failure, marks the task as "failed" in a separate clean transaction

---

## Search Integration

Search is delegated to Elasticsearch via the plugin system. The engine provides two hooks:

### post_activity_hook

Called after every activity, inside the transaction. Updates indices.

```python
async def update_search_index(repo, dossier_id, activity_type, status, entities):
    aanvraag = entities.get("oe:aanvraag")

    common_doc = {
        "dossier_id": str(dossier_id),
        "workflow": "toelatingen",
        "status": status,
    }

    specific_doc = dict(common_doc)
    if aanvraag and aanvraag.content:
        specific_doc["onderwerp"] = aanvraag.content.get("onderwerp")
        specific_doc["gemeente"] = aanvraag.content.get("gemeente")

    await es.index(index="dossiers-common", id=str(dossier_id), document=common_doc)
    await es.index(index="dossiers-toelatingen", id=str(dossier_id), document=specific_doc)
```

### search_route_factory

Registers workflow-specific search endpoints during app startup.

```python
def register_search_routes(app, get_user):
    @app.get("/dossiers/toelatingen/search", tags=["toelatingen"])
    async def search_toelatingen(
        q: str = None, gemeente: str = None, status: str = None,
        user: User = Depends(get_user),
    ):
        results = await es.search(index="dossiers-toelatingen", body=build_query(...))
        return {"results": results}
```

The `/dossiers` endpoint remains as a basic stub for simple listing.

---

## Request Format

### Single activity (typed endpoint — recommended)

```
PUT /dossiers/{dossier_id}/activities/{activity_id}/{activityName}
```

```json
{
  "workflow": "toelatingen",
  "used": [
    { "entity": "https://id.erfgoed.net/erfgoedobjecten/10001" }
  ],
  "generated": [
    {
      "entity": "oe:aanvraag/e1000000-...@f1000000-...",
      "content": { "onderwerp": "..." }
    }
  ]
}
```

- `workflow` only needed for the first activity (creates dossier)
- `role` omitted — engine uses `default_role` from the activity definition
- `type` omitted — inferred from the URL
- `informed_by` — optional, local UUID or cross-dossier URI (`urn:dossier:{id}/activity/{id}`)
- `used` = references only (existing entities or external URIs)
- `generated` = new entities with content (optional `derivedFrom` for revisions)

### Single activity (generic endpoint)

```
PUT /dossiers/{dossier_id}/activities/{activity_id}
```

Same body but with `"type": "dienAanvraagIn"` added.

### Batch activities (atomic)

```
PUT /dossiers/{dossier_id}/activities
```

```json
{
  "workflow": "toelatingen",
  "activities": [
    {
      "activity_id": "a300...-002",
      "type": "bewerkAanvraag",
      "used": [{ "entity": "https://..." }],
      "generated": [{
        "entity": "oe:aanvraag/id@new_version",
        "derivedFrom": "oe:aanvraag/id@old_version",
        "content": { ... }
      }]
    },
    {
      "activity_id": "a300...-003",
      "type": "doeVoorstelBeslissing",
      "used": [
        { "entity": "oe:aanvraag/id@new_version" }
      ],
      "generated": [{
        "entity": "oe:beslissing/id@version",
        "content": { ... }
      }]
    }
  ]
}
```

All activities execute in order within one transaction. If any fails, all roll back.
Entities from activity N are visible to activity N+1 via auto-resolve or explicit ref.

### Entity reference format

```
prefix:type/entity_id@version_id
```

Example: `oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001`

- `prefix:type` = the entity type (e.g. `oe:aanvraag`)
- `entity_id` = logical entity UUID (stable across versions)
- `version_id` = specific version UUID (unique per version)
- All IDs are client-generated UUIDs

---

## PROV Relationships

The engine automatically creates the following W3C PROV relationships:

| Relationship | Meaning |
|---|---|
| `wasGeneratedBy(entity, activity)` | This entity version was created by this activity |
| `used(activity, entity)` | This activity used (read) this existing entity version |
| `wasAssociatedWith(activity, agent, role)` | This agent performed this activity in this role |
| `wasAttributedTo(entity, agent)` | This entity was created by this agent |
| `wasDerivedFrom(new_version, old_version)` | This version is a revision of the old version |
| `wasInformedBy(activity_b, activity_a)` | Activity B was triggered by Activity A (side effects, cross-dossier tasks) |

`wasInformedBy` supports both local UUIDs and cross-dossier URIs (`urn:dossier:{id}/activity/{id}`).

---

## Access Control (dossier_access entity)

The `dossier_access` entity controls who can see what. It is managed by a `setDossierAccess` side effect.

```json
{
  "access": [
    {
      "role": "behandelaar",
      "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external", "oe:dossier_access"],
      "activity_view": "all"
    },
    {
      "agents": ["agent-uuid-here"],
      "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external"],
      "activity_view": "own"
    }
  ]
}
```

- `role` or `agents`: who this entry applies to
- `view`: which entity types are visible (empty = nothing). Include `"external"` for external URI entities.
- `activity_view`: `"own"` (only own activities), `"related"` (own + touching visible entities), `"all"`

Applied to: GET dossier, entity endpoints, PROV-JSON, PROV graph.
Users without any matching entry get HTTP 403.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `PUT` | `/dossiers/{id}/activities/{id}/{type}` | Execute a typed activity |
| `PUT` | `/dossiers/{id}/activities/{id}` | Execute a generic activity |
| `PUT` | `/dossiers/{id}/activities` | Execute batch activities atomically |
| `GET` | `/dossiers/{id}` | Get dossier detail (filtered by access) |
| `GET` | `/dossiers` | List dossiers (stub) |
| `GET` | `/dossiers/{workflow}/search` | Workflow-specific search (Elasticsearch) |
| `GET` | `/dossiers/{id}/entities/{type}` | All versions of an entity type |
| `GET` | `/dossiers/{id}/entities/{type}/{entity_id}` | All versions of a logical entity |
| `GET` | `/dossiers/{id}/entities/{type}/{entity_id}/{version_id}` | Single entity version |
| `GET` | `/dossiers/{id}/prov` | PROV-JSON export (filtered) |
| `GET` | `/dossiers/{id}/prov/graph` | Interactive timeline visualization |

Graph query parameters: `?include_system_activities=true`, `?include_tasks=true`

---

## Full Workflow Example

See `gov_dossier_toelatingen/workflow.yaml` for a complete example implementing
a heritage permit ("toelating beschermd erfgoed") workflow with:

- Client activities: dienAanvraagIn, bewerkAanvraag, vervolledigAanvraag, doeVoorstelBeslissing, tekenBeslissing, neemBeslissing
- System activities: setDossierAccess, duidVerantwoordelijkeOrganisatieAan, duidBehandelaarAan, setSystemFields
- Scoped authorization (municipality-based roles)
- Entity-derived authorization (RRN/KBO from aanvraag)
- Side effect chains (dienAanvraagIn → setDossierAccess + duidVerantwoordelijkeOrganisatieAan → duidBehandelaarAan)
- Two decision paths: direct (neemBeslissing with content) and indirect (doeVoorstelBeslissing → tekenBeslissing → neemBeslissing via side effect)
- Four-eyes principle (behandelaar proposes, separate ondertekenaar signs or declines)
- Post-activity search index hook (Elasticsearch stub)
- Workflow-specific search endpoint (/dossiers/toelatingen/search)
