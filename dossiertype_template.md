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
# They appear in the request body ("role": "behandelaar"),
# in activity descriptions, and in workflow rules.
#
# How these map to technical roles in your auth system is
# defined per activity in the authorization section.
# There is NO naming convention between functional and technical roles.
#
# Examples:
#   Functional role: behandelaar
#   Technical role could be: "behandelaar:amsterdam"
#                            "gemeente-toevoeger:amsterdam"
#                            "subsidie-behandeling-team-3"
#                            "some-completely-different-string"

roles:
  - name: ""                      # e.g. "aanvrager", "behandelaar", "ondertekenaar", "beslisser"
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
# Common entities (dossier_access, document, etc.) live in the engine.
# Workflow-specific entities live in the plugin library.
#
# Benefits over JSON Schema:
# - Python-native validation with type hints
# - IDE autocompletion and type checking
# - FastAPI auto-generates OpenAPI docs from the models
# - Versioning via class inheritance
# - Shared models across workflows via imports

entity_types:
  - name: ""                      # e.g. "aanvraag", "motivatie", "brief", "besluit"
    prefix: ""                    # e.g. "gov:aanvraag"
    description: ""
    cardinality: ""               # "single" = one logical entity per dossier
                                  #   - auto_resolve returns the one entity
                                  #   - requirements check latest version
                                  #   - engine rejects a second logical entity of this type
                                  # "multiple" = many logical entities per dossier
                                  #   - auto_resolve returns all of them
                                  #   - requirements check that at least one exists
                                  #   - client specifies which one via derivedFrom for revisions
    revisable: true               # can new versions be created?
    model: ""                     # Python import path to the Pydantic model
                                  # e.g. "gov_dossier_subsidie.entities.Aanvraag"
    # For schema versioning:
    # accepts:                    # older model versions still valid for existing entities
    #   - "gov_dossier_subsidie.entities.AanvraagV1"

  # Always include — managed by side effects, used for authorization.
  # This model lives in the engine, not in the plugin.
  - name: "dossier_access"
    prefix: "gov:dossier_access"
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
      #     from_entity: "aanvraag"
      #     field: "content.gemeente"
      #   # → resolves to "gemeente-toevoeger:amsterdam"
      #
      # 3. Entity-derived: the entity field value IS the technical role string.
      #    Only usable on existing dossiers, not for create_dossier.
      # - from_entity: "aanvraag"
      #   field: "content.toegewezen_rol"

  # View and list are always based on the dossier_access entity.
  # No configuration needed — the engine checks it automatically.
  # The dossier_access entity is managed by side effects.
