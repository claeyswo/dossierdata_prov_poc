# Dossier Engine — File Tree

A role-per-file guide to the engine. Each entry describes what the
file does, what it exports, and how it fits with its neighbours.
Produced by reading each file directly.

For the phase-by-phase execution flow, see
[pipeline_architecture.md](pipeline_architecture.md). For plugin
authoring, see [plugin_guidebook.md](plugin_guidebook.md). For the
workflow YAML contract, see [../dossiertype_template.md](../dossiertype_template.md).

---

## Top-level layout

```
dossier_engine/
├── __init__.py                — public re-export: create_app
├── app.py                     — FastAPI app factory, startup wiring
├── entities.py                — engine-provided entity models + built-in activity defs
├── file_refs.py               — FileId type + download_url auto-injection
├── lineage.py                 — PROV graph traversal (cross-type entity lookup)
├── migrations.py              — data migration framework
├── archive/                   — PDF/A-3 archive generation
├── auth/                      — authentication middleware
├── db/                        — Postgres rows, repository, session, graph loader, Alembic runner
├── engine/                    — activity execution pipeline
├── observability/             — audit logging + Sentry integration
├── plugin/                    — Plugin dataclass + validators + registries + normalize
├── prov/                      — PROV vocabulary (IRIs, JSON-LD, namespaces, activity names)
├── routes/                    — HTTP API surface
├── search/                    — Elasticsearch integration (common index + ACL)
└── worker/                    — task worker process (python -m dossier_engine.worker)
```

---

## Top-level files

### `__init__.py` (3 lines)

Re-exports `create_app` so callers can write
`from dossier_engine import create_app`. Nothing else.

### `app.py` (393 lines)

FastAPI app factory. The `create_app(config_path)` function is the
single-entry startup path for the web tier:

1. Loads `config.yaml` and constructs the `PluginRegistry` via
   `load_config_and_registry`, also exposed for the worker's startup
   path. For every plugin listed under `plugins:`, the function
   imports the module, calls its `create_plugin()`, injects the
   engine-provided `TaskEntity` and `SystemNote` models, appends the
   built-in `SYSTEM_ACTION_DEF` to the workflow's activity list, and
   appends a deep-copy of `TOMBSTONE_ACTIVITY_DEF` with the workflow's
   `tombstone.allowed_roles` overlaid (deny-all by default — if the
   workflow doesn't declare the block, the role list stays empty and
   no one can tombstone).
2. Configures the IRI namespace via `prov.iris.configure_iri_base`
   from `config.iri_base` (dossier prefix and ontology namespace),
   before any route that generates or parses IRIs is registered.
3. Builds a `NamespaceRegistry` seeded with built-in RDF/PROV prefixes,
   registers the default workflow prefix (typically `oe`), overlays
   app-level `namespaces:` from config.yaml, then overlays each
   plugin's own `namespaces:`. Every qualified type referenced by
   every plugin's YAML is validated against the registry via the
   local `_validate_plugin_prefixes` helper — activity names,
   `entity_types`, workflow-level `relations`, and per-activity
   `generates`/`used`/`relations`. First unknown prefix raises
   `ValueError` with the offending path.
4. Runs `validate_relation_declarations` and
   `validate_relation_validator_registrations` (Bug 78 of Round 26)
   against every plugin so the "relation types declared once at
   workflow level; activities reference by name" contract is enforced
   at boot.
5. Installs a single FastAPI `lifespan` async context manager (Bug 13,
   Round 33) that on startup: configures audit logging from
   `config.audit` or `$DOSSIER_AUDIT_LOG_PATH`, calls
   `db.init_db(db_url)`, then calls `db.alembic._run_alembic_migrations(db_url)`.
   Alembic failure is fail-fast — no fallback to `create_tables`. On
   shutdown, closes the ES client.
6. Initializes Sentry for the web tier via
   `observability.sentry.init_sentry_fastapi(app)` before CORS so
   Sentry sees the full request lifecycle including preflight handling.
7. Collects `poc_users` from every plugin, appends the `SYSTEM_USER`,
   constructs `POCAuthMiddleware`.
8. Adds CORS middleware from `config.cors.allowed_origins` (defaults
   to `["*"]`).
9. Registers `GET /health` (liveness — process up) and
   `GET /health/ready` (readiness — DB reachable via `SELECT 1`).
10. Pushes `global_access` and `global_admin_access` config into the
    `search` module so indexers include global roles in `__acl__` and
    admin endpoints can gate on admin roles without needing those
    threaded through their factory signatures.
11. Calls `routes.register_routes`, `routes.prov.register_prov_routes`,
    and `routes.admin_search.register_admin_search_routes`.

Re-exports `SYSTEM_USER` (from `auth`) and `_run_alembic_migrations`
(from `db.alembic`) so existing call sites that import these from
`dossier_engine.app` keep working after the Round 34 split.

### `entities.py` (198 lines)

Engine-provided entity Pydantic models and built-in activity
definitions that every workflow inherits without declaring:

- `DossierAccessEntry` / `DossierAccess` — the access-control entity
  (`oe:dossier_access`) that governs who can view what in a dossier.
  `DossierAccessEntry.activity_view` is typed as
  `Literal["all", "own"] | list[str] | dict`; see the field's inline
  comment and `routes/_helpers/activity_visibility.py` for read-path
  semantics. The `"related"` mode was removed in Round 31; Pydantic
  rejects it at write time and `parse_activity_view` deny-safes it
  at read time.
- `TaskEntity` — content model for `system:task` entities. Uses
  `Literal[...]` on `kind` and `status` so invalid YAML fails at
  Pydantic construction. The class docstring enumerates the full
  lifecycle (`scheduled → completed | scheduled → dead_letter |
  scheduled → cancelled | scheduled → superseded`) and the
  exponential-backoff retry semantics. Error telemetry goes to the
  Python logging system (and Sentry), NOT onto the task row.
- `SystemNote` — content model for `system:note` entities. Text plus
  optional `ticket` field; used to record why a systemAction ran.
- `SYSTEM_ACTION_DEF` — built-in `systemAction` activity. Generic
  system activity for migrations, corrections, administrative
  operations. `client_callable: true` but gated to the
  `systeemgebruiker` role. Accepts any entity type in `generates`.
- `TOMBSTONE_ACTIVITY_DEF` — built-in `tombstone` activity. Irreversible
  content redaction (breaks PROV provenance by design). `built_in:
  true` so the used/generated disjoint invariant is skipped (the
  tombstone shape validator in `pipeline/tombstone.py` has its own
  rules). `authorization.roles` is left empty by default — the
  allowed role list is overlaid at boot time from
  `workflow.tombstone.allowed_roles`, so a workflow that doesn't
  opt in denies every caller.

### `file_refs.py` (184 lines)

The `FileId` type (a `str` subclass, registered via
`__get_pydantic_core_schema__` — Pydantic v2's documented extension
point) plus the response-walking logic that auto-injects
`<field>_download_url` sibling keys into GET responses.

Naming rule: a field named `file_id` gets a `file_download_url`
sibling; a field named `brief` gets `brief_download_url`; a field
named `signed_pdf_id` gets `signed_pdf_download_url`. The walker
(`inject_download_urls → _walk_dict`) uses
`Model.model_fields` + `typing.get_args`/`get_origin` to identify
`FileId` annotations (including inside `Optional`, `list[FileId]`,
and nested BaseModels). Values are copied through unchanged — the
walker doesn't re-validate, so extra/legacy fields survive and
schema drift on read is tolerated. Signed URLs are minted by the
`SignFn` the caller passes in.

### `lineage.py` (226 lines)

Activity-graph traversal for "find a related entity of a different
type." Unlike a pure derivation walk (which follows `derivedFrom`
and `used` edges between versions of the same logical entity), this
walker inspects each activity's full signature — both the entities
it used and the entities it co-generated, plus the activity it was
informed by — so it can hop sideways across entity types.

Canonical use case: a sideways lookup where one entity's `entity_id`
must be resolved from an activity's scope that didn't touch it
directly. For example, given a `beslissing`, walk back through the
activity that generated it to find the `aanvraag` the beslissing
was about. (No production caller currently uses this — the scheduled-
task anchoring machinery that once depended on it has been removed
— but the walker is self-contained and preserved for future use.)

Defining behaviours:

- **Bug 53 frontier fix**: the frontier is a `set`, and we only
  enqueue activities we haven't already visited. Without this, a
  high-fan-in PROV graph (50 activities using an entity generated
  by activity A) enqueues A 50 times on one hop and compounds
  further.
- **Bug 54 ambiguity fix**: when a visited activity touches more
  than one distinct `entity_id` of the target type, the walker
  raises `LineageAmbiguous` (defined here) carrying the
  `activity_id`, `target_type`, and candidate ids. Before this,
  ambiguity and "no match" both silently returned `None`.
- **Intra-dossier by construction**: the walker refuses to traverse
  any activity whose `dossier_id` differs from the scope argument.
  Defense-in-depth against data corruption or PROV manipulation that
  would otherwise leak a confirmation signal about another dossier's
  graph.

### `migrations.py` (342 lines)

Data-migration framework. Applies content transforms to existing
entities, one dossier at a time, through the engine's normal
activity pipeline. Every migration produces a full PROV audit trail:
a `systemAction` activity per dossier with the old entity version in
`derivedFrom` (no `used` entry — the disjoint invariant forbids
listing the revised entity in both blocks) and the transformed
version in `generated`, plus a `system:note` recording the migration
UUID and message.

Exports `DataMigration` (dataclass: `id`, `message`, `target_type`,
`transform`, optional `filter`, optional `workflow`) and
`run_migrations(migrations, config_path, *, dry_run, batch_size)`.

The idempotency check is a DB-level query (find a `system:note` whose
content has `migration_id == migration.id` in the dossier) so a
crashed run can be restarted — already-migrated dossiers are skipped.
Each dossier's migration runs in its own transaction; a failure in
one doesn't affect others. `transform` returning `None` skips the
entity; returning the unchanged content also skips (no-op).

---

## `archive/` — PDF/A-3 archive generation

```
archive/
├── __init__.py        — re-exports generate_archive, render_timeline_svg
├── orchestrator.py    — the async generate_archive function
├── pdf.py             — ArchivePDF class (FPDF subclass)
├── svg_timeline.py    — static SVG timeline renderer
└── fonts.py           — DejaVu font path resolution
```

`generate_archive(session, dossier_id, dossier, registry, prov_json,
file_storage_root=None)` is the single public entry. Called from
`routes/prov.py` for the `/dossiers/{id}/archive` endpoint. Returns
PDF bytes.

### `archive/__init__.py` (30 lines)

Module docstring explaining the archive's role (cover page, static
SVG provenance timeline, entity content pages, embedded PROV-JSON
and bijlagen attachments). Re-exports `generate_archive` and
`render_timeline_svg`.

### `archive/orchestrator.py` (417 lines)

The async `generate_archive` function. Loads all dossier rows from
the DB (activities, entities, associations, used, agents), filters
out system activities and system-entity types for the visible
timeline, computes per-activity timeline metadata, calls
`render_timeline_svg` for the timeline image, then drives
`ArchivePDF` through its cover/timeline/entities/attachments
sections page by page. Entity-content pages cover ALL versions
including tombstoned ones (`[VERWIJDERD]` label, `(geen inhoud)`
placeholder). Bijlagen file bytes are read from
`file_storage_root/{temp,permanent}/{file_id}` when the argument
is set. PDF/A-3b XMP metadata is set via `pdf.set_xmp_metadata`,
and `prov.json` plus every bijlage is embedded via
`pdf.embed_file` as PDF/A-3 attachments.

### `archive/pdf.py` (49 lines)

`ArchivePDF` — a thin FPDF subclass with a workflow-aware header,
page-number footer (`Pagina {n}/{nb}`), landscape A4, and DejaVu
font setup (regular, bold, italic, mono). Fonts are resolved
through `archive.fonts.find_font`.

### `archive/svg_timeline.py` (197 lines)

