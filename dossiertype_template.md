# Dossier Type Definition Template

## Dossier Type

```yaml
name: ""                          # e.g. "subsidieaanvraag", "vergunningsaanvraag"
description: ""                   # human readable description
version: "1.0"                    # version of this workflow definition
```

---

## Workflow-level Relations (permission gate + kind declaration)

```yaml
# Declares the relation types that exist in this workflow. Activities
# opt in to specific types in their own `relations:` block (see the
# activity definition section). A type that appears here but not on
# any activity is legal-but-unused; a type that appears on an activity
# but not here is a configuration error.
#
# Every relation type MUST declare `kind`:
#   * `domain` — entity→entity edge (request uses `{from, to}`). Supports
#     add and remove operations. Optional `from_types`/`to_types`
#     constrain the ref kinds on each side — omit to accept any.
#   * `process_control` — activity→entity annotation (request uses
#     `{entity}`). Stateless; no remove operation; `from_types`/`to_types`
#     not legal.
#
# The `kind` field drives runtime dispatch. A request with the wrong
# shape (e.g. `{entity}` on a `kind: domain` relation) is rejected with
# 422. Load-time validation (Bug 78, Round 26) enforces the rules
# below; a plugin that violates them fails to start.
#
# Workflow-level relation entries accept ONLY these keys:
#   type, kind, from_types, to_types, description
# Any other key (including activity-level ones like `validator`,
# `validators`, `operations`) is rejected at load time with a clear
# error message. This is `_WORKFLOW_RELATION_KEYS` in plugin.py.
#
# Validators are declared at ACTIVITY level, not here. This file just
# names the types and their kinds. At the activity level, forbidden
# keys go the other way: `kind`, `from_types`, `to_types`, `description`
# are workflow-level-only and rejected if they appear on an activity's
# relation opt-in (`_ACTIVITY_RELATION_FORBIDDEN_KEYS`).
#
# relations:
#   - type: "oe:neemtAkteVan"
#     kind: "process_control"
#     description: >
#       Acknowledges that the activity is aware of a newer version of
#       a used entity and chose to proceed with the older version.
#
#   - type: "oe:betreft"
#     kind: "domain"
#     from_types: ["entity"]
#     to_types: ["external_uri"]
#     description: >
#       Links an entity to the external object it concerns.
#
#   - type: "oe:gerelateerd_aan"
#     kind: "domain"
#     from_types: ["dossier"]
#     to_types: ["dossier"]
#     description: >
#       Cross-dossier relation — links this dossier to another.
```

---

## Workflow-level Tombstone

```yaml
# Opts the workflow in to the tombstone activity, which redacts entity
# content while keeping the PROV graph intact. The tombstone activity
# is built into the engine, not declared per-workflow — the workflow
# just configures who is allowed to run it.
#
# If this block is omitted, the workflow has no tombstone capability
# at all and all tombstone attempts return 403.
#
# `allowed_roles` is a flat list of bare role names. The engine's
# loader (app.py's plugin-registration loop) reads this list and
# builds the tombstone activity's `authorization.roles` entries
# from it — one `{role: <name>}` dict per list item. Do NOT write
# the dict form here; this is consumed as a plain list of strings.
# Per-role scopes (property-derived, entity-derived) aren't
# supported for tombstone in the engine today — tombstone is an
# all-or-nothing capability per role.
#
# tombstone:
#   allowed_roles:
#     - "beheerder"
#     - "archivist"  # any number of role names
```

---

## Namespaces (IRI vocabularies this workflow uses)

```yaml
# Declare any external RDF vocabularies your workflow's entity types,
# relations, or PROV annotations reference. The engine's namespace
# registry uses this to emit correct PROV-JSON prefixes and to fail
# fast at plugin load if any declared entity type uses an undeclared
# prefix.
#
# Built-in prefixes are always available and don't need declaration:
#   prov, xsd, rdf, rdfs
# Your plugin's own prefix is registered automatically from
# config.yaml's iri_base.ontology.
#
# Adopt external vocabularies here if you need to reference them
# (e.g. FOAF for agents, Dublin Core for bibliographic entities):
#
# namespaces:
#   foaf: "http://xmlns.com/foaf/0.1/"
#   dcterms: "http://purl.org/dc/terms/"
#   skos: "http://www.w3.org/2004/02/skos/core#"
```

Using an entity type like `foaf:Person` means your plugin *stores* `foaf:Person` instances — you own the data. Linking to instances that live elsewhere is a different thing: those are external URIs (`{"entity": "http://example.org/agents/bob"}`).

---

## Workflow Constants (typed config with env-var override)

```yaml
# Plugin-scoped constants accessible as context.constants in handlers
# and plugin.constants in hooks/factories/validators. Declare a
# Pydantic BaseSettings class in your plugin (see dossier_toelatingen/
# constants.py), then optionally override its defaults here.
#
# Precedence (highest wins):
#   1. Environment variables (DOSSIER_{WORKFLOW_NAME}_...) — operator
#      escape hatch and the only acceptable place for secrets
#   2. This block — domain-level tuning that's committable
#   3. Class defaults in the Pydantic class
#
# Secrets (API keys, signing keys) should ONLY come from env vars.
# Never commit them to workflow.yaml.
#
# constants:
#   values:
#     aanvraag_deadline_days: 45      # overrides class default of 30
#     max_bijlagen_per_aanvraag: 30
#     # external_api_key: NEVER — use env vars for secrets
```

Your plugin's `create_plugin()` instantiates the constants class with these YAML values as kwargs. Environment variables win over both layers via Pydantic's `BaseSettings` mechanism. Frozen after load — no runtime mutation.

---

## Field Validators (lightweight, client-facing)

```yaml
# Declares named validators that the engine exposes at
# POST /{workflow}/validate/{validator_name}. Used for frontend-driven
# field validation between activities — "does this erfgoedobject URI
# resolve?", "is this handeling allowed for this type?".
#
# Authenticated endpoints — any logged-in user, regardless of role,
# may call them. The auth gate is purely DoS/enumeration-surface
# reduction; there's no role-based access logic inside.
#
# Shape: `dict[url_segment, dotted_path_OR_FieldValidator]`. The KEY
# leaks into the public URL, so keep it URL-safe (lowercase, hyphens
# or underscores, no slashes, no spaces). The VALUE is either:
#   * A dotted path string. Legacy/simpler shape. No request/response
#     typing — the handler receives a raw dict and returns a dict.
#   * A `FieldValidator` dataclass instance (plugin_guidebook.md:265
#     has the authoring story). Supports typed request_model /
#     response_model Pydantic classes so the endpoint gets proper
#     OpenAPI docs and input validation.
#
# field_validators:
#   # Legacy dotted-path form:
#   erfgoedobject: "your_plugin.validators.validate_erfgoedobject"
#
#   # Typed FieldValidator form (instantiated in plugin code, referenced
#   # here by the same URL-segment key):
#   handeling: "your_plugin.validators.handeling"   # exports a FieldValidator
```