```

---

## Activities

```yaml
activities:

  - name: ""                      # e.g. "DienAanvraagIn"
    label: ""                     # human readable, e.g. "Dien aanvraag in"
    description: ""

    # --- Dossier Creation ---
    can_create_dossier: false     # true = this activity can start a new dossier

    # --- Handler ---
    # Python function that computes the generated entity content.
    # If absent: content comes from the client (normal client activity).
    # If present: engine calls this function to produce the content (system activity).
    #
    # The function receives an ActivityContext with access to all used entities
    # and returns a dict that is validated against the Pydantic model.
    #
    # handler: "determine_responsible_org"   # function name in the plugin
    #
    # In the plugin:
    #   async def determine_responsible_org(context: ActivityContext) -> dict:
    #       aanvraag = context.get_used_entity("aanvraag")
    #       return {"organisatie": f"gemeente-{aanvraag.content['gemeente']}"}

    # --- Authorization ---
    authorization:
      access: ""                  # "everyone", "authenticated", "roles"
      roles:
        # Pattern 1: Direct match
        - role: ""                # e.g. "aanvrager", "subsidie-behandelaar"
        #
        # Pattern 2: Scoped match
        # - role: "gemeente-toevoeger"
        #   scope:
        #     from_entity: "aanvraag"
        #     field: "content.gemeente"
        #
        # Pattern 3: Entity-derived match
        # - from_entity: "aanvraag"
        #   field: "content.toegewezen_rol"

      # constraints:
      #   - type: "different_agents"
      #     activity: ""
      #     description: ""

    # --- Workflow Rules ---
    requirements:
      activities:                 # which activities must have been completed
        - ""
      entities:                   # which entity types must exist (latest version)
        - ""
      statuses:                   # dossier must be in one of these statuses
        - ""                      # e.g. "besluit_toegekend", "besluit_afgewezen"

    forbidden:
      activities:                 # which activities must NOT have been completed
        - ""
      statuses:                   # dossier must NOT be in any of these statuses
        - ""                      # e.g. "besluit_toegekend"

    # --- Used: what this activity uses ---
    # Defines all entity types involved in this activity.
    # Each entry describes:
    #   - What the client can send
    #   - What the server auto-resolves if the client doesn't send it
    #   - What handlers receive as input
    #
    # Two kinds:
    # 1. Local entities (managed by the engine, gov:type/id@version format)
    #    - Engine validates existence, type compatibility, derivedFrom chain
    #    - Can be a reference, new content, or both (accept field)
    # 2. External entities (URIs pointing to systems outside the engine)
    #    - Engine only validates that it's a well-formed URI
    #    - Always references, never new content
    #    - Detected by prefix: anything not starting with "gov:" is external
    #
    # Resolution logic per entry:
    #   1. Client sends it              → use what the client sent
    #   2. Client omits + auto_resolve  → server resolves latest version
    #   3. Client omits + required      → 422 error
    #   4. Client omits + not required  → not included
    #
    # This drives the typed request model in the API docs.
    used:
      # Local entity
      - type: ""                  # entity type name, e.g. "aanvraag", "motivatie"
        accept: ""                # "reference" = existing only
                                  # "new" = new content only (with optional derivedFrom)
                                  # "any" = both reference and new content allowed
        required: false           # is this entity required in the request?
        auto_resolve: "latest"    # if client omits it, server uses latest version
                                  # null = no auto-resolve (client must send it or it's absent)
        description: ""           # shown in API docs

      # External entity
      # - type: ""                # label for this external type, e.g. "document", "regelgeving"
      #   external: true          # engine only validates URI format, no existence check
      #   required: false
      #   description: ""

    # --- Generates: what new entity types this activity can create ---
    # A list of entity type names that this activity is allowed to produce.
    # For client activities: content comes from the used block.
    # For handler activities: content comes from the handler function.
    generates:
      - ""                        # e.g. "aanvraag", "motivatie", "brief"

    # --- Status: what status the dossier gets after this activity ---
    # Three options:
    #
    # 1. Fixed string: always this status
    status: ""                    # e.g. "ingediend", "motivatie_geschreven"
    #
    # 2. Derived from entity content:
    # status:
    #   from_entity: ""           # entity type, e.g. "besluit"
    #   field: ""                 # dot notation path, e.g. "content.uitkomst"
    #   mapping:                  # value → status
    #     value1: "status_a"
    #     value2: "status_b"
    #
    # 3. Handler computes it (only valid when handler is set):
    # status: null                # handler returns HandlerResult with status

    # --- Custom Validators ---
    validators:
      - name: ""                  # function name in the plugin
        description: ""

    # --- Side Effects ---
    # Activities triggered automatically after this activity completes.
    # The triggered activity must be defined in the activities list (with its own
    # used, generates, handler, authorization, etc.).
    # The engine creates the triggered activity with wasInformedBy link to this one.
    side_effects:
      - activity: ""              # name of the activity to trigger, e.g. "SetDossierAccess"
        # condition:              # optional: only trigger if condition is met
        #   field: ""             # dot notation path in entity content
        #   value: ""             # expected value
        #   entity_type: ""       # which entity to check

    # --- Tasks ---
    tasks:
      - type: ""                  # "notification", "scheduled_activity", "reminder", "custom"
        description: ""
        delay: null               # null = immediate, or ISO 8601 duration

        # For type: "notification"
        # channel: ""             # "email", "task_list", "sms"
        # template: ""
        # to:
        #   role: ""              # functional role

        # For type: "scheduled_activity"
        # activity: ""
        # delay: "P1D"

        # For type: "reminder"
        # condition:
        #   no_activity: ""
        #   within: "P42D"
        # action: ""

        # For type: "custom"
        # name: ""                # function name in the plugin, e.g. "process_payment"
        #                         # the engine calls this function when the task executes
        # condition:
        #   field: ""
        #   value: ""
        #   entity_type: ""