`render_timeline_svg(activities, entities_by_type, agents, used_map,
generated_map, derivations, *, width=1200)` — pure server-side Python
SVG generation. Takes activities (rendered as columns), entities
(rows grouped by type, versions placed under their generating
activity's column), and derivation edges; returns an SVG string. Used
for the archive's timeline image — static vector graphics that
survive PDF embedding without a JavaScript runtime.

Also defines the color palette (`COL_BG`, `COL_ACTIVITY`,
`COL_ENTITY`, `COL_AGENT`, `COL_SYSTEM`, `COL_TASK`, `COL_EXTERNAL`,
`COL_DERIVED`, `COL_TEXT`, `COL_MUTED`, `COL_LINE`), `_hex_to_rgb`,
and `_esc` (XML escape).

### `archive/fonts.py` (152 lines)

DejaVu font discovery. `find_font("regular" | "bold" | "italic" |
"mono")` returns a filesystem `Path`. Searches `$DOSSIER_FONT_DIR`
first (operator escape hatch), then a prioritized candidate list
covering Debian/Ubuntu, Alpine, RHEL/Rocky/Fedora, Arch, and macOS
paths. Raises `FileNotFoundError` with a clear, actionable message
(listing the `apt-get`/`apk`/`dnf`/`brew` commands per distro) if
no font is found — turning the previous "opaque `FileNotFoundError`
pointing at a Debian path" into a self-explanatory failure on any
host. `check_fonts_available()` is a startup-time fail-fast variant
for deployments that want to catch missing fonts before the first
archive request.

---

## `auth/` — authentication middleware

### `auth/__init__.py` (62 lines)

`POCAuthMiddleware`. Simulates auth by looking up the `X-POC-User`
header against the merged `poc_users` list (assembled by `app.py`
from every plugin's workflow YAML plus `SYSTEM_USER`). In production,
replace with JWT/OAuth middleware.

Also defines the `User` dataclass (`id`, `type`, `name`, `roles`,
`properties`, optional `uri`) and the canonical `SYSTEM_USER`
singleton (`agenten/system` URI, `systeemgebruiker` role). The
system user is used by the worker's task runner and the engine's
side-effect executor to attribute system-initiated work.
Re-exported by `dossier_engine.app` for back-compat.

---

## `db/` — Postgres rows, repository, session, graph loader, Alembic

```
db/
├── __init__.py         — re-exports init_db, create_tables, get_session_factory,
│                         run_with_deadlock_retry, Repository
├── session.py          — async engine + session factory + deadlock retry helper
├── alembic.py          — subprocess-based Alembic runner (fail-fast)
├── graph_loader.py     — load_dossier_graph_rows + DossierGraphRows
└── models/
    ├── __init__.py     — re-exports Base + 8 Row classes + Repository
    ├── rows.py         — SQLAlchemy table definitions + type shims
    └── repository.py   — Repository class (session-bound data access)
```

### `db/__init__.py` (9 lines)

Re-exports `init_db`, `create_tables`, `get_session_factory`,
`run_with_deadlock_retry` from `session.py`, and `Repository` from
`models/`.

### `db/session.py` (147 lines)

Async engine + session factory. `init_db(database_url, *, pool_size=10,
max_overflow=20, pool_recycle=1800, pool_timeout=30)` configures the
global `_engine` and `_session_factory`. Pool defaults are tuned for
medium load: 10 persistent + 20 burst = 30 max connections, 30-minute
recycle to dodge Postgres `idle_in_transaction_session_timeout`.
`get_session_factory()` returns the factory; `create_tables()` calls
`Base.metadata.create_all` and is used by tests (production always
uses Alembic).

`run_with_deadlock_retry(work, *, max_attempts=3, base_backoff_seconds=0.05)`
is the Bug 74 safety net. It runs `work(session)` inside an open
transaction; if Postgres reports `deadlock_detected` (SQLSTATE
40P01, detected via `_is_deadlock_error` which inspects both
`exc.orig.sqlstate` and `exc.__cause__`), it retries with
exponential backoff and ±25% jitter. The primary fix for Bug 74 is
structural (the worker acquires the dossier lock in the same order
user activities do, see `worker._execute_claimed_task`); this
wrapper exists as defence-in-depth for future lock-order inversions.

### `db/alembic.py` (92 lines)

`_run_alembic_migrations(db_url)`. Invoked by `app.py`'s lifespan
startup block. Runs `python3 -m alembic upgrade head` in a
subprocess (necessary because Alembic's `env.py` calls
`asyncio.run()` internally, which can't nest inside uvicorn's
running event loop). `alembic.ini` is located via
`__file__.parent.parent.parent` — missing `alembic.ini` is a hard
error (deployments that ship without migration infrastructure are
broken, not convenient).

Fail-fast policy: any non-zero exit aborts startup with
`RuntimeError`. The previous behaviour (falling back to
`create_tables()`) risked silently accepting a partially migrated
schema — `create_all` no-ops on existing tables, so the half-applied
state would survive, requests would land on a mismatched schema,
and the operator wouldn't notice until data corruption surfaced.
Refusing to start is safer; the operator fixes the migration and
retries.

Factored out of `dossier_engine.app` in Round 34 so `app.py` can
stay focused on FastAPI wiring. `app.py` still re-exports the
function.

### `db/graph_loader.py` (160 lines)

One call that fetches every row needed to reason about a dossier's
provenance graph — activities, entities, associations, used — plus
an agent lookup, with pre-built per-activity indexes
(`assoc_by_activity`, `used_by_activity`, `entity_by_id`) so callers
don't have to re-bucket. Extracted into `db/` in Round 30.5 from
`prov/json_ld.py`, where it was originally colocated with the
PROV-JSON document builder but had five callers across three route
modules plus the JSON builder.

Callers:
- `routes/prov.py` — `/prov` endpoint (feeds `prov.json_ld.build_prov_graph`).
- `routes/prov_columns.py` — `/prov/graph/columns` and `/prov/graph/timeline`.
- `routes/dossiers.py::get_dossier` — uses the indexes to avoid N+1
  in the visibility loop (Bug 9, Round 29).
- `prov.json_ld.build_prov_graph` — the PROV-JSON assembler.

`DossierGraphRows` is a dataclass bundling the rowsets + indexes.
Agent URIs for entity `attributed_to` fields are also loaded in the
same pass.

### `db/models/__init__.py` (44 lines)

Re-exports `Base`, `UUID_DB`, `JSON_DB`, the eight Row classes
(`DossierRow`, `ActivityRow`, `AssociationRow`, `EntityRow`,
`UsedRow`, `RelationRow`, `AgentRow`, `DomainRelationRow`), and
`Repository`. Split out of a single `models.py` file in Round 34.

### `db/models/rows.py` (227 lines)

The eight SQLAlchemy-mapped table definitions. All tables are
append-only: no UPDATEs, no DELETEs (except two specific cases —
see below). Columns use Postgres-native types via two aliases:
`UUID_DB = lambda: PGUUID(as_uuid=True)` and `JSON_DB = JSONB`.
Postgres 16+ required; the earlier POC's SQLite support was removed
during the worker production-readiness pass.

Tables (with notable details):

- **`dossiers`** — `id`, `workflow`, `cached_status` (denormalized),
  `eligible_activities` (JSON list), `created_at`. The cached fields
  are updated by the engine's finalization phase after each activity.
- **`activities`** — `id`, `dossier_id`, `type`, two typed `informed_by`
  columns (`informed_by_activity_id` for local UUIDs,
  `informed_by_uri` for cross-dossier IRIs — CHECK constraint enforces
  at most one is set), `computed_status`, `started_at`, `ended_at`,
  `created_at`. The `informed_by` property returns the display form.
  Indexes on `dossier_id`, `type`, and the composite `(dossier_id, type)`.
- **`associations`** — PROV `wasAssociatedWith`. `activity_id`,
  `agent_id`, `agent_name`, `agent_type`, `role`, `created_at`.
- **`entities`** — `id` (version UUID), `entity_id` (logical UUID,
  stable across revisions), `dossier_id`, `type`, `generated_by`,
  `derived_from`, `attributed_to`, `content` (JSONB, nullable for
  tombstoned rows), `schema_version` (NULL = legacy/unversioned),
  `tombstoned_by`, `created_at`. Multiple composite indexes for the
  query patterns the repo uses.
- **`used`** — PROV `used`. Composite PK on `(activity_id, entity_id)`.
- **`activity_relations`** — generic activity→entity relation under
  a named type (e.g. `oe:neemtAkteVan` for explicit acknowledgement
  of newer entity versions the activity chose not to act on). PK on
  `(activity_id, entity_id, relation_type)`.
- **`agents`** — `id`, `type`, `name`, `uri` (canonical external
  IRI), `properties` (JSONB), `created_at`, `updated_at`.
- **`domain_relations`** — entity↔entity or entity↔URI semantic links
  (`from_ref`, `to_ref`, `relation_type`), with the activity recorded
  as provenance (`created_by_activity_id`) plus optional supersession
  metadata (`superseded_by_activity_id`, `superseded_at`). Distinct
  from `activity_relations` (process-control edges); here neither
  endpoint is the activity. Superseded rows stay in the table for
  history.

The two places where rows are mutated rather than appended: tombstone
nulls `content` + stamps `tombstoned_by` (see
`Repository.tombstone_entity_versions`); domain-relation supersession
sets `superseded_by_activity_id` + `superseded_at`. Everywhere else
is INSERT-only.

### `db/models/repository.py` (555 lines)

The `Repository` class — the single object the rest of the codebase
uses to interact with the database. One file on purpose despite its
length; methods cross-reference each other heavily and splitting by
table would spread one logical unit across files. See the Round 34
refactor plan.

All writes are INSERTs (append-only; the two mutation exceptions are
called out in `rows.py`). Session-scoped caches on the Repository
instance reduce redundant reads within a single request:
`_ensured_agents` (skip redundant agent upserts), `_activities_cache`
(keyed by dossier_id — `derive_status`, `validate_workflow_rules`,
`compute_eligible_activities`, and the post-activity hook all call
`get_activities_for_dossier` within one `execute_activity`), and
`_dossier_cache`. All three die with the session.

Method groups:

- **Dossier** — `get_dossier`, `get_dossier_for_update` (takes
  `SELECT ... FOR UPDATE` lock, bypasses the session cache to force
  a fresh query; this is the optimistic-concurrency replacement —
  rather than ETags and client-side retry, the DB serializes at
  the dossier boundary where activities genuinely conflict),
  `create_dossier`.
- **Activity** — `get_activity`, `get_activities_for_dossier` (cached),
  `create_activity` (classifies `informed_by` into UUID vs URI once
  at write, stashing into the right typed column; readers stay
  typed).
- **Association** — `create_association`.
- **Entity** — `get_entity`, `get_singleton_entity`,
  `get_latest_entity_by_id`, `get_all_latest_entities`,
  `get_entities_by_type`, `get_entities_by_type_latest`,
  `get_entity_versions`, `entity_type_exists`, `create_entity`,
  `ensure_external_entity` (idempotent — deterministic UUIDv5 from
  `{dossier_id}:{uri}`), `tombstone_entity_versions` (the one place
  that UPDATEs entity rows).
- **Used** — `create_used`, `get_used_entity_ids_for_activity`,
  `get_entities_generated_by_activity`,
  `get_used_entities_for_activity`. The last two are activity-id-only
  (no dossier filter); their docstrings spell out the contract for
  callers traversing PROV IDs from untrusted inputs — they must
  verify dossier scope separately (the lineage walker does).
- **Relations** (process-control) — `create_relation`,
  `get_relations_for_activity`.
- **Domain relations** — `create_domain_relation` (idempotent on
  active `(type, from, to)`), `supersede_domain_relation`,
  `get_active_domain_relations`.
- **Agent** — `ensure_agent` (fast-path short-circuits via
  `_ensured_agents`; only writes when name/properties/uri actually
  changed).

---

## `engine/` — activity execution pipeline

```
engine/
├── __init__.py        — execute_activity orchestrator (25-line table of contents
│                        over the pipeline phases) + re-exports the whole surface
├── context.py         — ActivityContext, _PendingEntity, HandlerResult, TaskResult
├── errors.py          — ActivityError, CardinalityError
├── lookups.py         — lookup_singleton, resolve_from_trigger, resolve_from_prefetched
├── refs.py            — EntityRef parsing + canonical string format
├── response.py        — build_replay_response (for idempotent PUT replays)
├── scheduling.py      — resolve_scheduled_for (signed offsets / ISO 8601 / entity field forms)
├── state.py           — ActivityState (the mutable state threaded through phases)
└── pipeline/          — the per-phase implementations
```

### `engine/__init__.py` (259 lines)

The `execute_activity(...)` orchestrator. Built as a 25-line "table
of contents" that reads top-to-bottom like the numbered phases in
the design brief. Each phase reads and writes fields on
`ActivityState`; by the time the function returns, `state` carries
the full manifest the response builder serializes.

Phase order:

1. `check_idempotency` — if the `activity_id` already exists,
   return a replay response via `build_replay_response` and stop.
2. `ensure_dossier` — `SELECT ... FOR UPDATE` on the dossier row,
   creating it if the activity has `can_create_dossier: true` and
   the row doesn't exist yet.
3. `authorize` — `authorize_activity` over the activity's
   authorization block.
4. `resolve_role` — default or validate the PROV functional role.
5. `check_workflow_rules` — `validate_workflow_rules` against
   `requirements`/`forbidden` blocks; skipped on the very first
   activity of a brand-new dossier.
6. `resolve_used` — turn raw used refs into `EntityRow` objects,
   persist externals, auto-resolve system-caller slots.
7. `enforce_used_generated_disjoint` — structural check that a
   logical entity isn't in both blocks.
8. `process_generated` — type gate, derivation validation, schema
   version resolution, content validation, pending-entity
   registration.
9. `process_relations` — parse + dispatch-by-kind + validator
   firing.
10. `run_custom_validators` — activity-declared YAML validators.
11. `validate_tombstone` — shape-check for the built-in tombstone
    activity (no-op otherwise).
12. `create_activity_row` — `ensure_agent`, then persist the
    activity + `wasAssociatedWith` association.
13. `run_handler` — invoke the activity's handler if any, append
    handler-generated entities, stash `handler_result`.
14. `run_split_hooks` — invoke `status_resolver` / `task_builders`
    if the activity opted into the split style.
15. `persist_outputs` — local generated entities, externals,
    tombstone redactions, `used` links, relation rows, domain
    relations (add + supersede).
16. `determine_status` — resolve the literal / handler / mapped
    status and stamp `activity_row.computed_status`.
17. `flush` + `execute_side_effects` — recursive side-effect chain
    (up to depth 10).
18. `process_tasks` — schedule recorded/scheduled/cross-dossier
    tasks, fire-and-forget inline execution.
19. `cancel_matching_tasks` — cancel prior scheduled tasks whose
    `cancel_if_activities` includes the current activity.
20. `run_pre_commit_hooks` — plugin-declared synchronous hooks;
    exceptions propagate and roll the whole activity back.
21. `finalize_dossier` — derive status, call `post_activity_hook`,
    cache status + eligible activities, compute user-filtered
    allowed list. Skipped on the bulk path.
22. `build_full_response` — assemble the JSON-serializable response.

Also re-exports every public pipeline symbol so callers can import
`execute_activity`, `ActivityError`, `ActivityContext`,
`HandlerResult`, `TaskResult`, `EntityRef`, `authorize_activity`,
`derive_status`, `compute_eligible_activities`, `derive_allowed_activities`,
`filter_by_user_auth`, `lookup_singleton`, `resolve_from_trigger`,
and `Caller` from `dossier_engine.engine`.

### `engine/context.py` (312 lines)

Handler-facing types.

- `_PendingEntity` — duck-typed stand-in for an `EntityRow` that
  hasn't been persisted yet. Used during the same activity so
  handlers can read entities the engine is in the process of
  generating via `context.get_typed`. Exposes every column
  handlers or walkers might read (Bug 20, Round 30):
  `content`, `entity_id`, `id`, `attributed_to`, `schema_version`,
  `type`, `dossier_id`, `generated_by`, `derived_from`. Two fields
  are invariantly `None`: `tombstoned_by` (pending entities can't
  be tombstoned — tombstoning runs in a later phase) and
  `created_at` (set by the DB at INSERT time). A parity test
  (`tests/unit/test_refs_and_plugin.py::TestPendingEntityFieldParity`)
  enumerates every `EntityRow` column and fails loudly if this
  class drifts again.
- `ActivityContext` — passed to handlers, validators, split hooks,
  and side-effect condition functions. Wraps the repo, the dossier
  id, the resolved `used` entities, the plugin reference, and
  optionally the triggering activity id. Two user fields:
  - `user` — the executor (who is *doing* the work right now;
    system for side-effect/worker code, the request-maker for
    direct handlers).
  - `triggering_user` — who is *attributed* with the activity that
    caused this context to exist (the request-maker, even for
    nested side effects). The split matters for audit attribution:
    a `move_bijlagen_to_permanent` worker task that gets a 403
    from the file service emits `dossier.denied` with the
    aanvrager (triggering_user) as actor, not the system worker.
  Methods: `get_used_entity`, `get_used_row`, `get_typed` (routes
  through `plugin.resolve_schema` so the returned model matches
  the row's stored `schema_version`), `get_singleton_typed`,
  `get_singleton_entity` (both cardinality-guarded),
  `get_entities_latest`, `has_activity`, and the `constants`
  property (typed Pydantic BaseSettings instance — `None` if the
  plugin didn't declare one; accessing attrs on None raises
  AttributeError, which is the desired loud failure).
- `HandlerResult` — what a handler returns. Accepts a single
  `content` dict (auto-wrapped into `generated=[{"type": None,
  "content": content}]`), an explicit `generated` list (items are
  dicts or legacy `(type, content)` tuples), an optional `status`,
  and an optional `tasks` list.
- `TaskResult` — what a cross-dossier task function returns.
  `target_dossier_id` tells the worker which dossier to land the
  resulting activity in.

### `engine/errors.py` (44 lines)

`ActivityError(status_code, detail, payload=None)` — raised by
validators, handlers, and pipeline phases when an activity is
rejected. Carries an HTTP status, a human message, and an optional
structured payload merged into the JSON response body by
`routes/_helpers/errors.py::activity_error_to_http`.

`CardinalityError` — raised when engine or handler code does a
singleton lookup on a type the plugin declared as `multiple`.
Always a bug; surfaces as 500.

### `engine/lookups.py` (122 lines)

Entity lookup helpers.

- `lookup_singleton(plugin, repo, dossier_id, entity_type)` —
  the only sanctioned engine-level way to fetch a singleton. Raises
  `CardinalityError` if the plugin declared the type as `multiple`.
  Direct `repo.get_singleton_entity` calls are tolerated only from
  the dossier-access path in `routes/access.py`.
- `resolve_from_trigger(repo, trigger_activity_id, dossier_id,
  entity_type)` — given an informing activity (a side effect's
  parent or a scheduled task's trigger), find a related entity.
  Two-pass: trigger's generated first, then used. Exactly-one-match
  returns it; multiple distinct entity_ids → None (caller raises).
- `resolve_from_prefetched(repo, dossier_id, trigger_generated,
  trigger_used, entity_type)` — same logic but with the trigger's
  lists already fetched. Use when resolving multiple types from
  the same trigger to avoid 2N queries.

### `engine/refs.py` (175 lines)

`EntityRef` — the canonical entity-reference type. Parsed, frozen
(hashable, usable as dict keys and in sets). Canonical string form
is `prefix:type/entity_id@version_id`
(e.g. `oe:aanvraag/e1…@f1…`). The regex `ENTITY_REF_PATTERN`
accepts RDF/XML QName conventions: prefix and local name start with
a letter, followed by letters/digits/underscores/hyphens. Rejects
leading digits, bare colons, missing colons, and multiple colons.

`EntityRef.parse(ref)` accepts both shorthand and full platform
IRIs (`{DOSSIER_BASE}{did}/entities/{prefix:type}/{eid}/{vid}`),
returning `None` for external URIs or anything that doesn't match
either form. The full-IRI parsing is isolated in
`_parse_full_entity_iri`, which imports `DOSSIER_BASE` lazily from
`prov.iris` to avoid circular imports.

`is_external_uri(ref)` is the boolean classification — true iff
`EntityRef.parse(ref) is None`.

This module is the single source of truth for entity-ref parsing
and construction — callers must use `str(ref)` / `EntityRef.parse`
rather than `f"{type}/{eid}@{vid}"` string concatenation.

### `engine/response.py` (63 lines)

`build_replay_response(plugin, repo, dossier_id, activity_row,
user)` — builds the response for an idempotent PUT replay when the
`activity_id` already exists. Returns a subset of the full-execution
response (activity identity + dossier state + allowed activities);
`used` and `generated` come back empty because replay doesn't re-execute
them. Called from `pipeline/preconditions.py::check_idempotency`.

### `engine/scheduling.py` (404 lines)

Two resolvers live here — `scheduled_for` for tasks, and
`not_before`/`not_after` for workflow rules. They share most of
the grammar (signed relative offsets, ISO 8601, entity-field dict)
and most of the parsing helpers (`_parse_offset`, `_parse_iso`,
`_read_datetime_from_entity`).

`resolve_scheduled_for(value, now, resolved_entities)` — parses a
task's `scheduled_for` declaration. Four accepted forms:

- **Signed relative offset** — `+20d` / `-7d` / `+2h` / `+45m` / `+3w`.
  Resolved against `now`. Sign is mandatory (bare `20d` would be
  ambiguous with entity-field paths). Negative offsets resolve to
  the past; the worker picks up past-dated tasks on its next poll.
- **Absolute ISO 8601** — `2026-05-01T12:00:00Z` or with an explicit
  offset. Naive datetimes are normalized to UTC; the original string
  is preserved verbatim when it was already timezone-aware.
- **Entity field reference** — a dict `{from_entity, field}` that
  reads an ISO datetime (or date-only) string from an entity in
  `state.resolved_entities`. Same `from_entity`/`field` idiom
  authorization and finalization use. The value can be an ISO
  string, a date-only string (→ midnight UTC), or a Python
  `datetime` already (for handler-built tasks).
- **Entity field + offset** — the dict form plus an `offset` key
  containing a signed relative offset. The reminder idiom:
  `{from_entity: oe:aanvraag, field: expires_at, offset: "-7d"}`
  resolves to 7 days before the permit expiry.

The entity form fails loud with `ValueError` when the type isn't
in `resolved_entities` (activity didn't declare it in its
used/generated block), when the field is missing or null, or when
the value isn't a parseable datetime. `_schedule_recorded_task`
wraps these as 500 `ActivityError` at activity execution so YAML
authors get a clear error location.

`resolve_deadline(value, plugin, repo, dossier_id, *, rule_name)` —
parses `requirements.not_before` / `forbidden.not_after`. Three
accepted forms: absolute ISO, `{from_entity, field}`, and
`{from_entity, field, offset}`. The `"+Nd"`-from-now string form
is explicitly rejected — "relative to now" has no fixed meaning at
rule-evaluation time, and the error surfaces it rather than
silently sliding the deadline.

Unlike `resolve_scheduled_for`, this resolver does a DB lookup: it
hits `lookup_singleton(plugin, repo, dossier_id, type)` to fetch
the referenced entity, enforcing the singletons-only rule (also
enforced at plugin load by `validate_deadline_rules`). When the
singleton isn't in the dossier yet, the resolver returns `None`
and `validate_workflow_rules` treats the rule as inactive —
letting plugins compose deadlines with `requirements.entities`.

Complex scheduling logic that doesn't fit either DSL — multiple
entities, business-day math — belongs in a handler; the handler
returns a pre-formatted ISO string via `HandlerResult.tasks[...].scheduled_for`.

### `engine/state.py` (275 lines)

`ActivityState` — the mutable state object threaded through every
pipeline phase. Deliberately mutable (not pure-functional): a pure
pipeline would force each phase to return a new state object,
dominating the orchestrator with threading boilerplate and
obscuring the 25-line table of contents. Discipline: each phase
function's docstring declares which fields it reads and writes in
`Reads:` / `Writes:` sections.

Also defines three typed dataclasses that replace prior
`list[dict]` / `dict[str, ...]` shapes (IDE autocomplete, typo
catching at construction):

- `UsedRef` — a resolved used reference (`entity`, `version_id`,
  `type`, `external`, `auto_resolved`).
- `ValidatedRelation` — a process-control relation staged for
  persistence (`version_id`, `relation_type`, `ref`).
- `DomainRelationEntry` — a domain relation staged for persistence
  or removal (`relation_type`, `from_ref`, `to_ref`; always full
  IRIs after `expand_ref`).

Plus the `Caller` enum (`CLIENT` / `SYSTEM`, inherits from `str` so
legacy string-based call sites still compare equal).

`ActivityState` fields are grouped as inputs (set by the
orchestrator from request parameters) and phase outputs (each one
documented with which phase produces it).

---

## `engine/pipeline/` — per-phase implementations

```
pipeline/
├── __init__.py        — empty (marker)
├── authorization.py   — authorize_activity + validate_workflow_rules + _resolve_field
├── finalization.py    — run_pre_commit_hooks, determine_status, finalize_dossier,
│                        build_full_response
├── generated.py       — process_generated + derivation/versioning/content validators
├── handlers.py        — run_handler + _append_handler_generated
├── persistence.py     — create_activity_row + persist_outputs
├── preconditions.py   — check_idempotency, ensure_dossier, authorize, resolve_role,
│                        check_workflow_rules
├── split_hooks.py     — run_split_hooks (status_resolver + task_builders)
├── tasks.py           — process_tasks + cancel_matching_tasks + supersession
├── tombstone.py       — validate_tombstone (shape rules for the built-in activity)
├── used.py            — resolve_used (explicit + system-caller auto-resolve)
├── validators.py      — run_custom_validators (YAML-declared callable dispatch)
├── _helpers/          — cross-phase helpers (eligibility, identity, invariants, status)
├── relations/         — relation processing (process-control + domain, add + remove)
└── side_effects/      — recursive side-effect execution
```

### `engine/pipeline/__init__.py` (0 lines)

Empty — marker file.

### `engine/pipeline/authorization.py` (295 lines)

Two reusable functions. Both return `(ok: bool, error_message: str | None)`
so callers can raise or skip.

- `authorize_activity(plugin, activity_def, user, repo, dossier_id)`
  walks the activity's `authorization` block. `access: everyone`
  passes everyone, `access: authenticated` requires a user,
  `access: roles` tries each entry. Role entries come in three
  shapes:
  1. Direct match — `{role: "behandelaar"}`, user must have that
     string in `user.roles`.
  2. Scoped match — `{role: "gemeente-toevoeger", scope:
     {from_entity: "oe:aanvraag", field: "content.gemeente"}}`.
     The role is composed at runtime from base + value resolved
     from an entity field (`gemeente-toevoeger:brugge`).
  3. Entity-derived match — `{from_entity: "oe:aanvraag", field:
     "content.aanvrager.rrn"}`. The field value IS the role string.
     Used for owner-match checks.

- `validate_workflow_rules(activity_def, repo, dossier_id,
  known_status, known_activity_types, plugin, now)` checks
  structural preconditions from `requirements` + `forbidden`:
  required activities completed, required entity types exist,
  current status in required / not in forbidden, no forbidden
  activity already completed. Also evaluates **time-based rules**:
  `requirements.not_before` (earliest legal moment) and
  `forbidden.not_after` (deadline). Both delegate to
  `engine.scheduling.resolve_deadline` and accept the same three
  shapes (absolute ISO, entity-field dict, entity-field + offset).
  Deadline checks are skipped when `plugin` isn't supplied, which
  lets narrow unit tests of the non-deadline branches omit it.
  `now` defaults to the current UTC time; preconditions passes
  `state.now` for consistency with other time-sensitive phases.
  Accepts pre-fetched status and type set to avoid redundant
  queries inside the eligibility loop (which evaluates many
  activities against the same dossier state).

Also exports `_resolve_field(content, field_path)` — dot-notation
path resolver that strips a leading `content.` segment if present
(since callers already have the content dict in hand).

### `engine/pipeline/finalization.py` (232 lines)

Post-execution phases.

- `run_pre_commit_hooks(state)` — walks `plugin.pre_commit_hooks`
  in declaration order. Unlike `post_activity_hook` (whose
  exceptions are logged and swallowed), these raise to roll the
  whole activity back. For validation / mandatory side effects that
  MUST succeed or the activity is invalid (PKI signature checks,
  external ID reservations, mandatory file service operations).
  First raise wins.
- `determine_status(state)` — resolves the status contribution in
  three tries: literal string from YAML, handler override from
  `HandlerResult.status` (only if YAML was None), or entity-mapping
  rule (`{from_entity, field, mapping}` — reads the field on a
  generated entity and looks up the mapping). Writes
  `activity_row.computed_status` and mirrors to
  `state.final_status`.
- `finalize_dossier(state)` — bulk path (`state.skip_cache`)
  shortcuts to whatever the current activity stamped. Full path
  derives current status, fires `post_activity_hook` with try/except
  (its failure doesn't invalidate the activity; logged with
  `exc_info=True` so Sentry sees the full context), recomputes
  eligible activities, writes `cached_status` + `eligible_activities`
  onto the dossier row, and filters the eligible list by user auth
  for the response.
- `build_full_response(state)` — assembles the response dict
  (activity / used / generated / relations / dossier). Pure
  function; no state mutation.

### `engine/pipeline/generated.py` (374 lines)

`process_generated(state)` — validates and normalizes every entry
in the activity's `generated` block. External URIs short-circuit
to `state.generated_externals` for the persistence phase; local
entities go through a five-step validation:

1. Type gate — must be in the activity's `generates` list.
2. `_validate_derivation` — cross-check the declared `derivedFrom`
   against the dossier's actual lineage for this logical entity.
   If a prior version exists, `derivedFrom` is mandatory and must
   point at the current latest version (stale derivations are
   rejected with 422 `invalid_derivation_chain`; missing with 422
   `missing_derivation_chain`). Cross-entity derivation is always
   a 422 `cross_entity_derivation`. Error payloads include the
   current `latest_version` block so clients can rebase.
3. `_resolve_schema_version` — reads the activity's
   `entities[type]` declaration. Fresh entities need `new_version`
   (500 if declared versioning is missing it). Revisions inherit
   the parent's sticky version; if `allowed_versions` is declared
   and the parent's version isn't in it, 422
   `unsupported_schema_version`.
4. `_validate_content` — runs the content through the Pydantic
   model resolved via `plugin.resolve_schema(type, schema_version)`.
   Missing model = validation skipped (plugin opted out of typed
   validation for this type).
5. Pending-entity registration — constructs a `_PendingEntity` with
   all EntityRow-equivalent fields populated (Bug 20) and stashes
   it in `state.resolved_entities` so handlers can read it via
   `context.get_typed`.

Also exports `_parse_derived_from_version` and
`_latest_version_payload` (used by the payload builders).

### `engine/pipeline/handlers.py` (151 lines)

`run_handler(state)` — invoke the activity's handler if one is
registered. No-op for handler-less activities. Builds an
`ActivityContext` where `user == triggering_user == state.user`
(direct handlers run in the request pipeline, executor and trigger
are the same person). Passes `client_content` — the content dict
of the first generated item, if any — as the handler's second
argument (the legacy shape where the client's entire intent was
one content dict; modern handlers ignore this argument when they
don't care).

`_append_handler_generated(state, items)` — normalizes each
handler-returned entry. Externals (`type == "external"` with a
`uri` in content) route to `state.generated_externals`. Everything
else goes through `resolve_handler_generated_identity` from the
shared helper in `_helpers/identity.py`, then is appended to
`state.generated` with a fresh `version_id`.

Handler-generated entities only land in `state.generated` if the
client didn't supply any — handler output and client output are
mutually exclusive.

### `engine/pipeline/persistence.py` (215 lines)

Two phases; the split exists because the handler runs between them
(it needs `state.activity_row` to exist to stamp
`wasGeneratedBy`, but can also append to `state.generated`, so
persistence must wait).

- `create_activity_row(state)` — `ensure_agent` for the calling
  user, then `create_activity` + `create_association`
  (`wasAssociatedWith` edge carrying the functional role).
- `persist_outputs(state)` — the bulk write phase. In order:
  1. Local generated entities (`create_entity` with
     `wasGeneratedBy = activity_id`; parent linkage is via
     `derived_from`, not a `used` row).
  2. External entities — deterministic UUIDv5 entity_id from
     `{dossier_id}:{uri}` so the same external referenced multiple
     times in the dossier collapses to one logical entity.
  3. Tombstone redactions (if this is the built-in tombstone
     activity and `state.tombstone_version_ids` was populated by
     the tombstone phase). Runs after step 1 so the replacement
     exists before the originals are nulled.
  4. `used` link rows from `state.used_refs`.
  5. Process-control relation rows from
     `state.validated_relations`.
  6. Domain-relation add rows from
     `state.validated_domain_relations`.
  7. Domain-relation supersedes from
     `state.validated_remove_relations`.

  Also builds `state.generated_response` — the response-manifest
  list of `{entity, type, content, [schemaVersion]}` dicts,
  rebuilt from scratch each call.

### `engine/pipeline/preconditions.py` (173 lines)

The pre-execution phases — the steps that decide whether the request
can proceed at all.

- `check_idempotency(state)` — if `state.activity_id` already exists,
  return a replay response via `build_replay_response`. Rejects as
  409 if the existing row is for a different dossier or different
  type (the client reused an id by mistake). Type comparison goes
  through `prov.activity_names.local_name` to tolerate legacy rows
  stored with bare names being replayed after qualification.
- `ensure_dossier(state)` — `SELECT ... FOR UPDATE` via
  `repo.get_dossier_for_update`. The row-level lock is the
  optimistic-concurrency replacement: activities against the same
  dossier serialize; other dossiers stay fully parallel. If the
  dossier doesn't exist, 404 unless the activity has
  `can_create_dossier: true`; then `workflow_name` must be supplied
  (400 otherwise) and the dossier is created here.
- `authorize(state)` — delegates to `authorize_activity`; raises
  403 on failure.
- `resolve_role(state)` — defaults the functional role from
  `default_role` or the first `allowed_role`, or `"participant"`;
  rejects 422 if the supplied role isn't in `allowed_roles`.
- `check_workflow_rules(state)` — runs `validate_workflow_rules`,
  skipped on the very first activity of a brand-new dossier (no
  prior activities to satisfy `requirements.activities`). 409 on
  violation.

### `engine/pipeline/split_hooks.py` (117 lines)

`run_split_hooks(state)` — invokes the opt-in `status_resolver` and
`task_builders` declared on the activity. No-op if neither is
declared. If either is declared, materializes an empty
`HandlerResult` when the handler didn't run or didn't return one
(activities that compute only status + tasks without producing
content).

Mutual-exclusion rules, enforced with `ActivityError(500)`:

- `status_resolver` declared → handler.status must be None.
- `task_builders` declared → handler.tasks must be None/empty.

"Who decides X" is unambiguous: exactly one source per concern per
activity. Missing-registration also raises 500. Legacy handlers
that return `content + status + tasks` continue to work — they
just don't declare the split fields.

### `engine/pipeline/tasks.py` (281 lines)

Task scheduling and cancellation after persistence + side effects.
Tasks come from two sources (YAML `activity_def.tasks` and
handler-appended `HandlerResult.tasks`) and fall into four kinds:

- `fire_and_forget` — invoke `task_handler` inline, log-and-swallow
  exceptions (logged at WARNING with `exc_info=True` so Sentry
  picks them up as breadcrumbs).
- `recorded`, `scheduled_activity`, `cross_dossier_activity` — all
  go through `_schedule_recorded_task`: supersede, persist a
  `system:task` entity.

Cross-cutting machinery:

- **Supersession** — `_supersede_matching` rewrites existing
  scheduled tasks with the same `target_activity` in the same
  dossier as `status: superseded`. Only one scheduled instance of
  a given target per dossier is ever on the worker's queue at a
  time. Skipped when `allow_multiple: true`.
- **Scheduled-for resolution** — delegates to
  `engine.scheduling.resolve_scheduled_for` for signed-offset / ISO
  8601 / entity field-reference parsing. `state.resolved_entities`
  is passed through so the dict form (`{from_entity, field}`) can
  read datetime fields from entities the activity used or
  generated. A malformed value raises 500 at activity execution so
  YAML typos fail loudly.

`cancel_matching_tasks(state)` runs after the new tasks are written.
Walks existing scheduled `system:task` entities and cancels those
whose `cancel_if_activities` includes the current activity
(comparison uses `prov.activity_names.local_name` so bare names
from handlers match qualified names on activity definitions).
Tasks created at-or-after this activity's start time are skipped
(don't cancel what this activity just scheduled). `allow_multiple`
does not affect cancellation — a task being allowed to coexist
with others of its type doesn't change whether the event it's
waiting on has fired.

### `engine/pipeline/tombstone.py` (145 lines)

`validate_tombstone(state)` — shape validation for the built-in
`tombstone` activity. No-op for every other activity. Matches the
activity name by local part (`prov.activity_names.local_name`) so
legacy bare-named rows also resolve.

Shape rules (each violation is a 422 with a structured payload):

1. Non-empty `used` with at least one real entity row.
2. Single logical target — every used row must share the same
   `entity_id` AND the same `type`.
3. Exactly one replacement in `generated` matching the target
   `(type, entity_id)`.
4. At least one `system:note` in `generated` carrying the
   redaction reason.
5. No surprise extras — any other generated entity is rejected.

Re-tombstoning is intentionally allowed; a second tombstone over
an already-tombstoned entity nulls the rows again (no-op for
already-NULL content) and overwrites `tombstoned_by` with the new
activity's id.

Populates `state.tombstone_version_ids` — the persistence phase
reads this list and calls `tombstone_entity_versions` after the
replacement has been written.

### `engine/pipeline/used.py` (200 lines)

`resolve_used(state)` — turns raw refs into `EntityRow` objects.
Two passes:

- `_resolve_explicit` runs for every client-supplied ref. External
  URIs persist via `ensure_external_entity` and record with
  `external: True`. Local refs parse via `EntityRef.parse`, look
  up by version_id, and are rejected with 422 on invalid ref,
  missing entity, or cross-dossier reference.
- `_auto_resolve_for_system_caller` runs only when `caller ==
  Caller.SYSTEM`. Prefetches the informing activity's generated +
  used lists once (if any auto-resolve slot needs it), then for
  each `used_def` with `auto_resolve: latest`:
  1. Trigger scope — `resolve_from_prefetched`.
  2. Singleton fallback — `lookup_singleton` for singleton types.

Resolved entries are appended to `state.used_refs` with
`auto_resolved: True`. Multi-cardinality types that neither of
the two strategies can resolve fail silently here — downstream
phases that need them will raise.

### `engine/pipeline/validators.py` (64 lines)

`run_custom_validators(state)` — invokes every validator the
activity declares in its YAML `validators` block. Each entry is
`{name: <dotted path>, description: ...}`. Validators receive the
same `ActivityContext` handlers see, with the same two-user split
(executor = trigger = request-maker for direct validators). A
validator rejects either by raising `ActivityError` (full control
over status code and payload) or by returning a falsy value (the
engine wraps it in a generic 409 with the validator's name).

### `engine/pipeline/_helpers/__init__.py` (17 lines)

Docstring explaining the grouping. Cross-phase helpers that aren't
phases themselves:

- `eligibility.py` — `compute_eligible_activities`,
  `filter_by_user_auth`, `derive_allowed_activities`.
- `status.py` — `derive_status` (shared by authorization,
  eligibility, finalization, response).
- `invariants.py` — `enforce_used_generated_disjoint` (called once
  by the orchestrator but lives here because it's a cross-block
  rule, not a single-phase one).
- `identity.py` — `resolve_handler_generated_identity` (shared by
  the handler phase and the side-effect persistence helper).

### `engine/pipeline/_helpers/eligibility.py` (152 lines)

Two layers of eligibility.

- `compute_eligible_activities(plugin, repo, dossier_id,
  known_status=None)` — loops over every `client_callable` activity
  in the workflow, runs `validate_workflow_rules` against current
  dossier state, returns names. Result depends only on dossier
  state (not on user), so it's safe to cache on the dossier row.
  Passes `plugin` through so deadline rules (`not_before` /
  `not_after`) are evaluated — activities past their `not_after`
  or before their `not_before` drop out of the eligible list.
- `filter_by_user_auth(plugin, eligible, user, repo, dossier_id)` —
  cheap per-request filter. Returns
  `[{type, label, not_before?, not_after?}, ...]`. The optional
  deadline fields are ISO strings, present only when the activity
  declares the corresponding rule AND it resolves successfully
  (singleton missing → field absent). Frontends use them for
  "expires in 3 days" countdowns and disabled-but-visible hints.
  Resolves each declared rule once via `resolve_deadline`, so
  dict-form rules hit `lookup_singleton` once per activity — minor
  cost given the typical activity count and singleton cache hits.
- `derive_allowed_activities(plugin, repo, dossier_id, user)` —
  convenience wrapper that combines both. Used when no cache is
  available (replay responses, first-time response shaping).

**Cache staleness.** The `eligible_activities` cache on the dossier
row is invalidated on every activity execution, not on wall-clock
passage. An activity whose `not_after` ticks over while the
dossier is dormant stays in the cached list until the next activity
runs. Acceptable because the execution path always runs
`validate_workflow_rules` fresh — clicking a stale-listed expired
activity returns 422, never runs the activity. Cache is a display
optimisation; correctness is never stale.

### `engine/pipeline/_helpers/identity.py` (110 lines)

`ResolvedIdentity` named tuple (`gen_type`, `entity_id`,
`derived_from_id`) and
`resolve_handler_generated_identity(*, plugin, repo, dossier_id,
gen_item, allowed_types)`. Shared by the main pipeline's
handler-generated appender and the side-effect persistence helper
because both follow the same rules:

1. Type defaulting from `allowed_types[0]` if handler omitted it.
2. Empty content or unresolvable type → return None.
3. Explicit `entity_id` override → use verbatim.
4. Singleton type → revise existing or mint fresh.
5. Multi-cardinality → always mint fresh, no derivation.

Does NOT handle external URIs (callers short-circuit externals
before invoking this) and does NOT resolve `schema_version` (that's
caller-specific because the activity_def differs between the main
pipeline and side effects). Formerly `_identity.py` at the pipeline
root; underscore dropped because the `_helpers/` name already
signals privacy.

### `engine/pipeline/_helpers/invariants.py` (116 lines)

`enforce_used_generated_disjoint(state)` — structural check that a
logical entity is never in both `used` and `generated` for the same
activity. Revising IS using; the PROV graph encodes the parent-child
link via `wasDerivedFrom`, so listing the parent in `used` would
create a duplicate edge.

Built-in activities (`built_in: true`, i.e. the tombstone activity)
are exempt — they operate on multiple historical versions of the
same logical entity by design. The tombstone shape validator
handles its own cases.

Check runs between `resolve_used` and `process_generated` so it
catches overlap before the derivation-validation pass. Collision
reporting distinguishes local (by `entity_id`) from external (by
URI). 422 with `error: used_generated_overlap`.

### `engine/pipeline/_helpers/status.py` (35 lines)

`derive_status(repo, dossier_id)` — walks the activity history
newest-first and returns the first non-null `computed_status`.
`"nieuw"` if there are no activities. Status is not stored as a
single column on the dossier; it's derived from the activity log,
so rolling back an activity rolls back the status implicitly. The
cheapest read in the engine — one query + in-memory walk — and is
called from many places (authorization pre-check, eligibility,
finalization, replay responses).

### `engine/pipeline/relations/__init__.py` (44 lines)

Module docstring explaining the relation-processing contract:
process-control vs domain, the permission gate (workflow +
activity `relations:` blocks), the operations gate
(`operations: [add, remove]`), and the activity-level opt-in for
validator firing. Re-exports `process_relations` and
`_validate_ref_types`.

### `engine/pipeline/relations/declarations.py` (171 lines)

Pure read-only helpers over the workflow dict, plus the ref-type
gate.

- `_relation_declarations(activity_def)` — parses the activity's
  `relations:` block into `type → declaration dict`.
- `allowed_relation_types_for_activity(plugin, activity_def)` —
  permission gate; union of workflow-level and activity-level
  declared types.
- `_allowed_operations(activity_def, rel_type)` — the
  `operations: [add, remove]` constraint; defaults to `{"add"}`.
- `_relation_kind(plugin, activity_def, rel_type)` — resolves
  `kind` from the workflow-level declaration (single source of
  truth post-Bug-78; activity-level `kind:` is forbidden at load
  time). Raises `KeyError` if the type isn't declared (defensive —
  the load-time validator and the permission gate should catch
  this before dispatch reaches here).
- `_relation_type_declaration(plugin, activity_def, rel_type)` —
  looks up the declaration dict, activity level first then
  workflow level.
- `_validate_ref_types(rel_type, from_ref, to_ref, declaration)` —
  validates `from_ref`/`to_ref` against the workflow-level
  `from_types`/`to_types` using `prov.iris.classify_ref` on the
  original (pre-expansion) refs. Skipped if constraints not
  declared.

### `engine/pipeline/relations/dispatch.py` (240 lines)

Per-kind staging + validator firing.

- `_handle_domain_add(state, rel_item, rel_type, from_ref)` —
  validates `from_types`/`to_types`, expands shorthand refs to
  full IRIs via `prov.iris.expand_ref`, stages into
  `state.validated_domain_relations` and `state.relations_by_type`.
- `_handle_process_control(state, rel_item, rel_type)` — parses
  the `entity` ref (process-control can't reference external
  URIs), looks up the entity, stages into
  `state.validated_relations` and `state.relations_by_type`.
- `_resolve_validator(plugin, activity_def, rel_type, operation)` —
  finds the registered validator in two styles:
  1. Per-operation dict: `validators: {add: "...", remove: "..."}`
     (domain relations only; load-time validator forbids this form
     on process-control relations).
  2. Single-string: `validator: "..."` (fires for all operations).

  Bug 78 removed the Style 3 plugin-level-by-type-name fallback.
  Load-time validation rejects plugins whose `relation_validators`
  dict uses a declared relation-type name as a key, preventing
  Style 3 from being silently re-created by convention.
- `_dispatch_validators(state, allowed)` — for each
  activity-level opt-in type, invokes the registered add-validator
  (fires even on empty entries — the validator might enforce "at
  least one required") and the remove-validator (only if there are
  remove entries). An activity-level type not in the allowed set
  is a 500 — structural misconfig.

### `engine/pipeline/relations/process.py` (209 lines)

The pipeline-phase entry point.

`process_relations(state)` drives two parsing passes plus
`_dispatch_validators`.

- `_parse_relations(state, allowed)` — walks `state.relation_items`.
  For each: checks the permission gate, checks `"add"` is in the
  allowed operations, resolves `kind` from the workflow-level
  declaration, validates request-item shape (`entity:` for
  process_control; `from:` + `to:` for domain) against the
  declared kind (Bug 78 — dispatch is driven by declared kind, not
  guessed from request shape), dispatches to the right handler.
- `_parse_remove_relations(state, allowed)` — walks
  `state.remove_relation_items`. Validates type + operation
  permission, kind must be `domain` (defense-in-depth even though
  load-time validation already forbids `remove` on process_control),
  validates `from_types`/`to_types`, expands shorthand to full
  IRIs, stages into `state.validated_remove_relations`.

### `engine/pipeline/side_effects/__init__.py` (56 lines)

Module docstring laying out what side effects are (pared-down
pipeline without client blocks, validators, tombstone check,
status-from-content, tasks, finalization) and what they DO have
(conditions, auto-resolved used, schema versioning, recursive
chains up to depth 10). Re-exports `execute_side_effects` and the
three `_` helpers.

### `engine/pipeline/side_effects/execute.py` (233 lines)

`execute_side_effects(*, plugin, repo, dossier_id,
trigger_activity_id, side_effects, triggering_user, depth=0,
max_depth=10)` — the recursive entry point. Early-returns on empty
list or depth cap. Prefetches the trigger's generated + used
entity lists ONCE per call so auto-resolution of N entity types
doesn't issue 2N queries.

`_execute_one_side_effect` handles a single entry:

1. Check the condition gate via `_condition_met` (dict form or
   `condition_fn` predicate).
2. Find the activity def + handler; bail if either is missing.
3. Create the side-effect activity row + system association
   (`informed_by = trigger_activity_id`).
4. `_auto_resolve_used` from the trigger's scope (+ singleton
   fallback).
5. Build the `ActivityContext` with `user = SYSTEM_USER` and
   `triggering_user = triggering_user` (the original request-maker,
   pass-through unchanged during recursion).
6. Invoke the handler; stamp `computed_status` if returned.
7. `_persist_se_generated` for any handler-returned entities.
8. Recurse into nested side effects if declared, after a
   `session.flush()` so nested effects can see what this one
   just created.

Errors are NOT swallowed here (unlike `fire_and_forget` tasks) —
side effects are part of the activity's transaction and a raise
rolls the whole activity back.

### `engine/pipeline/side_effects/helpers.py` (224 lines)

Three helpers pulled out of `execute.py` for clarity.

- `_condition_met(...)` — evaluates the gate. Function form
  (`condition_fn`) takes precedence if both are somehow set;
  builds an `ActivityContext` matching what handlers see and
  calls the predicate. Unregistered predicate is logged as ERROR
  and returns False (fail-closed; load-time validation should
  prevent this, but raising here would abort the parent activity
  for a downstream mistake). Dict form (`condition: {entity_type,
  field, value}`) looks up the entity in trigger scope, falls back
  to singleton if the type is singleton, resolves the field, tests
  equality.
- `_auto_resolve_used(...)` — for each `used:` declaration on the
  side-effect activity with `auto_resolve: latest`, look in
  trigger generated first, then trigger used, then singleton
  fallback. Multi-cardinality types only resolve from trigger
  scope — never "latest of type" from the whole dossier (would
  be ambiguous). Writes a `used` link row for each resolved entity.
- `_persist_se_generated(...)` — resolves identity via
  `resolve_handler_generated_identity`, resolves schema version
  via `_resolve_schema_version` from `pipeline/generated.py`
  against the side-effect activity's declarations, persists with
  `attributed_to="system"`.

---

## `observability/` — audit logging + Sentry

```
observability/
├── __init__.py    — docstring only; no re-exports
├── audit.py       — NDJSON audit log (Wazuh-friendly)
└── sentry.py      — Sentry init + fingerprinted capture helpers
```

### `observability/__init__.py` (13 lines)

Docstring explaining the grouping (audit + Sentry, both cross-cutting
operator-facing tools). No re-exports; callers import the submodule
they need.

### `observability/audit.py` (298 lines)

Append-only audit log emission, distinct from the PROV graph
(successful state transitions) and Sentry (exceptions). Exists for
compliance questions: "who looked up applicant X's dossier?", "who
exported data between Y and Z?"

Design:

- NDJSON (one JSON object per line, `\n`-terminated). A Wazuh agent
  on the same host tails the file and forwards to the SIEM. The app
  never talks to Wazuh over the network — writes a file, Wazuh reads
  it.
- `configure_audit_logging(path, max_bytes=100MB, backup_count=10)`
  wires `RotatingFileHandler` with an `_NDJSONFormatter`. Safe to
  call multiple times; later calls are no-ops. Directory must
  already exist (we don't auto-create `/var/log/...`). Returns
  `False` if unwritable; in that case `emit_audit()` becomes a
  silent no-op — the audit sink must never fail the caller.
- The `dossier.audit` logger has `propagate = False` so events
  don't leak to stderr / Sentry / root (wrong retention, wrong trust
  boundary).

`emit_audit(*, action, actor_id, actor_name, target_type, target_id,
outcome, dossier_id=None, reason=None, **extra)` — emits one event.
Wire-level key is `event_action` (not `action`) because Wazuh
reserves 13 static-field names including `action`; remapping at the
producer avoids footguns at rule-writing sites.

`emit_dossier_audit(*, action, user, dossier_id, outcome, reason,
**extra)` — convenience wrapper for the ~90% case where the target
is the containing dossier. Used by every dossier-scoped route and
by the access-check path.

### `observability/sentry.py` (319 lines)

Sentry integration. Two entry points (one per process kind):

- `init_sentry_worker(dsn=None)` — used by the worker process.
- `init_sentry_fastapi(app, dsn=None)` — used by
  `dossier_engine.create_app`. Adds `FastApiIntegration` on top of
  `LoggingIntegration` so every unhandled request-handler exception
  is captured with full request context.

Both share `_init_sdk` which is idempotent (the `_initialized`
guard means the second call is a no-op) and is a silent no-op if
`sentry_sdk` isn't installed or `SENTRY_DSN` isn't set. Log records
ride along as **breadcrumbs only** (`LoggingIntegration(level=INFO,
event_level=None)`) — ordinary log records don't become Sentry
events on their own. Only the explicit capture helpers below (plus
FastAPI's built-in request-error capture) produce events, which
keeps fingerprinting discipline.

Three fingerprinted capture helpers for worker events:

- `capture_task_retry(*, exc, task_id, task_entity_id, dossier_id,
  function, attempt_count, max_attempts)` — WARNING level.
  Fingerprint `["worker.task.retry", <function>]` collapses all
  retries of the same function into one issue.
- `capture_task_dead_letter(...)` — ERROR level. Fingerprint
  `["worker.task.dead_letter", <function>, <task_entity_id>]` —
  each dead-lettered task gets its own issue (operators resolve
  them individually: investigate, fix, requeue).
- `capture_worker_loop_crash(exc)` — FATAL level. Fingerprint
  `["worker.loop.crash"]` — one issue for "the worker itself died."

`init_sentry = init_sentry_worker` back-compat alias.

---

## `plugin/` — plugin contract and load-time validation

```
plugin/
├── __init__.py       — re-exports the public surface
├── model.py          — Plugin dataclass + PluginRegistry + FieldValidator
├── normalize.py      — _normalize_plugin_activity_names (auto-qualify)
├── registries.py     — build_entity_registries_from_workflow +
│                       build_callable_registries_from_workflow + dotted-path helpers
└── validators.py     — five load-time validators
```

### `plugin/__init__.py` (59 lines)

Re-exports the public surface: `FieldValidator`, `Plugin`,
`PluginRegistry`, the registry-building functions, the five
load-time validators, and two private helpers that tests import
directly (`_import_dotted`, `_import_dotted_callable`,
`_normalize_plugin_activity_names`). Module docstring points at
`docs/plugin_guidebook.md` and `dossiertype_template.md` for plugin
authoring.

### `plugin/model.py` (287 lines)

The `Plugin` dataclass — carries all of a plugin's concrete
configuration. Big list of fields:

- `name`, `workflow`, `entity_models`, `entity_schemas` (versioned:
  `(type, version) → Pydantic`).
- Eight callable registries — `handlers`, `validators`,
  `task_handlers`, `status_resolvers`, `task_builders`,
  `side_effect_conditions`, `relation_validators`, `field_validators`.
  All keyed by dotted Python paths (Obs 95 / Round 28) — typos fail
  at plugin load, not at first-lookup runtime. The exception is
  `field_validators`: keys there become URL segments
  (`POST /{workflow}/validate/{key}`) so they stay user-facing
  strings, not dotted paths.
- `post_activity_hook` — called AFTER each activity completes
  (inside the transaction), typically updates Elasticsearch
  indices. Exceptions are swallowed by `finalize_dossier` (logged
  with traceback).
- `pre_commit_hooks` (list) — called after persistence but BEFORE
  the cached_status projection and commit. Exceptions RAISE and
  roll the whole activity back.
- `search_route_factory` — called during route registration,
  registers workflow-specific search endpoints.
- `build_common_doc_for_dossier` — plugin-owned builder for the
  engine-level common-index document, invoked by
  `search.common_index.reindex_all`.
- `constants` — typed Pydantic BaseSettings instance from env +
  YAML + class defaults.
- `_ENGINE_CARDINALITIES` — defaults for engine-provided types
  (`system:task` multi, `system:note` multi, `oe:dossier_access`
  single, `external` multi). Overridable via workflow YAML.

Methods: `cardinality_of`, `is_singleton`,
`resolve_schema(entity_type, schema_version)` (routes to versioned
schema if set, falls back to `entity_models`),
`find_activity_def(activity_type)` (accepts bare or qualified form;
compares by local name).

`FieldValidator` — a small dataclass with `fn`, optional
`request_model` / `response_model` (for OpenAPI typing), optional
`summary` / `description`.

`PluginRegistry` — the per-app registry. `register(plugin)` calls
`_normalize_plugin_activity_names` first so all subsequent lookups
see qualified names. `get(workflow_name)`,
`get_for_activity(activity_type)` (accepts bare or qualified — the
registry stores qualified form),
`all_plugins`, `all_workflow_names`.

### `plugin/normalize.py` (98 lines)

`_normalize_plugin_activity_names(plugin)` — qualifies bare
activity names and cross-references in place.

Walks `activities[*].name` and every cross-reference site:
`requirements.activities`, `forbidden.activities`,
`side_effects[*].activity`, `tasks[*].cancel_if_activities`,
`tasks[*].target_activity`. Uses the namespace registry's default
workflow prefix if available, `"oe"` otherwise (the fallback is
correct for test fixtures that skip `create_app`).

Called from `PluginRegistry.register`, so every plugin-load path
goes through it. Idempotent — already-qualified names are left
alone.

### `plugin/registries.py` (377 lines)

Builds the nine Plugin registries (entity models + entity schemas
+ eight callable registries) from dotted paths in the workflow YAML.

Helpers:

- `_import_dotted(path)` — resolves `pkg.module.ClassName` to a
  Pydantic `BaseModel` subclass. Rejects non-BaseModels at load
  time so typed validation can't silently turn into schemaless
  write-through.
- `_import_dotted_callable(path, *, context="")` — parallel
  resolver without the BaseModel check. Used for the eight
  callable registries (handlers, validators, etc.). Takes an
  optional `context` string so errors carry call-site attribution
  ("activity 'dienAanvraagIn' handler" is more useful than just
  the path).

Public entry points:

- `build_entity_registries_from_workflow(workflow)` — walks
  `entity_types[*]`, reads `model:` (the default/legacy entry) and
  `schemas:` (versioned). Returns `(entity_models, entity_schemas)`.
  Types without `model` or `schemas` are structural-only
  (cardinality declaration only) and are silently skipped.
- `build_callable_registries_from_workflow(workflow)` — builds
  the eight callable registries. Two nested resolvers:
  `_resolve_validator_ref` (for activity-level `validators:` list
  entries) and `_resolve_relation_validator_ref` (for relation
  declarations, kept separate from activity-level validators as a
  structural defence against Bug 78's name collision). Walks:
  workflow-level `relation_types[*]` (both `validator` and
  `validators` forms), then every activity for `handler`,
  `status_resolver`, `task_builders`, `validators[*].name`,
  `tasks[*].function`, `side_effects[*].condition_fn`, and
  `relations[*].validator` / `relations[*].validators`. Finally the
  top-level `field_validators:` block (URL-key → dotted-path map).
  Returns a dict of the eight registries keyed by their final
  Plugin field names.

`_RELATION_VALIDATOR_DICT_KEYS = frozenset({"add", "remove"})` —
shared with the validators module; the keys accepted on a
per-relation-type dict validator declaration.

### `plugin/validators.py` (614 lines)

Six load-time validators that check the workflow contract before
the engine accepts any request. Kept as one module (per the Round
34 plan) because the validators are independent concerns but
cohesive.

- `validate_workflow_version_references(workflow, entity_schemas)` —
  cross-checks every activity's `entities[type].new_version` /
  `allowed_versions` string against the declared `entity_schemas`
  registry. Prevents the silent-runtime-fallback footgun where an
  activity declares `new_version: v3` but only `v1` and `v2` are
  registered.
- `validate_side_effect_condition_fn_registrations(workflow,
  side_effect_conditions)` — checks that every
  `side_effects[*].condition_fn` name resolves in the plugin's
  predicate registry. Runs after the Plugin constructor because it
  needs the registry.
- `validate_side_effect_conditions(workflow)` — shape-checks every
  `side_effects[*]` gating entry. Runs early (on the raw workflow
  dict). Enforces mutual exclusion between `condition:` and
  `condition_fn:`, validates the dict form has exactly
  `{entity_type, field, value}` keys (catches the common confusion
  with `from_entity`/`mapping` shapes used by status rules).
- `validate_relation_declarations(workflow)` — the comprehensive
  Bug 78 relation-contract validator. Enforces "types declared
  once at workflow level; activities reference by name only."
  Workflow-level: `type` required, `kind` required (domain or
  process_control), `from_types`/`to_types` only on domain,
  unknown keys rejected. Activity-level: `type` must resolve to
  a workflow-level declaration, `kind`/`from_types`/`to_types`/
  `description` forbidden, `validator` and `validators` mutually
  exclusive, `validators` dict must have exactly `{add, remove}`,
  `validators` dict form forbidden on process_control,
  `operations: [remove]` forbidden on process_control.
- `validate_relation_validator_registrations(plugin)` —
  cross-checks that the plugin's `relation_validators` dict doesn't
  use declared relation-type names as keys (which would re-create
  the Style 3 by-type-name fallback that Bug 78 removed). Runs
  after the Plugin constructor.
- `validate_deadline_rules(workflow)` — shape-checks every
  `requirements.not_before` / `forbidden.not_after` declaration in
  every activity. Rejects relative offsets (+Nd from now has no
  meaning for deadlines), wrong types, unknown dict keys
  (catches `offet:` typos), and — the main semantic rule — entity
  references to non-singleton types (multi-cardinality types have
  no unambiguous "which instance's deadline applies" answer). The
  runtime resolver in `engine.scheduling.resolve_deadline` also
  defends against non-singletons as defense-in-depth against test
  harnesses that bypass the validator.

Plus the four constants these validators key on:
`_SIDE_EFFECT_CONDITION_REQUIRED`, `_VALID_RELATION_KINDS`,
`_WORKFLOW_RELATION_KEYS`, `_ACTIVITY_RELATION_KEYS`,
`_ACTIVITY_RELATION_FORBIDDEN_KEYS`.

---

## `prov/` — PROV vocabulary

```
prov/
├── __init__.py         — docstring only; callers import submodules directly
├── activity_names.py   — qualify / local_name / match_activity_def
├── iris.py             — IRI generation, expand_ref, classify_ref
├── json_ld.py          — PROV-JSON document builder
└── namespaces.py       — NamespaceRegistry singleton
```

### `prov/__init__.py` (24 lines)

Docstring listing the four submodules and their role. No
re-exports — callers import the submodule they need
(`from dossier_engine.prov.iris import ...`). Keeps the import graph
legible.

### `prov/activity_names.py` (93 lines)

Qualification helpers for activity names.

- `qualify(name, default_prefix=None)` — returns the qualified form.
  If `name` already has a colon, returns unchanged; otherwise
  prepends the namespace registry's `default_workflow_prefix` or
  the explicit argument (tests use the explicit form).
- `local_name(name)` — returns the local part (`oe:foo → foo`).
  Used to build URL path segments; URLs use local names only
  because colons in URL path segments cause trouble with some HTTP
  middleware.
- `match_activity_def(url_name, activity_defs)` — find the activity
  definition whose local name matches `url_name`. If multiple
  match (which indicates a workflow-level bug), returns the first
  by YAML order.

Equivalent activities declared in different forms resolve to the
same identity: `qualify("dienAanvraagIn") == qualify("oe:dienAanvraagIn")`
(assuming `oe` is the default prefix).

### `prov/iris.py` (344 lines)

IRI generation for PROV-JSON compliance. Centralises the W3C
PROV-compliant QName and full-IRI construction. The database
internal format (`type/entity_id@version_id`) is unchanged; this
module only translates at the PROV rendering boundary.

Constants: `DOSSIER_BASE` (per-dossier template, default
`https://id.erfgoed.net/dossiers/{dossier_id}/`) and `OE_NS`
(ontology namespace, default `https://id.erfgoed.net/vocab/ontology#`).
Both overridable at app startup via `configure_iri_base(dossier_prefix,
ontology_ns)` — called by `create_app` from `config.iri_base`.

Functions:

- `prov_prefixes(dossier_id)` — standard PROV-JSON prefix block.
  Pulls from the global namespace registry; always adds the
  per-dossier `dossier:` prefix.
- `entity_qname(entity_type, entity_id, version_id)` and
  `entity_full_iri(...)` — render an entity reference as
  `dossier:entities/oe:type/eid/vid` or the full expanded IRI.
- `activity_qname(activity_id)` / `activity_full_iri(...)`.
- `agent_qname(agent_id)`.
- `prov_type_value(entity_type)` and `agent_type_value(agent_type)` —
  `prov:type` dicts.
- `expand_ref(ref, dossier_id)` — expands a shorthand domain-
  relation ref to a full IRI. Already-full IRIs pass through
  unchanged. `dossier:` prefix handles both cross-dossier entities
  (`dossier:did/type/eid@vid`) and bare dossier refs (`dossier:did`).
- `classify_ref(ref)` — classifies a domain-relation endpoint as
  `"entity"`, `"dossier"`, or `"external_uri"`. Works on both
  expanded IRIs and shorthand. Used by `_validate_ref_types` to
  enforce `from_types`/`to_types` constraints.

Plus the private helpers `_strip_ns`, `_default_prefix`, `_type_ns`,
`_parse_full_entity_iri`, `_expand_entity_ref`.

### `prov/json_ld.py` (213 lines)

PROV-JSON document builder. `build_prov_graph(session, dossier_id)`
is the public entry — returns a PROV-JSON dict with the audit-view
shape (no per-user filtering; every activity / entity / association
included). Callers that want a filtered view filter the result
rather than pushing filtering into the builder — the concern of
"what to include" is separate from "how to serialise it."

Row-loading was moved to `db/graph_loader.py` in Round 30.5;
`build_prov_graph` calls it internally and adds the PROV-JSON
structure on top. `DossierGraphRows` and `load_dossier_graph_rows`
are re-exported here under their historical names for back-compat.

Two resolver closures bundled here because they're shared with the
graph renderers:

- `agent_key_resolver(agent_rows)` — canonical URI if available,
  else dossier-scoped `agent_qname`.
- `entity_key_resolver()` — external entities use their declared
  URI; local entities use the dossier-scoped version IRI.

### `prov/namespaces.py` (181 lines)

The prefix → IRI map for the running app. The engine supports
mixing multiple RDF vocabularies in one workflow (`oe:`, `foaf:`,
`dcterms:`, `prov:`); every prefix used anywhere must be declared
so the engine can expand to full IRIs, validate that no YAML uses
an undeclared prefix (typo protection at plugin load), and emit a
complete `prefixes` block in PROV-JSON.

`NamespaceRegistry` — mutable at startup, read-only thereafter.
Pre-populated with built-in `prov`, `xsd`, `rdf`, `rdfs` (you can't
override built-ins — `register` raises). `register(prefix, iri)`
rejects IRIs that don't end in `#` or `/` and rejects rebinding a
prefix to a different IRI. `default_workflow_prefix` is set by
`create_app` from `config.iri_base.ontology_prefix` (default `oe`).

Methods: `expand(qname)` (unqualified uses the default prefix;
unknown prefix returns the input unchanged — call `validate_type`
first if you care), `validate_type(qname)` (raises `ValueError`
with available prefixes on failure), `as_dict()`, `iri_for(prefix)`,
`__contains__`.

Module-level singleton: `_instance` + `namespaces()` accessor +
`set_namespaces(registry)` installer + `reset_namespaces()` for test
isolation.

---

## `routes/` — HTTP API surface

```
routes/
├── __init__.py        — register_routes orchestrator
├── access.py          — check_dossier_access, get_visibility_from_entry,
│                        check_audit_access
├── admin_search.py    — engine-level common-index admin endpoints
├── dossiers.py        — GET /dossiers, GET /dossiers/{id}
├── entities.py        — GET /dossiers/{id}/entities/... (three shapes)
├── files.py           — POST /files/upload/request (signed upload URL)
├── prov.py            — /prov, /prov/graph/timeline, /archive
├── prov_columns.py    — /prov/graph/columns (audit-level)
├── reference.py       — /{workflow}/reference/{list}, /{workflow}/validate/{name}
├── activities/        — activity execution endpoints (single, batch, typed)
├── _helpers/          — shared request/response models, serializers, error mapping
└── templates/         — Jinja2 templates for the two interactive graph views
```

### `routes/__init__.py` (103 lines)

The `register_routes(app, registry, get_user, global_access)`
orchestrator called by `app.create_app` at startup. Walks each
leaf module's `register` function in turn (`activities`,
`dossiers`, `entities`, `files`, `reference`), then loops over
every plugin's `search_route_factory` if set. Order doesn't matter
(no cross-dependencies between registrars) but is kept stable for
predictable OpenAPI ordering.

Also re-exports leaf-module symbols under their pre-Stage-6 names
(`_activity_error_to_http`, `ActivityRequest`, `FullResponse`, etc.,
and `check_dossier_access` / `get_visibility_from_entry`) so any
external code that still imports from `dossier_engine.routes`
keeps working. New callers should import from the leaf modules
directly.

### `routes/_helpers/__init__.py` (15 lines)

Docstring describing the five helpers. Grouped under `_helpers/`
in Round 34 to reduce crowding of the flat `routes/` directory.
Underscore prefix on individual file names dropped — the package
name already signals privacy.

### `routes/_helpers/activity_visibility.py` (158 lines)

`ActivityViewMode` dataclass (`base`, `include`, `explicit_types` —
all immutable frozensets) and two functions:

- `parse_activity_view(raw)` — normalises the raw `activity_view`
  value from an access entry. Accepts: `None` / `"all"` → show
  everything, `"own"`, `list[str]`, `{"mode": ..., "include": [...]}`.
  Everything else (including legacy `"related"` removed in Round 31,
  or unrecognized strings, or non-string/list/dict types) falls
  through to a deny-safe empty `ActivityViewMode`. Stale configs
  surface as empty timelines rather than silent semantic changes.
- `is_activity_visible(mode, *, activity_type, activity_id, user_id,
  visible_entity_ids, lookup_is_agent, lookup_used_entity_ids)` —
  evaluates visibility for a single activity. Include-list always
  wins; then base-mode dispatch. Unrecognized base → deny-safe
  False. The two `lookup_*` callables abstract over how agent /
  used-entity data is fetched: the dossier-detail endpoint uses DB
  queries, the PROV endpoints preload everything into dicts.

### `routes/_helpers/errors.py` (30 lines)

`activity_error_to_http(ActivityError)` — maps the engine's
structured exception to `HTTPException`. Merges `ActivityError.payload`
into the detail body so the client gets a single JSON object with
`detail` plus every payload key flattened alongside (e.g. `error`
discriminator, `latest_version` block, `overlaps` list).

### `routes/_helpers/models.py` (204 lines)

Pydantic request/response models for the activity API. Three layers:

- **Item models** — `UsedItem` (just `entity`), `GeneratedItem`
  (`entity`, optional `content`, optional `derivedFrom`),
  `RelationItem` (accepts both process-control shape with `entity`
  and domain shape with `from`+`to`; mutually exclusive — the
  model's `model_post_init` enforces).
- **Request models** — `ActivityRequest` (single activity, type
  comes from URL on typed endpoints), `BatchActivityItem` +
  `BatchActivityRequest` (batch carries type per-item).
- **Response models** — `ActivityResponse`, `AssociatedWith`,
  `UsedResponse`, `GeneratedResponse`, `RelationResponse`,
  `DossierResponse`, `FullResponse` (composes all four),
  `DossierDetailResponse` (the `GET /dossiers/{id}` shape with
  `currentEntities` and activity log appended).

### `routes/_helpers/serializers.py` (72 lines)

`entity_version_dict(row, siblings=None, include_entity_id=True)` —
renders an `EntityRow` as a JSON dict. Shape rules: always
`versionId`, `content`, `generatedBy`, `derivedFrom`,
`attributedTo`, `createdAt`. `entityId` optional (drop inside lists
already keyed by entity_id). `schemaVersion` only when set (legacy
NULL-version drops the field). Tombstoned rows keep the row in the
response but set `content: null`, add `tombstonedBy`, and add
`redirectTo` pointing at the live replacement (via a sibling walk
— prefer non-tombstoned siblings, fall back to latest tombstoned
if everything's been redacted).

### `routes/_helpers/typed_doc.py` (196 lines)

Markdown documentation generation for typed activity endpoints.
Each typed activity gets its own POST endpoint (e.g.
`/dossiers/{id}/activities/{aid}/dienAanvraagIn`), and the OpenAPI
description is generated from the activity's YAML — not hand-written
— so the docs always match the workflow.

- `build_activity_description(activity_def, plugin)` — top-level
  renderer. Walks the activity definition and emits markdown
  sections for description, authorization (access type + roles),
  requirements (activities, entities, statuses), used entities
  (with schemas via `format_entity_schemas_for_doc`), and
  generated entities (with schemas).
- `format_entity_schemas_for_doc(entity_type, activity_def, plugin)` —
  renders the JSON schema blocks for a content-bearing entity
  type. For activities with version discipline (`entities.<type>`
  declaration), enumerates every version and emits labeled blocks.
  For legacy activities without version discipline, one unlabeled
  block.

### `routes/access.py` (305 lines)

Shared access-control utilities — the functions every route module
calls to decide whether the user may see a given dossier, and what
parts of it.

- `check_dossier_access(repo, dossier_id, user, global_access)` —
  two-step lookup: global entries first (apply to every dossier),
  then per-dossier `oe:dossier_access` entity. Matches on `role` in
  user.roles or `agents` contains user.id. Default-deny: an
  un-provisioned dossier (no access entity or empty content)
  raises 403. This is safe because every dossier gets
  `oe:dossier_access` atomically on creation via the
  `dienAanvraagIn → setDossierAccess` side-effect chain in
  `workflow.yaml`; an un-provisioned dossier is an anomaly
  (migration half-apply, manual DB edit, plugin mis-wire), and
  reject is safer than permit. Denies emit `dossier.denied` audit
  events with a reason.

- `get_visibility_from_entry(entry)` — extracts `(visible_types,
  activity_view_mode)` from a matched access entry. Bug 79 /
  Round 27.5 fix: missing `view:` or an unrecognized value now
  returns `set()` (nothing visible) rather than the previous `None`
  (no restriction). The rationale ("a typo shouldn't lock people
  out") was backwards for security-adjacent code — a typo SHOULD
  lock people out because that's when the author notices. Logged
  (not audit-emitted) as a config-health finding; the operator
  greps the logs for the message and fixes the offending entry.
  `"all"` sentinel returns `visible_types=None`; list form returns
  the set (empty list = nothing but keep activity_view). Activity
  view is returned as-is; the caller dispatches via
  `parse_activity_view`.

- `check_audit_access(repo, dossier_id, user, global_audit_access)` —
  separate, stricter check for endpoints that expose the full
  unfiltered provenance record (PROV-JSON, columns graph, archive
  PDF). Role-only (audit access isn't granted ad-hoc to individual
  agents). Two sources: `global_audit_access` from config.yaml,
  then the dossier's `audit_access` list on the access entity. A
  user with basic `check_dossier_access` does NOT automatically
  get audit-level views. 403 on failure is generic — don't leak
  whether the user has basic access or just lacks audit rights.

### `routes/admin_search.py` (97 lines)

Admin endpoints for the common search index. Gated on
`global_admin_access` (a separate role tier from audit — admin
operations are destructive or bulk). Workflow-specific indices are
managed by each plugin's own admin routes; the common index spans
workflows and is engine-owned. Exports `register_admin_search_routes(app,
registry, auth_middleware, global_admin_access)` which registers
endpoints for recreating the common index and reindexing every
dossier (via `search.common_index.recreate_index` and
`reindex_all`).

### `routes/dossiers.py` (311 lines)

Dossier-level read endpoints. `register(app, *, registry, get_user,
global_access)` registers:

- `GET /dossiers/{id}` — full dossier detail. Does three things:
  1. **Cache hit path**: reads `cached_status` and
     `eligible_activities` from the dossier row, falling back to a
     fresh `derive_status` + `compute_eligible_activities` if the
     cache is cold. Warmed by `finalize_dossier` after every
     activity.
  2. **File URL signing**: every entity's content walks through its
     registered Pydantic model via `inject_download_urls`; `FileId`
     fields get signed `<field>_download_url` siblings scoped to
     the calling user and dossier. HMAC is over
     `(file_id, action, user_id, dossier_id, expires)`; the file
     service verifies.
  3. **Visibility filtering**: the calling user's `dossier_access`
     entry resolves to a set of visible entity-type prefixes
     (`get_visibility_from_entry`) and an activity-view mode
     (`parse_activity_view`). Entities outside the visible prefixes
     are dropped from `currentEntities`. Activities are filtered
     per the view mode (`all`, `own`, list, or combined dict; the
     `"related"` mode was removed in Round 31).
- `GET /dossiers` — stub listing across all dossiers, optionally
  filtered by workflow. Production callers use the workflow-specific
  Elasticsearch-backed search endpoints.

### `routes/entities.py` (288 lines)

Entity read endpoints — three shapes.

- `GET /dossiers/{id}/entities/{type}` — every version of every
  logical entity of the type (creation order).
- `GET /dossiers/{id}/entities/{type}/{entity_id}` — every version
  of one logical entity.
- `GET /dossiers/{id}/entities/{type}/{entity_id}/{version_id}` —
  a single version. Interesting case: tombstoned versions redirect
  with `301 Moved Permanently` to the live replacement's URL
  (findable via `get_latest_entity_by_id`, since per the
  deletion-scope decision the original row still exists with
  `content: null` + `tombstoned_by` set). This is the ONE endpoint
  that injects download URLs for file fields (Bug 57 fix); the two
  bulk endpoints deliberately don't (minting one signed URL per
  file per version across every version would be waste in the
  common case — clients follow up with a single-version fetch to
  actually download).

All three share `_load_with_access_check` for the dossier-load +
visibility-check preamble. Bulk endpoints render via
`entity_version_dict` from `_helpers/serializers.py`.

### `routes/files.py` (107 lines)

File upload signing endpoint. The dossier API never receives file
bytes — uploads go directly to the `file_service` on a separate
port. `POST /files/upload/request` mints a signed upload URL:
generates a fresh `file_id` (UUIDv4), signs an upload token over
`(file_id, action="upload", user_id)`, returns the file service URL
with the signature embedded as query parameters. Upload tokens
aren't dossier-scoped (a freshly uploaded file isn't yet attached
to any dossier); the dossier read path later issues dossier-scoped
download tokens for the same file_id.

### `routes/prov.py` (510 lines)

PROV export and visualization endpoints. `register_prov_routes(app,
registry, auth_middleware, global_access, global_audit_access)`
registers three endpoints:

- `GET /dossiers/{id}/prov` — audit-level PROV-JSON export. Calls
  `check_audit_access`, then `prov.json_ld.build_prov_graph(session,
  dossier_id)`. No per-user filtering.
- `GET /dossiers/{id}/prov/graph/timeline` — interactive timeline
  visualization. Honours per-user filtering via
  `get_visibility_from_entry` + `parse_activity_view` +
  `is_activity_visible`. Renders the
  `templates/prov_timeline.html` Jinja template with the filtered
  graph serialized as JSON for D3.
- `GET /dossiers/{id}/archive` — audit-level PDF/A archive. Calls
  `check_audit_access`, builds PROV-JSON via `build_prov_graph`,
  then delegates to `archive.generate_archive(session, dossier_id,
  dossier, registry, prov_json, file_storage_root)` which produces
  the PDF bytes. Content-disposition is `attachment;
  filename="archief-{dossier_id}.pdf"`.

The `_build_graph_html(dossier_id, workflow, nodes_json, edges_json)`
helper renders the timeline template (interactive D3 visualization)
and is called from the timeline endpoint.

### `routes/prov_columns.py` (450 lines)

Column-layout PROV graph visualization. `register_columns_graph(app,
registry, auth_middleware, global_audit_access)` registers
`GET /dossiers/{id}/prov/graph/columns` — audit-level; requires
`global_audit_access`. Three bands:

- Top: client activities + scheduled activities + cross-dossier
  dummies (via `wasInformedBy`).
- Middle: side effects (`systemAction` always here).
- Bottom: entities in per-type rows with derivation arrows.

Features: hover-an-entity highlights connected (`generatedBy`,
`attributedTo`, derivation chain); hover-an-activity highlights
used entities; scheduled activities (kind-3 task results) land in
the top row via `wasInformedBy`; recorded tasks show their latest
version under the generating activity. `_build_columns_html` is
the template renderer (`templates/prov_columns.html`).

### `routes/reference.py` (213 lines)

Workflow-scoped utility endpoints — reference data and field
validation.

`register(app, *, registry, get_user)` registers two families:

- `GET /{workflow}/reference/{list_name}` — static reference lists
  (bijlagetypes, documenttypes, etc.) served from the plugin's
  YAML. Sub-millisecond, no DB, freely cacheable. **Public** by
  product decision (shared dropdown data; not dossier state, not
  enumerable references).
- `POST /{workflow}/validate/{validator_name}` and
  `GET /{workflow}/validate` (validator list) — lightweight
  field-level validation between activities. Plugin-registered
  callables that check one thing (URI resolution, cross-field
  rules) without touching the activity pipeline. **Require
  authentication** (Bug 58) — not role-based; the rationale is
  closing an unauthenticated enumeration/DoS surface, since the
  validators effectively act as inventaris-lookup oracles.

Private helpers: `_register_reference_routes` and
`_register_validator_route` (which reads the plugin's
`FieldValidator` dataclass for OpenAPI typing when present, or
falls back to generic JSON request/response).

### `routes/templates/prov_columns.html` (416 lines)

Jinja2 template for the columns-layout graph view. Full-page HTML
with embedded CSS and D3-based interactive JavaScript. Receives
`dossier_id`, `workflow`, and the graph data as template variables.
Pure client-side interactivity — the server ships a JSON payload
and the browser handles hover highlighting / dragging / zoom.

### `routes/templates/prov_timeline.html` (398 lines)

Jinja2 template for the timeline-layout graph view. Full-page HTML
with embedded CSS and D3-based interactive JavaScript. Used by
`routes/prov.py`'s `/prov/graph/timeline` endpoint and honours the
per-user filtering applied on the server side.

### `routes/activities/__init__.py` (30 lines)

Re-exports `register`. Module docstring lists the two URL families:

- Workflow-scoped (workflow in URL, no DB lookup needed to resolve
  plugin):
  - `PUT /{workflow}/dossiers/{id}/activities/{aid}/{type}` (typed).
  - `PUT /{workflow}/dossiers/{id}/activities/{aid}` (generic single).
  - `PUT /{workflow}/dossiers/{id}/activities` (generic batch).
- Workflow-agnostic (engine resolves the workflow from the dossier
  row or from `request.workflow` on creation):
  - `PUT /dossiers/{id}/activities/{aid}` (generic single).
  - `PUT /dossiers/{id}/activities` (generic batch).

All call into `execute_activity`.

### `routes/activities/register.py` (258 lines)

The `register(app, *, registry, get_user, global_access)` entry
point called by `routes/__init__.py`. Registers the generic
workflow-agnostic endpoints and, for each loaded plugin, delegates
to `typed.py` helpers for the per-workflow typed routes and the
workflow-scoped generic route. Contains nested closures
`_handle_single` and `_handle_batch` that share FastAPI-level state
(registry, get_user) across the endpoints.

### `routes/activities/typed.py` (175 lines)

Per-workflow typed-route registrars.

- `_register_workflow_scoped_generic(app, *, registry, plugin,
  get_user, global_access)` — one generic route per workflow,
  accepting any activity type in the request body.
- `_register_typed_route(app, *, registry, plugin, activity_def,
  get_user, global_access)` — one route per (workflow, activity
  type) with typed request/response schemas and the activity-
  specific OpenAPI description from
  `_helpers/typed_doc.build_activity_description`.

Both close over shared handler logic in
`routes/activities/register.register()` via the `get_user` +
`registry` parameters.

### `routes/activities/run.py` (208 lines)

Pure-function helpers for activity execution, called from
`register.py` and `typed.py`.

- `_resolve_plugin_and_def(registry, activity_type, workflow_name)` —
  find the plugin and activity definition. Accepts bare or
  qualified activity names; qualifies before registry lookup.
  Two paths: registered activity (fast) or first-activity-on-new-
  dossier (falls back to `workflow_name` from the request body).
  Raises 404 if neither resolves.
- `_run_activity(plugin, activity_def, dossier_id, activity_id,
  user, role, used, generated, relations, remove_relations,
  workflow_name, informed_by)` — the `execute_activity` call plus
  response shaping. Wraps `ActivityError` into `HTTPException` via
  `activity_error_to_http`.
- `_emit_activity_success(*, user, dossier_id, activity_type,
  activity_id, status)` — post-commit audit emission
  (`dossier.activity_completed`). Deferred to after the request's
  transaction commits so failed activities don't produce misleading
  audit events.

---

## `search/` — Elasticsearch integration

```
search/
├── __init__.py       — settings, client, ACL, global-access config hooks
└── common_index.py   — cross-workflow dossier search index
```

### `search/__init__.py` (209 lines)

Elasticsearch plumbing shared by the engine (common index) and
plugins (workflow-specific indices). Three concerns:

- **Connection config** — `SearchSettings` is a `BaseSettings` with
  env prefix `DOSSIER_ES_` so the API key never touches committed
  YAML. `url` empty = no-op mode (POC / tests). `get_client()`
  lazily builds an `AsyncElasticsearch`; `close_client()` releases
  it at shutdown (wired via `app.py`'s lifespan).
- **Global access / admin config** — `configure_global_access(entries)`
  and `configure_global_admin_access(roles)` are called once at app
  startup. Indexers include global roles in per-doc `__acl__`
  (without them, global-access users would search and find nothing
  even though `GET /dossiers/{id}` would have returned the data).
  Admin endpoints gate on `get_global_admin_access()`.
- **ACL filtering** — `build_acl(access_entity_content,
  global_access)` flattens per-dossier access (roles + agents) +
  audit_access + global roles into a deduplicated order-stable list
  stored on each doc. `build_acl_filter(user)` returns the ES query
  fragment (`{terms: {__acl__: user.roles + [user.id]}}`) every
  search must AND into its query — even admin roles go through it.

### `search/common_index.py` (211 lines)

The cross-workflow common dossier index. One doc per dossier with
fields: `dossier_id` (keyword), `workflow` (keyword), `onderwerp`
(text, pulled from `oe:aanvraag` or equivalent — plugins decide
what maps here), `__acl__` (keyword list). `INDEX_NAME =
"dossiers-common"`.

Operations:

- `build_common_doc(dossier_id, workflow, onderwerp,
  access_entity_content)` — per-dossier doc builder. Usually
  called from each plugin's `post_activity_hook` via the
  plugin-owned `build_common_doc_for_dossier`.
- `index_one(doc)` — single-doc index/upsert. No-op if the client
  isn't configured.
- `recreate_index()` — drop and recreate with the mapping.
  Destructive; admin-gated.
- `reindex_all(repo, registry)` — iterate every dossier in
  Postgres, delegate to each plugin's `build_common_doc_for_dossier`,
  bulk-index. Dossiers whose plugin returns None are counted as
  skipped. Without plugin builders the engine would emit bare-
  minimum fallback docs with empty `onderwerp` and only global
  roles in `__acl__`, making every non-global user invisible in
  search.
- `search_common(query, user)` — the implementation behind
  `GET /dossiers?q=...`. AND-s `build_acl_filter(user)` into the
  query so users only see dossiers they're allowed to see.

---

## `worker/` — task worker process

```
worker/
├── __init__.py      — re-exports
├── cli.py           — main() / argparse entry (python -m dossier_engine.worker)
├── polling.py       — due-task discovery + atomic claim (SKIP LOCKED)
├── execution.py     — process_task + _execute_claimed_task + _refetch_task
├── task_kinds.py    — per-kind handlers + complete_task
├── failure.py       — retry scheduling + dead-lettering + requeue
└── loop.py          — worker_loop top-level coroutine
```

### `worker/__init__.py` (77 lines)

Module docstring explaining task kinds (recorded → function + result,
scheduled_activity → same-dossier activity, cross_dossier_activity
→ function decides target + activity there). All operations run in
a single DB transaction; anything failing rolls back everything.

Re-exports the public API (`main`, `worker_loop`,
`requeue_dead_letters`, `process_task`, `complete_task`,
`find_due_tasks`) and a large set of private helpers that tests
access directly (`_parse_scheduled_for`, `_is_task_due`,
`_claim_one_due_task`, `_compute_next_attempt_at`, `_record_failure`,
`_is_missing_schema_error`, `_select_dead_lettered_tasks`, the three
per-kind `_process_*`, `_execute_claimed_task`, `_refetch_task`,
`_resolve_triggering_user`).

### `worker/cli.py` (120 lines)

`python -m dossier_engine.worker` entry point. Parses argv, dispatches
to either `worker_loop` (normal operation) or `requeue_dead_letters`
(admin command). Argparse flags cover `--config`, `--poll-interval`,
`--once` (run one iteration for tests), and `--requeue-dead-letters`
(ops tool: fix the root cause of failures, then requeue the tasks
operators triaged and resolved).

### `worker/polling.py` (234 lines)

Five functions for "what's due, and which one do I claim?"

- `_parse_scheduled_for(value)` — ISO datetime parser tolerant of
  both `Z` suffix and `+00:00` offset forms. Naive datetimes are
  treated as UTC.
- `_build_scheduled_task_query(for_update=False)` — shared SELECT
  for scheduled `system:task` entities. `for_update=True` uses
  `FOR UPDATE SKIP LOCKED` for the claiming path.
- `_is_task_due(task, now)` — point-in-time due check. A task is
  due when `scheduled_for <= now` AND (`next_attempt_at` is None
  or `next_attempt_at <= now`).
- `find_due_tasks(session)` — read-only snapshot of currently-due
  tasks. Used by admin/debug tooling.
- `_claim_one_due_task(session)` — atomic claim via
  `FOR UPDATE SKIP LOCKED` so multiple workers don't fight over
  the same task row.

### `worker/execution.py` (172 lines)

Three functions wrapping a single claimed task's execution.

- `process_task(task, registry, config)` — the high-level entry
  point called by the loop. Opens a session, dispatches into
  `_execute_claimed_task` inside a transaction, handles exceptions
  by delegating to `_record_failure` (`worker.failure`) — which
  decides retry vs dead-letter.
- `_execute_claimed_task(session, task, registry)` — opens the
  transaction; takes the dossier lock in the same order user
  activities do (this is the structural fix for Bug 74 — the
  deadlock-retry wrapper in `db.session` is defence-in-depth
  behind it). `_refetch_task` re-reads the task row inside the
  transaction to catch cancel/supersede races (a user may have
  run an activity that cancels or supersedes the task between
  claim and execution). Dispatches to the per-kind handler from
  `task_kinds.py` by reading `task.content.kind`.
- `_refetch_task(session, task_entity_id)` — fresh read inside the
  active transaction. Returns the current latest version of the
  task entity; if that version's status is no longer `scheduled`
  (already cancelled, superseded, or completed by a race), the
  worker skips execution and the claim is released by transaction
  commit.

`_resolve_triggering_user` lives in `task_kinds.py`, not here (it's
consumed by the per-kind handlers and would create a circular
import).

### `worker/task_kinds.py` (370 lines)

Per-kind dispatch plus the shared `complete_task` finalizer.

- `complete_task(session, task, activity_id, activity_type, status,
  result)` — writes the `completeTask` activity (generates a new
  version of the task entity with `status` set to `completed` or
  `failed` and `result` populated). Shared by all three non-
  fire-and-forget kinds.
- `_process_recorded(...)` — kind 2. Resolves the plugin function,
  invokes it with an `ActivityContext` built with `SYSTEM_USER` as
  executor and `_resolve_triggering_user` as attribution, records
  the result via `complete_task`.
- `_process_scheduled_activity(...)` — kind 3. Executes the target
  activity in the same dossier via `execute_activity` with
  `caller=Caller.SYSTEM`. The engine's used-auto-resolve picks up
  any required entities via trigger scope (from `informed_by`) or
  singleton fallback.
- `_process_cross_dossier(...)` — kind 4. Invokes the plugin
  function which returns a `TaskResult` carrying `target_dossier_id`;
  executes the target activity in THAT dossier; completes the task
  in the SOURCE dossier with a URI pointing at the cross-dossier
  activity.
- `_resolve_triggering_user(repo, task)` — walks from the task's
  `generated_by` activity to its association row to recover the
  original request-maker's `User` object. Falls back to
  `SYSTEM_USER` if the chain breaks (defensive — the activity
  association is always written by the engine, but the worker
  shouldn't crash if an old row is malformed).

### `worker/failure.py` (437 lines)

Failure handling.

- `_compute_next_attempt_at(now, attempt_count, base_delay_seconds)` —
  exponential backoff with ±10% jitter. Shape:
  `base * 2**(attempt-1)`, same as the `run_with_deadlock_retry`
  wrapper in `db.session`.
- `_record_failure(session, task, exc)` — writes a failure outcome
  to the task row. Increments `attempt_count`, sets `last_attempt_at`;
  if `attempt_count >= max_attempts` → `status = "dead_letter"`
  (terminal, stops being picked up); otherwise sets
  `next_attempt_at` to backoff-from-now and leaves status
  `scheduled`. Sends telemetry via `capture_task_retry` /
  `capture_task_dead_letter` (fingerprinted Sentry events — see
  `observability.sentry`).
- `_is_missing_schema_error(exc)` — classifier. Exceptions like
  `sqlalchemy.exc.ProgrammingError` with "relation does not exist"
  mean the schema is out of date — retrying indefinitely won't
  help. Such errors are fast-tracked to dead-letter on the first
  hit rather than burning through `max_attempts`.
- `_select_dead_lettered_tasks(session, function=None, since=None)` —
  query builder for the requeue tool. Filters by optional function
  name and by optional `since` timestamp.
- `requeue_dead_letters(*, function=None, since=None, config_path=...)` —
  ops entry point. For each dead-lettered task: write a new
  `system:task` version with `status="scheduled"`, `attempt_count=0`,
  `next_attempt_at=None`. Invoked via `python -m
  dossier_engine.worker --requeue-dead-letters [--function X]
  [--since T]`.

### `worker/loop.py` (250 lines)

The main loop.

- `worker_loop(config_path="config.yaml", poll_interval=10,
  once=False)` — top-level coroutine. Wires up config loading via
  `load_config_and_registry`, DB init via `db.init_db`, Sentry via
  `init_sentry_worker`, and signal handlers (SIGTERM/SIGINT sets
  a `shutdown` asyncio.Event). Delegates to `_worker_loop_body`.
- `_worker_loop_body(session_factory, registry, shutdown,
  poll_interval, once)` — the actual loop. Opens a fresh session
  each iteration, calls `_claim_one_due_task`, dispatches the
  claimed task to `process_task`, sleeps `poll_interval` seconds,
  repeats until `shutdown` is set (or exits after one iteration
  if `once=True`). The whole loop is wrapped in a try/except that
  captures any loop-level crash via `capture_worker_loop_crash`
  before re-raising — operators see a fatal Sentry event fingered
  as "the worker itself died" distinct from per-task failures.

---

This file was produced by direct reading of every source file in
`dossier_engine/`. Line counts reflect the Round 34 tree state.
When adding a file or significantly changing a file's role, update
the corresponding entry here.