The engine resolves each dotted path at plugin load time (ImportError if broken). URL-segment keys that collide with other validators in the same plugin are a plugin-author error; no load-time check.

---

## Reference Data (static lookup lists)

```yaml
# Static reference data served at:
#   GET /{workflow}/reference              (returns everything)
#   GET /{workflow}/reference/{list_name}  (returns one list)
#
# Used for frontend dropdowns — bijlagetypes, documenttypes, categorieën,
# etc. Sub-millisecond endpoint (no DB hit; served straight from the
# in-memory plugin config). **Public** (no auth required) by product
# decision — these are shared dropdown data that don't leak dossier
# state or enumerable references.
#
# Shape: `dict[list_name, list]`. The list_name key is the path segment
# the client requests; the value is whatever payload the plugin wants
# to return (typically a list of dicts, but any JSON-serializable
# structure works — the engine returns it verbatim under `{"items": ...}`).
#
# reference_data:
#   bijlagetypes:
#     - code: "plan"
#       label: "Plattegrond"
#     - code: "foto"
#       label: "Foto"
#   documenttypes:
#     - code: "aanvraag"
#       label: "Aanvraagformulier"
#     - code: "advies"
#       label: "Advies"
```

A request for `GET /{workflow}/reference/unknown_list` returns a 404 listing the available keys — handy for debugging.

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
#
# ⚠ CONVENTION-ONLY BLOCK ⚠
# The engine does NOT read this workflow-level `roles:` block. It
# is declared here for human readers — so someone auditing the YAML
# can see the full cast of roles at a glance — but role resolution
# actually happens through the per-activity `allowed_roles`,
# `default_role`, and `authorization.roles` fields plus the role
# strings carried in users' session data. A typo here has zero
# runtime effect; a role used in an activity but missing from this
# list is NOT a load-time error. See the "Open follow-ups" section
# at the end of this file for the bug this surfaces.

roles:
  - name: ""                      # e.g. "oe:aanvrager", "oe:behandelaar", "oe:ondertekenaar"
    description: ""
```

---

## POC Users

```yaml
# Simulates what your auth framework provides in production.
#
# ⚠ POC-ONLY — NOT PRODUCTION ⚠
# This block exists because the current auth implementation
# (POCAuthMiddleware) looks users up by a plaintext X-POC-User
# header. Production will replace this with JWT/OAuth via the
# auth.mode setting in config.yaml. When that migration lands
# (Bug 28), this block becomes dead config. Don't invest in
# elaborate role hierarchies here — keep it minimal.
#
# Usernames must be UNIQUE across all plugins' poc_users blocks.
# The middleware builds a dict keyed by username, so the last
# duplicate wins silently. Bug 28 tracks that behavior.
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

  - id: ""                        # UUID. Stored as str — int also tolerated.
    username: ""                  # used in X-POC-User header. MUST BE UNIQUE.
    type: ""                      # "persoon", "medewerker", "systeem"
    name: ""                      # display name
    roles:                        # optional, default []
      - ""                        # TECHNICAL role strings from the auth system
    properties:                   # optional, default {}. must match what side effects reference
      field_name: "value"
    # uri: ""                     # optional, default None
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
#
# Load-time behavior:
#   * `model:` and every path in `schemas:` resolve via importlib at
#     plugin load. A bad dotted path is a fail-fast ImportError.
#   * A missing `type:` silently skips the entire entry (plugin.py:137).
#     Worth being explicit about — no load-time error surfaces.
#   * `cardinality:` — see the fallback note below.

entity_types:
  - type: ""                      # e.g. "oe:aanvraag", "gov:motivatie"
    description: ""
    cardinality: ""               # "single" = one logical entity per dossier
                                  #   - auto_resolve returns the one entity
                                  #   - requirements check latest version
                                  # "multiple" = many logical entities per dossier
                                  #   - auto_resolve returns all of them
                                  #   - client specifies which one via derivedFrom for revisions
                                  #
                                  # ⚠ FALLBACK: anything other than the literal
                                  # strings "single" or "multiple" (including typos
                                  # like "signle", "Single", or an omitted value)
                                  # silently resolves to "single" at is_singleton()
                                  # lookup time (plugin.py:988). See Open follow-ups.
    revisable: true               # ⚠ CONVENTION-ONLY: this field is NOT read by
                                  # the engine. Revision capability is actually
                                  # determined by the per-activity `entities:` block
                                  # and the presence of `derivedFrom` refs. Kept
                                  # here for human readability. See Open follow-ups.
    model: ""                     # Python import path to the Pydantic model
                                  # e.g. "dossier_subsidie.entities.Aanvraag"
                                  # Dotted path; resolved at plugin load.

    # --- Schema versioning (optional) ---
    # If this entity type has evolved over time and old rows need to
    # keep rendering, declare every version's Pydantic model under a
    # versioned key. The engine resolves `(type, schema_version)` → model
    # at read time, so a single entity_type can carry rows under multiple
    # schemas without migration.
    #
    # When `schemas` is set, the `model` field above is the legacy fallback
    # used for rows that predate versioning (schema_version is NULL). New
    # rows written through activities that declare version discipline
    # (see the activity-level `entities:` block) are stamped with a version
    # from this map.
    #
    # Shape: `dict[version_string, dotted_path_to_Pydantic_model]`.
    # Every activity-level `new_version`/`allowed_versions` reference is
    # cross-checked against this map at plugin load time by
    # `validate_workflow_version_references` — a version used but not
    # declared here fails fast.
    #
    # schemas:
    #   v1: "dossier_subsidie.entities.AanvraagV1"
    #   v2: "dossier_subsidie.entities.AanvraagV2"

  # Always include — managed by side effects, used for access control.
  # This model lives in the engine, not in the plugin.
  - type: "oe:dossier_access"
    description: "Bepaalt wie dit dossier kan zien en wat ze kunnen zien"
    cardinality: "single"
    revisable: true
    model: "dossier_engine.entities.DossierAccess"
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
#
# Auto-qualification of activity names (plugin.py:1111-1126):
# Activity `name:` values AND every reference-to-another-activity-name
# (in requirements.activities, forbidden.activities, side_effects[*].activity,
# tasks[*].cancel_if_activities, tasks[*].target_activity) get the
# workflow's default ontology prefix automatically prepended if they're
# not already qualified. Both `dienAanvraagIn` and `oe:dienAanvraagIn`
# work and are equivalent — choose the style you prefer and keep it
# consistent. Qualified form is less magical; bare form is terser.
# =======================================================================