```

---

## Pydantic Models

```yaml
# Entity content models are Pydantic classes.
# Common entities live in the engine package.
# Workflow-specific entities live in the plugin package.
#
# The engine provides:
#   gov_dossier_engine.entities.DossierAccess
#
# Each plugin provides its own:
#   gov_dossier_subsidie.entities.Aanvraag
#   gov_dossier_subsidie.entities.Motivatie
#   ...
#
# The route generator uses these models to:
# 1. Build typed request/response models per activity
# 2. Generate OpenAPI docs grouped by workflow (tag = workflow name)
# 3. Validate content on write
# 4. Store as JSONB in PostgreSQL (validated by Pydantic, stored as JSON)
#
# Versioning via inheritance:
#
#   class AanvraagV1(BaseModel):
#       type: str
#       bedrag: float
#       omschrijving: str
#
#   class AanvraagV2(AanvraagV1):
#       gemeente: str
#       categorie: str | None = None
#
#   entity_types:
#     - name: "aanvraag"
#       model: "gov_dossier_subsidie.entities.AanvraagV2"
#       accepts: ["gov_dossier_subsidie.entities.AanvraagV1"]
```

---

## API Docs Generation

```yaml
# The route generator reads the workflow YAML + Pydantic models and produces:
#
# For each plugin/workflow, a tagged group in Swagger/ReDoc:
#
#   ▸ subsidieaanvraag
#     PUT  /dossiers/{id}/activities/{id}  DienAanvraagIn
#     PUT  /dossiers/{id}/activities/{id}  WijzigAanvraag
#     PUT  /dossiers/{id}/activities/{id}  SchrijfMotivatie
#     ...
#
#   ▸ vergunningsaanvraag
#     PUT  /dossiers/{id}/activities/{id}  DienAanvraagIn
#     PUT  /dossiers/{id}/activities/{id}  VraagAdviesAan
#     ...
#
# Each activity endpoint shows:
#   - Typed request body built from the "used" section:
#     - For each used entry with accept "new" or "any":
#       entity ref + content field typed to the Pydantic model
#     - For each used entry with accept "reference":
#       just the entity ref string
#   - Typed response body:
#     - used: references (including auto_resolve'd entities)
#     - generated: entity ref + typed content from the Pydantic model
#   - Roles, requirements, forbidden, constraints in the description
#   - Error responses: 403, 409, 422
#
# Internally, all routes call the SAME generic handler:
#
#   PUT /dossiers/{id}/activities/{id}  (type=DienAanvraagIn, workflow=subsidieaanvraag)
#   PUT /dossiers/{id}/activities/{id}  (type=DienAanvraagIn, workflow=vergunningsaanvraag)
#       ↓
#   execute_activity(dossier_id, activity_id, request, user)
#       ↓
#   same engine for everything
#
# The typed routes are wrappers for documentation and request validation.
# The actual dispatch is: look up dossier → find workflow → find activity → validate → execute.
```

---

## Request Format

```yaml
# PUT /dossiers/{dossier_id}/activities/{activity_id}
# X-POC-User: jan.burger
#
# {
#   "type": "DienAanvraagIn",
#   "workflow": "subsidieaanvraag",    ← only needed on first activity (when can_create_dossier)
#   "role": "aanvrager",               ← FUNCTIONAL role
#   "used": [
#
#     // External entity (just a URI, engine validates format only):
#     {
#       "entity": "https://dms.gemeente.nl/documents/inkomensbewijs-abc123"
#     },
#
#     // Local entity — reference existing (accept: "reference" or "any"):
#     {
#       "entity": "gov:aanvraag/aaaa-aaaa@bbbb-bbbb"
#     },
#
#     // Local entity — new (accept: "new" or "any"):
#     {
#       "entity": "gov:aanvraag/aaaa-aaaa@bbbb-bbbb",
#       "content": { ... }             ← validated against Pydantic model
#     },
#
#     // Local entity — new version of existing (accept: "new" or "any"):
#     {
#       "entity": "gov:aanvraag/aaaa-aaaa@cccc-cccc",
#       "derivedFrom": "gov:aanvraag/aaaa-aaaa@bbbb-bbbb",
#       "content": { ... }             ← validated against Pydantic model
#     }
#   ]
# }
#
# Detection logic:
#   starts with "gov:"           → local entity
#     has content                → new entity or new version (generated)
#     has content + derivedFrom  → new version of existing (generated, derived)
#     no content                 → reference to existing (used)
#   anything else                → external entity (URI, validated for format only)
#
# Validation:
#   - Each used entry must match a "used" definition in the activity
#   - Local entities: type derived from gov: prefix, existence checked, content validated
#   - External entities: URI format validated, no existence check
#   - Only entity types listed in the activity's "used" section are accepted
```

---

## Response Format

```yaml
# {
#   "activity": {
#     "id": "gov:activiteit/{id}",
#     "type": "DienAanvraagIn",
#     "associatedWith": {
#       "agent": "gov:agent/{id}",
#       "role": "aanvrager",           ← functional role
#       "name": "Jan de Vries"
#     },
#     "startedAtTime": "...",
#     "endedAtTime": "..."
#   },
#   "used": [
#     { "entity": "gov:document/8a1b2c3d@8a1b2c3d", "type": "document" }
#   ],
#   "generated": [
#     {
#       "entity": "gov:aanvraag/aaaa-aaaa@bbbb-bbbb",
#       "type": "aanvraag",
#       "content": { ... }             ← typed, from Pydantic model
#     }
#   ],
#   "dossier": {
#     "id": "gov:dossier/{id}",
#     "workflow": "subsidieaanvraag",
#     "status": "ingediend",
#     "allowedActivities": ["WijzigAanvraag", "SchrijfMotivatie"]
#   }
# }
```

---

## How Functional and Technical Roles Relate

```yaml
# ┌─────────────────────────────────────────────────────────────────────┐
# │  FUNCTIONAL ROLE                                                    │
# │                                                                     │
# │  What someone does in the business process.                         │
# │  Lives in: roles section, request body, response, workflow rules.   │
# │  Examples: "aanvrager", "behandelaar", "beslisser"                  │
# │                                                                     │
# ├─────────────────────────────────────────────────────────────────────┤
# │  TECHNICAL ROLE                                                     │
# │                                                                     │
# │  What the auth system assigned to the user.                         │
# │  Lives in: user.roles (from JWT/OAuth), POC user config.            │
# │  Can be anything: "behandelaar:amsterdam",                          │
# │                   "gemeente-toevoeger:rotterdam",                    │
# │                   "subsidie-behandeling-team-3"                      │
# │                                                                     │
# ├─────────────────────────────────────────────────────────────────────┤
# │  THE MAPPING (in authorization rules per activity)                  │
# │                                                                     │
# │  authorization:                                                     │
# │    access: "roles"                                                  │
# │    roles:                                                           │
# │      - role: "gemeente-toevoeger"                                   │
# │        scope:                                                       │
# │          from_entity: "aanvraag"                                    │
# │          field: "content.gemeente"                                  │
# │      # → resolves to "gemeente-toevoeger:amsterdam"                 │
# │      # → checks user.roles for "gemeente-toevoeger:amsterdam"       │
# │                                                                     │
# │  The functional role name does NOT need to appear in the            │
# │  technical role string. They are independent.                        │
# └─────────────────────────────────────────────────────────────────────┘
```

---

## Plugin Structure

```yaml
# ┌──────────────────────────────────────────────────────────────┐
# │  gov_dossier_engine (framework, pip installable)              │
# │                                                              │
# │  gov_dossier_engine/                                         │
# │  ├── entities.py              ← DossierAccess, Document     │
# │  ├── engine/                  ← generic activity handler     │
# │  ├── routes/                  ← route generator              │
# │  ├── db/                      ← migrations, repository       │
# │  └── plugin.py                ← plugin interface             │
# │                                                              │
# │  No business logic. No domain-specific code.                 │
# ├──────────────────────────────────────────────────────────────┤
# │  gov_dossier_subsidie (plugin, pip installable)              │
# │                                                              │
# │  gov_dossier_subsidie/                                       │
# │  ├── __init__.py              ← plugin registration          │
# │  ├── workflow.yaml            ← this template, filled out    │
# │  ├── entities.py              ← Pydantic models              │
# │  │     Aanvraag, Motivatie, Brief, OndertekendeBrief, Besluit│
# │  ├── validators/              ← custom validation functions  │
# │  ├── side_effects/            ← system activity functions    │
# │  └── tasks/                   ← task handlers                │
# │                                                              │
# │  No framework code. No database schema. No generic logic.    │
# ├──────────────────────────────────────────────────────────────┤
# │  gov_dossier_app (deployment)                                │
# │                                                              │
# │  config.yaml + main.py (5 lines)                             │
# │  plugins:                                                    │
# │    - gov_dossier_subsidie                                    │
# │    - gov_dossier_vergunning                                  │
# └──────────────────────────────────────────────────────────────┘
```

---

## Full Example: Subsidieaanvraag

### Pydantic Models (gov_dossier_subsidie/entities.py)

```python
from pydantic import BaseModel, Field
from enum import Enum


