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
    tasks: []                     # list of task definitions (notifications, reminders)
```

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
| `wasInformedBy(activity_b, activity_a)` | Activity B was triggered by Activity A (side effects) |

---

## Access Control (dossier_access entity)

The `dossier_access` entity controls who can see what. It is managed by a `setDossierAccess` side effect.

```json
{
  "access": [
    {
      "role": "behandelaar",
      "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "oe:dossier_access"],
      "activity_view": "all"
    },
    {
      "agents": ["agent-uuid-here"],
      "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening"],
      "activity_view": "own"
    }
  ]
}
```

- `role` or `agents`: who this entry applies to
- `view`: which entity types are visible (empty = nothing)
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
| `GET` | `/dossiers` | List dossiers |
| `GET` | `/dossiers/{id}/entities/{type}` | All versions of an entity type |
| `GET` | `/dossiers/{id}/entities/{type}/{entity_id}` | All versions of a logical entity |
| `GET` | `/dossiers/{id}/entities/{type}/{entity_id}/{version_id}` | Single entity version |
| `GET` | `/dossiers/{id}/prov` | PROV-JSON export (filtered) |
| `GET` | `/dossiers/{id}/prov/graph` | Interactive timeline visualization |

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