activities:

  - name: ""                      # e.g. "dienAanvraagIn"
    label: ""                     # human readable, e.g. "Dien aanvraag in"
    description: ""               # ⚠ pure documentation; engine doesn't read this

    # --- Dossier Creation ---
    can_create_dossier: false     # true = this activity can start a new dossier.
                                  # When a can_create_dossier activity is the
                                  # first one for a new dossier, the workflow-
                                  # rules check is skipped on that first
                                  # invocation (preconditions.py:165-167) — by
                                  # design, since there are no prior activities
                                  # or statuses to check against. Authorization
                                  # still runs normally.

    # --- Client Callable ---
    # client_callable: false      # if false, only triggered by side_effects (system activity)
                                  # and is hidden from /eligible + /allowed lists.
                                  # Default is true. Note: the engine checks
                                  # `is False` specifically, so anything other
                                  # than the literal `false` (including absence
                                  # and the string "false") resolves to true.

    # --- Built-in (reserved) ---
    # built_in: true              # ⚠ RESERVED. Do NOT set this on your own
                                  # activities. The engine sets it on the
                                  # activities it injects (systemAction,
                                  # tombstone). Some invariants are relaxed
                                  # for built-in activities; setting it on a
                                  # plugin-defined activity will cause those
                                  # invariants to quietly not apply.

    # --- Functional Roles ---
    # The activity's role model is a three-way concept worth untangling:
    #
    # allowed_roles       = which role *strings* a client request may declare
    #                       in its `role:` field when invoking this activity.
    #                       Empty list means no enforcement. Not related to
    #                       user permissions — just declaration restrictions.
    #
    # default_role        = the role recorded when the client request omits
    #                       `role:`. The engine picks this value.
    #
    # authorization.roles = WHICH USERS may invoke this activity. Fully
    #                       orthogonal to the above two — this checks the
    #                       user's session roles, not the declared role on
    #                       the request.
    #
    # Common case: `allowed_roles: ["oe:aanvrager"]` + `default_role:
    # "oe:aanvrager"` + authorization that lets the citizen-user role
    # invoke it. Client sends no `role:`, engine defaults to aanvrager,
    # authorization verifies the session is allowed.
    #
    # Used in PROV associations (`wasAssociatedWith ... role:`).
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

    # --- Split-style hooks (optional, alternative to returning
    # status/tasks from the handler) ---
    #
    # Activities with a lot of status-decision or task-scheduling
    # logic can lift those concerns out of the handler into
    # dedicated, single-responsibility functions referenced by name
    # in YAML. The handler is then free to just compute content.
    #
    # The engine enforces "exactly one source per concern": declaring
    # a status_resolver forbids the handler from returning status;
    # declaring task_builders forbids the handler from returning
    # tasks. If both are set the activity execution fails with a
    # clear 500.
    #
    # status_resolver: "resolve_beslissing_status"
    # task_builders:
    #   - "schedule_trekAanvraag_if_onvolledig"
    #   - "send_ontvangstbevestiging"
    #
    # In the plugin:
    #   async def resolve_beslissing_status(ctx: ActivityContext) -> str | None:
    #       beslissing = ctx.get_typed("oe:beslissing")
    #       return "toelating_verleend" if beslissing.beslissing == "goedgekeurd" else None
    #
    #   async def schedule_trekAanvraag_if_onvolledig(ctx: ActivityContext) -> list[dict]:
    #       beslissing = ctx.get_typed("oe:beslissing")
    #       if not beslissing or beslissing.beslissing != "onvolledig":
    #           return []
    #       return [{"kind": "scheduled_activity", "target_activity": "trekAanvraagIn",
    #                "scheduled_for": "+30d", "cancel_if_activities": ["vervolledigAanvraag"]}]
    #
    # Register them by dotted path in this activity's YAML:
    #   status_resolver: "your_plugin.handlers.resolve_beslissing_status"
    #   task_builders:
    #     - "your_plugin.handlers.schedule_trekAanvraag_if_onvolledig"
    #
    # The engine resolves the paths at plugin load time via
    # ``build_callable_registries_from_workflow`` and populates
    # ``plugin.status_resolvers`` / ``plugin.task_builders`` automatically.
    # Plugin authors do NOT hand-build short-name dicts anymore (Obs 95 /
    # Round 28 — see plugin_guidebook.md "Dotted-path registration").
    #
    # See docs/plugin_guidebook.md "Split-style hooks" for decision
    # criteria (when to split vs keep a monolithic handler).

    # --- Authorization ---
    # WHO (not which role-string) is allowed to execute this activity.
    # Orthogonal to `allowed_roles`/`default_role` (which constrain the
    # role *declared on the request*); this block checks the user.
    #
    # Default if the `authorization:` block is omitted: `access:
    # "authenticated"` with no role restrictions. Any logged-in user
    # can run the activity.
    authorization:
      access: ""                  # "everyone", "authenticated", "roles"
                                  # Default: "authenticated" when the block is
                                  # present but `access:` is missing/empty.
      roles:
        # Pattern 1: Direct match
        # User must have the exact string in their session roles.
        - role: ""
        #
        # Pattern 2: Scoped match
        # Engine resolves `<role>:<value-from-entity>` and checks against
        # the user's session roles. Only applicable when a dossier exists
        # (not for can_create_dossier activities — there's no entity
        # to resolve against).
        # - role: "gemeente-toevoeger"
        #   scope:
        #     from_entity: "oe:aanvraag"
        #     field: "content.gemeente"
        #
        # Pattern 3: Entity-derived match
        # The entity field value IS the role string (no `role:` prefix).
        # Same dossier-exists constraint as Pattern 2.
        # - from_entity: "oe:aanvraag"
        #   field: "content.toegewezen_rol"
        #
        # ⚠ LOAD-TIME VALIDATION GAP (Bug 34 recon):
        # `scope:` dict typos — `feild:` for `field:`, missing `from_entity:`,
        # wrong key names — are NOT caught at plugin load. At runtime the
        # authorize_activity function catches any exception inside the
        # scope resolution and turns it into "this role entry doesn't
        # match, try the next one" — meaning a typo silently produces a
        # 403 for legitimate users. If authorization behaves oddly, check
        # scope dict keys against this template's names exactly.
        # See Open follow-ups for the tracking issue.

    # --- Workflow Rules ---
    requirements:
      activities:                 # which activities must have been completed
        - ""
      entities:                   # which entity types must exist (latest version)
        - ""
      statuses:                   # dossier must be in one of these statuses
        - ""
      # not_before (optional): earliest wall-clock moment the activity
      # becomes legal. Before this, a 422 "not yet available" is
      # returned. Accepts three forms — the same shape as the dict
      # forms of `scheduled_for`, minus the "+Nd from now" string form
      # (which has no fixed meaning at rule-evaluation time):
      #   * Absolute ISO 8601: "2026-01-01T00:00:00Z"
      #   * Entity field ref: {from_entity: "oe:x", field: "foo"} —
      #     MUST be a singleton; multi-cardinality types are rejected
      #     at plugin load.
      #   * Entity field + offset: {from_entity, field, offset: "+30d"}
      #     — the "30 days after X" idiom.
      # When the anchor entity doesn't exist yet, the rule is treated
      # as inactive (no deadline known → activity not blocked by it).
      # not_before: "2026-01-01T00:00:00Z"

    forbidden:
      activities:                 # which activities must NOT have been completed
        - ""
      statuses:                   # dossier must NOT be in any of these statuses
        - ""
      # not_after (optional): deadline past which the activity is no
      # longer legal. Same three shapes as `not_before` above. The
      # reminder idiom — "7 days before permit expiry" — is:
      #   not_after: {from_entity: "oe:permit", field: "expires_at", offset: "-7d"}
      # The resolved deadline surfaces in the eligible-activities
      # response as a flat `not_after` ISO string for frontend
      # countdowns. Eligibility cache is NOT time-aware: a stale
      # cached list can show an already-expired activity until the
      # next activity runs in the dossier — but the execution path
      # always does a fresh check, so clicking a stale-listed
      # expired activity returns 422, never lets it through.
      # not_after: "2026-12-31T23:59:59Z"

    # --- Used: references this activity reads ---
    # Only for existing entities (references) or external URIs.
    # No content here — content goes in the "generated" block.
    #
    # ⚠ AUTO-RESOLVE IS SYSTEM-CALLER-ONLY.
    # The `auto_resolve: "latest"` flag below is consulted ONLY when
    # the caller is `Caller.SYSTEM` (the worker executing a scheduled
    # task or cross-dossier activity) or during side-effect execution
    # (where the engine acts on behalf of the triggering user).
    #
    # For client-triggered activities — i.e. ordinary PUT requests
    # from the API — `auto_resolve:` is IGNORED. The client must send
    # explicit refs for every entity the activity needs. If the client
    # omits a ref, the corresponding slot is silently left empty: the
    # engine does NOT raise 422, does NOT look up the latest version,
    # does NOT consult the `required:` field. Downstream handlers /
    # validators may then raise (or, worse, silently fall through).
    #
    # See Open follow-ups #12 and #13 for:
    #   * The `required: true` field below not being read by the
    #     engine today (it's documentation-only).
    #   * The fact that the client-omits-declared-used-slot case
    #     silently succeeds rather than raising 422 (probably a bug).
    #
    # Resolution behavior per caller type:
    #
    #   Client caller (normal API PUT):
    #     - Client sends ref → engine validates it exists.
    #     - Client omits ref → slot silently left empty.
    #       (`auto_resolve` and `required` both IGNORED.)
    #
    #   System caller (worker for tasks, side effects):
    #     - Explicit ref → validated.
    #     - Omitted + `auto_resolve: "latest"` → engine resolves via
    #       trigger scope → dossier singleton lookup.
    #     - Omitted + no `auto_resolve` → slot left empty.
    #
    # Designing a client activity's `used:` block:
    #   Treat this as the authoritative list of what the CLIENT must
    #   send. Don't expect auto-resolve to cover for a forgetful
    #   client. If you want "the latest aanvraag" in a client-facing
    #   activity, have the client pass the ref it's working with.
    used:
      # Local entity reference
      - type: ""                  # entity type, e.g. "oe:aanvraag". REQUIRED —
                                  # missing key crashes with KeyError at runtime
                                  # (Open follow-up #9).
        required: false           # ⚠ READ FOR OPENAPI DOCS ONLY — not enforced
                                  # at request time. Client-facing activities do
                                  # NOT get a 422 when the client omits a
                                  # declared used ref, regardless of this value.
                                  # See Open follow-up #12. Setting `true` does
                                  # change the generated API docs label though,
                                  # so it's worth setting accurately.
        auto_resolve: "latest"    # Only consulted for Caller.SYSTEM (worker)
                                  # and side-effect resolution. IGNORED for
                                  # client-triggered activities.
                                  # null = no auto-resolve (also the default).
        description: ""           # documentary only — rendered in OpenAPI docs
                                  # but not otherwise consumed.

      # External entity (URI)
      # - type: "object"
      #   external: true          # engine only records the URI, no existence check
      #   required: false         # (see note above — OpenAPI-docs-only, not enforced)
      #   description: ""         # (see note above — documentary only)

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

    # --- Schema versioning (optional, per entity type) ---
    # For each entity type this activity reads or writes, you can
    # constrain which versions the activity accepts and which version
    # it stamps on new rows. Requires the referenced entity_type to
    # declare a `schemas:` map (see Entity Types section).
    #
    # `new_version` — the version stamped on fresh entities this
    #   activity generates. Applies when the client supplies no
    #   `derivedFrom` (i.e. it's a brand new logical entity) OR when
    #   the handler appends a generated entity.
    #
    # `allowed_versions` — the versions the activity will accept as
    #   the parent of a revision. If the client passes a `derivedFrom`
    #   pointing at an entity whose stored `schema_version` is not in
    #   this list, the engine returns `422 unsupported_schema_version`.
    #   For revisions, the engine inherits the parent's `schema_version`
    #   onto the new row (sticky) rather than re-stamping with
    #   `new_version`.
    #
    # entities:
    #   "oe:aanvraag":
    #     new_version: "v2"
    #     allowed_versions: ["v1", "v2"]
    #   "oe:beslissing":
    #     new_version: "v1"
    #     allowed_versions: ["v1"]

    # --- Activity-level relations opt-in ---
    # The workflow-level `relations:` block declares every type that
    # exists in this workflow, with its `kind` (domain | process_control),
    # its optional `from_types`/`to_types` constraints, and a description.
    # Activities then opt in here by type NAME — and can add:
    #   * `operations`: subset of [add, remove] (remove forbidden on
    #     process_control types)
    #   * `validator`: single-string validator name (legal for both kinds)
    #   * `validators`: {add, remove} dict with BOTH keys required (legal
    #     on domain only — process_control has no remove)
    #
    # FORBIDDEN at activity level: `kind`, `from_types`, `to_types`,
    # `description`. Those live at workflow level ONLY. The load-time
    # validator (Bug 78, Round 26) rejects plugins that violate this.
    #
    # A type that appears here but not in the workflow-level block is
    # a load-time error. A validator name that doesn't resolve in
    # `plugin.relation_validators` is a load-time error.
    #
    # relations:
    #   # Process-control — single-string validator is the only legal form.
    #   - type: "oe:neemtAkteVan"
    #     validator: "validate_neemt_akte_van"
    #
    #   # Domain — single-string validator applies to both add and remove.
    #   - type: "oe:betreft"
    #     operations: ["add"]
    #     validator: "validate_betreft_target"
    #
    #   # Domain — separate validators per operation.
    #   - type: "oe:gerelateerd_aan"
    #     operations: ["add", "remove"]
    #     validators:
    #       add: "validate_gerelateerd_add"
    #       remove: "validate_gerelateerd_remove"

    # --- Status ---
    # What status this activity sets on the dossier. Three forms:
    #
    # (a) String — the literal status to apply:
    #     status: "ingediend"
    #
    # (b) null / absent — no status change from YAML. The handler may
    #     still set status via `HandlerResult.status` or a `status_resolver`
    #     (see the split-style hooks section above).
    #
    # (c) Dict — data-driven: derive the status from a generated entity's
    #     content. Useful when the outcome depends on what the user submitted.
    #
    #     status:
    #       from_entity: "oe:besluit"       # entity type to read
    #       field: "content.uitkomst"        # dot-notation path
    #       mapping:
    #         toegekend: "besluit_toegekend"
    #         afgewezen: "besluit_afgewezen"
    #
    # Precedence when multiple sources are set:
    #   1. YAML `status:` (string or dict form) takes precedence over
    #      `status_resolver:` and over `HandlerResult.status`.
    #   2. `HandlerResult.status` + `status_resolver:` together raise a
    #      clear 500 (split_hooks.py:73-82). YAML `status:` + either of
    #      the other two does NOT raise — the YAML silently wins.
    #
    # ⚠ LOAD-TIME VALIDATION GAP (Obs 59):
    # Dict-form typos — `feild:` for `field:`, `mappings:` for `mapping:`,
    # missing `from_entity:` — are NOT caught at plugin load. At runtime
    # they raise KeyError inside finalize_dossier and surface as HTTP 500
    # mid-activity. If you use the dict form, double-check the three
    # required keys against this template.
    status: ""

    validators: []                # list of {name: "validator_fn_name"}
                                  # Cross-entity validators that run post-
                                  # handler, pre-commit. Each name resolves
                                  # to a dotted path via plugin.validators
                                  # (set up by build_callable_registries).

    side_effects: []              # list of {activity: "SystemActivityName"}
                                  # Activities to trigger after this one
                                  # completes. Two forms accepted:
                                  #  - bare string (legacy, normalized to
                                  #    {activity: <n>} at load).
                                  #  - dict with optional `condition:` or
                                  #    `condition_fn:` gating (mutex).
                                  # Bare strings are back-compat — new code
                                  # should use the dict form so condition/
                                  # condition_fn gates are easy to add
                                  # later. See the side_effects section
                                  # further below for condition shapes.

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

      # `kind:` must be one of: "fire_and_forget", "recorded",
      # "scheduled_activity", "cross_dossier_activity". The Pydantic
      # TaskEntity model enforces this at entity-construction time (Bug
      # 39, Round 32 — `kind: Literal[...]`). However, YAML → TaskEntity
      # construction happens at runtime, not plugin load, so a YAML typo
      # here crashes mid-activity with a Pydantic ValidationError rather
      # than failing fast at startup. Same with `status:` inside a task
      # entity. See Open follow-ups.
      #
      # `target_activity:` / `cancel_if_activities` entries: auto-qualified
      # but NOT cross-checked against this workflow's own `activities:`
      # list at load time. A typo like "mySetfyyysCorrection" only
      # surfaces when the worker tries to execute the target.

      # --- Type 1: Fire-and-forget ---
      # Runs inline during activity execution. No entity, no PROV.
      # If it fails, the activity still succeeds.
      - kind: "fire_and_forget"
        function: "your_plugin.tasks.send_notification_email"

      # --- Type 2: Recorded task ---
      # Worker picks it up, calls the function, creates a completed version.
      # PROV: activity → task_v1 (scheduled) → completeTask → task_v2 (completed)
      - kind: "recorded"
        function: "your_plugin.tasks.log_audit_event"

      # --- Type 3: Scheduled activity (same dossier) ---
      # Worker executes the target activity at the scheduled time.
      #
      # scheduled_for accepts four forms:
      #   * Signed relative offset: "+Nd" / "-Nd" / "+Nh" / "+Nm" /
      #     "+Nw" (resolved against the activity's start time). The
      #     sign is required. Good for "N days after the triggering
      #     activity" (+30d) or "N days before some reference
      #     point" (-7d, used inside the dict form below).
      #   * Absolute ISO 8601: "2026-05-01T00:00:00Z". Good for fixed
      #     wall-clock times (calibration dates, regulatory cutoffs).
      #   * Entity field reference: a dict that reads an ISO datetime
      #     (or date-only) from an entity this activity uses or
      #     generates. Same from_entity/field idiom as authorization.
      #       scheduled_for:
      #         from_entity: "oe:aanvraag"
      #         field: "content.registered_at"   # or "registered_at"
      #   * Entity field + offset: the dict form plus a signed offset.
      #     The reminder use case: "fire 7 days before permit expiry":
      #       scheduled_for:
      #         from_entity: "oe:aanvraag"
      #         field: "expires_at"
      #         offset: "-7d"
      #
      # For schedules depending on more than one entity, or logic the
      # DSL doesn't cover, compute the ISO string in a handler and
      # return it in HandlerResult.tasks (see "Conditional Task
      # Queueing from Handlers" below and dossier_toelatingen's
      # handle_beslissing for a worked example using
      # context.constants).
      #
      # cancel_if_activities: activity types that cancel this task if
      # they occur after the task was created.
      # PROV: activity → task (scheduled) → target_activity (wasInformedBy original)
      #       → completeTask (wasInformedBy target_activity) → task (completed)
      - kind: "scheduled_activity"
        target_activity: "trekAanvraagIn"
        scheduled_for: "+30d"           # 30 days from activity start
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
        function: "your_plugin.tasks.find_related_dossier"
        target_activity: "ontvangMelding"
        allow_multiple: true

    # --- Conditional tasks live in handlers ---
    # YAML tasks are unconditional: if the activity runs, the task is
    # scheduled. For "only schedule trekAanvraagIn when beslissing is
    # onvolledig" style rules, put the logic in a handler and append
    # tasks to HandlerResult.tasks. See "Conditional Task Queueing from
    # Handlers" below and dossier_toelatingen/handlers/__init__.py for
    # worked examples.