class AanvraagType(str, Enum):
    subsidieaanvraag = "subsidieaanvraag"


class Aanvraag(BaseModel):
    type: AanvraagType
    bedrag: float = Field(ge=0, le=100000)
    omschrijving: str = Field(min_length=10, max_length=5000)
    gemeente: str


class Conclusie(str, Enum):
    toekennen = "toekennen"
    afwijzen = "afwijzen"


class Motivatie(BaseModel):
    tekst: str = Field(min_length=10)
    conclusie: Conclusie
    voorwaarden: list[str] = []


class Brief(BaseModel):
    onderwerp: str
    formaat: str = "application/pdf"


class Handtekening(BaseModel):
    methode: str
    tijdstip: str


class OndertekendeBrief(BaseModel):
    handtekening: Handtekening


class Uitkomst(str, Enum):
    toegekend = "toegekend"
    afgewezen = "afgewezen"


class Besluit(BaseModel):
    uitkomst: Uitkomst
    bedrag: float | None = None
    voorwaarden: list[str] = []
```

### Engine Model (gov_dossier_engine/entities.py)

```python
from pydantic import BaseModel


class DossierAccessEntry(BaseModel):
    role: str | None = None
    agents: list[str] = []
    view: list[str] = []
    activity_view: str = "related"  # "own", "related", "all"