```

---

## Engine-provided activities

The engine auto-registers two activity-set features that any workflow can opt into. Both follow the same pattern: a top-level YAML block declares which roles may use the feature; the engine handles everything else — entity types, activity definitions, validators, handlers, pipeline phases.

### Tombstone

```yaml
tombstone:
  allowed_roles: ["beheerder"]
```

Registers a `tombstone` activity that redacts entities (nulls content, stamps `tombstoned_by`) and produces replacement rows. Omit the block to disable.

### Exception grants

```yaml
exceptions:
  grant_allowed_roles: ["beheerder"]
  retract_allowed_roles: ["beheerder"]
```

Registers `grantException`, `retractException`, `consumeException` and the `system:exception` entity type. Lets an administrator legally authorize one-shot bypass of the workflow-rules layer (`requirements` / `forbidden` / `not_before` / `not_after`) for a specific activity. Single-use by default — when the exempted activity runs, the engine auto-injects `consumeException` to flip the exception's status from `active` to `consumed`. See the Plugin Guidebook's "Exception grants" reference entry for the lifecycle.

The `system:exception` Pydantic model lives in `dossier_engine.entities` (`Exception_` class). Its `status` field is required, no default — every grant payload must send `status: "active"` explicitly. This is a PROV invariant: the engine validates content but doesn't persist Pydantic-coerced output, so a default here would produce stored content that doesn't match what the agent submitted.

---


## Task Functions

Task functions are defined in the plugin's `tasks/` module and referenced from the
YAML by fully-qualified dotted path (Obs 95 / Round 28).

```python
# Type 1 and 2: receives ActivityContext
async def send_notification_email(context: ActivityContext):
    aanvraag = await context.get_singleton_typed("oe:aanvraag")
    # ... send email ...

async def log_audit_event(context: ActivityContext):
    # ... log to external audit system ...
    pass

# Type 4: returns TaskResult with target dossier
from dossier_engine.engine import TaskResult

async def find_related_dossier(context: ActivityContext):
    aanvraag = await context.get_singleton_typed("oe:aanvraag")
    # ... determine target dossier ...
    return TaskResult(
        target_dossier_id="d5000000-...",
        content={"bericht": "Gerelateerd dossier is goedgekeurd"},
    )
```

Register each function by referencing its fully-qualified dotted path from the
activity's `tasks:` list in `workflow.yaml`:

```yaml
tasks:
  - kind: "fire_and_forget"
    function: "your_plugin.tasks.send_notification_email"
  - kind: "recorded"
    function: "your_plugin.tasks.log_audit_event"
  - kind: "cross_dossier_activity"
    function: "your_plugin.tasks.find_related_dossier"
    target_activity: "ontvangMelding"
```

The engine resolves each path at plugin load time. A typo fails fast with a
clear error naming the activity and YAML field — no more runtime-of-first-
invocation footguns from short-name dicts.

Type 3 (`scheduled_activity`) has no function — the worker just executes the
target activity.

---

## Conditional Task Queueing from Handlers

Tasks defined in the YAML `tasks:` block always execute. For conditional tasks — where
the decision depends on entity content at runtime — handlers can append tasks dynamically.

`HandlerResult` accepts a `tasks` list alongside `generated` and `status`:

```python
async def neem_beslissing(context: ActivityContext, content: dict | None) -> HandlerResult:
    beslissing = await context.get_singleton_typed("oe:beslissing")
    handtekening = await context.get_singleton_typed("oe:handtekening")

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
python -m dossier_engine.worker --once