class DossierAccess(BaseModel):
    access: list[DossierAccessEntry]
```

### Workflow Definition (gov_dossier_subsidie/workflow.yaml)

```yaml
name: "subsidieaanvraag"
description: "Behandeling van subsidieaanvragen"
version: "1.0"

roles:
  - name: "aanvrager"
    description: "Persoon die de aanvraag indient"

  - name: "behandelaar"
    description: "Medewerker die de aanvraag behandelt"

  - name: "ondertekenaar"
    description: "Medewerker die de brief ondertekent"

  - name: "beslisser"
    description: "Medewerker die het besluit neemt"

  - name: "systeem"
    description: "Geautomatiseerd systeem"

poc_users:
  - id: "550e8400-e29b-41d4-a716-446655440000"
    username: "jan.burger"
    type: "persoon"
    name: "Jan de Vries"
    roles: ["aanvrager"]
    properties:
      bsn: "123456789"
      email: "jan@example.nl"

  - id: "660e8400-e29b-41d4-a716-446655440001"
    username: "petra.behandelaar"
    type: "medewerker"
    name: "Petra Bakker"
    roles: ["gemeente-toevoeger:amsterdam"]
    properties:
      medewerker_nummer: "M001"
      afdeling: "subsidies"
      gemeente: "amsterdam"
      email: "p.bakker@amsterdam.nl"

  - id: "770e8400-e29b-41d4-a716-446655440002"
    username: "kees.ondertekenaar"
    type: "medewerker"
    name: "Kees Smit"
    roles: ["ondertekenaar:amsterdam", "beslisser:amsterdam"]
    properties:
      medewerker_nummer: "M042"
      afdeling: "subsidies"
      gemeente: "amsterdam"
      email: "k.smit@amsterdam.nl"

  - id: "880e8400-e29b-41d4-a716-446655440003"
    username: "systeem.berekeningen"
    type: "systeem"
    name: "Berekeningsmodule"
    roles: ["systeem"]
    properties:
      systeem_naam: "berekeningsmodule-v1"

  - id: "990e8400-e29b-41d4-a716-446655440004"
    username: "lisa.andere.gemeente"
    type: "medewerker"
    name: "Lisa Jansen"
    roles: ["gemeente-toevoeger:rotterdam"]
    properties:
      medewerker_nummer: "M099"
      gemeente: "rotterdam"
      email: "l.jansen@rotterdam.nl"

entity_types:
  - name: "aanvraag"
    prefix: "gov:aanvraag"
    description: "De subsidieaanvraag"
    cardinality: "single"
    revisable: true
    model: "gov_dossier_subsidie.entities.Aanvraag"

  - name: "motivatie"
    prefix: "gov:motivatie"
    description: "Motivatie bij het besluit"
    cardinality: "single"
    revisable: true
    model: "gov_dossier_subsidie.entities.Motivatie"

  - name: "brief"
    prefix: "gov:brief"
    description: "Gegenereerde brief"
    cardinality: "single"
    revisable: false
    model: "gov_dossier_subsidie.entities.Brief"

  - name: "ondertekende_brief"
    prefix: "gov:ondertekende_brief"
    description: "Ondertekende brief"
    cardinality: "single"
    revisable: false
    model: "gov_dossier_subsidie.entities.OndertekendeBrief"

  - name: "besluit"
    prefix: "gov:besluit"
    description: "Het genomen besluit"
    cardinality: "single"
    revisable: false
    model: "gov_dossier_subsidie.entities.Besluit"

  - name: "dossier_access"
    prefix: "gov:dossier_access"
    description: "Bepaalt wie dit dossier kan zien en wat ze kunnen zien"
    cardinality: "single"
    revisable: true
    model: "gov_dossier_engine.entities.DossierAccess"

authorization:
  create_dossier:
    access: "roles"
    roles:
      - role: "aanvrager"