# Run continuously, polling every 10 seconds
python -m dossier_engine.worker

# Custom interval and config
python -m dossier_engine.worker --interval 5 --config dossier_app/config.yaml
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

Registers workflow-specific search endpoints during app startup. The plugin is free to choose its own URL shape, but the production convention is **workflow-name-first** — `/{workflow}/...` — so that every route registered by the plugin is name-spaced under its workflow and doesn't collide with the engine's built-in `/dossiers/...` routes. The engine's route modules (activities, entities, PROV, archive) live under `/dossiers/{id}/...`; plugin-specific endpoints live under `/{workflow}/...`.

```python
def register_search_routes(app, get_user):
    # Workflow-specific search lives under /{workflow}/..., NOT under
    # /dossiers/{workflow}/... — the latter would shadow the engine's
    # own /dossiers/... routes.
    @app.get("/{workflow}/dossiers", tags=["{workflow}"])
    async def search_dossiers(
        q: str = None, gemeente: str = None, status: str = None,
        user: User = Depends(get_user),
    ):
        results = await es.search(index="dossiers-{workflow}", body=build_query(...))
        return {"results": results}

    # Admin endpoints follow the same convention. toelatingen uses
    # /{workflow}/admin/search/recreate, /{workflow}/admin/search/reindex,
    # /{workflow}/admin/search/reindex-all — see dossier_toelatingen/__init__.py
    # for the canonical example.
```

Substitute `{workflow}` with the plugin's actual name when registering — FastAPI won't interpret `{workflow}` in the path-decorator string the way it does `{id}` in a handler parameter. It's a placeholder in this template to show the shape; the real plugin code would use `"/toelatingen/dossiers"` literally.