activities:

  - name: "DienAanvraagIn"
    label: "Dien aanvraag in"
    description: "Burger dient een subsidieaanvraag in"
    can_create_dossier: true

    authorization:
      access: "roles"
      roles:
        - role: "aanvrager"

    requirements:
      activities: []
      entities: []

    forbidden:
      activities: ["DienAanvraagIn"]

    used:
      - type: "document"
        external: true
        required: false
        description: "Ondersteunende documenten (inkomensbewijs, ID, etc.) als URI"
      - type: "aanvraag"
        accept: "new"
        required: true
        description: "De nieuwe aanvraag met bedrag, omschrijving, gemeente"

    generates:
      - "aanvraag"

    status: "ingediend"

    validators: []

    side_effects:
      - activity: "BerekenReferentienummer"
      - activity: "SetDossierAccess"

    tasks:
      - type: "notification"
        description: "Stuur ontvangstbevestiging naar aanvrager"
        delay: null
        channel: "email"
        template: "ontvangstbevestiging"
        to:
          role: "aanvrager"

  - name: "WijzigAanvraag"
    label: "Wijzig aanvraag"
    description: "Aanvrager wijzigt de aanvraag"
    can_create_dossier: false

    authorization:
      access: "roles"
      roles:
        - role: "aanvrager"

    requirements:
      activities: ["DienAanvraagIn"]
      entities: ["aanvraag"]

    forbidden:
      activities: ["SchrijfMotivatie"]

    used:
      - type: "aanvraag"
        accept: "new"
        required: true
        description: "Nieuwe versie van de aanvraag (derivedFrom vorige versie)"

    generates:
      - "aanvraag"

    status: "ingediend"

    validators: []
    side_effects: []
    tasks: []

  - name: "SchrijfMotivatie"
    label: "Schrijf motivatie"
    description: "Behandelaar schrijft een motivatie"
    can_create_dossier: false

    authorization:
      access: "roles"
      roles:
        - role: "gemeente-toevoeger"
          scope:
            from_entity: "aanvraag"
            field: "content.gemeente"

    requirements:
      activities: ["DienAanvraagIn"]
      entities: ["aanvraag"]

    forbidden:
      activities: ["NeemBesluit"]

    used:
      - type: "aanvraag"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De aanvraag (auto-resolved als niet meegegeven)"
      - type: "motivatie"
        accept: "new"
        required: true
        description: "De motivatie met tekst, conclusie, eventuele voorwaarden"

    generates:
      - "motivatie"

    status: "motivatie_geschreven"

    validators:
      - name: "validate_budget_available"
        description: "Controleert of er nog budget beschikbaar is"

    side_effects:
      - activity: "BerekenRisicoscore"
      - activity: "SetDossierAccess"

    tasks:
      - type: "notification"
        description: "Zet op takenlijst voor briefgeneratie"
        delay: null
        channel: "task_list"
        template: "motivatie_geschreven"
        to:
          role: "behandelaar"

  - name: "WijzigMotivatie"
    label: "Wijzig motivatie"
    description: "Behandelaar wijzigt de motivatie"
    can_create_dossier: false

    authorization:
      access: "roles"
      roles:
        - role: "gemeente-toevoeger"
          scope:
            from_entity: "aanvraag"
            field: "content.gemeente"

    requirements:
      activities: ["SchrijfMotivatie"]
      entities: ["motivatie"]

    forbidden:
      activities: ["GenereerBrief"]

    used:
      - type: "aanvraag"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De aanvraag"
      - type: "motivatie"
        accept: "new"
        required: true
        description: "Nieuwe versie van de motivatie (derivedFrom vorige versie)"

    generates:
      - "motivatie"

    status: "motivatie_geschreven"

    validators: []
    side_effects: []
    tasks: []

  - name: "GenereerBrief"
    label: "Genereer brief"
    description: "Genereer de beschikkingsbrief"
    can_create_dossier: false

    authorization:
      access: "roles"
      roles:
        - role: "gemeente-toevoeger"
          scope:
            from_entity: "aanvraag"
            field: "content.gemeente"

    requirements:
      activities: ["SchrijfMotivatie"]
      entities: ["motivatie", "aanvraag"]

    forbidden:
      activities: ["NeemBesluit"]

    used:
      - type: "aanvraag"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De aanvraag"
      - type: "motivatie"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De motivatie"
      - type: "brief"
        accept: "new"
        required: true
        description: "De gegenereerde brief"

    generates:
      - "brief"

    status: "brief_gegenereerd"

    validators: []
    side_effects: []
    tasks: []

  - name: "OndertekenBrief"
    label: "Onderteken brief"
    description: "Ondertekenaar ondertekent de brief"
    can_create_dossier: false

    authorization:
      access: "roles"
      roles:
        - role: "ondertekenaar"
          scope:
            from_entity: "aanvraag"
            field: "content.gemeente"
      constraints:
        - type: "different_agents"
          activity: "SchrijfMotivatie"
          description: "Vier-ogen: ondertekenaar mag niet de auteur van de motivatie zijn"

    requirements:
      activities: ["GenereerBrief"]
      entities: ["brief"]

    forbidden: {}

    used:
      - type: "brief"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De brief die ondertekend wordt"
      - type: "ondertekende_brief"
        accept: "new"
        required: true
        description: "De ondertekende brief met handtekening metadata"

    generates:
      - "ondertekende_brief"

    status: "brief_ondertekend"

    validators: []

    side_effects:
      - activity: "SetDossierAccess"

    tasks: []

  - name: "NeemBesluit"
    label: "Neem besluit"
    description: "Beslisser neemt het besluit"
    can_create_dossier: false

    authorization:
      access: "roles"
      roles:
        - role: "beslisser"
          scope:
            from_entity: "aanvraag"
            field: "content.gemeente"

    requirements:
      activities: ["OndertekenBrief"]
      entities: ["ondertekende_brief"]

    forbidden:
      activities: ["NeemBesluit"]

    used:
      - type: "aanvraag"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De aanvraag"
      - type: "motivatie"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De motivatie"
      - type: "ondertekende_brief"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De ondertekende brief"
      - type: "besluit"
        accept: "new"
        required: true
        description: "Het besluit met uitkomst, eventueel bedrag en voorwaarden"

    generates:
      - "besluit"

    status:
      from_entity: "besluit"
      field: "content.uitkomst"
      mapping:
        toegekend: "besluit_toegekend"
        afgewezen: "besluit_afgewezen"

    validators: []

    side_effects:
      - activity: "BerekenBezwaartermijn"

    tasks:
      - type: "notification"
        description: "Stuur besluit naar aanvrager"
        delay: null
        channel: "email"
        template: "besluit_genomen"
        to:
          role: "aanvrager"

      - type: "custom"
        description: "Verwerk betaling als toegekend"
        name: "process_payment"
        delay: "P3D"
        condition:
          field: "content.uitkomst"
          value: "toegekend"
          entity_type: "besluit"

      - type: "reminder"
        description: "Sluit bezwaartermijn na 42 dagen"
        delay: null
        condition:
          no_activity: "StartBezwaarprocedure"
          within: "P42D"
        action: "close_appeal_period"

  # ---------------------------------------------------------------
  # System activities (triggered by side_effects, not by clients)
  # These have a handler that computes the generated entity content.
  # ---------------------------------------------------------------

  - name: "BerekenReferentienummer"
    label: "Bereken referentienummer"
    description: "Genereert een uniek referentienummer voor de aanvraag"

    handler: "calculate_reference_number"

    authorization:
      access: "roles"
      roles:
        - role: "systeem"

    requirements:
      activities: []
      entities: []

    forbidden: {}

    used:
      - type: "aanvraag"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De aanvraag waarvoor een referentienummer wordt gegenereerd"

    generates:
      - "berekende_aanvraag_velden"

    status: null                  # does not change dossier status

    validators: []
    side_effects: []
    tasks: []

  - name: "SetDossierAccess"
    label: "Stel dossiertoegang in"
    description: "Bepaalt wie dit dossier kan zien op basis van de huidige stand"

    handler: "set_dossier_access"

    authorization:
      access: "roles"
      roles:
        - role: "systeem"

    requirements:
      activities: []
      entities: []

    forbidden: {}

    used:
      - type: "aanvraag"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De aanvraag (voor gemeente scope)"

    generates:
      - "dossier_access"

    status: null

    validators: []
    side_effects: []
    tasks: []

  - name: "BerekenRisicoscore"
    label: "Bereken risicoscore"
    description: "Berekent risicoscore op basis van aanvraag en motivatie"

    handler: "calculate_risk_score"

    authorization:
      access: "roles"
      roles:
        - role: "systeem"

    requirements:
      activities: []
      entities: []

    forbidden: {}

    used:
      - type: "aanvraag"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De aanvraag"
      - type: "motivatie"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "De motivatie"

    generates:
      - "risicoscore"

    status: null

    validators: []
    side_effects: []
    tasks: []

  - name: "BerekenBezwaartermijn"
    label: "Bereken bezwaartermijn"
    description: "Berekent de bezwaartermijn op basis van het besluit"

    handler: "calculate_appeal_deadline"

    authorization:
      access: "roles"
      roles:
        - role: "systeem"

    requirements:
      activities: []
      entities: []

    forbidden: {}

    used:
      - type: "besluit"
        accept: "reference"
        required: false
        auto_resolve: "latest"
        description: "Het besluit"

    generates:
      - "bezwaartermijn"

    status: null

    validators: []
    side_effects: []
    tasks: []
```