The engine's generic `GET /dossiers` endpoint remains available as a basic stub for cross-workflow listing.

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
  ],
  "audit_access": ["ondertekenaar"]
}
```

- `role` or `agents`: who this entry applies to
- `view`: which entity types are visible (empty = nothing). Include `"external"` for external URI entities.
- `activity_view`: `"own"` (only own activities), `"related"` (own + touching visible entities), `"all"`
- `audit_access` (optional): list of roles that get full-provenance views (`/prov`, `/prov/graph/columns`, `/archive`) for this dossier. Combines with `global_audit_access` from config.yaml. Role-only (no per-agent grants).

Applied to: GET dossier, entity endpoints, PROV graph timeline.
Users without any matching `access` entry get HTTP 403.

**Two-tier model.** The `access` list gates business views (entity reads, timeline). The `audit_access` list (+ `global_audit_access` in config.yaml) gates the full-record views — PROV-JSON export, columns graph, archive PDF. A user with ordinary `access` does NOT automatically get audit views; the audit role has to be explicit. See the dossier-engine README's "Audit-level access" section.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `PUT` | `/dossiers/{id}/activities/{id}/{type}` | Execute a typed activity |
| `PUT` | `/dossiers/{id}/activities/{id}` | Execute a generic activity |
| `PUT` | `/dossiers/{id}/activities` | Execute batch activities atomically |
| `GET` | `/dossiers/{id}` | Get dossier detail (filtered by access) |
| `GET` | `/dossiers` | List dossiers (stub — use workflow search) |
| `GET` | `/{workflow}/dossiers` | Workflow-specific search (Elasticsearch; registered by the plugin's `search_route_factory`) |
| `POST` | `/{workflow}/admin/search/{recreate,reindex,reindex-all}` | Admin-only index management (plugin-registered) |
| `GET` | `/dossiers/{id}/entities/{type}` | All versions of an entity type |
| `GET` | `/dossiers/{id}/entities/{type}/{entity_id}` | All versions of a logical entity |
| `GET` | `/dossiers/{id}/entities/{type}/{entity_id}/{version_id}` | Single entity version |
| `GET` | `/dossiers/{id}/prov/graph/timeline` | Timeline — user view (dossier access) |
| `GET` | `/dossiers/{id}/prov` | PROV-JSON export (audit access) |
| `GET` | `/dossiers/{id}/prov/graph/columns` | Column layout — full record (audit access) |
| `GET` | `/dossiers/{id}/archive` | PDF/A-3b archive — full record (audit access) |

No query-parameter toggles on the PROV endpoints. Timeline always hides system activities and tasks; audit endpoints always include them. Behavior is determined by access tier.

---

## Full Workflow Example

See `dossier_toelatingen/workflow.yaml` for a complete example implementing
a heritage permit ("toelating beschermd erfgoed") workflow with:

- Client activities: dienAanvraagIn, bewerkAanvraag, vervolledigAanvraag, doeVoorstelBeslissing, tekenBeslissing, neemBeslissing, trekAanvraagIn
- System activities: setDossierAccess, duidVerantwoordelijkeOrganisatieAan, duidBehandelaarAan, setSystemFields
- Shared split-style hooks: `resolve_beslissing_status` + `schedule_trekAanvraag_if_onvolledig` referenced from both tekenBeslissing and neemBeslissing
- Scoped authorization (municipality-based roles)
- Entity-derived authorization (RRN/KBO from aanvraag)
- Side effect chains (dienAanvraagIn → duidVerantwoordelijkeOrganisatieAan → duidBehandelaarAan + setDossierAccess)
- Two decision paths: direct (neemBeslissing) and indirect (doeVoorstelBeslissing → tekenBeslissing)
- Four-eyes principle (behandelaar proposes, separate ondertekenaar signs or declines)
- Conditional tasks: task builder schedules trekAanvraagIn only when beslissing is onvolledig
- Recorded tasks (type 2): send_ontvangstbevestiging, log_beslissing_genomen, etc.
- Task cancellation: vervolledigAanvraag cancels pending trekAanvraagIn deadline
- External entities: heritage object URIs persisted with full PROV trail
- Post-activity search index hook (Elasticsearch stub)
- Workflow-specific search endpoint (/dossiers/toelatingen/search)
- Forbidden rules: dienAanvraagIn cannot be called twice

---

## Open follow-ups / known issues

**Scope note.** This section captures known drift and bugs that surfaced
during the exhaustive cross-reference of this template against the engine
code (`docs/workflow-yaml-inventory.md` — 826 lines, produced as the
input to this rewrite). Items listed here are NOT papered over by the
template rewrite itself — they're real engine or design issues that need
separate fixes. Keeping them visible here so future plugin authors
don't waste time debugging documented behavior that doesn't actually work,
and so the next cleanup round has a punch-list.

### 1. `relation_types` block is unused (plugin.py:243)

The engine has a dormant code path at `plugin.py:240-256` that reads
`workflow.get("relation_types", [])` and scans each entry for validator
dotted paths. But toelatingen's production YAML uses `relations:`
(not `relation_types:`), and `validate_relation_declarations` +
runtime dispatch both operate on `relations:`. The `relation_types`
scan reads nothing in production.

Likely dead code from a mid-refactor where both key names were in play.
Needs a decision: delete the scan, or if `relation_types:` was intended
as the canonical name, rename consistently and update this template.
Do not use `relation_types:` in any workflow YAML until resolved.

### 2. Workflow-level `roles:` is not enforced by the engine

The `## Roles` section above is flagged as convention-only because the
engine never reads `workflow.get("roles")`. A typo in a role name here
has zero effect; a role used in an activity's `allowed_roles` /
`authorization.roles` but missing from this list is NOT a load-time
error. If the project wants this list to be enforced, it's a new feature
(not a bug fix); if not, the `roles:` block probably shouldn't live at
workflow top-level at all.

### 3. `entity_types[*].revisable` is not read by the engine

Same shape as Finding 2. The `revisable: true/false` field is documented
in this template and allowed by the test-suite's key allow-list
(`test_guidebook_yaml.py`), but no engine code reads it. Revision
capability is determined at runtime by the per-activity `entities:`
block plus whether the request carries a `derivedFrom` ref. The field
here is cosmetic. Either wire it up as a hard load-time enforcement
(activity tries to revise a non-revisable entity → 422) or remove from
the contract.

### 4. Entity type `cardinality:` silently falls back to `"single"`

`plugin.py:988` resolves the value as `c if c in ("single", "multiple")
else "single"`. A typo like `"signle"`, `"Single"`, `"many"`, or an
omitted value all resolve to `"single"`. This silently changes
auto-resolve behavior and singleton lookup semantics without surfacing
an error. Either tighten to a load-time `Literal["single", "multiple"]`
check (rejecting unknowns), or document the fallback (done above, in
the Entity Types section).

### 5. Variable-name collision: `act` in archive.py

`dossier_engine/archive.py` uses the variable name `act` for two
different things: workflow-level activity definitions (dicts from YAML)
AND PROV-JSON activity rows (dicts from DB representation). Confusing
when grepping for "all keys consumed on a workflow activity" — shows up
as false positives for keys like `time` and `agent` that are only on the
PROV-JSON form. Worth renaming one of the two (e.g. `act_row` vs
`act_def`) for legibility.

### 6. 🐛 YAML `status:` silently overrides `status_resolver:`

When both a YAML `status:` (any form) AND a `status_resolver:` are
declared on the same activity, the YAML wins and the resolver's
return value is ignored (`finalization.py:98` reads `activity_def.get("status")`
first). The symmetric case — `HandlerResult.status` + `status_resolver`
together — DOES raise a clear 500 at `split_hooks.py:73-82`. This
asymmetry is a footgun: authors refactoring a handler to split out
status into a resolver may leave a stale `status:` in YAML and see
the resolver silently ignored. Fix options:
  * Raise at load time when both YAML `status:` and `status_resolver:`
    are set (cleanest; fail-fast).
  * Raise at runtime in finalization, symmetric to split_hooks.
  * Document the precedence explicitly (done above — but a proper
    fix is preferable).

### 7. Load-time validation gaps for YAML → Pydantic-entity boundaries

Three Pydantic models have been tightened to `Literal[...]` on their
string enum fields (TaskEntity.kind, TaskEntity.status — Bug 39, Round 32;
DossierAccessEntry.activity_view — Bug 27, Round 31). But YAML →
entity construction happens at runtime, not at plugin load, so a YAML
typo still crashes mid-activity rather than failing fast at startup.

Specific gaps:
  * `activities[*].tasks[*].kind` — YAML typo only crashes when the
    activity runs.
  * `activities[*].status` dict form — YAML typos in `from_entity:`,
    `field:`, `mapping:` crash inside `finalize_dossier` (Obs 59).
  * `activities[*].authorization.roles[*].scope` dict typos crash
    inside `authorize_activity` where they're swallowed by a broad
    `except Exception` (Bug 34).

Pattern is the same: add a `validate_*` function in plugin.py that walks
the relevant YAML structure and rejects malformed shapes at plugin load.
Existing `validate_side_effect_conditions` is the template to mirror.

### 8. No cross-reference validation of activity name references

Names in `tasks[*].target_activity`, `tasks[*].cancel_if_activities`,
`side_effects[*].activity`, `requirements.activities`,
`forbidden.activities` are auto-qualified (given the workflow's default
prefix) but NOT cross-checked against the workflow's own `activities:`
list at load time. A typo like `target_activity: "mySetfsysCorrection"`
resolves at runtime with a misleading error.

Additive load-time validator: walk every activity-name reference, check
it exists in `{act["name"] for act in workflow["activities"]}`. Similar
shape to the ones listed in Finding 7.

### 9. `used[*]` required `type:` is a bracket read, not a `.get`

`used.py:150` does `etype = used_def["type"]` — missing or null `type:`
crashes with KeyError rather than a clean load-time error with the
activity name. Small fix: use `.get` with an early raise, or add a
load-time shape check for `used[*]` entries.

### 10. `namespaces` re-register behavior undocumented

If a per-plugin `namespaces:` entry uses a prefix already registered
(by the engine or at app-level), the current behavior is undocumented
in this template. Need to trace `NamespaceRegistry.register()` to
determine if it's silent-accept, silent-overwrite, or reject. Whatever
it does, the template should state it.

### 11. `generates:` shape ambiguity

Documented as `list[str]` and used as such in production YAML, but the
engine's check pattern (`entity_type not in allowed_types`) technically
allows either list-of-strings OR list-of-dicts (dicts silently falsify
the `in` test and skip the check). Either tighten at load to reject
dicts in `generates:`, or document the dict form and what it means.
The current ambiguity is a latent bug waiting for someone to "improve"
the YAML by adding per-type metadata under `generates:`.

### 12. 🐛 Client callers that omit declared `used` refs don't get a 422

Surfaced during this template rewrite. The prior template claimed:

  *"Client omits + required → 422 error"*
  *"Client omits + auto_resolve → server resolves latest version"*

Neither claim is true for client callers. `used.py:56` runs auto-resolve
**only when `state.caller == Caller.SYSTEM`**. For the client path, a
missing declared `used` slot silently succeeds: no auto-resolve, no
422, just an empty slot in `state.resolved_entities`. Downstream handlers
might then raise AttributeError or silently fall through — either way,
the failure mode is opaque to the API client.

This is both a design question (should the engine enforce `required:`
for client callers?) and a documentation question (the old template
described the intended behavior as if it were real). The rewrite now
documents the actual behavior, but a proper fix is probably to add a
client-side required-check in `_resolve_explicit` that raises 422 when
a `required: true` slot wasn't supplied. That would make `required:`
functional for the first time.

Scope decision: the caller-class split is by design (system callers
legitimately need auto-resolve; clients shouldn't be allowed to skip
explicit references). The bug is the silent empty slot, not the absence
of auto-resolve.

### 13. `used[*].required` is read for OpenAPI docs only, not runtime

Similar shape to Findings 2 and 3 (`roles:`, `revisable:`) but with a
small caveat. The `required:` field appears in every `used` entry but
no runtime code reads it for enforcement. It IS read at
`_typed_doc.py:80` to render the field label in the auto-generated
OpenAPI docs ("**required**" vs "optional"). So setting `required: true`
makes the API docs say the field is required — but the engine doesn't
actually enforce that at request time (see Finding 12). Purely
cosmetic for behavior; documentary for API docs. If Finding 12 is
fixed by wiring up a client-side required-check, `required:` finally
becomes functional. Until then it's contract-by-convention.

`used[*].description` IS read the same way — included in the OpenAPI
field documentation. Also cosmetic for behavior.

---

**Inventory source.** The full enumeration of every YAML key the engine
reads, with file:line references, defaults, and template-coverage flags,
lives in `docs/workflow-yaml-inventory.md`. Refer there when adding new
engine consumers or removing dormant ones — that file is the
source-of-truth for "does the engine actually read this key?".
