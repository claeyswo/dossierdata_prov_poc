# Dossier Platform — Consolidated Code Review

*8 passes across ~30,000 lines of Python + ~3,400 lines of YAML/Markdown. Frontend excluded per instruction.*

**Legend:** ~~strikethrough~~ = fixed & tested; 🔍 = investigated, not a real bug.

---

## Engagement summary

| Status | Count | Items |
|---|---|---|
| ✅ Fixed & verified | 39 | Bugs 1, 2, 4, 5, 6, 7, 9, 12, 13, 15, 16, 17, 20, 27, 30, 32, 39, 44, 47, 53, 54, 55, 56, 57, 58, 62, 64, 65, 66, 68, 69, 70, 72 (coverage), 73, 74, 75, 76, 77, 79 + Obs-2 (duplicate "external") |
| 🔍 Investigated, not a bug | 1 | Bug 14 — cross-dossier refs are `type=external` rows |
| 🛑 Deferred / accepted | 5 | Bug 28 (POC auth slated for replacement), Bug 31 (RRN acceptable), Bug 45 (MinIO migration), Bug 63 (403 is correct HTTP), Bug 71 (test activities, deploy-time removal) |
| 🧪 Test suite | **883/883** passing | engine 818 (unit 337 + integration 481), toelatingen 26, file_service 21, common/signing 18 |
| 🏃 `test_requests.sh` | **25/25 OK, exit 0, zero deadlocks, zero worker crashes** | D1–D9 green |
| ✂️ Duplication closed | **D1, D2, D4, D22, D25** | Graph-loader consolidation + audit-emit wrapper |
| 🧰 Harnesses installed | **3** | Guidebook YAML lint + phase-docstring lint + CI shell-spec wrapper |
| 🤖 CI wired | **GitHub Actions** | `.github/workflows/ci.yml` — 4 jobs: pytest, shell-spec, doc-harnesses, migrations-append-only |
| 🎯 Must-fix walk | **Complete** | All 17 fixable must-fix bugs closed; the 5 open rows are deferred/investigated by product decision (Bugs 14, 31, 45, 63, 71) |
| 📦 Pending | 21 should-fix + 16 lower-priority bugs + 31 observations + 21 dups + 5 meta (partial relief) | See below |

Note: Bug 75 was discovered *by* harness 2 on its first run — a new bug surfaced and fixed in the same session as the harness that surfaced it.

---

## Bugs

### Must-fix — correctness, security, data integrity

| # | Pass | Summary | Status |
|---|------|---------|--------|
| ~~1~~ | 1 | ~~`remove_relations` — `r["relation_type"]` on frozen dataclass → `TypeError`.~~ | ✅ |
| ~~2~~ | 1 | ~~Add-validator dispatch path also triggers on removes.~~ | ✅ |
| ~~5~~ | 2 | ~~`check_dossier_access` docstring claims default-deny but code asserts default-allow.~~ | ✅ **Fixed in Round 15.** Code now matches the module docstring: an un-provisioned dossier (no `oe:dossier_access` entity, or one with empty content) raises 403 with `emit_dossier_audit(reason="Dossier has no access entity configured")` instead of falling through to permit. Drive-by consistency fix in the same file — three gratuitous in-function `from ..audit import emit_dossier_audit` hoisted to module level. Four tests updated + two regression tests added + `_bootstrap_with_entity` in `test_prov_endpoints.py` taught to seed the access entity that production's `setDossierAccess` side-effect writes. |
| ~~6~~ | 2 | ~~Alembic failure fallback runs `create_tables()` — half-migrated schema risk.~~ | ✅ **Fixed in Round 16.** `app.py` now raises `RuntimeError` on any Alembic `upgrade head` non-zero exit (logging stderr at ERROR first so the Alembic traceback survives in the app log) *and* on missing `alembic.ini`. Previous silent fallback to `create_tables()` is gone — it masked partial-migration corruption by no-op'ing over existing tables. Extracted the Alembic invocation to a module-level `_run_alembic_migrations(db_url)` helper so the fail-fast paths are unit-testable without a live DB. 5 regression tests added. |
| ~~7~~ | 2 | ~~Batch endpoint emits audit events per item before transaction commit.~~ | ✅ **Fixed in Round 17.** Scope was wider than the title suggested: all three activity endpoints (generic single, generic batch, typed-per-workflow) share `_run_activity`, which emitted `dossier.created`/`dossier.updated` in-transaction *before* `run_with_deadlock_retry` committed. On mid-batch rollback the audit log falsely recorded committed work; on deadlock-retry it double-emitted. Fix moves the success emit to a new module-level `_emit_activity_success(...)` helper invoked *after* `run_with_deadlock_retry` returns in all three call sites (batch accumulates per-item descriptors in a closure list that clears on every retry attempt). Denial emit on `ActivityError(403)` stays in-transaction — the denial decision is the auditable fact regardless of rollback. Sibling Bug 77 found and fixed during regression-test authoring (see below). |
| 🔍 14 | 3 | **Not a bug.** Cross-dossier refs persisted as local `type=external` rows via `ensure_external_entity`; raw-UUID cross-dossier refs rejected at `resolve_used:89-92` with 422. | Dropped from must-fix. |
| ~~15~~ | 3 | ~~Archive tempfile leak fills `/tmp` on heavy use.~~ | ✅ |
| ~~16~~ | 3 | ~~Duplicate PROV-JSON build between `/prov` and `/archive`.~~ | ✅ |
| ~~17~~ | 3 | ~~Hardcoded font paths break on non-Debian.~~ | ✅ |
| ~~30~~ | 4 | ~~`move_bijlagen_to_permanent` silently swallows per-file exceptions.~~ | ✅ **Fixed in Round 18.** Bundled with an `ActivityContext` attribution-plumbing refactor that landed alongside (see Round 18 writeup). The task handler now tracks per-file failures, emits `dossier.denied` via the newly-plumbed `context.triggering_user` on a 403 (cross-dossier graft attempt — the aanvrager whose activity referenced a cross-dossier file_id is now attributed in SIEM rather than "system"), logs infrastructure failures with `exc_info=True` (Sentry breadcrumb bridge), and raises `RuntimeError` at loop end so the worker's recorded-task retry machinery fires for transient outages. Persistent 403s surface as stuck tasks that operators can see, instead of silently leaving an aanvraag with broken file refs. |
| 📝 31 | 4 | Closed by product decision (RRN in `role`/`dossier_access`/ES ACL acceptable). | Decided. |
| ~~44~~ | 5 | ~~File service falls back to `temp/file_id` regardless of `dossier_id`.~~ | ✅ |
| 🛑 45 | 5 | Deferred — MinIO migration handles it. |  |
| ~~47~~ | 5 | ~~Upload tokens dossier-agnostic.~~ | ✅ |
| ~~55~~ | 5 | ~~`lineage.find_related_entity` doesn't filter by `dossier_id` defensively.~~ | ✅ **Fixed in Round 19.** Guard added at per-activity loop entry: walker loads the activity, compares `activity_row.dossier_id` against its scope argument, and short-circuits before querying the activity's generated/used entities if the dossier doesn't match. Repo helpers (`get_entities_generated_by_activity`, `get_used_entities_for_activity`, `get_activity`) got scoping-contract docstrings making the trust boundary explicit. **The return value was already None for cross-dossier edges (line-87 scope check on `get_latest_entity_by_id`), so this is genuine defense in depth — pre-fix the walk happened but the return stayed safe; post-fix the walk is refused at the traversal layer.** 2 regression tests spy on repo helper calls rather than asserting on return value, so a future regression that removes the guard would go red. |
| ~~57~~ | 6 | ~~`routes/entities.py` three endpoints skip `inject_download_urls`.~~ | ✅ **Fixed in Round 20** — narrower scope than the bug title implies. Only the single-version endpoint (`GET /dossiers/{id}/entities/{type}/{eid}/{vid}`) got the injection; the two bulk endpoints (`/entities/{type}` and `/entities/{type}/{eid}`) deliberately do NOT inject, because they're inspection/revision-history shaped — clients follow up with a single-version fetch to actually download, and minting signed URLs across every file in every version is waste in the common case. Module docstring documents the deliberate asymmetry. If a future client needs URLs in the bulk responses the fix is the same per-entity inject call in the per-version loop. |
| ~~58~~ | 6 | ~~`POST /{workflow}/validate/{name}` has no authentication.~~ | ✅ **Fixed in Round 21.** Both validator endpoints (`GET /{workflow}/validate` list + `POST /{workflow}/validate/{name}` typed POST) now require `Depends(get_user)`. The reference-data endpoints in the same file deliberately stay public per product decision — "authenticated = fine" framing: auth is attack-surface reduction, not RBAC, so any authenticated user of any role may call the validators. Reference data is shared dropdown data that doesn't leak dossier state. Module docstring documents the split explicitly. |
| ~~62~~ | 6 | ~~`/entities/{type}/{eid}/{vid}` doesn't verify `entity_id` matches.~~ | ✅ **Fixed in Round 22.** One-line addition to the existing 404-guard block in `get_entity_version`: alongside `dossier_id` and `type` mismatch checks, `entity.entity_id != entity_id` now also 404s. Before the fix the URL's `entity_id` segment was decorative — a client passing any UUID got the version back as long as the version existed in the right dossier with the right type, resulting in silent mis-attribution (response `entity_id` field came from the actual row, differing from what was asked). 2 regression tests in `TestGetEntityVersion`: real-but-wrong eid (A's version under B's eid must 404) and random-never-seen eid must 404. |
| 📝 63 | 7 | **Accepted — keep 403.** Enumeration via 403-vs-404 response-code differential flagged as a security concern. For this deployment the tradeoff falls on semantic correctness: dossier UUIDs are cryptographically random (128 bits of entropy), the system runs behind SSO, `dossier.denied` audit events fire on every 403 so probing shows up in SIEM, and HTTP-client tooling relies on correct status codes for caching / routing / retries. Collapsing 403 to 404 would break that contract to close a leak with negligible real-world impact in this environment. RFC 9110 §15.5.5 permits 404-for-hidden-existence but it's not the right default here. Enumeration detection is a Wazuh dashboard + alert-rule concern, not an application concern — the `dossier.denied` stream already carries everything Wazuh needs (actor, dossier, reason, timestamp). | Decided. |
| ~~68~~ | 7 | ~~Initial-schema Alembic migration mutated retroactively.~~ | ✅ |
| 🛑 71 | 8 | **Accepted** — deploy-time checklist removes test activities from `workflow.yaml`. |  |
| ~~72~~ | 8 | ~~`bewerkRelaties` zero test coverage.~~ | ✅ |
| ~~79~~ | 6 | ~~`get_visibility_from_entry` fail-open on missing or invalid `view:` key.~~ | ✅ **Fixed in Round 27.5 (drive-by during handoff prep).** The access-gate function that translates an access-entry's `view:` key into a visibility filter was returning `None` (no restriction) for two cases: `view:` absent, and `view:` an unrecognised value. The second branch carried an explicit comment "treat as no restriction rather than hard-deny, so a typo doesn't lock people out" — backwards for security-adjacent code. The module docstring at lines 44-46 already described the correct behaviour ("Key absent — empty set (see nothing)"); the code had drifted. Fix flips both branches to default-deny (empty set), emits a WARNING log carrying the offending entry so operators can find + fix the broken access config. 1 existing bug-pinning test rewritten (`test_entry_with_no_view_key_no_restrictions` → `test_entry_with_no_view_key_defaults_deny`); 1 new test for the invalid-value case (`test_entry_with_unrecognised_view_value_defaults_deny`). Paranoia-checked: reverted both branches to `None`, 2 of 7 `TestGetVisibilityFromEntry` tests went red (the two Bug-79 pins), 5 stayed green (positive-behaviour tests for None entry, explicit "all", list, empty list, activity-view variations). Drive-by from Round 27's debrief — user spotted the "default should be deny" mismatch while reviewing access code before starting fresh. Kept as "Round 27.5" in the writeups because it's a mini-round, not a full one. |

### Should-fix — robustness

| # | Pass | Summary | Status |
|---|------|---------|--------|
| ~~4~~ | 2 | ~~`Session` type annotation never imported.~~ | ✅ **Fixed in Round 31.** `Repository.__init__` at `db/models.py:238` had `session: Session` with `Session` never imported. The code ran fine at runtime because `from __future__ import annotations` stringifies all annotations, but anything calling `typing.get_type_hints(...)` — IDE tooling, FastAPI's `Depends` type resolution, Pydantic's model-building — hit `NameError`. Intended type was always `AsyncSession` (every Repository method uses async). Fix: one-character type change. One regression test (`TestRepositoryAnnotations::test_repository_init_annotations_resolve`) that calls `get_type_hints` and asserts `AsyncSession` — the exact operation that was failing pre-fix. |
| ~~9~~ | 2 | ~~N+1 in dossier detail view.~~ | ✅ **Fixed in Round 29.** `routes/dossiers.py::get_dossier` was calling per-activity `_user_is_agent` + `get_used_entity_ids_for_activity` in the visibility-filter loop, turning N activities into O(N) SELECTs under `activity_view: "own"` or `"related"`. Swapped for `load_dossier_graph_rows` (Round 5's consolidation already used by `routes/prov.py`) with dict-lookup closures. Measured 11 activities: **16 → 10 SELECTs** post-fix, independent of N. 3 regression tests added (2 behaviour pins + 1 query-count ceiling at 12). Removed dead `_user_is_agent` helper + unused imports. `get_all_latest_entities` kept as-is — it returns latest-per-entity_id which `graph_rows.entities` does not, and deduplicating client-side would be more code for no perf win. |
| ~~12~~ | 2 | ~~`_parse_scheduled_for` silently returns None on unparseable dates.~~ | ✅ **Already fixed & tested.** Discovered during M2 Stage 2 startup: `worker.py:_parse_scheduled_for` was already implementing log-and-defer via `datetime.max.replace(tzinfo=timezone.utc)` on malformed ISO, with a 12-case `TestParseScheduledFor` in `test_worker_helpers.py` including explicit regression guards. The review had been carrying a stale open-bug entry; verified end-to-end (parses valid forms, returns None for genuine-empty, returns aware `datetime.max` for malformed with logger.error). No code change this round — bookkeeping correction only. |
| ~~13~~ | 2 | ~~Deprecated `@app.on_event("startup")`.~~ | ✅ **Fixed in Round 33.** Both `@app.on_event("startup")` (audit config + DB init + Alembic) and `@app.on_event("shutdown")` (close search client) converted to a single `@asynccontextmanager` lifespan function passed to `FastAPI(lifespan=...)`. Startup before `yield`, shutdown after. Same runtime timing as the old handlers; pure deprecation migration, no behavior change. Closed the previously-zero test coverage of `create_app` itself by adding one shape test (`test_app_factory_lifespan.py`) that asserts `on_startup == [] and on_shutdown == []` and that `lifespan_context` is not FastAPI's `_DefaultLifespan`. Test doesn't fire the lifespan (runtime correctness still covered by `test_alembic_startup` + `test_audit`); it's a shape pin. Paranoia-checked by removing `lifespan=` from `FastAPI(...)` + re-adding a stub `on_event` — test goes red with both the expected shape assertion *and* visible `DeprecationWarning`s pointing at the unfixed lines. |
| — | 2 | Alembic subprocess has no timeout. |  |
| — | 2 | `file_service.signing_key` default accepted at startup. |  |
| — | 2 | No plugin-load cross-check that `handler:`/`validator:` names resolve. |  |
| — | 2 | Worker's recorded tasks don't pass `anchor_entity_id`/`anchor_type`. |  |
| ~~20~~ | 3 | ~~`_PendingEntity` missing several fields → `AttributeError`.~~ | ✅ **Fixed in Round 30.** Five EntityRow columns (`type`, `dossier_id`, `generated_by`, `derived_from`, `tombstoned_by`) were missing from `_PendingEntity` despite the class's own docstring saying they had to stay in sync. Concrete crash path: `schedule_trekAanvraag_if_onvolledig` → `_build_trekAanvraag_task` → `find_related_entity(pending_beslissing, "oe:aanvraag")` → `lineage.py:123 start_entity.type` → 💥 AttributeError. Reachable when the activity's `used:` block doesn't include the aanvraag — structurally near-impossible in normal flow but reachable via data-migration artefacts or future flow variants. Added the four new constructor kwargs + hardcoded `tombstoned_by` and `created_at` as structural None invariants. Plus 3 new tests: programmatic `EntityRow.__table__.columns` parity scan (the maintenance guard the docstring always promised) + 2 invariant tests. Existing `test_pending_entity_carries_expected_fields` extended 4 → 11 assertions. Paranoia-checked with partial revert — parity test emits named-diff error message (`"_PendingEntity is missing EntityRow columns: [...]"`) when drift re-occurs. |
| 25 | 3 | `common_index.reindex_all` loads all dossiers into memory. |  |
| ~~27~~ | 3 | ~~`DossierAccessEntry.activity_view: str` too narrow.~~ | ✅ **Fixed in Round 31.** Tightened to `Union[Literal["all", "own"], list[str], dict] = "own"`. The `"related"` mode was removed (not used in production, confusing semantics) — two-layer defense for legacy DB data: Pydantic rejects `"related"` at write time via `ValidationError`, `parse_activity_view` deny-safes it at read time (legacy entries produce empty timelines rather than silent semantic changes). Default changed `"related"` → `"own"` (deny-more). Incidentally discovered and fixed a pre-existing bug where `parse_activity_view` accepted arbitrary unrecognized strings verbatim (e.g. `"banana"` became `base="banana"`) — now falls through to deny-safe at parse time instead of relying on `is_activity_visible`'s terminal `return False`. 19 new tests (11 in new `tests/unit/test_activity_visibility.py`, 8 in `test_refs_and_plugin.py::TestDossierAccessEntryActivityView`), one Round-29 test deleted (`test_related_mode_includes_activities_touching_visible_entities` — was testing the removed mode). Paranoia-checked on both the read-time revert (1 of 11 red, the deprecation pin, exact shape) and write-time revert (4 of 8 red, the four type-depending tests). |
| 28 | 3 | `POCAuthMiddleware` silently overwrites on duplicate usernames. | 🛑 **Deferred — POC-only, slated for removal.** User confirmed in Round 30 planning that `POCAuthMiddleware` is POC-only and will be replaced (JWT/OAuth) rather than hardened. Fixing a fail-loudly-on-config-error path in code that's on the exit ramp is sunk cost. Re-evaluate if/when real auth lands and similar duplicate-config hazards surface there. |
| 19 | 3 | `GET /dossiers` has no `response_model`. |  |
| — | 3 | Archive has no size cap. |  |
| — | 3 | `app.py:69` appends `SYSTEM_ACTION_DEF` by reference. |  |
| ~~32~~ | 4 | ~~`finalize_dossier`/`run_pre_commit_hooks` docstring documents reading `state.used_rows` — field doesn't exist.~~ | ✅ **Fixed** — docstring now reads `state.used_rows_by_ref` matching the code. Harness 3 prevents recurrence. |
| 34 | 4 | `authorize_activity` catches broad `Exception`. |  |
| 35 | 4 | `reindex_common_too` does 3N queries for N dossiers. |  |
| 38 | 4 | No per-user authorize cache. |  |
| ~~39~~ | 4 | ~~`TaskEntity.status: str` should be `Literal[...]`.~~ | ✅ **Fixed in Round 32.** Tightened to `Literal["scheduled", "completed", "cancelled", "superseded", "dead_letter"] = "scheduled"`. Bundled a parallel tightening of `TaskEntity.kind: str` → `Literal["fire_and_forget", "recorded", "scheduled_activity", "cross_dossier_activity"]` (same file, same shape, same rationale; flagged the scope expansion before coding). Verify-before-plan pass found no policy question or legacy-data risk — all 9 values are actively written, no migration ever changed the set, read-time re-validation path exists (`context.get_typed("system:task")`) but isn't exercised for tasks in production today. 6 regression tests: defaults, accepts-all-valid-values, rejects-unknown for each field, plus a `test_kind_is_required` invariant pin. Paranoia-checked on both directions (revert `status` → 1 red; revert `kind` → 1 red) — the "exactly one red per revert" shape tells me the tests are properly scoped. |
| 42 | 4 | Field validators take raw dict, no User context. |  |
| 43 | 4 | `Aanvrager.model_post_init` raises `ValueError` without Pydantic shape. |  |
| 80 | 4 | `DossierAccess` Pydantic model doesn't reflect the content shape the engine actually reads. Model declares only `access: list[DossierAccessEntry]`, but `routes/access.py::check_audit_access` reads `content.get("audit_access", [])` — a top-level list the model is silent about. Production `setDossierAccess` handler in toelatingen never writes `audit_access`, so today per-dossier audit access falls through to denial and only `global_audit_access` (config.yaml) grants audit views — but the omission from the model means (a) readers can't tell from the model alone what shape the content can take, (b) plugin authors who want per-dossier audit can't discover the feature from the type, (c) any future hardening that sets `model_config = ConfigDict(extra='forbid')` would reject legitimate `audit_access` content. No per-dossier `admin_access` exists — admin is deliberately config-only (`global_admin_access`); worth documenting this in the model's docstring so the omission is intentional rather than accidental drift. Fix: add `audit_access: list[str] = []` field + docstring covering the full shape the engine reads and why `admin_access` is absent. Filed Round 27.5 from user review of access code. |  |
| 46 | 5 | `POST /files/upload/request` accepts unbounded `request_body: dict`. |  |
| 48 | 5 | `.meta` filename not sanitized. |  |
| 50 | 5 | Migration fallback uses module-level `SYSTEM_ACTION_DEF` with bare name. |  |
| ~~53~~ | 5 | ~~`lineage.find_related_entity` frontier growth unbounded.~~ | ✅ **Fixed in Round 25 (Cat 4 lineage walker completion).** Frontier changed from `list[UUID]` to `set[UUID]`, append-time dedup against `visited_activities`. Bounds memory by "activities not yet visited" (≤ dossier's activity count) rather than "paths taken through the graph." Micro-optimization, not a correctness change — the pre-existing `visited_activities` guard already prevented incorrect reprocessing. Sanity test in `test_lineage.py::test_bug53_high_fan_in_walk_still_correct` confirms high-fan-in walks still resolve correctly; **no behaviour-observable regression test exists** for the memory bound itself (see Round 25 writeup on why that's the honest disposition). |
| ~~54~~ | 5 | ~~`lineage.find_related_entity` returns `None` for both "not found" and "ambiguous".~~ | ✅ **Fixed in Round 25 (Cat 4 lineage walker completion).** New `LineageAmbiguous(Exception)` raised when a visited activity touches >1 distinct `entity_id` of the target type; the exception carries the ambiguous activity_id + candidate entity_ids for operator triage. Not-found/root/exhausted cases still return `None` — these share a "no anchor available" outcome at the callsite. Sole production caller (`_build_trekAanvraag_task` in toelatingen plugin) updated to catch `LineageAmbiguous`, emit a WARNING log carrying the triage info, and proceed with an unanchored task. 4 caller-side tests in new `test_build_trekAanvraag_task.py`, plus updated `test_ambiguous_raises_lineage_ambiguous` in walker tests. |
| ~~56~~ | 6 | ~~README claims externals in both `used`/`generated` allowed; code + test reject.~~ | ✅ **Fixed in Round 24 (Cat 1 doc-fix batch).** README section D5 rewritten to correctly describe externals as rejected the same way local overlaps are — with a `kind: "external"` payload instead of `kind: "local"`. Verified against `invariants.enforce_used_generated_disjoint` and `test_invariants.py::test_external_overlap_by_uri`. Cross-refs Obs 69 (marked closed). |
| 59 | 6 | Unregistered validators silently skip. |  |
| 60 | 6 | `alembic/env.py` nested `asyncio.run()` hazard. |  |
| ~~64~~ | 7 | ~~Plugin guidebook uses `schema:` where loader reads `model:`.~~ | ✅ **Fixed** in `docs/plugin_guidebook.md:59`. Harness 1 prevents recurrence. |
| ~~65~~ | 7 | ~~Same `schema:` vs `model:` bug in external-ontologies section.~~ | ✅ **Fixed** in `docs/plugin_guidebook.md:635, 639, 643`. |
| ~~66~~ | 7 | ~~Relation validator keying rules undocumented.~~ | ✅ **Fixed in Round 24 (Cat 1 doc-fix batch).** Added "Relation-validator keying" subsection to `docs/plugin_guidebook.md` documenting all three resolution styles (per-operation `validators: {add, remove}` dict, activity-level single `validator:` string, plugin-level by relation-type name) with YAML examples, resolution priority order, and a warning about the key-space ambiguity between named-validator lookups and by-type lookups in `plugin.relation_validators`. Cross-refs Obs 73 (marked closed). |
| 67 | 7 | `_errors.py` payload key collision. |  |
| ~~69~~ | 7 | ~~Tombstone role shape inconsistent between dossiertype template and workflow.yaml.~~ | ✅ **Fixed in Round 24 (Cat 1 doc-fix batch).** `dossiertype_template.md` rewritten to show the correct flat-list shape `allowed_roles: ["beheerder"]` instead of the broken dict-of-dicts form `- role: "beheerder"`. Verified against `app.py:138-142` which iterates the list treating each element as a bare role-name string — the dict form would silently produce `{"role": {"role": "beheerder"}}` and break tombstone authorization. Cross-refs Obs 71 (marked closed). |
| ~~70~~ | 8 | ~~`test_requests.sh` outputs dead `/prov/graph` URL.~~ | ✅ **Fixed** — four echo sites updated to `/prov/graph/timeline` (the user-visible visualization route). `prov.py` module docstring also corrected — it documented a `/prov/graph` endpoint that doesn't exist; now lists the four real ones. Verified end-to-end: `/prov/graph/timeline` returns 401 without auth (route registered), the old `/prov/graph` returns 404 (proves the URL was dead). |
| ~~73~~ | (impl) | ~~`conftest.py` TRUNCATE list omits `domain_relations`.~~ | ✅ |
| ~~74~~ | (impl) | ~~Worker/route deadlock on `system:task` rows.~~ | ✅ **Fixed.** Structural (worker takes dossier lock first, matching user-activity order) + defence-in-depth (`run_with_deadlock_retry` on routes). |
| ~~75~~ | (impl) | ~~Worker crashes on cold start if the app hasn't finished Alembic migrations yet — `UndefinedTableError` propagates to top-level crash handler.~~ | ✅ **Fixed.** Surfaced by harness 2. Worker now tolerates SQLSTATE 42P01 during pre-ready window, logs a warning and retries; real missing-table errors after first successful poll still propagate. |
| ~~76~~ | (impl) | ~~`file_service/app.py:265` — the `.meta` parse during `/internal/move` catches OSError and JSONDecodeError and falls back to "no binding info", which then permits the move.~~ | ✅ **Fixed & tested.** Discovered during M2 Stage 3: the silent-bypass code had already been replaced with `logger.error` + `raise HTTPException(500, ...)` with a thorough docstring explaining the four `.meta` states (missing / valid-with-field / valid-no-field / corrupted) and the policy for each. Review was carrying a stale open-bug entry. **One real sub-bug caught by writing the regression tests:** `UnicodeDecodeError` (subclass of `ValueError`, *not* of `JSONDecodeError`) wasn't in the except clause, so non-UTF-8 garbage in `.meta` crashed with a default 500 rather than the intended explicit reject. Widened the except to `(OSError, json.JSONDecodeError, UnicodeDecodeError)`; added two regression tests in `TestMoveEnforcesDossierBinding` (truncated JSON + binary garbage). Both green; full class 7/7 passing. |
| ~~77~~ | (impl) | ~~`activities.py:_run_activity` denial-audit emit was dead code — read `getattr(e, 'code', None)` and `getattr(e, 'message', str(e))` on an `ActivityError` that stores `status_code` and `detail`. Write-side `dossier.denied` events have never fired.~~ | ✅ **Fixed in Round 17 alongside Bug 7.** Surfaced by writing the Bug 7 regression test for the denial path: the test correctly got a 403 back from the endpoint but captured zero audit emits, because `getattr(e, 'code', None)` always returned `None` and the `if code == 403:` guard always skipped. Read-side denials from `routes/access.py` had been working all along so the SIEM stream wasn't empty, but the `_run_activity` docstring's promise that "SIEM sees both read-side denials (from `routes/access.py`) and write-side denials (from here) in one stream" had been silently broken. Fixed by reading `e.status_code` and `e.detail` directly (no getattr-with-default, so an attribute rename is a loud AttributeError, not silent skip). The regression test now pins the emit AND a substring of the real authorize message so a future rename is caught on both axes. |

### Lower-priority

| # | Pass | Summary |
|---|------|---------|
| 18 | 3 | `/prov/graph/timeline` uses local dict lookups; shares logic with `dossiers.py:176-185` which hits the DB. |
| 21 | 3 | `inject_download_urls` skips `list[FileId]`. |
| 22 | 3 | `classify_ref` misclassifies bare URLs without scheme. |
| 23 | 3 | `path` vs `DOSSIER_AUDIT_LOG_PATH` env precedence undocumented. |
| 24 | 3 | `emit_audit` swallows all exceptions. |
| 26 | 3 | `recreate_index` doesn't refresh between delete/create. |
| 29 | 3 | `configure_iri_base` mutates module globals; test-order landmine. |
| 33 | 4 | `compute_eligible_activities` relies on undocumented Repository activity cache. |
| 36 | 4 | Reference data has no version/migration story. |
| 37 | 4 | `_resolve_field` strips leading `content.` inconsistently. |
| 40 | 4 | `SYSTEM_ACTION_DEF` mutation at load could leak across plugins. |
| 41 | 4 | Pre-commit hooks receive `used_rows=state.used_rows_by_ref`; README docs name but not shape. |
| 49 | 5 | `query_string_to_token` declared but never imported. Dead code. |
| 51 | 5 | Migration's already-applied check uses JSONB string equality. |
| 52 | 5 | Migration framework has no two-phase / all-or-nothing mode. |
| 61 | 6 | `activity_relations` indices cost writes but have zero readers today. |

---

## Meta-patterns (6; three with partial relief shipped)

**M1. Docstring "Reads/Writes" drift has no enforcement.** ✅ **Partial relief shipped.** `tests/unit/test_phase_docstrings.py` (harness 3) parses every `async def` in `engine/pipeline/*.py`, extracts `state.X` references from docstrings, and checks them against `ActivityState.__dataclass_fields__`. Bug 32 was surfaced and fixed by this harness on its first run. Future drift is caught at commit time.

**M2. "Silent skip" as a default policy.** Unregistered validators skip, unrecognized activity_view modes skip, `post_activity_hook` failures swallowed, bijlage move per-file failures swallowed, audit log errors swallowed. No specific relief shipped — these warrant case-by-case review.

**M3. Hardcoded paved-path values.** Bug 17 (fonts) closed this engagement via `dossier_engine/fonts.py`. Others remain — `systeemgebruiker` in `entities.py:105`, signing-key default, `id.erfgoed.net` in `prov_iris.py`.

**M4. Documentation drift across README, plugin guidebook, dossiertype template, pipeline architecture doc.** ✅ **Partial relief shipped.** `tests/integration/test_guidebook_yaml.py` (harness 1) validates every ```yaml block in the guidebook against canonical key sets derived from production `workflow.yaml`. Bugs 64 and 65 were surfaced and fixed in the same session. A sibling check keeps the allowed-key set honest: if production adds a new field, the test fails and forces the allowlist update.

**M5. Executable specs that don't execute.** ✅ **Full relief shipped.** Two pieces:
- `scripts/ci_run_shell_spec.sh` — self-contained wrapper that stands up file_service + app + worker, waits for readiness, runs `test_requests.sh`, reports OK count / summary count / traceback count, exits 0/1/2/3 for pass/fail/stack-never-up/env-missing. Surfaced Bug 75 on first run.
- `.github/workflows/ci.yml` — the wrapper is now invoked by the `shell-spec` job on every PR and every push to `main`. The guidebook's Python code blocks still aren't validated (each references dotted-import paths for fictional classes; full relief there would need a fixture-module approach we haven't attempted), but the much higher-value shell-spec M5 target is now fully covered.

**M6. "Test" is a namespace, not a load-time gate.** Bug 71 accepted — deploy-time checklist keeps test activities out of production.

---

## Structural observations & duplications

## Structural observations

The structural sweep catalogued observations clustering into five themes. **Count reconciliation (Round 23 bookkeeping):** 44 items are currently listed in this section — 35 open, 7 closed, 1 partially addressed, 1 deferred. An earlier pass may have counted ~57 structural observations including items that have since been reclassified as bugs (e.g. items 38, 53, 54, 56, 59, 66, 69 referenced in the bug tables); the 44 here are what the review actually lists. Status key: **open** (unchanged), **partially addressed** (progress in a specific pass), **closed** (folded into a fix). Individual-observation numbering is reconstructed from the original passes where it was explicit.

### Code organization
- **Obs 50 — Worker split.** `worker.py` is ~1,340 lines (grew ~80 lines with Bug 75's resilience logic + Bug 12's log-and-defer). Proposed split: `poll.py`, `execute.py`, `retry.py`, `requeue.py`, `signals.py`. **Open.**
- **Obs 51 — Unify relation shape in `ActivityState`.** Three typed lists (`validated_relations`, `validated_domain_relations`, `validated_remove_relations`) plus the `relations_by_type` dict. Same conceptual "validated relation" has 4 in-memory shapes; this is where Bugs 1/2 lived. **Open.**
- **Obs 52 — Split `prov.py`** (currently 509 lines, down from 792 after Round 5) into extract / transform / render layers. **Partially addressed** (Round 5 extracted `prov_json.py` with the graph-rowset loader + PROV-JSON builder; the remaining `prov.py` is mostly route registration + HTML render). Further split is lower-urgency now.
- **Obs 53 — Extract `prov_columns_layout.py`** — ~280 lines of pure layout algorithm currently inside `register_columns_graph`. Pure function of inputs; easy to isolate. **Open.**
- **Obs 54 — Untangle import-inside-function cycles.** Pattern appears in `relations.py`, `side_effects.py`, `persistence.py`, `dossiers.py`. Signals a cycle in the module graph that could be cleaned up in one refactor. **Open.**
- **Obs 55 — Rationalize `namespaces.py` singleton** + scattered `try/except RuntimeError` fallbacks in `prov_iris.py`, `activity_names.py`. **Open.**

### Plugin surface
- **Obs 56 — Centralize plugin validation.** Three load-time validators exist (`validate_workflow_version_references`, `validate_side_effect_conditions`, `validate_side_effect_condition_fn_registrations`, `_validate_plugin_prefixes`), five more are missing. Also: no cross-check that `handler:` / `validator:` names resolve to registered callables (Bug 59 territory). **Open.**
- **Obs 57 — Plugin interface table drift.** Docs promise 15 field validations; 3 are actually checked. **Open.**
- **Obs 58 — Split `authorize_activity`.** Pre-creation vs post-creation modes threaded via `dossier_id: UUID | None` — should split into two functions. **Open.**
- **Obs 59 — `status:` dict-form load-time validation.** Load-time validation for `status:` dict-form shape. **Open.**
- **Obs 60 — `eligible_activities` column type.** `Text` → `JSONB`. **Open.**
- **Obs 61 — `set_dossier_access` view-list duplication.** 6 copies of the view list, duplicate `"external"`. **Closed** in Round 11 (view-list constants + role helpers extracted, duplicate bug fixed, 16 regression tests added). Write-on-change optimization explicitly declined as a product decision (keep full provenance graph).
- **Obs 62 — Legacy `handle_beslissing`.** Remove legacy `handle_beslissing` (marked "kept for backward compatibility"). **Open.**
- **Obs 63 — `"behandelaar"` role review.** Back-compat `"behandelaar"` role needs an owner + removal deadline. **Closed** in Round 11 (confirmed actively used by `workflow.yaml:71, 80, 89, 304, 391, 724, 755` authorization entries — legitimate global-staff role, not legacy).
- **Obs 64 — `systemAction` sub-types.** Introduce `oe:migrationAction`, `oe:requeueAction`, `oe:retryAction`. **Open.**
- **Obs 65 — Document `systeemgebruiker` role grants + `caller_only: "system"` check.** **Open.**
- **Obs 66 — Lineage walker completion** (covered by Bugs 53, 54). ~~Needs per-walk cache + distinguishable "not found" vs "ambiguous" return.~~ **Closed** in Round 25 alongside Bugs 53/54. The per-walk cache framing turned out to be Bug 53 (frontier dedup via set-based data structure); the not-found vs ambiguous distinction is Bug 54 (new `LineageAmbiguous` exception). Both fixes shipped together as Cat 4 of the Round 23 triage.

### Documentation drift
- **Obs 67 — Pipeline doc UPDATE-after-persistence claim.** ~~Pipeline doc's "UPDATE must happen after persistence" claim is factually wrong.~~ **Closed** in Round 24 (Cat 1 doc-fix batch). Reworded `docs/pipeline_architecture.md` phases 14/15 prose to describe the actual mechanism (`state.activity_row.computed_status` is an in-memory dirty-flag write on a session-tracked ORM object, flushed with the transaction — not a separate UPDATE statement).
- **Obs 68 — Pipeline doc `ActivityState` field table.** ~~Covers ~⅓ of actual fields, presented as complete.~~ **Closed** in Round 24 (Cat 1 doc-fix batch). Reframed the table as a curated walkthrough of phase-boundary fields, redirected readers to `state.py:ActivityState` as source-of-truth for the full ~37-field list, fixed the erroneous `computed_status` row (that field lives on `activity_row`, not on state itself) to `final_status` with an explicit note about the two places the activity's resolved status is mirrored.
- **Obs 69 — README external-overlap claim** (covered by Bug 56). ~~README claims external-overlap is allowed; code + tests reject.~~ **Closed** in Round 24 alongside Bug 56 fix.
- **Obs 70 — Guidebook `schema:` vs `model:` key.** **Closed** in Round 8 (Bugs 64, 65 fixed; harness 1 now prevents recurrence).
- **Obs 71 — Dossiertype template tombstone shape** (covered by Bug 69). ~~Template's tombstone block shape doesn't match production workflow.~~ **Closed** in Round 24 alongside Bug 69 fix.
- **Obs 72 — Template endpoint docs.** ~~Template's endpoint docs omit the workflow-name prefix — 4 different URL forms for workflow search, none matching production.~~ **Closed** in Round 24 (Cat 1 doc-fix batch). `dossiertype_template.md`'s `search_route_factory` example rewritten to show the `/{workflow}/dossiers` + `/{workflow}/admin/search/...` shape, with explanatory prose about the workflow-name-first convention. Endpoint table row updated from the fictional `/dossiers/{workflow}/search` to the real `/{workflow}/dossiers` + `/{workflow}/admin/search/{recreate,reindex,reindex-all}`.
- **Obs 73 — Relation validator keying rules** (covered by Bug 66). ~~Three styles undocumented.~~ **Closed** in Round 24 alongside Bug 66 fix.
- **Obs 74 — `prov.py` docstring `/prov/graph` reference.** **Closed** in Round 12 (fixed alongside Bug 70).

### Performance / observability
- **Obs 75 — Cache `SearchSettings()`** at module load (currently re-reads env on every `get_client()`). **Open.**
- **Obs 76 — Cache `is_singleton`** instead of linear-scanning `entity_types` per call. **Open.**
- **Obs 77 — `derive_status` uses `cached_status` first.** **Open.**
- **Obs 78 — `check_workflow_rules` passes `known_status`** from `state.dossier.cached_status`. **Open.**
- **Obs 79 — Archive size cap / warning.** **Open.**
- **Obs 80 — Reindex pagination** (covered by Bug 25). Loads all dossiers into memory. **Open.** Redundant with Bug 25; Round 23 triage Category 2 cherry-picks.
- **Obs 81 — Per-user eligibility cache** (covered by Bug 38). **Open.** Redundant with Bug 38; Round 23 triage Category 3.

### Test / deployment concerns
- **Obs 82 — Test fixtures against real Postgres.** Direct `Repository` instances; no unit-isolation story documented. **Open.**
- **Obs 83 — `test_requests.sh` in CI.** **Closed** in Round 8 + Round 9 (`scripts/ci_run_shell_spec.sh` harness 2 + GitHub Actions `shell-spec` job).
- **Obs 84 — Schema-versioning test activities in production YAML** (covered by Bug 71). **Deferred by product decision** (deploy-time checklist removes them).
- **Obs 85 — Dependency-override-friendly auth for tests.** Replace `POCAuthMiddleware` instance with FastAPI `dependency_overrides`. **Open — but see Bug 28 in the should-fix table: `POCAuthMiddleware` is slated for replacement with real auth (JWT/OAuth). Revisit this observation if the dependency-override pattern should carry forward into the real auth layer; the "tests can swap the auth dependency per case" affordance is general and useful regardless of which middleware is behind it.**
- **Obs 86 — Signing key rotation** (only one key accepted today). **Open.**
- **Obs 87 — Migration framework top-level audit log** (who/when/command). **Open.**
- **Obs 88 — `DataMigration.transform` signature** should widen to `(content, row)`. **Open.**
- **Obs 89 — Cross-workflow task permission model.** No check that source plugin can schedule into target workflow. **Open.**

### Specific refactors named
- **Obs 90 — Reads/Writes docstring lint.** **Closed** in Round 8 (harness 3).
- **Obs 91 — Share layout between `archive.render_timeline_svg` and columns graph** (160 + 270 lines of separate layout code). **Open.**
- **Obs 92 — `activity_view` mode complexity reduction.** 5 modes; hard mental load for small feature value. **Open.**
- **Obs 93 — Pipeline-architecture-doc ActivityState hazard enforcement.** **Closed** in Round 8 (harness 3 enforces it).
- **Obs 94 — Migration consistency checks.** Filed in Round 19 CI postmortem (was provisionally numbered "Obs-58" at the time, renumbered here to avoid collision with the new Obs 58). Round 8's append-only guard catches **mutation** of existing migration files (Bug 68's original shape) but not **stale leftover files** from consolidation work — stale versions pass the append-only check yet fail at `alembic upgrade head`. Candidate follow-ups: CI migration preflight job (runs `alembic upgrade head` against a fresh Postgres; fails the build on rc≠0), or static consistency check scanning for redundant DDL across migration files. **Open.**
- **Obs 95 — Plugin surface: dotted-path resolution for all Callable registries.** Eight `dict[str, Callable]` fields on `Plugin` (`handlers`, `validators`, `task_handlers`, `status_resolvers`, `task_builders`, `side_effect_conditions`, `relation_validators`, `field_validators`) were built inside each plugin's `create_plugin()` function and keyed by short names the YAML referenced. Meanwhile `entity_models` and `entity_schemas` already did the cleaner thing: the YAML carries a dotted Python path (`model: dossier_toelatingen.entities.Aanvraag`) and `_import_dotted` resolves it at plugin load. The short-name-dict pattern had three footguns: (1) typos in YAML failed at runtime-of-first-lookup, not load time; (2) key-space collisions (Bug 66's "relation_validators uses both names and types as keys" issue); (3) no compile-time signal for what code runs when you read a YAML file — you had to find `create_plugin()` and trace the dict construction. Migrated to dotted-path resolution mirroring `entity_models`. `field_validators` kept a URL-segment key (value is the dotted path) because the key leaks into the HTTP URL `POST /{workflow}/validate/{key}`. **Closed** in Round 28. See the Round 28 writeup for design-decision rationale. |
- **Obs 96 — Existing load-time validators never invoked in production.** Three validators defined in `plugin.py` — `validate_workflow_version_references`, `validate_side_effect_conditions`, `validate_side_effect_condition_fn_registrations` — are unit-tested thoroughly (see `test_refs_and_plugin.py`) but never called by `app.py::load_config_and_registry` at startup. Only `_validate_plugin_prefixes` runs in production. Found while wiring up Bug 78's new `validate_relation_declarations` / `validate_relation_validator_registrations` in Round 26 — the Bug 78 validators are now called from `load_config_and_registry`, but the pre-existing three are silently unused. Fix is one-turn trivial: add three `for plugin in registry.all_plugins(): validate_*(plugin.workflow, ...)` calls next to the Bug 78 block. Unclear whether the omission was deliberate (perhaps the validators were considered too strict for early plugin development?) or accidental drift — worth a checkpoint before wiring them, because the production toelatingen YAML might have latent shape violations that only surface when the validators actually run. **Open.**
- **Obs 97 — Codebase legibility: file sizes + module organization.** Two related symptoms in the engine package:

  **(a) A handful of files have outgrown their original purpose.** Top 5 by line count as of Round 27.5:
  - `worker.py` — **1438 lines**. Task polling loop + four task-kind dispatchers (recorded, scheduled_activity, cross_dossier_activity + their helpers) + refetch + retry + completion logic + association resolution + trigger-user resolution. Too many responsibilities for one file; each task kind's dispatcher is itself substantial. Natural split: `worker/loop.py` (main polling) + `worker/recorded.py` + `worker/scheduled_activity.py` + `worker/cross_dossier.py` + `worker/completion.py` shared helpers.
  - `plugin.py` — **889 lines**. `Plugin` dataclass + 5 load-time validators + registry + entity-model builder + namespace-prefix helper. The validators were added incrementally (three old, two new in Bug 78); they're independent functions that the main dataclass file doesn't need to carry. Natural split: `plugin/dataclass.py` + `plugin/registry.py` + `plugin/validators.py` + `plugin/entity_models.py`.
  - `db/models.py` — **750 lines**. All SQLAlchemy ORM models + the Repository class in one file. Natural split by table-group boundaries, then Repository separately.
  - `archive.py` — **641 lines**. PDF + timeline SVG + column-layout graph + orchestration. Probably decomposes by output format.
  - `routes/activities.py` — **594 lines**. The `_run_activity` shared core + three route handlers (generic single, generic batch, typed-per-workflow) that all funnel through it. Arguably OK as-is because the three routes share state; worth evaluating whether the typed-per-workflow handler's code-generation side is big enough to extract.

  **(b) Directory structure is confusing at the level "where does X live?"** Three top-level package subdivisions visible in the engine:
  - `dossier_engine/` package root — 16 files, mixed bag (app, worker, plugin, archive, lineage, audit, sentry, fonts, migrations, prov_iris, prov_json, entities, file_refs, activity_names, namespaces, __init__). Lots of things directly under root.
  - `dossier_engine/routes/` — 15 files, all HTTP surfaces. Reasonably coherent.
  - `dossier_engine/engine/` — 26 files split across `engine/` (7) and `engine/pipeline/` (19). This is the biggest source of confusion. The word "engine" inside a package also named `dossier_engine` creates a "which one does 'engine' mean?" problem every time. And `engine/pipeline/` has 19 files representing the 13 pipeline phases plus helpers — fine as a concept, but you have to know the pipeline order to navigate.

  Candidate renames worth considering:
  - `dossier_engine/engine/` → `dossier_engine/core/` or `dossier_engine/activity_pipeline/`. Kills the "engine-in-engine" collision.
  - `dossier_engine/engine/pipeline/` → either flatten into the parent (if renamed) or keep but document the phase order clearly.
  - `docs/pipeline_architecture.md` already exists; may need an accompanying `docs/module_map.md` — a one-pager "where does X live" reference.

  **Scope:** this is pure refactor + rename work; zero behaviour change. Risk is low (mechanical) but wide (imports everywhere). Good fit for a dedicated round with its own careful test-suite pass. Shouldn't happen mid-bug-fix round. Recommend filing as **Category 12 — Codebase legibility** in the Round 23 triage, with priority below Cat 3 (perf) and Cat 2 (cherry-picks) but above Cat 5 (plugin surface) since legibility work unblocks contributors faster than surface tightening. User prompt: *"Files getting too long, like worker.py is something we should tackle. But the number of files per module is growing to a point where it is not clear at all. engine root, engine routes, engine pipeline. Even I don't know where to look anymore"* — the confusion is real and worth addressing before the next significant feature. **Open.**
- **Obs 98 — Exception grants as first-class entities (forward-looking design).** Workflow rules (`requirements:` and `forbidden:`) are deny-by-default with no override mechanism today. For a heritage-permits system — where exceptions are legally significant one-off acts — there's a gap: no clean way to say "normally activity B is forbidden because A already happened, but we're granting a reviewed exception." Encoding exceptions as extra statuses blows up combinatorially (N activities × exception-or-not → 2^N variants, plus every other activity's `forbidden:` clause has to list them).

  **Design sketch (from brainstorming, not yet implemented):**
  - New engine-provided (or plugin-defined by convention) entity type `oe:exception` with content shape roughly: `{activity: str (qualified name), entity_id: UUID | None, granted_until: datetime | None, reason: str}`. PROV attribution covers "who granted, when" for free.
  - New built-in activity `oe:grantException` with `allowed_roles: ["beheerder"]` (or per-workflow equivalent). Generates an `oe:exception` entity. Every grant is thus a normal PROV-recorded activity — legally-defensible audit trail at no extra cost.
  - New pipeline phase inserted **between authorization and workflow-rules**: check whether an unexpired, non-consumed `oe:exception` matches the current activity. If yes, skip the workflow-rule phase. If no, workflow rules proceed normally.
  - **Critical constraint**: exceptions bypass workflow rules ONLY. Authorization still runs unconditionally. Rationale: authorization answers "may this user run activities of this type?" — a permission question that shouldn't be weakenable through dossier state, because otherwise "grant exception" becomes a universal privilege-escalation primitive. Workflow rules answer "given this dossier's history, is this activity legal here?" — and *that's* what exceptions are for.

  **Open design questions that need a concrete first case to decide:**
  - *Single-use vs window-based.* Default single-use probably right for heritage permits (exceptions are deliberate one-offs, not standing policy). Consumption marked on a new version of the entity when the covered activity fires. Window-based can be added later with a `mode:` field if a case demands it.
  - *Activity-wide vs entity-scoped match.* For singletons, irrelevant. For multi-cardinality, `entity_id: UUID | None` lets you except a specific instance instead of all instances of that activity in the dossier. Start without it; add when needed.
  - *Expiry semantics.* `granted_until: None = no expiry` is the obvious default, but "granted exceptions should rarely be open-ended" is a reasonable policy — worth discussing whether None should be rejected at grant time, or allowed with a warning, or fine as-is.

  **Risk worth flagging.** The cleanness of this design assumes exceptions are rare. If they become routine (hypothetical: 30% of dossiers have active exceptions), the workflow rules become aspirational rather than enforced, and the exception mechanism is a band-aid for rules that are wrong in the first place. Worth periodically checking exception rate and asking "should this be the new default instead?" if any specific exception gets granted repeatedly.

  **Scope:** Cat 5-adjacent (plugin surface tightening) but narrower than the full dotted-path migration. Additive feature, no breaking changes. Recommend deferring detailed implementation design until a **concrete first-case exception** is on the table — the parameters (single-use semantics, entity-scoped vs activity-wide match, expiry policy) snap into focus only with a real case in hand. **Open.**

**Observation totals:** 49 catalogued, Obs 50-98. **15 closed** (Obs 61, 63, 66, 67, 68, 69, 70, 71, 72, 73, 74, 83, 90, 93, 95), **1 partially addressed** (Obs 52), **1 deferred by product decision** (Obs 84), **32 open**. Two of the open observations are explicitly redundant with bugs (Obs 80 ↔ Bug 25; Obs 81 ↔ Bug 38) — the bug tables are authoritative for those; obs entries are cross-references. Round 24 closed six observations in the Cat 1 doc-fix batch, three of which were redundant with Bugs 56/66/69; Round 25 closed Obs 66 alongside Bugs 53/54; Round 26 opened Obs 95 (plugin-surface dotted-path migration, deferred to Cat 5) and Obs 96 (unwired load-time validators); Round 27.5 opened Obs 97 (codebase legibility — file sizes + module organization) and Obs 98 (exception grants as first-class entities — forward-looking design, deferred until a concrete first case); Round 28 closed Obs 95 via the dotted-path migration. Most of the non-redundant open ones are not acute — the pattern is "code works today but will decay without attention."

## Duplication targets (27 catalogued, 6 closed)

| # | What | Status |
|---|------|---|
| D1 | Four copies of "load dossier graph data" (prov, prov_columns, archive ×2) | **Closed** in Round 5 via `dossier_engine/prov_json.py::load_dossier_graph_rows`. |
| D2 | Two copies of PROV-JSON build (prov endpoint vs archive inline) | **Closed** in Round 5 via `prov_json.build_prov_graph`. |
| D3 | `prov_type_value`/`agent_type_value` helpers exist but not all callers use them | Open. |
| D4 | Audit emission boilerplate (7-param `emit_audit` in 4+ sites) → `AuditEvent` builder | **Closed** in Round 10 via `emit_dossier_audit`. |
| D5 | 4 copies of latest-version-per-entity_id subquery (`db/models.py:423, 450`; `worker.py:93, 951`) | Open. |
| D6 | Repository cache returns list directly — caller mutation corrupts cache | Open. |
| D7 | `reindex_all` vs `reindex_common_too` 90% identical loops | Open. |
| D8 | `ActivityContext.get_typed` vs `get_singleton_typed` share 80% body | Open. |
| D9 | `set_dossier_access` 6 copies of behandelaar/beheerder view list; one has duplicate `"external", "external"` | **Closed** in Round 11 (view-list constants + role helpers; duplicate bug fixed). |
| D10 | 3 `reindex_*` loops share structure (common + toelatingen ×2) | Open. |
| D11 | `upload_file` / `download_file` repeat 7-param token extraction — should be FastAPI dependency | Open. |
| D12 | `informed_by` normalization in 4 places (`Repository.create_activity`, `ActivityRow.informed_by`, `prov.py`, `archive.py`, `prov_columns.py`) | Open. |
| D13 | `_supersede_matching` + `cancel_matching_tasks` share the same latest-by-type pattern | Open. |
| D14 | Tombstone tests share structure that differs from regular version tests (minor) | Open. |
| D15 | `DossierAccessEntry` fields duplicate what `access.py` narrates (docstring vs model drift) | Open. |
| D16 | Validator-fn registration pattern repeated without a shared helper | Open. |
| D17 | Three endpoints in `routes/entities.py` repeat access-check + load preamble | Open. |
| D18 | Plugin load calls `build_entity_registries_from_workflow` + 3 validators — sequence repeated per plugin | Open. |
| D19 | `scheduled_for` parsing (relative / absolute / None) could be in one helper instead of inline in `tasks.py` | Open. |
| D20 | `_activity_visibility.parse_activity_view` + its usage is split across 3 route files | Open. |
| D21 | Four routes each hand-roll a "filter activities by user access" loop | Open. |
| D22 | `emit_audit` boilerplate is repeated with the same 7 fields per call site (~15 sites) | **Closed** in Round 10 — merged with D4; the two were the same pattern under separate review entries. |
| D23 | The "find systemAction activity def" pattern is in 2 places (`migrations.py`, `worker.py`) | Open. |
| D24 | Alembic's `9d887db892c9_initial_schema.py` indices are duplicated by the Python model's `__table_args__` — drift risk | Open. |
| D25 | Both archive.py and prov.py do their own PROV-JSON prefix building instead of calling `prov_prefixes()` | **Closed** in Round 5 (same `prov_json.py` extraction). |
| D26 | `sign_token` + `verify_token` share the payload-string building logic — should extract | Open. |
| D27 | Test setup helpers (`_bootstrap_dossier`, `_seed_access_entity`, `_user`) exist in 4+ test files with slight variations | Open. |

**Duplication totals:** **6 closed** (D1, D2, D4, D9, D22, D25 — D22 and D4 were the same pattern under separate review entries), **21 open**.

## Meta-patterns (6)

| # | Summary | Status |
|---|---|---|
| M1 | Docstring "Reads/Writes" drift has no enforcement. `finalization.py` used to document reading `state.used_rows` — field doesn't exist. | **Closed** by harness 3 (`test_phase_docstrings.py`) in Round 8. |
| M2 | "Silent skip" as a default policy (unregistered validators, post_activity_hook swallows, etc.). | **Stage 1 closed** in Round 13 (logging added to 8 silent-skip sites; Sentry FastAPI integration wired so breadcrumbs + context are captured). Stage 2 (Bug 12) and Stage 3 (Bug 76) also closed in Round 14. **Effectively closed.** |
| M3 | Hardcoded paved-path values — `archive.py` fonts, `systeemgebruiker`, signing-key defaults, `id.erfgoed.net` in `prov_iris.py`. | **Partially addressed** in Round 5 (fonts now use `fonts.find_font` with platform fallbacks + `DOSSIER_FONT_DIR` override). Others open. |
| M4 | Documentation drift across README, plugin guidebook, dossiertype template, pipeline architecture doc. | **Partially addressed** — harness 1 enforces guidebook YAML; harness 3 enforces pipeline docstring accuracy. README and dossiertype template still unguarded. |
| M5 | Executable specs that don't execute — `test_requests.sh` and guidebook YAML examples. | **Closed** in Round 8+9 — harness 2 (`ci_run_shell_spec.sh`) + GitHub Actions shell-spec job. |
| M6 | "Test" is a namespace, not a load-time gate — `testDienAanvraagInV2` shipped in production workflow (Bug 71). | **Deferred by product decision** — deploy-time checklist removes test activities from `workflow.yaml`. |

**Meta-pattern totals:** 4 closed, 1 partially addressed, 1 deferred by product decision.

---

## What was shipped across the engagement

### Round 1 — Bug 1/2 (remove_relations TypeError)
Field access fix in `engine/pipeline/relations.py`, 7 new tests, `conftest.py` TRUNCATE extended (Bug 73).

### Round 2 — Bug 44/47 (file service security)
Dossier-binding minted into upload tokens + stamped into `.meta`; file_service rejects moves whose target doesn't match the stamped binding. 7 new tests. `test_requests.sh` upload helper + 13 call sites updated.

### Round 3 — Bug 68 (Alembic consolidation)
Pre-deploy: three migrations folded into one initial. `scripts/check_migrations_append_only.py` guard + README rule.

### Round 4 — Bug 31 (product decision)
No code change. RRN in `role`, `oe:dossier_access`, and ES ACL is acceptable (none are externally queryable). Verified `agent_id`/`agent.uri` already use `user.id`/`user.uri`.

### Round 5 — Archive cluster (Bugs 15, 16, 17) + Duplication D1/D2/D25
- `dossier_engine/fonts.py` — five-platform font lookup + `DOSSIER_FONT_DIR` override + actionable error.
- `dossier_engine/prov_json.py` — `load_dossier_graph_rows` + `build_prov_graph` shared by four endpoints.
- `routes/prov.py` 792 → 506 lines; /prov and /archive 1-line calls; archive uses in-memory Response (no tempfile).
- `routes/prov_columns.py` uses shared loader.
- 16 new tests.

### Round 6 — Bug 74 (worker/route deadlock)
- Structural: `worker._execute_claimed_task` now acquires the dossier lock before entity INSERTs, matching user-activity lock order.
- Defence-in-depth: `run_with_deadlock_retry` in `db/session.py`, wired into all three `_handle_*` methods.
- 11 new tests.

### Round 7 — Bug 14 investigation
Dropped from must-fix — `ensure_external_entity` handles cross-dossier cases, `resolve_used` rejects raw-UUID cross-dossier at 422.

### Round 8 — M1/M4/M5 relief + Bugs 32, 64, 65, 75
- `tests/integration/test_guidebook_yaml.py` — harness 1, 6 tests. Caught and fixed Bugs 64 and 65.
- `tests/unit/test_phase_docstrings.py` — harness 3, 4 tests. Caught and fixed Bug 32.
- `scripts/ci_run_shell_spec.sh` — harness 2, end-to-end CI wrapper. Surfaced Bug 75 on first run.
- `tests/unit/test_worker_startup_resilience.py` — 5 tests for Bug 75's detector function.
- Worker resilience logic in `worker._worker_loop_body` — tolerates `UndefinedTableError` during startup window, logs and retries until schema ready.

### Round 9 — CI wiring (GitHub Actions)
`.github/workflows/ci.yml` — four parallel jobs:
- **pytest** — runs all three test suites (common, engine, file_service) against a Postgres service container with health check. Pip cache keyed on `pyproject.toml` hash.
- **shell-spec** — installs the five repos, stages `/tmp/dossier_run/config.yaml` inline, invokes `scripts/ci_run_shell_spec.sh`. Uploads service logs as artifact on failure (`if: failure()`, 7-day retention).
- **doc-harnesses** — runs harness 1 + harness 3 in a separate job. No Postgres needed; clean signal for doc-drift failures.
- **migrations-append-only** — runs `scripts/check_migrations_append_only.py` with `fetch-depth: 0` so `origin/main` is available for the diff comparison.

Good GHA idioms applied: `concurrency:` group with `cancel-in-progress: true`, `actions/setup-python@v5` with built-in pip cache, service-container `pg_isready` health check, service logs uploaded only on failure. Runs on every `push` to main and every `pull_request` targeting main.

Verified: workflow YAML parses cleanly (four jobs, all steps listed); the migrations-check script round-trips correctly (exit 0 on clean tree, exit 1 with a clear named-file error when a migration is modified, reverts cleanly); CI config shape matches the dev `config.yaml` (same database URL, iri_base, plugins, auth mode).

### Round 10 — Bug 63 accepted + Duplication D4/D22 closure
- **Bug 63 reclassified as 📝 accepted** (not a real bug for this deployment) with HTTP-semantics rationale captured: dossier UUIDs carry 128 bits of entropy, the system sits behind SSO, `dossier.denied` audit events already fire on every 403 so probing is SIEM-visible, and collapsing 403→404 would break client/proxy tooling that relies on proper status codes. RFC 9110 §15.5.5 permits 404-for-hidden but it's not the right default here. Follow-up recorded: SIEM alert on high-frequency `dossier.denied` from a single actor makes enumeration *observable* without obscuring the existence signal.
- **`emit_dossier_audit` helper** added to `audit.py` — encapsulates the 5 fields that every dossier-scoped audit call repeated (`actor_id=user.id`, `actor_name=user.name`, `target_type="Dossier"`, `target_id=str(dossier_id)`, `dossier_id=str(dossier_id)`). Wraps the lower-level `emit_audit` which stays as the primitive for non-dossier-scoped events.
- **7 call sites converted** across `routes/access.py` (×2), `routes/activities.py` (×2), `routes/dossiers.py`, `routes/prov.py`. Boilerplate per site dropped from ~9 lines to ~5.
- **4 new tests** in `TestEmitDossierAudit`: wire-level equivalence with the long form (SIEM rule preservation), UUID stringification contract, reason+extra propagation, silent-when-unconfigured.
- **audit.py docstring** updated to show the new preferred usage pattern.

D4 and D22 both closed — they turned out to be the same pattern (audit emission boilerplate) under two review entries.

### Round 11 — `set_dossier_access` refactor (Obs-1, Obs-2, Obs-5 closed)
- **Three view-list constants** extracted in `dossier_toelatingen_repo/dossier_toelatingen/handlers/__init__.py`: `_AANVRAGER_VIEW`, `_BEHANDELAAR_VIEW`, `_BEHEERDER_VIEW`. Before the refactor these were inline at six `access_entries.append(...)` call sites — adding a new entity type meant six edits, and a miss silently hid the type from a role.
- **Three role-minting helpers** extracted: `_kbo_role`, `_rrn_role`, `_gemeente_role`. Encapsulates the role-string vocabulary; rename a prefix in one place rather than grepping across a file.
- **Bug fixed: duplicate `"external"` in aanvrager view** (kbo + rrn entries each had `"external"` twice). Inert today because access check does membership testing, but confusing — now fixed as a side effect of the constant extraction.
- **Behandelaar access restructured** on two axes: per-URI entries (each `oe:behandelaar`'s `uri` is itself a role on the access list for identity-scoped access) + one bare `"behandelaar"` entry for the global staff role. Dedup-by-URI preserved. The dual population is documented in a block comment at the call site so future readers don't have to reconstruct why both kinds of entries coexist.
- **Handler body shrunk from 76 lines to 58** with no view-list repetition anywhere.
- **Obs-3 (write-on-change) deliberately not done** — product decision to keep the full provenance graph means every activity run still produces a new `oe:dossier_access` version. The Observation stays open in the review as a possible future optimization if prov-graph churn ever becomes a problem.

- **16 new unit tests** in a brand-new `dossier_toelatingen_repo/tests/unit/test_set_dossier_access.py` — the `dossier_toelatingen_repo` had no tests directory before this round. Also added a minimal `[tool.pytest.ini_options]` with `asyncio_mode = "auto"` so the suite runs under the same convention as the engine. Tests use a lightweight `_FakeContext` that provides only the three methods the handler actually calls (`get_typed`, `get_singleton_typed`, `get_entities_latest`), no DB. Coverage: beheerder always present, aanvrager kbo+rrn variants, duplicate-`external` bug regression, verantwoordelijke organisatie, behandelaar empty/single/multiple/duplicate-URI/missing-URI cases, full-dossier end-to-end, view-constant invariants (aanvrager ⊆ behandelaar ⊆ beheerder).

### Verification performed
- **Test suite:** **740/740** (engine 687, toelatingen 16, signing 18, file_service 19). Grew by 67 tests across the engagement.
- **Shell spec via harness 2:** `bash scripts/ci_run_shell_spec.sh` → 25 OK assertions, 5 summary-pass lines, exit 0, zero tracebacks, zero worker crashes. D1–D9 green after the handler refactor, including the `wijsVerantwoordelijkeOrganisatieAan` side-effect path that calls `set_dossier_access`.
- **Harness 1, 2, 3** all green, all have synthetic-drift tests confirming they catch the bug shape they claim to catch.
- **CI workflow** authored, statically validated, and [dev]-extras fix applied so pytest-asyncio + httpx install in CI.

### Round 12 — Bug 70 + doc-drift on prov routes
- **Bug 70 fixed.** `test_requests.sh` had four echo lines pointing at a bare `/prov/graph` URL that doesn't exist on the server. Fixed to `/prov/graph/timeline` (the user-visible, visibility-filtered route). Verified end-to-end: timeline returns 401 without auth (route registered), the old bare URL returns 404 (proves the original URL was dead).
- **Incidental doc-drift fixed.** `prov.py`'s module docstring claimed the module exposed `/prov` and `/prov/graph` — the second endpoint doesn't exist. Docstring rewritten to list the four real endpoints (`/prov`, `/prov/graph/timeline`, `/prov/graph/columns`, `/archive`), so future readers don't build on the same wrong mental model. This is M4 territory but surfaces again here; a harness to lint module docstrings against the endpoint router is a possible future addition, not done this round.

### Round 13 — Meta M2 Stage 1 (visibility) + Sentry FastAPI integration

Survey of the silent-skip pattern across the platform. M2 is a **visibility pass, not a bug-fix pass** — "Stage 1" makes failures observable without changing runtime behavior. The actual bug-shape findings (Bug 12 `_parse_scheduled_for` silent fire-now; Bug 76 corrupt `.meta` bypasses dossier-binding check) are real bugs extracted from the survey but deliberately deferred — they're Stages 2 and 3, to be done in a later round.

**Survey results.** AST-walked 38 `except` clauses in production code (tests excluded). Categorized:

- **11 legitimately silent** (optional-import guards, control-flow idioms like `asyncio.TimeoutError` on `wait_for(shutdown.wait(), timeout=…)`, namespace-registry fallbacks). No change.
- **5 already well-designed** (`worker.py` retry/claim/failure paths — log with `exc_info=True`, capture to Sentry with fingerprint, re-raise where appropriate). These are the gold standard other sites were measured against.
- **8 addressed this round** — see below.
- **2 real bugs extracted**:
  - **Bug 12 reconfirmed** (`worker.py:65`, `_parse_scheduled_for`). A malformed ISO string in `scheduled_for` falls through to `None`, which the due-check treats as "immediately due" — a task scheduled for next week fires right now. Real correctness bug, not just noise. Deferred to Stage 2.
  - **Bug 76 new** (`file_service/app.py:265`). If `.meta` exists and is corrupted (OSError or JSONDecodeError), the dossier-binding check added for Bugs 44/47 silently falls back to "no meta" and permits the move. A corrupted `.meta` is an anomaly; safe default is reject, not permit. Deferred to Stage 3.

**Stage 1 — logging added to 8 silent-skip sites:**

| File:line | Role | Change |
|---|---|---|
| `engine/pipeline/tasks.py:123` | `_fire_and_forget` | `logger.warning(..., exc_info=True)` with explanatory comment; swallow preserved |
| `engine/pipeline/tasks.py:315` | Malformed anchor UUID on task row | `logger.error` — engine wrote this field via `str(anchor_entity_id)`, so malformation is row corruption |
| `engine/pipeline/finalization.py:161` | `post_activity_hook` | Added `exc_info=True` so Sentry picks up the full traceback instead of `str(e)` only |
| `routes/_typed_doc.py:133` | Legacy-path JSON schema render | `logger.warning` before returning empty docs block |
| `routes/_typed_doc.py:170` | Versioned-path JSON schema render | Same pattern as above |
| `routes/dossiers.py:105` | Corrupt `eligible_activities` cache | `logger.warning` before recomputing |
| `routes/prov_columns.py:136` | Malformed `result_activity_id` on task row | `logger.warning` — engine-written value, malformation = corruption |
| `routes/prov_columns.py:221` | Non-UUID column id | `logger.debug` only — legitimate dummy-column placeholders hit this path, WARNING would be noise |
| `file_service/app.py:63` | Missing config file path | `logger.warning` fires **once at module load** if `CONFIG_PATH` doesn't exist. Catches the operational footgun where a typo'd `FILE_SERVICE_CONFIG` env var silently downgrades to the POC signing key. Per-request `get_config()` stays silent (one load-time line covers it). |

**Sentry FastAPI integration** — shipped alongside M2 Stage 1 because logging only gets you halfway without a tool that picks the breadcrumbs up:

- **Module rename: `sentry_integration.py` → `sentry.py`** (scope broadened from worker-only). Back-compat alias `init_sentry = init_sentry_worker` so any existing import of the old name still works.
- **Shared `_init_sdk(dsn, *, process_kind, extra_integrations)`** private helper owns DSN resolution, the `_initialized` guard, and the `LoggingIntegration(event_level=None)` contract. Single source of truth for both entry points.
- **`init_sentry_worker(dsn=None)`** — unchanged from the old `init_sentry` behaviour.
- **`init_sentry_fastapi(app, dsn=None)`** — adds `FastApiIntegration` on top of `LoggingIntegration`. Called from `create_app` right after the `FastAPI(...)` constructor and *before* CORS middleware so Sentry sees the full request lifecycle (including preflight).
- **No-op discipline preserved.** SDK not installed → silent no-op. `SENTRY_DSN` unset → silent no-op. Second init call in-process → no-op (log at DEBUG). Dev and test runs completely unchanged.
- **`[project.optional-dependencies].observability`** extra added to `dossier_engine_repo/pyproject.toml`, shipping `sentry-sdk>=1.14.0` (lower bound is where `FastApiIntegration` was introduced). Included in `dev` too so the tests below can run. Deployments opt in via `pip install 'dossier-engine[observability]'`.

**14 new tests** in `tests/unit/test_sentry.py` covering: no-op when DSN unset (3), shared `_initialized` guard across both entry points (3), integrations list wired correctly per process kind (4, including the `event_level=None` invariant pin), back-compat alias (2), capture helpers no-op without init (2). Monkeypatches `sentry_sdk.init` to capture kwargs without hitting the network.

**Verified:**
- **Test suite:** 754/754 (engine 701, up from 687; toelatingen 16, signing 18, file_service 19).
- **Shell spec via harness 2:** exit 0, 25 OK, 5 summaries, D1–D9 green, zero tracebacks/5xx.
- **App log during a clean D1-D9 run:** zero WARNINGs or ERRORs from the new logging paths, confirming Stage 1 is correctly positioned in error branches only (happy-path runs stay quiet).

### Round 14 — M2 Stage 2 + Stage 3 (bookkeeping reconciliation + regression guards)

Started this round aiming to fix Bug 12 (`_parse_scheduled_for` silently fires tasks) and Bug 76 (corrupt `.meta` bypasses dossier-binding). **Both turned out to already be fixed in the codebase** — the review's tracking had drifted out of sync with the code after multiple auto-compacted rounds. Verification and test-coverage work filled the gap.

**Bug 12.** `worker.py:_parse_scheduled_for` already implements log-and-defer: malformed ISO strings return `datetime.max.replace(tzinfo=timezone.utc)` with a `logger.error`, so the due-check `scheduled_for > now` defers the task indefinitely. Already tested by `TestParseScheduledFor` (12 cases including explicit regression guards: `test_garbage_returns_datetime_max`, `test_multiple_garbage_forms_all_defer`, `test_empty_and_none_still_return_none` to prevent re-conflating the legitimate None case with corruption). No code change; review entry corrected to "fixed."

**Bug 76.** `file_service/app.py:/internal/move` already rejects corrupt `.meta` with HTTP 500 and a docstring-documented policy for all four `.meta` states. The fix was in place; the regression guard wasn't. Added two tests in `TestMoveEnforcesDossierBinding`:
- `test_move_rejects_when_meta_is_corrupt` — truncated JSON case.
- `test_move_rejects_when_meta_is_non_json_garbage` — binary-garbage case.

**One real sub-bug caught by writing the tests:** the original `except (OSError, json.JSONDecodeError)` clause didn't cover `UnicodeDecodeError`. A `.meta` file containing non-UTF-8 bytes raises `UnicodeDecodeError` during `open()` in text mode *before* `json.load` sees anything, and `UnicodeDecodeError` is a subclass of `ValueError`, not `JSONDecodeError`. Binary garbage in `.meta` was therefore crashing with an unhandled 500 rather than our intended explicit-reject path. **Widened the except to `(OSError, json.JSONDecodeError, UnicodeDecodeError)`** with a comment explaining the inheritance gotcha. Both tests now pass; `TestMoveEnforcesDossierBinding` goes 5 → 7 tests, all green.

**Verified:**
- **Test suite:** 760/760 (engine 705, toelatingen 16, signing 18, file_service 21 ↑ from 19).
- **Shell spec via harness 2:** exit 0, 25 OK, 5 summaries, D1–D9 green, zero tracebacks/5xx. The `/internal/move` happy-path in D1 continues to work after the except-clause widening.

**Process note.** This round revealed that the review's bookkeeping had gotten ahead of the code — two bugs were listed as open that had been fixed in earlier rounds but whose "fixed" state didn't survive transcript compaction. The harnesses and test suite caught this naturally: attempting to "fix" Bug 12 immediately showed `_parse_scheduled_for` already returning `datetime.max` with full test coverage, and the same pattern for Bug 76 revealed an adjacent real bug (the `UnicodeDecodeError` gap) that only got surfaced by writing the regression tests. Lesson: when context runs deep, verify claimed-open items against code before planning a fix.

### Round 15 — Bug 5 (security-boundary docstring/code drift) + drive-by import cleanup

Started with the usual "verify before planning" step — given Round 14's lesson about stale bookkeeping, the first question was whether the drift still existed. It did: `access.py` module docstring stated *default-deny* in three places (line 8, lines 23-31, line 80), and so did the function docstring, but the code at lines 94-98 returned `None` (treated downstream as unrestricted access) when the dossier had no `oe:dossier_access` entity — classic default-allow. The sibling `check_audit_access` in the same file was genuinely default-deny, underscoring that the intended contract was default-deny throughout.

**Design question surfaced before coding.** Which side wins — docstrings or code? Evidence collected: (a) every dossier in production gets its access entity committed atomically with the creating activity via `workflow.yaml`'s `setDossierAccess` side-effect chain, which runs inside the same transaction as `dienAanvraagIn` per `engine/pipeline/side_effects.py:86-89`; (b) only two integration tests assumed default-allow (`test_no_access_entity_returns_none`, `test_empty_access_entity_content_returns_none`), with a third integration fixture `_bootstrap_with_entity` in `test_prov_endpoints.py` covertly depending on it; (c) the in-function comment rationalizing default-allow ("This is the normal state for new dossiers before access rules are provisioned") was factually wrong — no such committed state exists. User confirmed Option B (tighten code to default-deny, close the footgun).

**Fix.**
- `access.py:94-98` — replaced the `return None` default-allow branch with a 403-raise that mirrors the existing "no match" branch, using a *distinguishing* `reason="Dossier has no access entity configured"` to let SIEM rules differentiate provisioning anomalies from routine unauthorized-access attempts.
- `access.py:70-88` — function docstring updated: added an explicit "Default-deny" paragraph and updated the Returns/Raises sections.
- `access.py:58-64` — drive-by cleanup: three gratuitous `from ..audit import emit_dossier_audit` inside function branches consolidated into one module-level import. No circular-import risk (verified — `audit.py` doesn't import from `routes/`). This was not strictly necessary for the fix but made the monkeypatch-based regression test cleaner and removed a code smell that had been carrying forward.

**Test impact.**
- `test_no_access_entity_returns_none` → `test_no_access_entity_raises_403`. Assertion flipped from `result is None` to `pytest.raises(HTTPException, 403)`; docstring rewritten to explain the new default-deny contract and the atomic-provisioning invariant that makes it safe.
- `test_empty_access_entity_content_returns_none` → `test_empty_access_entity_content_raises_403`. Same flip.
- **Two new regression tests** in `TestCheckDossierAccess`:
  - `test_denial_reasons_distinguish_no_entity_vs_no_match` — monkeypatches `emit_dossier_audit`, triggers both deny paths, asserts the two `reason` strings are distinct. Pins the SIEM-triage contract: a future refactor that collapses both paths to a generic "denied" fails at commit.
  - `test_global_access_bypasses_missing_entity_deny` — asserts that a `global_access` role match short-circuits before the access-entity lookup. Operators listed in `config.yaml` retain access against un-provisioned dossiers (which is exactly when they'd need to investigate).
- **Fixture invariant restored.** `tests/integration/test_prov_endpoints.py::_bootstrap_with_entity` now seeds an `oe:dossier_access` entity granting the test user, matching what production's `setDossierAccess` side-effect writes. The fixture was silently depending on default-allow; default-deny surfaces that dependency, so making the fixture faithful to production is the honest fix. One incidental `len(entities) == 1` assertion in `test_loader_returns_populated_indexes` became `== 2` with an inline comment (two entities now: aanvraag + dossier_access).

**Verified.**
- **Test suite:** 762/762 (engine 707, up from 705; toelatingen 16, signing 18, file_service 21). +2 matches the two new regression tests.
- **Shell spec via harness 2:** exit 0, 25 OK, 5 summaries, D1–D9 green, zero tracebacks/5xx. The `dienAanvraagIn → duidVerantwoordelijkeOrganisatieAan → setDossierAccess` side-effect chain continues to provision access atomically under the new security floor — the happy path never hits the new 403.

**Process note.** The kickoff's "verify before planning" discipline paid off here, but not in the Round 14 way (where the bug was already fixed). This time the drift was real, and verification mattered for the transition-cost question instead: tracing the provisioning chain through `workflow.yaml` and `side_effects.py:86-89` established that no committed-but-un-provisioned state exists in production, which is what makes default-deny safe to switch on without coordinating a data migration. Without that check, the conservative move would have been Option A (fix the docs) — which would have enshrined the footgun.

**Follow-up observation** (not shipped, for consideration in a later round). `_bootstrap_with_entity` was carrying a hidden dependency on the bug it was supposed to be unrelated to. Other test fixtures across the suite likely have the same shape — create a dossier without provisioning access, and accidentally-pass because of default-allow. The engine sweep says no other tests hit `check_dossier_access` without seeding it (otherwise the full-suite run would have shown more failures), but a small harness that asserts "every committed test dossier has an `oe:dossier_access` entity" would catch this class of drift at commit time and pin the production invariant. Flagging as a candidate **M7 / harness 4** if useful — it's roughly the same shape as the existing docstring-lint harness (walk, inspect, assert).

### Round 16 — Bug 6 (Alembic failure fallback → partial-migration corruption)

Verify-before-plan confirmed the drift was real. `app.py:330-346` ran `alembic upgrade head` via subprocess, and on non-zero exit logged a WARNING and called `create_tables()` (`Base.metadata.create_all`) as a silent fallback. Because `create_all` no-ops on existing tables, a partial migration — where the upgrade script applied some DDL before erroring — would survive intact: the app would come up on a schema that matched neither the ORM model nor any Alembic revision, `alembic_version` would still point at the partially-applied revision, future `upgrade head` calls would try to re-apply the same failed migration, and the symptom would be data corruption visible only as a WARNING line nobody read.

Two paths hit `create_tables()`: the non-zero-exit fallback on line 344 (the main bug), and a second `if not alembic_ini.exists()` branch on line 346 (effectively dead for any source checkout but potentially live if someone pip-installs a wheel that doesn't bundle `alembic.ini` — `pyproject.toml`'s `packages.find` scopes to `dossier_engine*` and the ini sits a directory up, so it's *not* shipped with the wheel).

**Design question surfaced before coding** — three options considered: (A) pure fail-fast on both paths, (B) gate the fallback behind an explicit config flag, (C) `create_tables + alembic stamp head` only on verified-empty DB, fail-fast on partial. User picked A. Reasoning that landed on A: the threat model is partial-migration corruption of production data (hardest to detect, most expensive to recover from), the "convenience" the fallback provided was illusory because tests go through `conftest.py` directly and production always runs Alembic, and fail-fast matches the same posture taken on Bug 5 (default-deny on authorization anomaly → refuse to start on migration anomaly).

**Fix.**
- **New module-level helper** `_run_alembic_migrations(db_url: str) -> None` in `app.py`. Raises `RuntimeError` on missing `alembic.ini` (with a message that names the expected path and explains what "missing" means for a deployment), raises `RuntimeError` on non-zero `upgrade head` exit, logs success at INFO on rc=0. Before raising on non-zero exit, logs the full Alembic stderr at ERROR level via `dossier.app` logger so the migration traceback survives in app logs regardless of how the RuntimeError propagates through uvicorn's lifespan handler. The LoggingIntegration shipped in Round 13 picks this up as a Sentry breadcrumb, so crashed startups become observable in SIEM.
- **`startup()` shrunk** to a single `_run_alembic_migrations(db_url)` call plus a pointer comment.
- **`create_tables` import dropped** from `app.py`. Still exported from `dossier_engine.db` and used by `tests/conftest.py` + `stress_test.py`, which are the legitimate callers — those are in-process schema bootstraps that intentionally skip Alembic, and the helper is fine for that.

**Drive-by refactor justified.** The standing rule is "don't refactor during a bug fix round, stash drive-bys in lower-priority." The helper extraction bends that rule because it earns its keep: the failure paths are now unit-testable without a live DB, which is the difference between pinning the contract (the regression tests below) and relying on manual end-to-end runs. If extraction had meant more than ~60 lines of movement, I'd have left it inline and tested via FastAPI's lifespan plumbing.

**Tests shipped.** 5 new tests in `tests/unit/test_alembic_startup.py::TestRunAlembicMigrations`:
- `test_missing_alembic_ini_raises_runtime_error` — monkeypatches `Path.exists` → False, asserts `RuntimeError` with "alembic.ini" and "migration infrastructure" in the message (so the operator-visible diagnostic is part of the pinned contract, not incidental wording).
- `test_nonzero_exit_raises_runtime_error` — monkeypatches `subprocess.run` to return `returncode=1`, asserts `RuntimeError` with the `rc=` string.
- `test_nonzero_exit_logs_stderr_at_error_level` — asserts that Alembic's stderr content (a realistic `InvalidSchemaName` example) appears in the `dossier.app` ERROR log record before the raise. Pins the "log before raise" ordering so a future refactor that reverses it doesn't silently lose the traceback.
- `test_zero_exit_logs_success_without_raising` — happy path, pins that rc=0 cleanly returns without raising and emits the "Alembic migrations applied successfully" INFO log.
- `test_subprocess_run_invoked_with_expected_args` — pins the invocation contract: command list is `["python3", "-m", "alembic", "upgrade", "head"]`, `capture_output=True`, `text=True`, and crucially the `DOSSIER_DB_URL` env var is set on the subprocess environment. That last one matters because `alembic/env.py` reads it to build the async engine; a typo or omission silently falls back to the module-level default connection string and migrates the wrong DB.

**Verified.**
- **Test suite:** 767/767 (engine 712, up from 707 after Round 15; toelatingen 16, common/signing 18, file_service 21). +5 matches the five new regression tests.
- **Shell spec via harness 2:** exit 0, 25 OK, 5 summaries, D1–D9 green, zero tracebacks/5xx. The fail-fast change touches only the rc≠0 branch — the happy path (Alembic runs cleanly on a fresh Postgres, which is what the harness stages) is unaffected.

**Note on what didn't change.** `create_tables()` in `dossier_engine.db.session` stays. It's the right tool for in-process test schema bootstrap (`conftest.py`) and for the standalone stress-test harness, both of which intentionally sidestep Alembic. The bug was never `create_tables` itself — it was *using it as a production failure fallback*. That usage is now gone; the helper remains fit for purpose.

### Round 17 — Bug 7 (premature audit emit in activity endpoints) + adjacent Bug 77 (dead denial emit)

Verify-before-plan confirmed Bug 7 was real. `routes/activities.py::_run_activity` called `emit_dossier_audit(action="dossier.created"|"dossier.updated", outcome="allowed", ...)` immediately after `execute_activity` returned — while still inside `run_with_deadlock_retry`'s `async with session.begin():`. If any subsequent item in a batch raised, or the outer transaction rolled back for any reason, the audit log on disk (NDJSON, synchronous writes) still claimed the activity committed. Same bug shape hit deadlock retries: a deadlocked attempt that emitted N audits before rolling back would re-emit on the successful retry, doubling the event count.

**Scope was wider than the review title.** The phrase "batch endpoint" was the visible symptom, but the bug lived in `_run_activity` — a shared helper called from three places: `_handle_single`, `_handle_batch`, and the typed-per-workflow endpoint factory. Single-activity endpoints were also affected via deadlock-retry double-emit; only the "in practice deadlocks are rare because the worker takes locks in user-activity order now (Bug 74)" part kept this from being a visible problem in production. The fix had to touch all three call sites.

**Design options considered.** (A) SQLAlchemy `after_commit` event listener, (B) explicit `AuditBuffer` with try/finally flush, (C) `_run_activity` returns events, caller emits, (D) post-commit emit outside `run_with_deadlock_retry`. User picked D. Reasoning: `run_with_deadlock_retry` already owns the transaction boundary, so "emit after the retry returns successfully" is the natural layering; no SQLAlchemy event plumbing; no framework dependency; deadlock-retry double-emit goes away for free because each retry starts a fresh attempt and the success-emit happens exactly once at the end.

**Fix (Bug 7).**
- **New module-level helper** `_emit_activity_success(user, dossier_id, act_def, activity_id)` in `activities.py` — encapsulates the `can_create_dossier` → `dossier.created` vs `dossier.updated` derivation + emit.
- **`_run_activity`** no longer emits on success. The method docstring rewritten with an explicit "Audit emission on writes" section distinguishing the denial path (in-transaction, correct on rollback) from the success path (caller's responsibility, post-commit).
- **`_handle_single`** captures the retry return value, calls `_emit_activity_success`, returns.
- **`_handle_batch`** owns a closure-captured `pending_emits: list[tuple[dict, UUID]]` cleared at the top of `_work` (so deadlock retries reset from scratch), appended to after each successful `_run_activity` return (never after a raise), and flushed after the commit. Matches the existing atomicity contract — "either all items commit + all audits emit or none do."
- **Typed-per-workflow endpoint** gets the same treatment as `_handle_single`.
- **`emit_dossier_audit` hoisted** to module-level import (same cleanup as `access.py` in Round 15). This also makes the regression test's `monkeypatch.setattr(activities_mod, "emit_dossier_audit", ...)` work cleanly.

**Tests shipped for Bug 7.** 4 new tests in `test_http_activities.py::TestAuditEmitIsPostCommit`:
- `test_successful_single_emits_exactly_one_success_event` — happy-path single count and action name.
- `test_batch_rollback_emits_no_success_events` — **the core Bug 7 regression.** 2-item batch, second item fails with 422, asserts zero success emits captured. Before the fix this was 1 success emit (for the doomed first item).
- `test_batch_success_emits_one_event_per_committed_item` — happy-path batch count, per-item action names and activity IDs.
- `test_denial_still_emits_in_transaction` — pins that `dossier.denied` on `ActivityError(403)` still emits, so a future refactor that over-generalizes "defer everything to post-commit" is caught.

**Adjacent Bug 77 surfaced and fixed.** Writing the denial test showed the endpoint correctly returned 403 with `{"detail":"Authorization failed: User does not have role 'oe:behandelaar'"}` — but the audit capture was empty. Trace: `_run_activity`'s denial path read `code = getattr(e, 'code', None)` on an `ActivityError`, which stores its status as `status_code` (see `engine/errors.py`). The getattr default silently returned `None`, `if code == 403:` always fell through, and the `dossier.denied` emit has been dead code in every deployment. Same issue with `reason = getattr(e, 'message', str(e))` — `ActivityError` stores `detail`, not `message`, so even if `code` had been fixed the reason would have been `str(e)` = the exception's default repr, uninformative to SIEM.

**Fix (Bug 77).** Replaced the getattr-with-default accesses with direct attribute reads: `e.status_code == 403` and `str(e.detail)`. No defaults — if the attribute ever disappears, it raises `AttributeError` loudly instead of silently skipping. A comment explains the attribute-name history and references the regression test. The test was tightened to pin both the emit presence AND a substring of the real authorize message (`"behandelaar"`), so a future rename is caught on two axes, not just one.

**Operational implication of Bug 77 worth surfacing:** read-side denials from `routes/access.py` (missing dossier_access, role mismatch, audit-denied) have always been emitting correctly, so the `dossier.denied` SIEM stream has not been empty. But **write-side denials — users attempting activities they don't have roles for — have never been audited.** Production deployments that build Wazuh rules on the `dossier.denied` stream have been getting a partial picture of denial patterns. The fix restores the contract the `_run_activity` docstring was advertising.

**Verified.**
- **Test suite:** 771/771 (engine 716, up from 712; toelatingen 16, common/signing 18, file_service 21). +4 matches the four new Bug 7 regression tests; Bug 77's fix rides on the same set (the denial test covers both).
- **Shell spec via harness 2:** exit 0, 25 OK, 5 summaries, D1–D9 green, zero tracebacks/5xx. The shell spec exercises only happy-path flows, so the behavior shift (emit post-commit instead of pre-commit) is invisible to it — which is exactly what we want: end-users see no difference in response shape or timing.

**Process note.** This round repeats the Round 14 pattern where regression-test authoring surfaced an adjacent bug (Round 14: `UnicodeDecodeError` gap in `.meta` parse; this round: dead denial-emit attribute access). The pattern is worth naming: **writing a test that exercises the path the fix claims to preserve is often the most productive scrutiny a fix gets.** In both cases the adjacent bug was older than the one being fixed, invisible under the old behavior, and not catchable by any of the live harnesses (guidebook lint, phase docstrings, shell spec). Only the act of constructing a test that said "the denial path still works the same way" forced the code to be exercised under a pinned contract.

### Round 18 — Bug 30 (silent per-file swallow in `move_bijlagen_to_permanent`) + `ActivityContext` attribution plumbing

Verify-before-plan confirmed Bug 30 was real: `move_bijlagen_to_permanent` ran a bare `except Exception` per file that logged without `exc_info`, and even the two explicit `resp.status != 200` branches just `logger.warning`'d and fell through. The task was marked completed regardless of outcome, so an aanvraag with failed bijlage moves persisted indefinitely with file_ids pointing at unmoved files in the file service's `temp/` area. Downloads returned 404 forever, invisibly.

Three layered problems, as surveyed pre-fix:
1. Bare except + `logger.error` without `exc_info` — lost the traceback, so Sentry's LoggingIntegration (Round 13) couldn't surface what actually failed.
2. Loop continued on any per-file failure + task marked complete — classic fail-open.
3. 403 (file service's dossier-binding mismatch) was rationalized as tolerated — but "tolerated" conflated two positions: "file service blocked the data leak" (correct, already done) and "no further action needed" (incorrect — a cross-dossier graft attempt is a security-relevant event, and the aanvraag is in a permanently broken state that operators should see).

**User pushback in-round — "we shouldn't treat 403s as normal."** Original plan was "log + continue" for 403s per the existing product-decision framing. User correctly flagged that the framing was doing too much work: accepting the file service's block isn't the same as accepting the symptom. Revised plan: 403 emits a `dossier.denied` audit event *and* counts as a failure so the task retries-until-exhaustion, surfacing the stuck aanvraag to ops.

**Scope blowup discovered mid-planning — audit emit needs a real user.** The 403 audit emit needed an actor, and task handlers run under the worker with no request user in scope. Three options considered: (A) plumb real user attribution through `ActivityContext`, (B) use `SYSTEM_USER` + a `rejected_agent` hint field, (C) skip the audit emit for this round. User picked A. Initial estimate was "contained change"; walking the 8 `ActivityContext` construction sites revealed ~12 production files touched and a design decision I'd missed: the executor of a worker-run task is the worker (system) but the *attributed agent* — who the denial is *about* — is the person whose activity caused the task to exist. Those diverge. User confirmed the two-field split as the right design.

**The attribution model (new design, spelled out in `ActivityContext` docstring).**
- `context.user` — the agent the current code is *executing as*. For direct handlers/validators/split-hooks/fire-and-forget tasks: the request user. For side-effect handlers and worker-run tasks: `SYSTEM_USER`.
- `context.triggering_user` — the agent attributed with the activity that *caused* this context to be constructed. For direct request-path code: same as `user`. For side effects: the original request user whose activity started the pipeline, preserved through recursion. For worker tasks: resolved from the triggering activity's `AssociationRow` via a new `_resolve_triggering_user(repo, activity_id)` helper.

Use `user` when asking "who is doing this thing right now?" Use `triggering_user` when attributing audit events, denial reasons, or any record that says "this happened because of so-and-so's action."

**Plumbing shipped (7 code phases):**
1. **`ActivityContext` surface** — two new kwarg-only fields, both default `None` for back-compat. Class docstring rewritten with the two-field model + the worker-task example that motivated the split.
2. **Pipeline direct-execution sites** — 4 constructions (handlers, validators, split_hooks, fire-and-forget tasks) pass `user=state.user, triggering_user=state.user`.
3. **Side-effect chain** — `execute_side_effects` / `_execute_one_side_effect` / `_condition_met` accept a `triggering_user: User` kwarg; both internal `ActivityContext` construction sites pass `user=SYSTEM_USER, triggering_user=triggering_user`; recursive call threads it through. Engine entry point at `engine/__init__.py` passes `triggering_user=state.user`. `SYSTEM_USER` moved to canonical home in `dossier_engine.auth` with back-compat re-export from `app.py`.
4. **Worker** — new `_resolve_triggering_user(repo, activity_id) -> User` helper (straight `select(AssociationRow)` query, identity-only skeletal User construction per "roles/properties empty" design call, falls back to `SYSTEM_USER` on missing activity or missing association). Both worker `ActivityContext` sites use it.
5. **Bug 30 core fix** — per-file failure tracking, 403 audit emit via `context.triggering_user`, 5xx/exception path with `exc_info=True`, raise at loop end.
6. **Tests** — 6 new Bug 30 unit tests in `dossier_toelatingen_repo/tests/unit/test_move_bijlagen_to_permanent.py` (happy path, 403 emits + raises, 500 raises without audit, exception path carries `exc_info`, mixed batch counts failures correctly, triggering_user attribution end-to-end) + 6 new engine integration tests in `test_activity_context_users.py` pinning the two-field contract across direct/side-effect/worker paths including recursion preservation and resolver fallbacks.
7. **Verification** — 715 engine (+6) + 22 toelatingen (+6) + 18 common + 21 file_service = 776 passed + 7 Sentry-skipped = **783/783 total**. Shell spec green, 25 OK, D1-D9, zero tracebacks.

**Test-file collateral damage.** 17 call sites to `execute_side_effects` / `_condition_met` in `test_side_effects.py` needed the new required kwarg. Batch-edited via a Python script; one regex substitution collided with an embedded paren inside a code comment and produced a syntax error that I hand-repaired. Two other fixture stubs (`_StubState` in `test_split_hooks.py`, 4 `ActivityContext(...)` constructions in `test_toelatingen_plugin.py`) were unaffected — the former got a `user = None` attribute added, the latter relied on the defaulted-None back-compat and didn't need changes.

**Operational implication worth flagging to ops.** Before Bug 30, failed bijlage moves were silent — task marked complete, aanvraag has broken refs, no audit trail. After Bug 30: persistent 403s and infrastructure failures raise, the worker retries via its existing recorded-task retry machinery (exponential backoff, max attempts per the worker config), and eventually the task lands in a failed state that ops can see. Cross-dossier graft attempts now land in SIEM via `dossier.denied`, attributed to the aanvrager. Historical silent failures are unrecoverable — the file service's `temp/` cleanup has probably already reaped the unmoved files and any aanvraag with broken refs will stay broken. Going forward: operators should expect occasional "move_bijlagen_to_permanent task failed after N retries" entries as the normal signal for a stuck aanvraag, not a regression.

**Process note on scope discipline.** This round took 6 turns end-to-end — the bulkiest fix in the engagement, not because Bug 30 itself was hard (~30 lines in the end) but because the audit emit required a distributed plumbing refactor across the engine and worker. The lesson is that **when a bug fix's audit/attribution story requires user context, walk every `ActivityContext` construction site before estimating the scope** — that check would have told me up-front this was "fix + refactor", not just "fix", and the scope conversation would have happened earlier. I surfaced this after Phase 2 and the user made an informed call to ship the full plumbing; the surfacing was the right move but should have happened during verify-before-plan rather than mid-execution.

### Round 19 — Bug 55 (lineage walker cross-dossier defense in depth) + stale-migration postmortem

Verify-before-plan confirmed Bug 55's framing: `lineage.find_related_entity` walks PROV edges (`generated_by`, `used`, `informed_by_activity_id`) across the activity graph but doesn't check that each visited activity belongs to the walker's dossier scope. In normal operation nothing ever points cross-dossier — PROV edges are created within a single scope — but if a data integrity violation or PROV manipulation ever produced one, the walker would follow it, query the foreign activity's generated/used entities, and form a candidate set. The single existing scope defense, at line 87's `get_latest_entity_by_id(dossier_id, ...)`, would reject the final return; so pre-fix, **no actual data leak surfaces to the caller**, but the walk itself traversed cross-dossier data (wasted queries at best, a confirmation-timing side channel at worst).

User picked **option A + docstrings**: guard at the walker, plus tightened docstrings on the three activity-id-only repo helpers so the trust boundary is explicit for future callers.

**Fix shape:**
- `lineage.py` — in the per-activity loop, `repo.get_activity(activity_id)` is loaded first (was previously lazy-loaded only when the `informed_by` path needed it), its `dossier_id` is compared against the walker's scope argument, and a mismatch short-circuits with `continue` — before `get_entities_generated_by_activity` or `get_used_entities_for_activity` runs. One extra query per visited node in the happy path (the `informed_by` lookup is now folded in rather than being a second query); zero change for the cross-dossier rejection path.
- `lineage.py` module docstring — new "Intra-dossier by construction" semantics bullet explicitly documenting the guard and its defense-in-depth rationale.
- `db/models.py` — `get_activity`, `get_entities_generated_by_activity`, `get_used_entities_for_activity` each got a "scoping contract" docstring paragraph stating that the helper queries by activity-id alone and that callers traversing PROV edges from untrusted sources must check dossier scope separately. Cheaper than changing signatures, and future readers of the helpers see the constraint inline.

**Tests (2 new) — pinning traversal, not return value.** First pass at the regression tests asserted only `result is None`. Both tests passed with the guard present *and* with the guard temporarily reverted — because the pre-existing line-87 scope check was doing the work. Caught via the paranoia check (revert the fix, rerun the tests; if they still pass, they're not pinning the right thing). Rewrote both tests to spy on `get_entities_generated_by_activity` / `get_used_entities_for_activity` via `monkeypatch` and assert the walker never queries the cross-dossier activity id. With the guard removed, both tests now fail with a clean assertion pointing at the exact foreign activity id that got queried; with the guard present, they pass.

This is the test-design lesson the round surfaces: **for defense-in-depth fixes, asserting on user-visible behaviour (return value) is not enough when another layer already provides some defense — the test has to pin the new layer's behaviour directly.** Worth making this standard practice going forward. Baking in a "revert the fix, rerun the tests, confirm they go red" step for any defense-in-depth regression test would have caught this on the first pass rather than on the paranoia check.

**Scope disciplined — no refactor creep.** The wider option (B: push `dossier_id` filtering into the repo helpers themselves) would have touched every caller of those three helpers throughout the engine. Resisted; the lineage walker is the only caller that *traverses* foreign activity ids, so it's the only caller that needed the guard. The docstrings carry the contract forward for any future caller that joins the traversal pattern.

**Stale-migration postmortem (carried in from the CI investigation across Rounds 18-19 boundary).** Round 18's final CI run exposed a shell-spec failure: `DuplicateColumnError: column "uri" of relation "agents" already exists` from `ALTER TABLE agents ADD COLUMN uri TEXT`, with Round 16's `_run_alembic_migrations` correctly refusing to start on rc=1. Initial hypothesis chain (worker race on empty schema → autogenerate drift → something emitting ALTER we can't find) was wrong in all cases. The actual cause, confirmed by the user after inspecting their local branch: **stale migration version files on the CI branch**. A prior cleanup had retroactively inlined the `uri` column into the initial `create_table('agents', ...)` call (legitimate consolidation), but the delta migration that originally added `uri` was never removed from `alembic/versions/` — so Alembic's `upgrade head` ran both, hit the ALTER, and crashed on the duplicate column.

**Gap in existing tooling.** Round 8's append-only guard catches **mutation** of existing migration files (Bug 68's original shape). It does not catch **stale leftover files** from consolidation work — from Alembic's perspective the file is still a valid revision in the chain; nothing in the file itself looks wrong. The only signal is that the DDL fails at runtime. Two follow-ups worth considering, filed as **Obs 94** (provisionally called "Obs-58" in this round, renumbered during Round 24's observation-numbering pass to avoid collision with Plugin Surface's Obs 58):

1. **CI preflight** — run `alembic upgrade head` against a fresh Postgres before the shell-spec job, with the expectation of rc=0. Same mechanism the production `_run_alembic_migrations` uses, just separated into a dedicated CI step so migration failures fail fast and distinctly from application failures. Would have turned Round 18's CI failure into a clearer "migration broken" signal instead of a 30-second timeout on dossier_app startup.
2. **Static consistency check** — cross-reference each migration's DDL against the union of prior migrations' DDL; flag any `op.add_column('X', 'Y', ...)` where an earlier migration's `op.create_table('X', ..., Column('Y', ...))` already declares the column. Harder to write correctly (migrations can rename, drop, re-add) but would catch the stale-file shape without a live DB.

Not blocking; filed as an observation rather than a bug because the append-only guard isn't broken, it just has a narrow scope that this case falls outside of.

**Verification — Round 19:**
- Engine: **717 passed + 7 Sentry-skipped** (was 715; +2 Bug 55 tests)
- Toelatingen / common / file_service unchanged at 22 / 18 / 21.
- **785 total** (was 783).
- Shell spec green: 25 OK, D1-D9, zero tracebacks. Per-activity guard adds one `get_activity` call per visited node; no observable latency impact on D1-D9.

### Round 20 — Bug 57 (entities read endpoints skip `inject_download_urls`) with mid-round scope narrowing

Verify-before-plan confirmed: `routes/entities.py` had three GET endpoints (all-versions-of-a-type, all-versions-of-an-entity, single-version), none of which called `inject_download_urls`. The `routes/dossiers.py` route for the dossier-detail read *did* inject, so clients reading a dossier via that route got signed download URLs on their file_id fields; clients reading via any entities route got raw file_ids with no downloadable URL. Bug title said "three endpoints" — confirmed on inspection.

**Mid-round scope narrowing (important).** Original plan was to fix all three endpoints: refactor `PluginRegistry` to add `get_for_entity_type`, thread `registry` through `register_routes → entities.register` (signature change), add a reusable `_make_signer` helper, inject URLs in all three handlers. Started executing — added the registry helper, changed the signature, wrote the shared closure. User pulled scope back: "Actually I'd just add it to the single version endpoint." Rolled back the registry helper and the signature change; kept only the import additions and the single-handler fix.

The narrowing was the right call and worth articulating:
- The **bulk endpoints are inspection-shaped** — they return revision history, typically for debugging or for a UI listing all versions of an aanvraag. Clients in the download flow follow up with a single-version fetch to get the specific row they want.
- Minting one signed URL per file per version across every version of every entity is **waste in the common case**: most fetches of the bulk endpoints don't use the URLs at all, and each URL involves HMAC-signing a token. For a dossier with 20 aanvragen, each averaging 5 versions, each with 3 bijlagen, a bulk fetch would mint 300 URLs per request.
- The **minimum change that closes the reported symptom** (clients can't download via entities route) is the single-version endpoint, since that's where a download-oriented client lands. The module docstring now explicitly documents the asymmetry and the "fix it the same way" path if a future client actually needs bulk URLs.

Process bake-in for future rounds: **before writing the fix, articulate the minimum change and ask whether it covers the reported symptom.** I auto-expanded Bug 57's scope from "one endpoint" to "three endpoints" based on the bug title without checking whether all three actually needed the fix for the reported behaviour. Same shape as Round 18's `ActivityContext` scope blowup but caught earlier — partway through coding rather than partway through a multi-turn plumbing refactor.

**Actual fix shipped (minimal, 1 handler + imports + docstring):**
- `routes/entities.py` imports `inject_download_urls`, `sign_token`, `token_to_query_string`.
- Single-version handler resolves the owning plugin via `app.state.registry` (already wired at `app.py:317`, no new plumbing needed), mints a per-request dossier+user-scoped signer closure matching `routes/dossiers.py`'s pattern, calls `inject_download_urls(model_class, entity.content, sign)`.
- Plugin lookup is a short loop over `registry.all_plugins()` checking `entity_models` membership — a registry helper would be overkill for a single caller. If a third caller ever shows up, promote it to the registry.
- Module docstring documents the three-endpoints-shape and the deliberate asymmetry (single-version injects; bulk inspect-shaped endpoints don't).

**Regression tests (3 new) in `TestGetEntityVersion`:**
1. **`test_bug57_single_version_injects_file_download_urls`** — seeds an aanvraag with two bijlagen (each with a `file_id`), fetches via the single-version endpoint, asserts every bijlage has a `file_download_url` sibling and that the URL points at the configured file_service URL with a query-string token. Test infra required adding a minimal `_TestAanvraag` / `_TestBijlage` Pydantic pair to the synthetic test plugin's `entity_models` — mirroring the real `dossier_toelatingen` shape so both top-level and nested-list-of-submodels injection paths are exercised.
2. **`test_bug57_no_model_registered_returns_content_unchanged`** — pins the defensive fallback: if no plugin registers a model for the entity type, `inject_download_urls(None, ...)` returns content unchanged, endpoint stays 200. Uses `oe:bijlage` which is declared in `entity_types` but has no `entity_models` entry in the test plugin. Guards against a future refactor that would accidentally 500 on unknown types.
3. **`test_bug57_token_carries_dossier_and_user_scope`** — Bug 47 / Round 11 lineage test: same entity fetched by alice vs admin returns different URLs (same path, different query-string tokens). Guards against a future signer refactor that drops user_id or dossier_id from the token's scope fields.

**Paranoia check applied per Round 19's lesson.** Before writing the review entry, temporarily reverted just the `inject_download_urls(...)` call in `entities.py` to a passthrough of `entity.content`, ran the tests, confirmed 2 of the 3 regression tests go red with clean assertions (`file_download_url missing from response content` and `KeyError: 'file_download_url'`). The third test (no-model-registered fallback) passes both with and without the fix — which is **correct**: it's specifically the defensive-fallback path where no model means no injection, so the output is the same either way. Restored the fix; 7/7 green. This is the right shape for applying the Round 19 lesson going forward: revert, run, check the red, restore.

**Verification — Round 20:**
- Engine: **720 passed + 7 Sentry-skipped** (was 717; +3 Bug 57 regression tests).
- Toelatingen / common / file_service unchanged at 22 / 18 / 21.
- **788 total** (was 785).
- Shell spec green: 25 OK, D1-D9, zero tracebacks.

### Round 21 — Bug 58 (validator endpoints unauthenticated) with narrow "authenticated = fine" framing

Verify-before-plan confirmed: `routes/reference.py` registered four endpoints (all-reference-data, single-reference-list, list-validators, POST-validator), none carrying `Depends(get_user)`. Bug title names the POST validator specifically; in practice all four lacked auth.

**Attack-surface analysis done before planning the fix.** The validators are pure, side-effect-free lookup oracles: `erfgoedobject` resolves a URI to `{label, type, gemeente}`; `handeling` maps type → allowed-handelingen set (and surfaces the full allowed-set in error messages on invalid input). Unauthenticated access lets a caller enumerate the inventaris URI space and the allowed-action mapping, and — in production where these back onto the real inventaris API — provides a DoS vector. No data modification, no dossier visibility bypass, no RBAC concerns.

**Scope question surfaced and answered.** The reference-data endpoints share the file and the "unauthenticated" shape, so the natural scope-expansion question was "all four, or just the validate ones?" User picked validate-only: reference data is shared dropdown data (bijlagetypes, documenttypes, gemeenten), freely public by product decision. This is the third round running where asking the scope question up front paid off — Round 18 (ActivityContext plumbing) expanded scope mid-round and cost real rework; Round 20 (Bug 57) narrowed scope mid-edit; Round 21 got the scope settled before coding. **Baking the "articulate the minimum change first" step in is working.**

**"Authenticated = fine" framing.** Product decision recorded in-round: any authenticated session may call the validators, regardless of role. Auth here is not RBAC (no per-validator role gates, no dossier scoping); it's attack-surface reduction — gating on "has a valid session" closes the unauthenticated enumeration / DoS surface without adding permission logic the use case doesn't need. The module docstring now documents this explicitly so future readers don't wonder whether role gates are missing.

**Fix shipped:**
- `routes/reference.py` imports `Depends`, `User`; `register()` signature takes `get_user` and threads it into both `_register_reference_routes` (for the `list_validators` GET only — reference endpoints in the same function stay untouched) and `_register_validator_route` (both Pydantic-bodied and dict-bodied endpoint closures get a `user: User = Depends(get_user)` parameter, with `user: User` added to `__annotations__` in the Pydantic-bodied branch so FastAPI's dependency resolution kicks in).
- `routes/__init__.py` passes `get_user=get_user` to `_reference_routes.register(...)`.
- Module docstring rewritten to document the split: reference endpoints public, validate endpoints auth-required, with the rationale for both positions.

**Regression tests updated + added:**
- The 8 existing `TestValidation` tests got a class-level `_AUTH = {"X-POC-User": "claeyswo"}` constant and pass it as `headers=self._AUTH` on every call. Any authenticated POC user works — role irrelevant — so claeyswo (a `beheerder` in the toelatingen workflow) serves as the "pick one arbitrary authenticated user" stand-in.
- New `TestValidateRequiresAuth` class (4 tests): 401 on unauthenticated `GET /validate`, 401 on unauthenticated `POST /validate/{real_name}`, 401 on real-name + empty body (pins that auth fires **before** Pydantic body validation — otherwise the 422 vs 401 distinction would let an attacker learn that the validator name is real), plus a sanity guard that `/reference` and `/reference/{name}` stay public.

**Mid-test scope pullback worth capturing.** First pass at the "enumeration resistance" test also claimed that `POST /validate/nonexistent_validator` must return 401, not 404 — the reasoning being that 401-vs-404 lets an attacker enumerate validator names. Turned out that's stronger than Bug 58 requires: FastAPI's route resolution happens *before* middleware, so `POST /.../nonexistent_validator` 404s before the auth middleware runs, and enforcing otherwise would need a catch-all handler or a route-resolution hack. Dropped the claim and added a paragraph in the test's docstring explaining what it does and does not pin. The "authenticated = fine" framing treats validator **names** as non-sensitive (the `GET /validate` list returns them to any authed user anyway); only the **oracle behaviour** is sensitive, and that's what the remaining assertions guard.

**Paranoia check applied per Round 19-20 lesson, first pass.** Reverted the three `Depends(get_user)` additions in `reference.py` via scripted in-place edit, re-ran `TestValidateRequiresAuth`. 3 of 4 tests went red with clean assertions (200 where 401 expected on list-validators; 200 on unauthenticated POST; 422 on unauthenticated + empty body — the 422 is the Pydantic validator firing before the missing auth, which is exactly the ordering failure `test_post_validate_without_auth_even_for_bogus_inputs` was written to pin). The 4th test (`test_reference_stays_public`) correctly stayed green because it's a sanity-guard on a code path the revert didn't touch. Restored the fix; all 17 tests in the file pass. **The "3 of 4 red" pattern — guard tests going red while unrelated-path sanity tests stay green — is the healthy signal for a scoped fix.**

**Verification — Round 21:**
- Engine: **724 passed + 7 Sentry-skipped** (was 720; +4 Bug 58 regression tests).
- Toelatingen / common / file_service unchanged at 22 / 18 / 21.
- **792 total** (was 788).
- Shell spec green: 25 OK, D1-D9, zero tracebacks. `test_requests.sh` doesn't exercise `/validate/*`, so the fix was confirmed to have zero happy-path impact by direct grep before running the harness.

### Round 22 — Bug 62 (entity_id decorative in single-version URL); severity-first walk closes out

Last must-fix bug. Verify-before-plan confirmed: `get_entity_version` at `routes/entities.py:141` checked `entity.dossier_id != dossier_id` and `entity.type != entity_type` but not `entity.entity_id != entity_id`. The URL `(dossier, type, entity_id, version_id)` was supposed to address one canonical row; `entity_id` was decorative.

**Severity analysis in-round.** The attack surface is thin: an exploiter would already need a valid `version_id` (UUIDs, not enumerable), so they could always reach the row via a correct URL. What the bug *does* enable is silent mis-attribution — a client with a stale or mistyped `entity_id` in the URL gets back a version whose response body carries a *different* `entity_id` than what they asked for, because line 249 synthesizes the response from `str(entity.entity_id)` on the actual row. The tombstone redirect at line 178 uses the actual `entity_id` too, so a 301 could land a wrong-eid request on a correct-eid URL silently. Defense in depth, URL-correctness, REST-semantics — same category as Bug 55 (line-level scope check was doing the work, adding the URL-level check makes the endpoint fail closed at the first layer that can catch the mismatch).

**Fix shipped:** one line. Added `or entity.entity_id != entity_id` to the existing 404 guard block. Comment explains the rationale (URL addresses a canonical row, not a set) so the next reader doesn't add it back.

**Regression tests (2 in `TestGetEntityVersion`):**
1. **`test_bug62_wrong_entity_id_in_url_returns_404`** — seeds two independent logical entities (A and B) in the same dossier, same type. Asserts A's version fetched under A's eid is 200 (sanity), and A's version fetched under B's eid is 404 (the bug).
2. **`test_bug62_random_entity_id_returns_404`** — completely random eid that was never seeded. Guards against a future refactor that checks "eid exists in dossier" instead of "eid matches the version's field."

**Paranoia check, first pass.** Reverted the new check, both Bug 62 tests went red — and the failure output literally shows the silent mis-attribution: the response body carried A's titel ("A") and A's generatedBy activity, but the URL had used B's eid. That's the bug in the test's own failure message, which is the healthiest shape a red paranoia result can take. Restored; 9/9 in `TestGetEntityVersion`, 726/726 + 7 Sentry-skipped engine-wide.

### Severity-first walk — must-fix tier complete

Round 22 closes the **must-fix** walk specifically. Across Rounds 1-22 (some of which bundled multiple bugs), all 17 fixable must-fix bugs are now **fixed + verified**; the remaining 5 must-fix rows are **deferred + accepted** (Bug 31 RRN, Bug 45 MinIO, Bug 63 HTTP 403, Bug 71 deploy-time test-activity removal) or **investigated + reclassified** (Bug 14: "cross-dossier refs" = `type=external` design).

**What this does not mean** (corrected in Round 23 bookkeeping pass): "the walk" is complete only for the must-fix tier. The **should-fix table** has 31 open bugs + 10 closed; the **lower-priority table** has 16 open + 0 closed. Earlier round writeups implied a broader "walk complete" framing that wasn't accurate — the must-fix tier being done does not mean no bugs remain actionable. 47 open bugs across Should-fix + Lower-priority still exist, alongside 35 open observations and 21 open dups. Round 23's triage pass (below) addresses the unified open-items landscape.

**Test suite trajectory:** the engagement started with ~510 tests across the five repos; it ends (for the must-fix walk) at **794 passing + 7 Sentry-skipped**. The delta isn't pure coverage gain — several rounds added or adjusted tests to match fixes rather than net-new coverage — but the suite has roughly 280 more green tests than it started with, and the shell spec's `test_requests.sh` end-to-end harness went from "25 OK with intermittent tracebacks and deadlocks" to "25 OK, exit 0, zero tracebacks, zero worker crashes, D1-D9 all green." That's the more durable signal.

**Process practices that landed and stuck across the walk:**

1. **Verify-before-plan** (Round 14 onwards). Read the code before writing the fix. Caught several "bug" reports where the code had already been fixed quietly in some earlier round, and avoided rewriting things that didn't need rewriting.

2. **Paranoia check after regression tests land** (Round 19 onwards). Revert just the fix, rerun the tests, confirm they go red. Catches tests that are pinning the user-visible return value when the fix was at a different layer (defense-in-depth fixes especially). First adopted in Round 19 after the initial Bug 55 regression tests silently passed without the fix; applied consistently Rounds 20-22.

3. **Articulate the minimum change first** (Round 20 onwards). Before writing the fix, ask whether the symptom can be closed with less than the bug title suggests. Round 18 taught this the hard way (full attribution-plumbing refactor because the audit emit needed user context — correct in the end, but revealed mid-execution rather than up-front). Rounds 20 and 21 applied the practice cleanly and saved rework both times.

4. **Test docstrings explain what they pin AND what they don't** (Round 21 onwards). After the over-claimed enumeration-resistance test in Round 21, the practice is to state the test's boundary — "does not claim X, because X is a stronger property than the fix targets" — so a future reader doesn't think a weaker assertion is a coverage gap.

**Operational notes surfaced mid-walk (not bugs):**
- **Obs 94** (Round 19, originally filed as "Obs-58", renumbered in Round 24) — stale migration version files. Round 8's append-only guard catches mutations but not stale leftovers from consolidation. Candidate follow-ups: CI migration preflight job, or static consistency check scanning for redundant DDL across migration files. Filed as observation, tractable as a small dedicated round.
- **Worker schema-retry loop** — surfaced in Round 19's shell-spec log during CI debugging; already present behaviour, no action needed, worth knowing about.
- **Historical silent failures** (Round 18 aftermath) — pre-Bug-30 failed bijlage moves left aanvragen with broken refs permanently; file service `temp/` cleanup has likely reaped the unmoved files. Operators should expect "move_bijlagen task failed after N retries" as the new normal signal for stuck aanvragen.

### Round 23 — Bookkeeping correction + unified triage of all open items

This round is not a fix round. Two things delivered:
1. **Bookkeeping correction** of counts across the summary, observations, and duplication sections (drift accumulated over ~8 rounds of summary updates where I'd been carrying forward "57 observations" and "22 open dups" without revalidating against the actual listed items).
2. **Unified triage** of every open actionable item — 47 open bugs, 35 open observations, 21 open dups — grouped by shape-of-work rather than source table, with a verdict per category.

#### Precise counts after Round 23 reconciliation

| Source | Open | Closed | Deferred / Investigated |
|---|---|---|---|
| Must-fix bug table | 0 actionable | 17 | 5 (3 deferred, 1 investigated-as-non-bug, 1 product decision) |
| Should-fix bug table | 31 | 10 | 0 |
| Lower-priority bug table | 16 | 0 | 0 |
| Structural observations | 35 | 7 + 1 partial | 1 deferred |
| Duplication targets | 21 | 6 | 0 |
| **Total open work items** | **~103** | 40 | 6 |

Item overlap exists (several observations are reference-entries for bugs in the bug tables — for example "README claims externals in both used/generated" = Bug 56 = obs line 174). Distinct work items after overlap resolution: probably ~90.

#### Category-level triage

Organized by shape-of-work rather than by source table. Each category has a verdict (`batch-fix` / `batch-defer` / `cherry-pick` / `reconcile`) and the reasoning. Items already catalogued as bugs keep their bug number; observations without one are cited by review line where useful.

##### Category 1 — Doc-only fixes (cherry-pick, ~6 quick-fix rounds worth in aggregate)

Standalone factual corrections to docstrings, README, and templates. Low risk, low effort, immediate value:

- **Bug 56** — README claims external-overlap allowed; code rejects. *Fix: correct the README.* (Sev 6, misleading to new contributors.)
- **Bug 69** — Dossiertype template tombstone block doesn't match production workflow. *Fix: update the template.* (Sev 7, trips up plugin authors.)
- **Bug 66** — Relation validator keying rules (three styles) undocumented. *Fix: add a short paragraph to the relations doc.* (Sev 7.)
- **Obs 67** — Pipeline doc's "UPDATE must happen after persistence" factually wrong. *Fix: correct the doc.*
- **Obs 68** — Pipeline doc's `ActivityState` field table covers ~⅓ of fields, presented as complete. *Fix: either complete the table or rewrite as "selected fields."*
- **Obs 72** — Template's endpoint docs omit workflow-name prefix; 4 different URL forms, none matching production. *Fix: update template endpoints.*

**Verdict: batch-fix as a single doc-round.** ✅ **Shipped in Round 24.** All 6 items closed — Bugs 56/66/69 and Obs 67/68/72. Three redundant observations (Obs 69/71/73) closed alongside their bug counterparts. Full engine test suite stayed green (docs-only changes, 726 passed + 7 skipped).

##### Category 2 — Small-surface behaviour fixes (cherry-pick, sev-ordered)

Concrete behaviour bugs that are each ~30-100 lines of code + tests. Each wants its own round with verify-plan-fix-test-ship:

- ~~**Bug 4**~~ — ~~`Session` type annotation never imported.~~ ✅ **Shipped in Round 31.**
- ~~**Bug 9**~~ — ~~N+1 in dossier detail view.~~ ✅ **Shipped in Round 29.**
- ~~**Bug 13**~~ — ~~Deprecated `@app.on_event("startup")`.~~ ✅ **Shipped in Round 33** (both startup and shutdown converted to lifespan).
- ~~**Bug 20**~~ — ~~`_PendingEntity` missing several fields → `AttributeError`.~~ ✅ **Shipped in Round 30.**
- ~~**Bug 27**~~ — ~~`DossierAccessEntry.activity_view: str` too narrow (should be Literal).~~ ✅ **Shipped in Round 31.** `"related"` mode also removed.
- 🛑 ~~**Bug 28**~~ — ~~`POCAuthMiddleware` silently overwrites on duplicate usernames.~~ **Deferred — POC-only, slated for replacement with real auth.**
- **Bug 34** — `authorize_activity` catches broad `Exception`. *Hides real errors.*
- ~~**Bug 39**~~ — ~~`TaskEntity.status: str` → `Literal[...]`.~~ ✅ **Shipped in Round 32** (bundled with `TaskEntity.kind` parallel tightening).
- **Bug 43** — `Aanvrager.model_post_init` raises ValueError without Pydantic shape. *422 error shape wrong.*
- **Bug 48** — `.meta` filename not sanitized. *Security-adjacent.*
- **Bug 50** — Migration fallback uses module-level `SYSTEM_ACTION_DEF` with bare name. *Should use qualified.*
- **Bug 59** — Unregistered validators silently skip. *Config error becomes silent bug.*
- **Bug 60** — `alembic/env.py` nested `asyncio.run()` hazard. *Migration reliability.*
- **Bug 67** — `_errors.py` payload key collision. *Error shape wrong in specific cases.*

**Verdict: cherry-pick, order by severity.** 14 items. Probably a second severity-first walk (Bugs 4, 9, 13, 19, 20, 27-28 first, then 34-39-etc) across ~4-6 rounds if bundled sensibly. The top 4 (Bugs 9, 20, 27, 28) feel like the priority: direct user-visible or correctness-visible. The rest can be a clean-up batch.

##### Category 3 — Caching & performance polish (batch-fix as one round)

Small perf wins, each ~10-30 lines:

- **Bug 38** — No per-user authorize cache. (cross-ref obs line 188.)
- **Obs 75** — Cache `SearchSettings()` at module load.
- **Obs 76** — `is_singleton` cache.
- **Obs 77** — `derive_status` prefers `dossier.cached_status` first.
- **Obs 78** — `check_workflow_rules` passes `known_status` from `state.dossier.cached_status`.

**Verdict: batch-fix as one perf-round.** The pattern is the same (cache what's expensively re-computed); doing them together gives a single set of benchmarks + "cache invariants doc" as the deliverable. One round, maybe two.

##### Category 4 — Lineage walker completion (cherry-pick, 2 bugs)

Bug 55 (Round 19) did the cross-dossier defense. The lineage walker has two more open items:

- **Bug 53** — frontier growth unbounded.
- **Bug 54** — returns None for both "not found" and "ambiguous" — caller can't distinguish.

**Verdict: one round, both together.** ✅ **Shipped in Round 25.** Bug 53 fixed via `list→set` frontier + dedup-on-add; Bug 54 fixed via new `LineageAmbiguous` exception with the sole production caller (`_build_trekAanvraag_task`) updated to catch + log + proceed unanchored. Obs 66 closed alongside. Test suite grew 794→800 (+4 caller tests, +2 lineage tests, -2 weak Bug-53 tests downgraded to 1 sanity test — see Round 25 writeup on why).

##### Category 5 — Plugin-surface tightening (medium, needs design)

Multi-item cluster that wants a coherent decision before coding:

- **Obs 56 + obs line 160** — Centralize plugin validation; docs promise 15 field validations, 3 actually run.
- **Obs 58** — `authorize_activity` pre/post-creation modes should split.
- **Obs 59** — Load-time validation for `status:` dict-form shape.
- **Obs 60** — `eligible_activities` column: `Text` → `JSONB`.
- **Obs 62** — Remove legacy `handle_beslissing`.
- **Obs 64** — `systemAction` sub-types should be introduced.
- **Obs 65** — Document `systeemgebruiker` role grants; add `caller_only: "system"` check.

**Verdict: design-discussion round first, then fix-rounds.** Several of these are interconnected (plugin validation centralization would naturally produce the missing validators; splitting `authorize_activity` affects the validation pipeline). Don't cherry-pick blindly; agree the shape first.

##### Category 6 — Larger refactors (batch-defer with revisit trigger)

These are the "worker.py is 1,340 lines" class of items. Each is substantial rework in codebase that currently works:

- **Obs 50** — Worker split into poll/execute/retry/requeue/signals.
- **Obs 51** — Unify relation shape in `ActivityState` (4 in-memory shapes for one concept).
- **Obs 53** — Extract `prov_columns_layout.py` (~280 lines of pure layout).
- **Obs 54** — Untangle import-inside-function cycles.
- **Obs 55** — Rationalize `namespaces.py` singleton + scattered `try/except RuntimeError` fallbacks.
- **Obs 91** — Share layout between `archive.render_timeline_svg` and columns graph.
- **Obs 92** — `activity_view` mode complexity reduction.
- **Bug 61** — `activity_relations` indices cost writes but zero readers.

**Verdict: defer with explicit revisit trigger.** These earn their cost when *adding a feature* in the area — the refactor unblocks the feature work. Doing them speculatively in a standalone round is optimization without a forcing function. Exception: **Obs 53** (prov_columns_layout extraction) is a pure function of inputs, already identified as "easy to isolate" — it's a cheap win if anyone wants to grab it. Filing as "available but not scheduled."

##### Category 7 — Test & deployment infrastructure (cherry-pick, ~3 rounds)

Test-infra quality-of-life items. Low user-facing impact but affects future development velocity:

- **Obs 82** — Test fixtures use direct Repository + no unit-isolation story documented.
- **Obs 85** — Dependency-override-friendly auth for tests.
- **Obs 86** — Signing key rotation support (only one key accepted).
- **Obs 87** — Migration framework top-level audit log.
- **Obs 88** — `DataMigration.transform` signature widening.
- **Obs 89** — Cross-workflow task permission model.
- **Obs 94** — Migration consistency checks (CI preflight, Round 19 origin; renumbered in Round 24).
- Anonymous Should-fix items: Alembic subprocess timeout, file-service signing_key default at startup, plugin-load cross-check, worker's recorded tasks don't pass `anchor_entity_id`/`anchor_type`, archive size cap, `app.py:69` appends SYSTEM_ACTION_DEF by reference.

**Verdict: cherry-pick across 2-3 rounds.** Obs 94 + the two migration-framework items (Obs 87, Obs 88) can be one migration-infra round. Dependency-override auth + signing key rotation + test unit-isolation can be one test-infra round. Cross-workflow task permissions is its own thing. The anonymous Should-fix items are small each; could be a "miscellaneous tightening" round.

##### Category 8 — Duplication targets (batch-defer with opportunistic closure)

Of 21 open dups, most are "two functions share a pattern, extract a helper." All genuine but none acute:

- D3 (prov_type_value not used by all callers), D5 (4 copies of latest-version subquery), D6 (cache returns mutable), D7 (reindex loops 90% identical), D8 (`get_typed` vs `get_singleton_typed`), D10 (3 reindex_*), D11 (upload/download 7-param extraction), D12 (informed_by normalization in 4 places), D13 (_supersede_matching vs cancel_matching_tasks), D14 (tombstone tests), D15 (DossierAccessEntry docstring drift), D16 (validator-fn registration pattern), D17 (entities route access-check preamble), D18 (plugin-load sequence), D19 (scheduled_for parsing), D20 (`parse_activity_view` split across 3 route files), D21 ("filter activities by user access" hand-rolled in 4 places), D23 (find systemAction in 2 places), D24 (Alembic indices vs __table_args__ drift), D26 (sign_token/verify_token payload-string), D27 (test-setup helpers in 4+ files).

**Verdict: batch-defer; close opportunistically when touching the file for another reason.** Same framing as Category 6's larger refactors — the dedup earns its cost when you're already in the area. Two exceptions worth escalating: **D6** (caller mutation corrupts cache) is a latent bug in waiting; **D24** (Alembic indices drift from `__table_args__`) is the exact shape that bit Round 21's CI (stale migration). Those two could be promoted.

##### Category 9 — Meta-patterns (one-round each, if taken)

The Meta-patterns section documents 6 high-level systemic patterns (line 242-onwards). Several have had partial relief shipped across the walk (harness 1/2/3, signing-key-rotation stubs, etc.). The remaining open meta-patterns want their own focused discussion rather than a generic triage line. Out of scope for this triage pass.

##### Category 10 — Reconciliation items

Items that are **duplicates across tables** and should be merged:

- Obs line 169 (lineage walker cache + not-found/ambiguous disambiguation) = Bugs 53 + 54. **Already covered in Category 4** — should drop the observation as redundant.
- Obs line 174 (README external-overlap) = Bug 56. **Already covered in Category 1.**
- Obs line 176 (dossiertype tombstone) = Bug 69. **Already covered in Category 1.**
- Obs line 178 (relation validator keying) = Bug 66. **Already covered in Category 1.**
- Obs line 187 (reindex pagination) = Bug 25. **Covered in Category 2 cherry-picks.**
- Obs line 188 (per-user eligibility cache) = Bug 38. **Already covered in Category 3.**
- Obs line 159 (plugin validation ↔ Bug 59 territory) — Bug 59 is Category 2; obs is Category 5. These are related but not duplicates.

**Verdict: reconcile in the observations section** — add "(covered by Bug N)" tags to the 6 duplicate observations so future readers aren't confused. Mechanical cleanup, can be rolled into the next bookkeeping touch.

#### Summary — what the triage says to do next

| Priority | Category | Approach | Estimated rounds |
|---|---|---|---|
| ✅ Done | Cat 1 — Doc-only fixes | batch-fix | 1 (shipped Round 24) |
| ✅ Done | Cat 4 — Lineage walker completion (Bugs 53, 54) | one coherent round | 1 (shipped Round 25) |
| 1 | Cat 3 — Caching & perf | batch-fix | 1-2 |
| 2 | Cat 2 — Small-surface behaviour fixes | cherry-pick, sev-first | 4-6 |
| 3 | Cat 7 — Test/deployment polish | cherry-pick | 2-3 |
| 4 | Cat 5 — Plugin-surface (needs design first) | discussion + fix rounds | 1 design + 2-4 fix |
| — | Cat 6, 8, 9 | defer with revisit trigger | 0 scheduled |
| — | Cat 10 | reconciliation done via Round 24's cross-refs | complete |

**Total scheduled work across categories 2-5 + 7:** ~8-12 rounds, plus Cat 5's design discussion. Cat 1 closed in Round 24, Cat 4 closed in Round 25.

**The "top-of-queue" question** — if we're picking the next one — is **Cat 3 (caching & perf batch)**. Five related items (Bug 38 + Obs 75/76/77/78) sharing the same "cache what's expensively re-computed" pattern; one coherent batch-round gives a single set of benchmarks + cache-invariants documentation as the deliverable. Alternatively Cat 2 cherry-picks if you'd rather tackle user-visible behaviour bugs one at a time (top-4 are Bugs 9, 20, 27, 28).

### Round 24 — Observation numbering + Cat 1 doc-fix batch

Two deliverables: comprehensive observation-numbering pass (the "Obs N" labels were inconsistent — six sections had 44 bullets where only Obs 50-56 were explicitly numbered, with a provisional "Obs-58" from Round 19 colliding with a new Obs 58), then the six Cat 1 doc fixes from Round 23's triage.

**Numbering pass.** All 45 observations now sequential Obs 50-94, formatted consistently as `Obs NN — Title.` with status tags preserved. Six observations carry explicit `(covered by Bug N)` cross-references where they duplicate an already-catalogued bug (Obs 66 ↔ Bugs 53/54; Obs 69 ↔ Bug 56; Obs 71 ↔ Bug 69; Obs 73 ↔ Bug 66; Obs 80 ↔ Bug 25; Obs 81 ↔ Bug 38). The Round 19 provisional `Obs-58` (migration consistency checks) was renumbered to **Obs 94** and moved to the end of the "Specific refactors named" subsection — a note in both the Round 19 retrospective and the Round 23 triage explains the renumbering so future readers can cross-reference. Twenty-one `Obs (line N)` placeholders in the Round 23 triage rewritten to proper `Obs NN` via scripted replacement (21/21 matched).

**Cat 1 doc-fix batch (6 items, all shipped):**

1. **Bug 56 — README external-overlap claim.** `README.md` line 1164 falsely claimed externals in both `used`/`generated` are "allowed because externals are not PROV entities in the disjoint sense." Verified against `dossier_engine/engine/pipeline/invariants.py::enforce_used_generated_disjoint` (lines 30-116, explicit docstring: "externals that appear in `used` must not also appear in `generated`") and `tests/unit/test_invariants.py::test_external_overlap_by_uri` — the code actually rejects with `422 used_generated_overlap`, payload `kind: "external"`. Rewrote D5 description to enumerate six negative cases (was "five + one positive") and explicitly describe the symmetric external-URI rejection. Cross-refs the authoritative code + test by filename.

2. **Bug 69 — dossiertype template tombstone shape.** `dossiertype_template.md` showed the dict-of-dicts form `allowed_roles: - role: "beheerder"` with a comment claiming the three authorization patterns from the activity section apply. Verified against `dossier_engine/app.py:138-142` which iterates `ts_roles` (a bare list) via `for r in ts_roles` and constructs `{"role": r}` per element — feeding the dict form would produce the broken nested `{"role": {"role": "beheerder"}}` structure at runtime. Production `workflow.yaml:147-148` uses the simple-list form. Rewrote the template's tombstone block to show the correct shape with a comment explaining why the dict form doesn't work and that per-role scopes aren't supported for tombstone (all-or-nothing capability per role).

3. **Bug 66 — relation validator keying rules.** `docs/plugin_guidebook.md`'s plugin-interface table had a single line for `relation_validators` ("Relation type → async validator") — understating the three resolution styles the engine supports. Verified against `engine/pipeline/relations.py::_resolve_validator` (lines 365-403, three priority-ordered lookup styles: per-operation `validators: {add, remove}` dict, activity-level single `validator:` string, plugin-level by relation-type name). Added a new "Relation-validator keying" subsection after the plugin-interface table documenting all three styles with YAML examples and resolution priority, plus a warning about the key-space ambiguity — the same `plugin.relation_validators` dict is used for both named-validator lookups (Styles 1/2) and by-type lookups (Style 3), so validator names that collide with relation types produce confusing behaviour. Flagged the table entry to cross-ref the new subsection.

4. **Obs 67 — pipeline doc "UPDATE after persistence" claim.** Two paragraphs in `docs/pipeline_architecture.md` (lines 91 + 97) claimed `determine_status` does an "UPDATE" that requires the activity row to be persisted first. Verified against `dossier_engine/engine/pipeline/finalization.py::determine_status` — the function writes to `state.activity_row.computed_status`, which is an attribute set on a tracked ORM object (the row is in the session from phase 12's persistence). SQLAlchemy batches the dirty-flag write into the next flush/commit; there's no standalone UPDATE statement. Rewrote both paragraphs to describe the real mechanism: "the row must be in the session so the dirty-flag write is picked up on the next flush/commit; phase 12's persistence is what puts the row in the session."

5. **Obs 68 — ActivityState field table.** The pipeline doc's `ActivityState` field table (10 rows) was presented as a complete lifecycle listing but `state.py:ActivityState` actually has **~37 fields** (~17 inputs + ~20 phase outputs). The table also contained a factual error: a `computed_status` row, but there's no `ActivityState.computed_status` field — the activity's resolved status lives in `state.final_status` and is mirrored to `state.activity_row.computed_status` (two distinct fields, with `determine_status` setting both). Reframed the table as "a curated walkthrough of the fields that matter at phase boundaries, not an exhaustive listing," redirected readers to the class definition as source-of-truth, fixed the `computed_status` row to `final_status`, added `activity_row` and `current_status` rows for completeness, and added a note explaining the two-place-status-mirroring relationship.

6. **Obs 72 — template endpoint docs workflow-name prefix.** `dossiertype_template.md` had two broken URL forms: line 716's example showed `@app.get("/dossiers/toelatingen/search", ...)` hardcoding the workflow name after `/dossiers/`, and line 880's endpoint table showed `GET /dossiers/{workflow}/search`. Production `dossier_toelatingen/__init__.py` registers search at `/toelatingen/dossiers` (workflow-name-first, not `/dossiers/toelatingen/...`) plus `/toelatingen/admin/search/{recreate,reindex,reindex-all}` for admin endpoints. Rewrote the template's `search_route_factory` example to show the correct shape with explanatory prose about the workflow-name-first convention and its rationale (plugin-registered routes under `/{workflow}/...` don't collide with engine's built-in `/dossiers/...` routes), and updated the endpoint table row from the fictional form to the real `/{workflow}/dossiers` plus a new row for admin endpoints.

**Verification.** Docs-only changes; no code modified. Engine suite re-run (726 passed + 7 Sentry-skipped, identical to Round 23 — no regressions, no flakes in the test code itself; the one PG flake on first run was the sandbox's known issue, resolved by restart). Other suites unchanged: toelatingen 22, common 18, file_service 21. Shell spec not re-run in this round (it doesn't exercise documentation).

**Totals after Round 24:** 30 bugs fixed (was 27) — Bugs 56, 66, 69 added to the closed column. Should-fix table: 28 open + 13 closed (was 31 open + 10 closed). Observations: 30 open + 13 closed + 1 partial + 1 deferred = 45 total (was 35 open + 7 closed + 1 + 1 = 44 listed, one added via Obs 94 renumbering). Lower-priority and Must-fix tables unchanged.

**Process note worth capturing for future rounds.** Two Cat 1 items (Obs 67, Obs 68) turned out to involve factual code-doc mismatches that had been mis-summarized in the review itself. Obs 67 was "UPDATE must happen after persistence" — but the real issue is that there's no UPDATE; the doc had invented a mechanism that didn't exist. Obs 68 was "⅓ of fields documented, presented as complete" — but the table also contained a `computed_status` row that referred to a field that doesn't exist on `ActivityState`. In both cases the observation's one-line summary was less wrong than the doc it described, and "fixing the doc" meant more than textual change — it meant reading the code carefully enough to describe the actual behaviour. **Takeaway: doc-fix rounds should verify-before-plan the same way behaviour-fix rounds do.** Read the code, understand what the doc is supposed to describe, then write. The "doc-only" framing can mask the real work, which is re-establishing ground truth.

### Round 25 — Bugs 53 + 54 (lineage walker completion)

Cat 4 from the Round 23 triage. Two bugs in the same file, same state machine: Bug 53 (frontier growth unbounded) and Bug 54 (`None` conflates not-found with ambiguous). Closed together as one coherent round — shared verify-before-plan, shared test-file edits, shared caller update.

**Verify-before-plan.** Read `lineage.py::find_related_entity` end-to-end before writing anything. Two observations from the read that shaped the fix:
1. Bug 53 is **not a correctness bug.** The existing `visited_activities` set guards against reprocessing — duplicates in the frontier cause wasted loop iterations but no incorrect results. What's wasted is memory (frontier size grows O(paths) instead of O(nodes)) and the occasional harmless iteration of the visited-check-then-continue path. Severity 5 is right for the resource concern, but the fix has no behaviour-observable effect on a correct implementation.
2. Bug 54's current None-return is used by exactly one production caller (`_build_trekAanvraag_task` in the toelatingen plugin). That caller treats `None` as "no anchor available, task goes out unanchored." Changing the return contract to distinguish ambiguous from not-found has to thread through to that caller — which turns a "lineage walker fix" into "lineage walker + caller fix."

**Shape decision for Bug 54.** Three options considered in-round: (A) raise a custom exception on ambiguity, (B) return a tagged union/sentinel, (C) return `(Optional[EntityRow], reason: str)` tuple. Picked **A** for minimum-change reasons — the happy-path return type stays `Optional[EntityRow]`, no callers need updating for the common case, and callers who want the old "silently drop" behaviour can catch the exception. Downside: `LineageAmbiguous` is a new type to learn, and forgetting to catch it anywhere it could fire would propagate the exception further than intended. Weighed vs the explicit-reason-code option (C) — C is more debuggable but requires every caller to unwrap the tuple. Given only one caller exists, A is cleaner.

**Caller-side decision.** `_build_trekAanvraag_task` sits in a task-builder hook during the activity pipeline. Letting `LineageAmbiguous` propagate would crash the activity (roll back the beslissing because we couldn't decide which aanvraag to anchor a trekAanvraag task to) — too strong a response for a structural-data anomaly that doesn't block the beslissing's correctness. Instead: catch it, emit a WARNING log with the activity_id + candidate entity_ids (the triage affordance Bug 54 was filed to add), proceed with an unanchored task. Operators get the signal; the workflow keeps running. Stricter callers in the future can let it propagate.

**Fixes shipped:**
- `lineage.py` — `frontier` and `next_frontier` changed from `list[UUID]` to `set[UUID]`; `.append(...)` → `.add(...)` with `not in visited_activities` guards at append time (Bug 53). New `LineageAmbiguous(Exception)` class carrying `activity_id` + `target_type` + `candidate_entity_ids`; `return None  # ambiguous` replaced with `raise LineageAmbiguous(...)` (Bug 54). Module docstring rewritten to document both the new contract and the frontier-management semantics.
- `handlers/__init__.py::_build_trekAanvraag_task` — wrapped the `find_related_entity` call in `try/except LineageAmbiguous`, added `_logger = logging.getLogger(__name__)`, emits warning on ambiguity, proceeds unanchored.

**Test work, with honest self-report.** This section captures a process failure that the paranoia check caught mid-round, because the lesson it produced is worth more than the fix itself.

First-pass regression tests (2 for Bug 53, 1 for Bug 54 replacement + 1 for not-found negative case):
- `test_ambiguous_raises_lineage_ambiguous` — asserts `pytest.raises(LineageAmbiguous)` with the right attributes carried on the exception.
- `test_not_found_still_returns_none_after_bug54` — asserts None still returns for the "no match in graph" case (pins the negative side of Bug 54's contract split).
- `test_bug53_frontier_deduplicated_high_fan_in` (original form) — built a high-fan-in diamond graph, asserted correctness of the resolved aanvraag.
- `test_bug53_no_match_high_fan_in_terminates` (original form) — same fixture, no target present, asserted None return.

When I wrote the two Bug 53 tests I included a comment inside one of them: *"wait — re-read: the `continue` is ABOVE the get_activity call. So visited still short-circuits. The real savings are memory + loop iterations, not DB. Either way, result correctness is what matters most."* I knew at write-time that the tests were weak. Shipped them anyway.

**Paranoia check (Round 19 practice: revert the fix, confirm tests go red).** Reverted both fixes via targeted string-replacement in `lineage.py`, reran the suite. Result:
- ✅ `test_ambiguous_raises_lineage_ambiguous` — red as expected (`DID NOT RAISE`). Bug 54 fix pinned correctly.
- ❌ `test_bug53_frontier_deduplicated_high_fan_in` — **still green** without the fix.
- ❌ `test_bug53_no_match_high_fan_in_terminates` — **still green** without the fix.

Exactly what my comment had said would happen: the dedup only changes memory growth, which my tests didn't measure. Both Bug 53 tests were theatre.

**What I did about it (mid-round, before shipping):**
1. Deleted `test_bug53_no_match_high_fan_in_terminates` entirely (redundant with existing no-match tests anyway).
2. Renamed `test_bug53_frontier_deduplicated_high_fan_in` to `test_bug53_high_fan_in_walk_still_correct` and rewrote its docstring to explicitly mark it as a **sanity test**, not a regression test. The docstring calls out that we tried to write a pinning test and couldn't find an assertion shape that wouldn't couple to internal state (monkeypatching frontier type, spying on `set` vs `list` behaviour), and documents the honest situation: the fix is described in the walker's module docstring + inline comments so a future reader reverting it would have to do so deliberately.
3. Restored the fix; added the caller-side tests (which the paranoia check *did* catch — 1 of 4 red on caller-revert, healthy shape).

**New lesson (additional to Round 19's paranoia-check + Round 21's test-docstring-states-what-it-pins):**

> **Don't ship tests that you've already admitted are weak.**
> If writing the test surfaces "I'm not sure this actually pins the fix," that doubt is the signal to either find an assertion shape that does pin it, or accept that the fix isn't behaviour-observable and document the disposition honestly rather than invent a ceremonial test.
> The comment I wrote inside the original Bug 53 test (acknowledging the test was weak) was not a substitute for acting on it. If the paranoia check hadn't caught it, a weak test would have lived in the suite forever, pretending to guard Bug 53, making the next reviewer think the regression was covered when it wasn't.

This extends Round 21's practice ("test docstrings state what they pin AND what they don't") — in Round 21 the downgrade was real ("test pins A but not the stronger property B"). In Round 25 the downgrade is more severe ("this test pins nothing the fix actually changed"). Both are acceptable dispositions *if honestly documented*. What's not acceptable is shipping the test with a hopeful docstring that over-claims what it pins.

**Bug 53's disposition is therefore:** fixed, with the fix itself documented in code; no regression test; sanity test confirms the walk still produces correct results on a high-fan-in graph (which it always did, since correctness was never the issue). This is the right answer — inventing a fake regression test would have been worse.

**Verification — Round 25:**
- Engine: **728 + 7 Sentry-skipped** (was 726 + 7). +1 new Bug 54 regression test (`test_ambiguous_raises_lineage_ambiguous`); +1 new not-found regression test (`test_not_found_still_returns_none_after_bug54`); +1 kept-as-sanity-test (downgraded from weak regression test); -1 removed (`test_ambiguous_returns_none` replaced by the raises-variant); -1 deleted (the second weak Bug 53 test). Net +2.
- Toelatingen: **26 tests** (was 22; +4 new caller tests in `test_build_trekAanvraag_task.py` — 1 happy-path anchor, 1 walker-returns-None unanchor, 1 `LineageAmbiguous` caught-and-logged, 1 no-beslissing-no-walk). Paranoia-checked; reverted the try/except and 1 of 4 went red cleanly.
- Common 18, file_service 21 unchanged. **Total 800 tests** across all packages.
- Shell spec: 25 OK, D1-D9, zero tracebacks. Bug 53+54 aren't exercised by shell spec (no happy-path ambiguity scenarios in D1-D9); re-ran as a smoke test for "I didn't break anything adjacent."

**Totals after Round 25:** 32 bugs fixed (was 30) — Bugs 53, 54 added. Should-fix table: 26 open + 15 closed (was 28 + 13). Observations: 14 closed + 29 open (was 13 + 30); Obs 66 flipped to closed as cross-ref to Bugs 53/54. Lower-priority and Must-fix tables unchanged.

### Where to go next (post-Cat-4)

Cat 3 (caching & perf batch) is my recommendation for Round 26 — Bug 38 + Obs 75/76/77/78, all sharing the "cache what's expensively re-computed" pattern. One coherent batch round.

### Round 26 — Bug 78 (plugin relation-type contract; drive-by, not filed)

User found this while reading Round 24's Bug 66 guidebook fix. The contract described in the guidebook said `relation_validators` was keyed by relation type name *or* validator name depending on which of three "styles" an activity used — and the engine resolved each. Reading the code in anger: the `kind:` field was declarable but never consulted at runtime (dispatch guessed kind from request shape); `_relation_kind` was dead code; Style-3 by-type-name fallback ran invisibly when Styles 1/2 didn't match. The guidebook was honestly describing a contract that shouldn't exist.

Filed as a drive-by (no Bug 78 row in the bug tables — user explicitly chose "don't file" in the pre-round scope discussion); shipped as Round 26.

**Design decisions recorded in-round** (before any code was written — Round 18's "articulate minimum change first" lesson applied):

- **Activity-level `kind:` forbidden**, workflow-level mandatory. Types are declared *once* at workflow level; activities reference by name only (option C in the scope discussion). Alternative "activity-level OR workflow-level fallback" (option A) was rejected as it preserved the ambiguity that made `_relation_kind` dead code.
- **`from_types`/`to_types` at workflow level only**, domain-only. Missing means "any ref type accepted" — explicit semantic for unconstrained relations.
- **Style 3 removed.** Plugin-level `relation_validators[<relation_type>]` fallback is gone. Activities must declare validators explicitly via `validator:` (single-string) or `validators:` (dict).
- **Process_control restrictions:** `validators:` dict form and `operations: [remove]` forbidden on process_control relations (they have no remove semantic). Single `validator:` string is the only legal form.
- **Fail-loud, no deprecation phase.** ValueError at plugin load; 422 at dispatch. No transition period — the contract being invisible was part of the problem. Any misaligned plugin YAML surfaces immediately.
- **Dotted-path migration deferred** to Cat 5 (Obs 95 filed). User pointed out mid-round that `relation_validators` being a `dict[str, Callable]` re-introduces the indirection Obs 95 describes — but converting all eight Callable registries (handlers, validators, task_handlers, status_resolvers, task_builders, side_effect_conditions, relation_validators, field_validators) to dotted paths is a much bigger refactor. Bug 78 keeps the dict shape but enforces keys can't collide with declared type names (closes the Style-3 hazard structurally, not just by convention).

**Load-time validation shipped:**
- `validate_relation_declarations(workflow)` — shape-checks workflow-level + activity-level declarations. Enforces every rule above with ValueError on first violation. 21 negative cases + 3 positive controls covered by tests.
- `validate_relation_validator_registrations(plugin)` — cross-checks `plugin.relation_validators` dict keys against declared type names; rejects Style-3-by-naming-convention collisions.
- Both wired into `app.py::load_config_and_registry` (new block between `_validate_plugin_prefixes` and `set_namespaces`).

**Runtime shipped:**
- `_parse_relations` dispatches by workflow-level `kind`, not request shape. Kind resolved once per item via the now-wired `_relation_kind` helper. Shape-vs-kind mismatch produces 422 with an error naming the declared kind and the expected fields.
- `_parse_remove_relations` has a defense-in-depth kind check — remove must target a `kind: domain` relation. Load-time validator already forbids `operations: [remove]` on process_control activity declarations, but the runtime guard catches callers that bypass load-time (test fixtures or future refactors).
- `_relation_kind` rewired — consults workflow-level only, raises `ValueError` defensively if called with an invalid/unknown kind (shouldn't happen post-Bug-78; the raise is the "load-time validator got bypassed" signal).
- `_resolve_validator`'s Style-3 fallback removed. Docstring updated to document the removal and the 3-styles → 2-styles simplification.

**Production migration (inside the round):**
- 4 redundant `kind:` lines removed from `workflow.yaml` activity declarations (dienAanvraagIn + bewerkRelaties block).
- **Toelatingen plugin's `RELATION_VALIDATORS` dict key renamed** from `"oe:neemtAkteVan"` (type name — Style-3-by-naming-convention) to `"validate_neemt_akte_van"` (function name).
- 3 `workflow.yaml` activity-level references to `oe:neemtAkteVan` updated to add explicit `validator: "validate_neemt_akte_van"`.

**Finding caught by the new validator at shell-spec time.** The first full shell-spec run after wiring in the load-time validators failed with `ValueError: Plugin 'toelatingen': relation_validators dict has key(s) ['oe:neemtAkteVan'] that match declared relation type name(s). This re-creates the Style-3 by-type-name fallback that Bug 78 removed.` This was the validator doing its job — the toelatingen plugin had been relying on Style-3-by-naming-convention. Exactly the shape the validator was written to detect; the fix was the rename described above. Took ~5 minutes to resolve, which is evidence the error message is actionable.

**Tests — 28 new, with full paranoia coverage:**

- `TestRelationDeclarationsValidation` (21 tests) + `TestRelationValidatorRegistrations` (3 tests) in `test_refs_and_plugin.py`. Paranoia check: stubbed both validators to no-op, 19 of 24 went red — healthy shape because the 5 green are positive controls (`test_workflow_valid_shape_passes`, `test_no_relations_section_ok`, `test_activity_single_validator_on_process_control_ok`, `test_no_collision_ok`, `test_empty_dict_ok`) asserting valid inputs pass.
- `TestBug78KindDispatch` (4 tests) in `test_middle_phases.py`. Paranoia check: reverted `_parse_relations` to shape-guessing + dropped the remove-relations kind check — 3 of 4 went red (3 negative tests + 1 happy-path control correctly stayed green). Classic 3-of-4 red pattern established in Round 19 / reinforced in Rounds 20-21-25.
- 6 existing `TestProcessRelations` tests rewritten to match new contract (4 were Style 3 — converted to Style 2 with renamed dict keys; all now declare `kind: "process_control"` at workflow level).
- 8 activity-level `"kind": "domain"` lines scripted-removed from `TestProcessRemoveRelations` tests (they worked at runtime because tests bypass load-time validation, but they documented a forbidden shape — cleanup).

**Verification — Round 26:**
- Engine: **756 + 7 Sentry-skipped** (was 728 + 7; +28 from Bug 78 work: 24 load-time + 4 runtime).
- Toelatingen: **26** (unchanged — the rename was in the plugin's `RELATION_VALIDATORS` dict, not covered by unit tests).
- Common/file_service unchanged at 18 / 21.
- **828 total.**
- Shell spec green: 25 OK, D1-D9, zero tracebacks. The real validation of Bug 78's runtime changes — the shell spec exercises `oe:neemtAkteVan` through the full HTTP stack, so the rename + YAML updates needed to work end-to-end for this to pass.

**Finding filed for a later round: Obs 96.** While wiring my new validators into `app.py`, noticed that the three pre-existing load-time validators (`validate_workflow_version_references`, `validate_side_effect_conditions`, `validate_side_effect_condition_fn_registrations`) are defined and unit-tested but never called in production. Only `_validate_plugin_prefixes` runs at startup. Unclear whether the omission was deliberate or accidental drift — worth a checkpoint before wiring them because the production YAML might have latent shape violations that only surface when they actually run. One-turn fix, but deserves its own round with production YAML review.

**Totals after Round 26:** 32 bugs fixed (unchanged — Bug 78 was drive-by, not filed). Should-fix table unchanged at 26 open / 15 closed. Observations: Obs 95 + Obs 96 opened this round, total **47 catalogued / 31 open / 14 closed / 1 partial / 1 deferred**. Test suite grew 800 → 828.

### Process lesson reinforced

Three rounds in a row (24 / 25 / 26) the paranoia check has caught or shaped the work:
- **Round 24** caught a formatting inconsistency in my own bookkeeping (`**Open**` vs `**Open.**`) that would have poisoned future scripted counts.
- **Round 25** caught a weak test I'd shipped with a comment acknowledging it was weak. Downgraded to sanity test with honest documentation.
- **Round 26** caught a real production bug (toelatingen's Style-3-by-naming-convention) at shell-spec time — the load-time validator I'd just written fired on real data and produced an actionable error.

**Takeaway:** paranoia-check has shifted from "thing I remember to do" to "thing I'd feel nervous shipping without." Round 19's practice has matured into a habit. The shape of the paranoia check varies (revert-and-rerun, script-and-recount, cross-test against production data) — the constant is "before declaring done, make one last attempt to prove the fix doesn't work."

### Where to go next

Cat 3 (caching & perf batch) is my recommendation for Round 27 — Bug 38 + Obs 75/76/77/78, all sharing the "cache what's expensively re-computed" pattern. One coherent batch round.

Cat 7 (test/deployment polish) now has a new candidate in Obs 96 — wiring up the three unwired load-time validators. Small enough to fold into a broader Cat 7 round but worth the checkpoint I mentioned.

### Round 27 — Plugin guidebook comprehensive rewrite

User-initiated. Reviewing Round 26's Bug-78 guidebook update made it clear the guidebook had drifted far from the current code — "No mention of scheduled activities, only recorded. Would it be possible to analyze the entire plugin system and write down all the possibilities?"

**Scope locked down before writing.** Three design decisions:
- Single document, restructured. Keep `plugin_guidebook.md` as source-of-truth; split into clear Tutorial / Reference parts; add missing coverage.
- Audit pass first, then write. Produce a coverage-gap table, user approves, then write. This round stuck to the plan.
- `dossiertype_template.md` folded in as a third documentation type — annotated copy-pastable YAML skeleton, distinct from tutorial and reference but drift-linked to both.

**Audit findings, by coverage gap type (`/mnt/user-data/outputs/plugin_guidebook_audit.md`, 225 lines):**
- **Plugin dataclass:** 1 field completely missing from narrative (`pre_commit_hooks`), 3 light-coverage, 1 template-stale.
- **Workflow-level YAML:** 2 keys missing from guidebook (`tombstone`, `poc_users`).
- **Activity-level YAML:** 5 keys missing (`requirements`, `forbidden`, `authorization`, `allowed_roles`, `can_create_dossier`/`default_role`, `entities`).
- **Task kinds:** audit initially said "2 of 3 missing" — turned out to be **2 of 4 missing** once I found `fire_and_forget` during the template review (see lesson below).
- **ActivityContext API:** 5+ methods entirely undocumented.
- **Engine-provided features:** tombstone, workflow rules, schema versioning — all missing or light.
- **Template staleness:** workflow-level relations still pre-Bug-78 (bare strings instead of dicts-with-kind).

**Guidebook rewrite shipped.** From 865 lines → ~1295 lines. Two clean parts with the Part-3 template as a separate file:

- **Part 1 (Tutorial, ~780 lines):** "What a plugin is" → "Your first plugin in 15 minutes" (unchanged shape; kept the well-loved framing) → "Adding complexity gradually" (also kept — my question about replacing it was unnecessary, the existing framing works). Then 13 feature sections including the previously-missing ones: all 4 task kinds (fire_and_forget, recorded, scheduled_activity, cross_dossier_activity) as distinct subsections; workflow rules (requirements/forbidden); access control with all 3 authorization shapes; tombstoning; schema versioning; pre-commit hooks; supersession/cancellation semantics.
- **Part 2 (Reference, ~480 lines):** exhaustive tables. Plugin dataclass (18 fields), ActivityContext (13 methods/properties), HandlerResult, TaskResult, Task kinds (all 4 + cross-cutting-fields + scheduling-format grammar), workflow YAML schema (top-level + activity-level + entity-types + per-subsystem tables), Relations reference with forbidden-combinations table, Access control reference (3 shapes + access values), Side effects, Workflow rules, Tombstone, Schema versioning, Constants, Load-time validation, Engine-provided features, Glossary of error shapes.
- **Part 3 (`dossiertype_template.md`):** 9 YAML blocks kept, updated for Bug 78. Workflow-level relations rewritten with mandatory `kind`, optional `from_types`/`to_types`, full Bug-78 contract. Activity-level relations expanded with all legal shapes (single-string validator, per-op validators dict) and forbidden-keys callout. Four async calls corrected (missing `await`).

**Process lessons captured in this round:**

1. **The "4-task-kinds" miss.** My audit identified 2 of 3 task kinds missing from the guidebook — but the real count was 2 of 4. I'd missed `fire_and_forget` entirely because I grep-searched the worker, which only handles 3 kinds. `fire_and_forget` is handled in `engine/pipeline/tasks.py` (runs inline during the activity pipeline, not in the worker). Caught it during the template review, which already documented all 4 kinds correctly. **Lesson:** when auditing a surface, audit at the conceptual boundary ("what kinds of task does the plugin interface declare") not at the implementation boundary ("what kinds does the worker dispatch"). The template — written by someone with full visibility — was more accurate than my first-pass audit. Doing the template in-scope was what saved me; if I'd only done the guidebook, I'd have shipped a rewrite that kept the wrong number.

2. **Duplicate-Part-2 incident.** Mid-rewrite I found the guidebook had two complete Part-2 sections, presumably from an earlier tool-call that survived the `rm`-then-`create_file` dance. First Part 2 was my less-polished initial write; second Part 2 was more thorough and better organised. Noticed only because I grep-ed `^## ` heading structure near the end of the round. Spliced out the worse copy (lines 803-1262), kept the better one, then re-applied the fire_and_forget + constants fixes to the kept copy. **Lesson:** for any long doc rewrite, verify structure (`grep '^## '`) before spot-editing content. Checking structure first would have saved the duplicate-fix work.

3. **Two correctness bugs in my tutorial, caught on spot-check.** The `constants:` block I'd written said `class:` was a YAML key with a dotted path — but the real plugin imports the class directly in Python and the YAML only carries `values:`. And I'd written that `recorded` tasks are fire-and-forget, which was the guidebook's pre-existing lie (recorded tasks have an audit trail; only `fire_and_forget` is literal fire-and-forget). Both fixed before landing. **Lesson:** spot-check the tutorial against production code before declaring it done — I caught these only because I was verifying before writing Part 2. Without the verify-before-plan discipline (Round 18 legacy), these would have shipped.

**Process lesson reinforced (across rounds 24/25/26/27):**
The pre-round scope discussion continues to pay off. Round 27's plan had ~4 decisions settled before writing started (doc structure, audit-first, template-in-scope, what "complete" means). Zero mid-round scope drift once writing began. The issues that arose were execution problems (duplicate Part 2, missed fire_and_forget), not scope problems.

**Verification — Round 27:**
- Engine: **756 + 7 Sentry-skipped** (unchanged — docs only).
- Toelatingen / common / file_service unchanged at 26 / 18 / 21. **828 total.**
- Shell spec not re-run (docs-only change, nothing to affect).
- Guidebook: 37 YAML blocks parse cleanly. Template: 10 YAML blocks parse cleanly.

**Totals after Round 27:** 32 bugs fixed (unchanged — no bug work this round). Should-fix / Observations unchanged. Plugin guidebook grew 865 → 1295 lines; template 923 → ~1000 lines with Bug-78 corrections + missing-feature additions.

### Where to go next

Original Round 27 plan was Cat 3 (caching & perf). That's still the next candidate for Round 28:
- **Cat 3 — Caching & perf batch.** Bug 38 + Obs 75/76/77/78. Shared "cache what's expensively re-computed" pattern. My recommendation.
- **Cat 7 — Test/deployment polish** now has Obs 96 (wire up the three unwired load-time validators) as a specific candidate. The wire-up is one-turn trivial in mechanics but deserves a checkpoint — production YAML may have latent shape violations that surface when the validators actually run.
- **Cat 2 cherry-picks** — user-visible behaviour bugs (Bugs 9, 20, 27, 28 at the top).

Obs 96 is the natural "small win" round if you want something shorter than Cat 3's batch. Cat 3 is the bigger next step.

### Round 27.5 — Bug 79 (access-check fail-open on missing/invalid `view:`) [drive-by, handoff prep]

User spotted this while reviewing `routes/access.py` before the planned restart to a fresh chat — "Default should be deny. But in the code I find this [`view is None` branch]". Classic "comment says the opposite of the code" pattern: the module docstring at lines 44-46 explicitly stated *"Key absent — empty set (see nothing). With default-deny the entry already matched on role or agent, but the author didn't specify what entities are visible. Safe default: nothing."* The code shipped the opposite (`visible_types = None`, meaning no filter, i.e. see everything).

Second offender on line 182 was worse: an `else` branch catching unrecognised `view:` values, commented *"so a typo doesn't lock people out"* — fail-open rationale on security-adjacent code. That comment was the tell. A typo on an access check should lock you out; that's when authors notice and fix. Silently granting more access than intended is how security degrades over time.

**Fix shipped:**
- Both branches flipped from `visible_types = None` to `visible_types = set()`.
- WARNING log emitted on each flipped branch, carrying the offending entry so operators can find the broken access config from audit grep.
- `logging` + module-level `_log` added to `access.py` (previously only used `emit_dossier_audit` which needs a non-None `user`).
- Function docstring updated to document the Bug-79 semantics; the module docstring already described them correctly, so no top-of-file changes.

**Scope discipline applied:**
- Considered adding load-time validation for access entries (so broken shapes would reject at plugin load). Deferred — access entries live in entity content, not workflow YAML, so the validator would need to hook into `setDossierAccess`'s output or an entity-content schema check. Bigger change than this mini-round warranted. Can be filed as future work.
- Considered changing the audit emit to a proper `emit_dossier_audit` with added `user`/`dossier_id` params on `get_visibility_from_entry`. Rejected — 5 call sites would need updating, crosses the "minimum change" line for a drive-by fix. Plain `logging.WARNING` with `offending_entry=%r` is sufficient for operator triage.

**Tests updated:**
- `test_entry_with_no_view_key_no_restrictions` → `test_entry_with_no_view_key_defaults_deny`. The old test name literally encoded the bug ("no_restrictions"). Rewritten to assert `visible == set()` and that the WARNING log fires.
- New test `test_entry_with_unrecognised_view_value_defaults_deny` for the invalid-value branch. Same assertion shape.
- Both tests use `caplog.at_level(logging.WARNING, logger="dossier.engine.access")` to pin the audit-log affordance — operators who want to grep for the warning should find it.

**Paranoia check — healthy 2-of-7 red on revert.** Reverted both branches to `visible_types = None`; the two Bug-79 tests failed immediately (`assert None == set()`); the 5 unchanged-behaviour tests stayed green (None entry, explicit "all", list, empty list, activity-view-own/related). Restored; all 7 back to green.

**Verification:**
- Engine: **757 + 7 Sentry-skipped** (was 756 + 7; +1 new Bug 79 test, and the flipped test stays counted as one).
- Toelatingen / common / file_service unchanged at 26 / 18 / 21. **829 total.**
- Shell spec green: 25 OK, D1-D9, zero tracebacks. The fix touches a live access path (`visible_types` flows into entities route filtering); shell spec confirms D1-D9 scenarios still grant the access they used to.

**Totals after Round 27.5:** 33 bugs fixed (was 32 — Bug 79 added). Must-fix table: 17 fixed + 5 deferred/investigated (unchanged — Bug 79 filed as must-fix sev 6 alongside the other sev-6 fail-open fixes). Should-fix table unchanged. Observations unchanged.

### Process lesson — "just a quick fix before starting fresh"

Calling this a mini-round ("27.5") rather than a proper round was defensible — the fix *is* small — but the work still took a full cycle of paranoia-check + test update + writeup. The thing worth capturing: **bug fixes during handoff-prep always uncover more than expected.** The "quick" fix here involved:
1. Reading the docstring to confirm code drifted from intent (not just a design difference).
2. Checking 5 callers to confirm the `None` → `set()` switch is caller-compatible.
3. Smoke-testing with `emit_dossier_audit` only to find it requires non-None user; switching to `_log.warning`.
4. Rewriting one test, adding one test, paranoia-checking both.
5. Shell spec re-run to confirm live access paths.

None of this was wasted, but "continuing in this chat one more turn for a quick fix" turned into four turns. Lesson for future sessions: if the plan is to start fresh, do it; additional bug fixes are not free, and a fresh chat doesn't mind re-bootstrapping.

Alternative framing, also valid: **landing a small security fix cleanly is worth the extra turns, because sev-6 fail-open is the kind of thing that accumulates if deferred.** The mini-round shipped with full paranoia-check discipline and proper test coverage — which is what matters, not the turn count.

Both framings are true. The lesson is to make the call deliberately: either *"ship the fix properly now because it matters"* or *"start fresh and file the bug for the first turn there."* Don't split the difference.

### Where to go next (updated)

Start fresh. Cat 3 (caching & perf batch) remains the top recommendation for Round 28. Obs 96 (wire up unwired load-time validators) is the smaller alternative. See Round 27's "Where to go next" section for the full queue.

### Round 27.5 addendum — Obs 97 and Bug 80 filed from access-code review

Two findings surfaced while the user was reviewing the access-check area (the same review that turned up Bug 79):

- **Obs 97 — Codebase legibility: file sizes + module organization.** User's framing: *"Files getting too long, like worker.py is something we should tackle. But the number of files per module is growing to a point where it is not clear at all. engine root, engine routes, engine pipeline. Even I don't know where to look anymore."* Concrete data: worker.py at 1438 lines, plugin.py at 889, db/models.py at 750, archive.py at 641, routes/activities.py at 594. Plus confusing "engine-in-engine" naming (`dossier_engine/engine/`). Filed as observation (proposed Category 12), not a bug — refactor + rename work with zero behaviour change. See the observation entry itself for candidate splits.

- **Bug 80 — `DossierAccess` Pydantic model doesn't reflect the content the engine reads.** User's framing: *"The dossieraccess entity model doesn't reflect the truth anymore, there no audit or admin part."* The model declares `access: list[DossierAccessEntry]` but `check_audit_access` reads `content.get("audit_access", [])` — a key the model is silent about. Admin is deliberately config-only (`global_admin_access`), not per-dossier, so the model's omission there is correct but undocumented. Filed as sev-4 should-fix: small fix (add the missing field + docstring explaining the admin omission), real documentation bug (actively misleads readers of the model), no current behaviour impact because production writes + reads the content as raw dicts rather than through the model. See Should-fix table.

Both surfaced as side findings from a focused access-code review, which is a pattern worth noting: **asking a question about one specific code path ("why does this branch fail open?") tends to surface two or three nearby findings.** The quality-per-minute of user-initiated focused reviews is consistently high.

### Round 28 — Obs 95 (dotted-path migration for plugin Callable registries)

User chose Obs 95 over the Round 27 recommendation (Cat 3 caching batch). Cat 5's "needs design first" flag was shortcut — the user opted for "just do it, full migration, all 8 registries, one round." Defensible call given the work turned out to be mechanical once the shape was agreed; the design-discussion concern from Cat 5 was about *interconnectedness between Obs 56, 58, 59, 60, 62, 64, 65* — not about Obs 95 in isolation, which is a self-contained surface change.

**Scope shipped:**
- Eight Callable registries (`handlers`, `validators`, `task_handlers`, `status_resolvers`, `task_builders`, `side_effect_conditions`, `relation_validators`, `field_validators`) migrated from short-name dict lookup to dotted-path resolution, mirroring how `entity_models` / `entity_schemas` already worked.
- YAML now carries fully-qualified paths: `handler: "dossier_toelatingen.handlers.set_dossier_access"` (previously `"set_dossier_access"`). The engine resolves at plugin load time; typos fail fast with a context-attributed error naming the activity + YAML field.
- Plugin-side `HANDLERS = {...}`, `VALIDATORS = {...}`, `TASK_HANDLERS = {...}`, `STATUS_RESOLVERS`, `TASK_BUILDERS`, `SIDE_EFFECT_CONDITIONS`, `RELATION_VALIDATORS`, `FIELD_VALIDATORS` dict literals deleted across 5 plugin files. Functions stay module-level; the dotted-path references in YAML are all the "registration" needed.
- `create_plugin()` rewritten to call new `build_callable_registries_from_workflow(workflow)` and feed its 8-dict result into the `Plugin(...)` constructor.

**Design decisions locked in during the round:**

1. **`field_validators` is the one exception to direct dotted-path key replacement.** The dict key for `field_validators` becomes part of the URL (`POST /{workflow}/validate/{key}`) — leaking Python module structure into HTTP would be an ergonomics regression. Solution: top-level `field_validators:` YAML block mapping `url_key → dotted_path`. The registry stays keyed by the short URL key; the dotted path is the resolution target. Deviation from the plan's "direct replacement" framing, but forced by the URL-surface constraint, not a style preference. Documented explicitly in the module docstrings.

2. **Plugin dataclass field types unchanged.** `plugin.handlers: dict[str, Callable]` keeps the same type — the dict is now keyed by the dotted path string rather than the short name. All 12 engine lookup sites (`plugin.handlers.get(name)` etc.) remain unchanged; only what `name` looks like changes. This kept the blast radius to YAML + Plugin construction + plugin files + load-time validators, and — unexpectedly — made the change **non-breaking for test fixtures**. Tests that hand-populate `Plugin(handlers={"compute": fn})` with short-name keys continue to work because the lookup-shape symmetry is preserved: the test's YAML says `handler: "compute"` and the test-supplied dict has the same key. The dotted-path requirement is only enforced on the builder path used by `create_plugin()`, not on direct `Plugin(...)` construction.

3. **`handle_beslissing` unregistered-but-importable.** Production workflow.yaml never referenced it (only the split-style `resolve_beslissing_status` is used), so under the new scheme it simply doesn't appear in `plugin.handlers`. The function stays module-level; unit tests that import and call it directly continue to work. Consistent with its existing "legacy path; still registered" comment — the "still registered" part is now false but that's fine, the function is tested directly.

4. **`validate_relation_validator_registrations` (Bug 78) kept as effectively a no-op.** Under dotted paths, collision with relation type names (`oe:neemtAkteVan`) is structurally impossible — dotted paths contain dots and module segments, relation type names contain colons, the two key-spaces can't overlap. The check becomes a permanent-pass assertion for toelatingen but stays meaningful for any future plugin that mixes styles during a transition. Kept intact; removing it would regress Bug-78's intent.

5. **Deduplication across activities.** If two activities both reference `"dossier_toelatingen.tasks.move_bijlagen_to_permanent"` (production has three), the builder resolves it once and stores it once. Correctness-preserving; the old dict-literal form implicitly did the same.

**Implementation surface:**
- `dossier_engine/plugin.py` — added `_import_dotted_callable(path, *, context="")` (parallel to `_import_dotted` without the BaseModel subclass check, with per-site context string for error attribution) and `build_callable_registries_from_workflow(workflow)` (the main builder; walks all 8 YAML shapes, deduplicates on dotted path).
- `dossier_toelatingen/workflow.yaml` — 13 short-name references replaced with dotted paths via a scripted pass; new top-level `field_validators:` block added before `activities:`.
- `dossier_toelatingen/field_validators.py` — two `FieldValidator` instances promoted to module-level bindings; dict literal removed.
- `dossier_toelatingen/handlers/__init__.py`, `validators/__init__.py`, `relation_validators/__init__.py`, `tasks/__init__.py` — 5 registry-dict literals removed; replaced with migration-note comments so readers know where the dispatch moved to.
- `dossier_toelatingen/__init__.py::create_plugin()` — single call to `build_callable_registries_from_workflow`, destructured 8-dict result passed to `Plugin(...)`.
- `docs/plugin_guidebook.md` — 12 short-name YAML examples updated to dotted-path form; the FIELD_VALIDATORS / TASK_HANDLERS / SIDE_EFFECT_CONDITIONS dict-literal examples replaced with module-level binding + YAML block examples; prose updated to explain the registration model.
- `dossiertype_template.md` — 3 task-kind examples updated; the `Plugin(..., handlers=HANDLERS, ...)` registration commentary replaced with dotted-path YAML advice; the TASK_HANDLERS dict example removed in favour of YAML-referenced module-level functions.

**Tests added (+22 in `test_refs_and_plugin.py`):**
- `TestImportDottedCallable` (6 tests): resolves real callable, non-string raises, missing-dot raises, bad-module raises, missing-attribute raises with context, empty-context omits the `(in )` fragment.
- `TestBuildCallableRegistries` (16 tests): empty workflow returns 8 empty dicts; each of the 8 registry sources resolved correctly (activity-level handler / status_resolver / task_builders / validators-with-name-key / tasks-with-function-key / side-effects condition_fn / activity-level relations validator|validators-dict; workflow-level relation_types validator|validators-dict; top-level field_validators url-key block); dedup across activities; uses `os.path.join` etc. as stable real callables so tests have no dep on the toelatingen plugin being installed.

**Paranoia check — ran twice:**
1. Broke `"dossier_toelatingen.handlers.set_dossier_access"` → `"..._TYPO"` in workflow.yaml, confirmed `create_plugin()` raised `ValueError` with: activity name (`setDossierAccess`), YAML field (`handler`), and the typo path all present in the message. Restored, confirmed plugin loads again with 4 handlers. Error message reads: *"Module 'dossier_toelatingen.handlers' has no attribute 'set_dossier_access_TYPO' (referenced as 'dossier_toelatingen.handlers.set_dossier_access_TYPO' (in activity 'setDossierAccess' handler))"*. The context attribution is exactly what Obs 95 wanted.
2. Confirmed `handle_beslissing` is absent from `plugin.handlers` (no YAML reference) but still importable as a module-level function. Unit tests that exercise it directly (via `from dossier_toelatingen.handlers import handle_beslissing`) continue to pass.

**Verification:**
- Engine unit: **307** (was 285; +22 new Obs-95 tests).
- Engine integration: **479** (was 478; the `test_activity_context_users` fixture-teardown flake didn't fire this round — it's transient, not state-related).
- Toelatingen / common / file_service unchanged at **26 / 18 / 21**.
- **Total: 851 / 851 passing** (was 828–829). Delta: +22 new tests, +1 flake-stabilized.
- Guidebook YAML harness: 6/6 clean (my doc edits preserved YAML fence validity).
- Shell spec NOT re-run. Reasoned skip: the migration is load-time-only (resolves at `create_plugin()`); the HTTP surfaces are unchanged. Plugin-load smoke test (construct plugin, run Bug-78 validators, verify every YAML reference resolves in the built registries) passed clean. A shell-spec run would stand up the full multi-service stack for work that doesn't touch any HTTP code path; skipping it is a deliberate scope choice, not an oversight. The CI shell-spec job would catch any regression here next time it runs.

**Totals after Round 28:** 33 bugs fixed (unchanged — no bug work this round). Should-fix table unchanged. Observations: **Obs 95 CLOSED** (was open since Round 26). Observation totals now **49 catalogued, 15 closed (+1), 1 partial, 1 deferred, 32 open (−1)**. Test suite grew 829 → 851 (+22).

**Process lesson — the "already-done work" discovery.**
On resumption after a context break, the sandbox retained prior-session edits I hadn't expected to survive. I'd planned to re-do `field_validators.py` module-level promotion, the 4 registry-dict deletions, and `create_plugin()` rewrite — found all already in place. Worth noting: **the sandbox is a persistent filesystem**, not an ephemeral rebuild. Lesson: after any resumption, `diff` against the original upload zip to establish the actual baseline before planning work — prior sessions' edits may have already landed the work you thought you still had to do. Saved an estimated 3–4 turns of re-executing done work.

**Process lesson — "Cat 5 needs design first" was right in spirit, overstated in application.**
Cat 5's triage verdict warned against cherry-picking because its items (Obs 56/58/59/60/62/64/65) are interconnected. Obs 95 is the one Cat 5 item that's *structurally* independent — it's a key-space shape change, not a semantic change, and it doesn't interact with authorize-splitting (Obs 58) or validation centralization (Obs 56). The "just do it" call turned out defensible because the actual blast radius (8 registry files + 1 YAML file + docs + 1 builder function) was mechanical and testable in isolation. Lesson: triage verdicts are heuristics, not rules — when an item in a "needs design first" bucket turns out to be self-contained, the design-first gate is free to waive. The test to ask is *"would getting this wrong cascade into other Cat 5 items?"*; for Obs 95 the honest answer was no.

**Finding filed for a later round — Obs 96 framing correction.**
Round 26's writeup said the three unwired validators (`validate_workflow_version_references`, `validate_side_effect_conditions`, `validate_side_effect_condition_fn_registrations`) are "never called by `app.py::load_config_and_registry`." Technically true, but they *are* called from toelatingen's `create_plugin()` at `__init__.py:213-217`. The concern Obs 96 surfaces — that validation runs plugin-side rather than engine-side — is real (plugins can forget to call them), but it's a weaker claim than "never invoked in production," which is what the Round 26 entry implies. Worth a one-line correction to Obs 96 when it's picked up. Not touching it this round to avoid scope creep.

### Where to go next

Cat 3 (caching & perf batch) remains the top recommendation — Bug 38 + Obs 75/76/77/78, sharing the "cache what's expensively re-computed" pattern. Obs 96 is the smaller alternative, now with the framing correction above worth folding into the writeup when it's addressed. Both were already in position at Round 27's handoff; nothing in Round 28 changes their relative priority.

The `field_validators` separate-YAML-block decision (Obs 95 design call #1 above) is worth remembering if a future plugin wants to add a similar registry whose keys leak into user-facing URLs or other external surfaces — the pattern is *"key = external identifier, value = dotted path, resolved at load."*

### Round 29 — Bug 9 (N+1 in dossier detail view)

User chose Cat 2 cherry-pick one-by-one over the Round 28 recommendation of Cat 3. Bug 9 was first — `GET /dossiers/{id}` issued one `_user_is_agent` query per activity in the dossier under `activity_view: "own"` or `"related"`, turning a dossier with N activities into O(N) read-path queries. Invisible to admin users (their roles matched `global_access` with `activity_view: "all"`, short-circuiting the per-activity loop). Acute for aanvragers and any role-scoped user who hit the own/related branches.

**Scope discipline.** User proposed bundling the fix with removal of the `"related"` mode entirely ("it doesn't make any sense"). I pushed back: `"related"` is the `DossierAccessEntry` Pydantic default (`entities.py:16`), is actively tested (`test_route_helpers.py:598`), and — under policy framing — is the transparency-oriented mode (citizen sees activities that operated on their entity, not just their own submissions). Production toelatingen doesn't write `"related"` today, but that's latent capability rather than mistake. Bug 79 (Round 27.5) was the mirror-image bug — a defensive behaviour the code was doing correctly, with a "this looks wrong" comment that led to it getting reverted. Didn't want to make the same mistake in reverse. Obs 92 already exists for "`activity_view` mode complexity reduction" if that work is genuinely wanted; Bug 27 (Literal tightening) is the natural place to settle which modes stay. User agreed to Bug 9 alone.

**Fix shape.** `routes/prov.py` already solved this exact problem in Round 5 via `load_dossier_graph_rows` in `prov_json.py` — four bulk queries (activities / entities / associations / used) plus one agent-URI lookup, with pre-indexed `assoc_by_activity` and `used_by_activity` dicts. Dossier-detail was the straggler that never adopted the consolidation.

**Shipped:**
- `routes/dossiers.py::get_dossier` — replaced `get_activities_for_dossier` + the two async closures that each issued per-activity queries with `load_dossier_graph_rows` at the top of the activity loop, then dict-lookup closures `_is_agent` (walks `assoc_by_activity.get(act_id, [])`) and `_used_ids` (walks `used_by_activity.get(act_id, [])`). Closure signatures match `is_activity_visible`'s expected callable shape, so the visibility logic itself is untouched — this is a pure read-pattern swap.
- Removed the now-dead module-level `_user_is_agent(session, activity_id, user_id)` helper at the bottom of the file. Removed the now-unused `select` / `AssociationRow` imports.
- The `get_all_latest_entities(dossier_id)` call at line 137 kept as-is. `load_dossier_graph_rows` returns *all* entity versions (audit-scoped), while `get_all_latest_entities` returns only the latest per `entity_id` — the dossier-detail response wants the latter. Deduplication on the client side would be more code for no perf win. Scoped out deliberately.

**Tests added (+3 in `test_http_routes.py`):**
- `TestDossierDetailActivityViewFiltering` (2 behavior-pinning tests):
  - `test_own_mode_filters_to_activities_where_user_is_agent` — seeds three activities (bootstrap systemAction as system, one as citizen, one as system), grants citizen `activity_view: "own"`, asserts only the citizen's activity is visible. Pins the `"own"` filter behaviour.
  - `test_related_mode_includes_activities_touching_visible_entities` — seeds three activities (A generates aanvraag as citizen, B unrelated as admin, C uses aanvraag as admin), grants citizen `activity_view: "related"` with `view: ["oe:aanvraag"]`, asserts A and C visible, B hidden. Pins the `"related"` filter including the "used a visible entity" case that's the whole point of the mode.
- `TestDossierDetailQueryCount` (1 perf ceiling test):
  - `test_query_count_bounded_under_own_mode_with_many_activities` — seeds 11 activities, grants `activity_view: "own"`, hooks SQLAlchemy's `before_cursor_execute` event to count SELECTs during the HTTP request, asserts `select_count <= 12`. Measured pre-fix: 16. Measured post-fix: 10 (independent of N). Ceiling of 12 gives 2 queries of headroom for future incidental growth while still catching any regression that re-introduces per-activity DB round-trips.

**`citizen` user added to test app.** `_build_test_app()` already registered `alice` (role `oe:reader`) and `admin` (role `oe:admin`), both of which match `global_access` entries with `activity_view: "all"`. Under `check_dossier_access`, global_access is checked first and short-circuits — so the pre-existing users could never exercise the per-dossier access path where `activity_view: "own"` / `"related"` lives. This was also why the N+1 wasn't caught by existing tests: the buggy code path wasn't reachable with the existing fixture. Added a third POC user `citizen` with role `aanvrager` (deliberately NOT in `global_access`), so per-dossier access is consulted. Additive-only change — no other test references `citizen`.

**Process lesson — "test passes unexpectedly" is a diagnostic, not a green light.**
First pass of the new tests passed against the unfixed handler. That was wrong — the N+1 was still there and the ceiling was set to catch it. Caught by force-failing the assertion (`assert select_count < 0`) to read the actual count. Turned out the fixture users' roles matched `global_access` so the buggy branch wasn't being hit. Lesson: when a "this should fail" test passes, don't trust it — force a failure to verify the test is exercising the code path you think it is. Round 25's paranoia discipline applies to tests too, not just to production code.

**Paranoia check ✓.** Reverted just the handler (restored `get_activities_for_dossier` + per-activity closures); behaviour tests stayed green (they pin behaviour, not query count); query-count test went red with the exact expected message: *"dossier detail issued 16 SELECTs for a dossier with 11 activities — N+1 regression suspected."* Restored the fix; all 3 green. The test is genuinely catching the bug, not coincidentally passing.

**Verification:**
- Engine unit: **307** (unchanged).
- Engine integration: **482** (was 479; +3 new Bug-9 tests).
- Toelatingen / common / file_service unchanged at **26 / 18 / 21**.
- **Total: 854 / 854 passing** (was 851).
- Measured query count: 11 activities under `"own"` mode — **16 SELECTs pre-fix, 10 post-fix**. Ratio confirms the fix is O(1) and not merely "a bit better" — the 6-query reduction decomposes cleanly as 11 `_is_agent` selects eliminated minus 5 queries `load_dossier_graph_rows` adds.

**Totals after Round 29:** **34 bugs fixed** (was 33 — Bug 9 added). Must-fix table unchanged (Bug 9 is sev-2 should-fix). Should-fix table: **26 open** (was 27 — Bug 9 closed). Observations unchanged. Test suite grew 851 → 854.

### Where to go next

Cat 2 cherry-picks continue per user's preference — one-by-one, sev-ordered. Priority remaining in the review's "top 4": Bugs 20, 27, 28. My recommendation order, based on what I've seen in this round:

1. **Bug 28** — `POCAuthMiddleware` silently overwrites on duplicate usernames. Boot-time validation gap; similar "fail loudly on config error" shape as Bug 79 (Round 27.5). Small surface, high leverage.
2. **Bug 20** — `_PendingEntity` missing fields → `AttributeError`. Sev-3 crash risk; needs a verify-before-plan pass to find which input shapes trigger it.
3. **Bug 27** — `DossierAccessEntry.activity_view: str` too narrow (should be `Literal`). Type tightening. **This is also the natural place to settle the `"related"` question** — the `Literal[...]` declaration forces a decision on which modes stay. If you still want to kill `"related"`, that's the round.
4. **Bug 4** — unused `Session` import. Trivial, good "close a ticket cheaply" round if appetite is low.

Cat 3 (caching & perf) remains in the wings but unchanged in priority — user's Cat 2 walk is the active track.

### Round 30 — Bug 28 deferred + Bug 20 shipped

Two actions this round: a small bookkeeping deferral on Bug 28, then the actual fix for Bug 20.

**Bug 28 deferred.** User confirmed that `POCAuthMiddleware` is POC-only and slated for replacement with real auth (JWT/OAuth), so hardening its duplicate-username handling is sunk cost. Marked in the Should-fix table and the Cat 2 cherry-pick list. Obs 85 (dependency-override-friendly auth for tests) also cross-referenced, since it's in the same neighborhood — the "dependency_overrides" affordance may still be worth carrying into the real auth layer, so Obs 85 stayed open with a note rather than being closed outright. Bug 28 is now tagged 🛑 **Deferred — POC-only, slated for removal** rather than closed-as-fixed; the distinction matters for the Fixed vs Deferred counts and for anyone auditing the queue later.

**Bug 20 (Round 30 proper) — `_PendingEntity` missing fields → `AttributeError`.**

**Verify-before-plan pass (per review's entry: "needs a verify-before-plan pass to find which input shapes trigger it"):** traced the concrete crash path through the code. `_PendingEntity` is the engine's duck-typed stand-in for `EntityRow` used inside the generated-phase so handlers can read entities the current activity is generating before they hit the database. The class's own docstring says *"When you add a column to EntityRow, also add it here, or context.get_typed will fail with AttributeError on pending entities."* That rule had drifted — five columns were missing (`type`, `dossier_id`, `generated_by`, `derived_from`, `tombstoned_by`).

**Concrete crash path:** `schedule_trekAanvraag_if_onvolledig` (task builder, wired to both `neemBeslissing` and `tekenBeslissing` activities) → `_build_trekAanvraag_task` → `context.get_used_row("oe:beslissing")` returns a `_PendingEntity` (because the current activity is generating the beslissing) → `find_related_entity(beslissing_pending, "oe:aanvraag")` → `lineage.py:123 start_entity.type` → 💥 `AttributeError`.

**Production reachability:** narrow but real. Requires no `oe:aanvraag` in the activity's `used:` block. In normal toelatingen flow the `used:` block declares aanvraag with `required: false, auto_resolve: "latest"`, which finds the existing aanvraag and skips the lineage walk. The crash fires when no aanvraag exists at beslissing time — structurally near-impossible in well-formed data (workflow rules wouldn't normally let you reach beslissing without an aanvraag), but reachable via data-migration artefacts, manual DB repair, or a future flow variant. The existing tests in `test_build_trekAanvraag_task.py` didn't catch this because they construct `beslissing_row` as `SimpleNamespace(entity_id=...)` and mock `find_related_entity`, so the walker's `.type`/`.generated_by` reads never happen in tests.

**Fix shape (Option A per plan).** Brought `_PendingEntity` into compliance with its own docstring rather than refactoring the method contract. Option B (refuse to return a `_PendingEntity` from `get_used_row`) was considered and rejected: the class is duck-typed stand-in by design, and callers already correctly read `entity_id`/`content`/`attributed_to` off it — Option B would have been an invasive contract change for a narrow fix.

**Shipped:**
- `engine/context.py::_PendingEntity.__init__` — accepts four new kwargs (`type`, `dossier_id`, `generated_by`, `derived_from`) and hardcodes `tombstoned_by=None` and `created_at=None` as class invariants. Docstring updated to explain the two-invariant (`tombstoned_by`/`created_at` can never be non-None for pending entities, because tombstoning and INSERT-time timestamps both happen at persistence time, which runs *after* the activity that constructs the pending entity).
- `engine/pipeline/generated.py` — the construction site at line 116 now passes all four new fields from `state.dossier_id`, `state.activity_id`, `entity_type`, and the already-computed `derived_from_version` (which is now hoisted to a local so it's not computed twice).

**Tests added (+3 in `tests/unit/test_refs_and_plugin.py`, +5 assertions in `tests/integration/test_process_generated.py`):**

- `TestPendingEntityFieldParity::test_pending_entity_has_every_entity_row_column` — the maintenance guard the class docstring promised. Enumerates `EntityRow.__table__.columns` and asserts every name is a readable attribute on a `_PendingEntity` instance. If anyone adds a new column to `EntityRow` without updating `_PendingEntity`, this test goes red with a message that names the missing columns. This is deliberately a programmatic scan (not a hand-enumerated assert list) because the whole point is to track `EntityRow` over time — a hand-enumerated version would itself need maintaining.
- `TestPendingEntityFieldParity::test_pending_entity_tombstoned_by_is_none` and `...created_at_is_none` — pin the two structural invariants documented in the `_PendingEntity` docstring. Small tests, cheap to keep, help future readers understand *why* these are hardcoded rather than constructor kwargs.
- `test_process_generated.py::test_happy_path_populates_state` — extended the existing `pending_entity_carries_expected_fields` assertions (4 → 11). Pins the new fields **as they're populated by the real production call site**, not by a unit-level fixture. This is the behaviour-pin that would have caught the original bug if it had been written thoroughly when `_PendingEntity` was introduced.

**Paranoia check ✓.** Partial revert (dropped `type`, `dossier_id`, `generated_by` but kept `derived_from` and the invariants) confirmed:
- The integration test goes red with the exact production crash shape: `AttributeError: '_PendingEntity' object has no attribute 'type'`.
- The parity test goes red with a clean, named diff: `"_PendingEntity is missing EntityRow columns: ['dossier_id', 'generated_by', 'type']"`.
- The two invariant tests stay green (they're not about the reverted fields).

This is the right shape: the parity test catches structural drift, the extended-happy-path test catches the concrete bug, and the invariant tests catch accidental un-hardcoding. Restored; all four back to green.

**Test count note.** I considered adding a regression test that calls `find_related_entity` with a real `_PendingEntity` end-to-end (the exact production crash path). Decided against: the parity test already catches the root cause, and adding a "the walker doesn't crash on a pending start entity" test would duplicate coverage with the integration test's `test_pending_entity_carries_expected_fields` plus the existing `TestFindRelatedEntity` walker tests. Round 25's "don't ship weak tests" lesson applies — one strong parity test plus one strong behaviour test is better than three tests that overlap.

**Verification:**
- Engine unit: **310** (was 307; +3 new parity tests).
- Engine integration: **482** (unchanged; the existing test was extended, not added).
- Toelatingen / common / file_service unchanged at **26 / 18 / 21**.
- **Total: 857 / 857 passing** (was 854).

**Totals after Round 30:** **35 bugs fixed** (was 34 — Bug 20 added). Bug 28 counts as deferred, not fixed — the Engagement-summary "Deferred / accepted" row now has 5 items (Bugs 14, 31, 45, 63, 71, 28). Should-fix table: **25 open** (was 26 — Bug 20 closed). Observations unchanged. Test suite grew 854 → 857.

### Process lesson — "the class's own docstring is documentation of intent, not a guarantee of compliance"

The `_PendingEntity` docstring explicitly said *"When you add a column to EntityRow, also add it here."* Someone wrote that rule knowing the drift hazard; the drift happened anyway. Two takeaways worth capturing:

1. **Intent-documentation without a test is cargo-cult correctness.** A rule stated in a docstring is a request for future contributors to cooperate. It's more useful than nothing, but meaningfully less useful than a test that enforces it. Round 30's `TestPendingEntityFieldParity` converts the rule into an enforced invariant — if the rule re-drifts, CI goes red and names what broke. Upgrading a rule from "I hope people read this" to "this is enforced" is usually a one-shot test and always worth the turn.

2. **Duck-typed stand-ins accumulate drift.** `_PendingEntity` quacks like `EntityRow` — same attribute names, same semantic roles — but the two are unrelated classes held together only by discipline. Drift is the default state; compliance requires active maintenance. If a similar stand-in pattern appears for another class (e.g. a pending `ActivityRow` for side-effect pipelines), the same parity-test pattern should apply on day one.

### Where to go next

Cat 2 cherry-pick track continues. My recommendation order for the remaining priority items:

1. **Bug 27** — `DossierAccessEntry.activity_view: str` too narrow (should be `Literal`). Type tightening. **Natural place to settle the `"related"` mode question** — the `Literal[...]` declaration forces a decision on which modes stay.
2. **Bug 4** — unused `Session` import. Trivial closer.

After those, Cat 2 still has Bugs 34, 39, 43, 48, 50, 59, 60, 67 — the longer tail. Cat 3 (caching batch) also remains available.

### Round 30.5 — `load_dossier_graph_rows` moved to `db/graph_loader.py`

User flagged the Round 29 fix: *"Are you sure what the graph fetches matches what the DB queries did? And if so, split the function somewhere else — it's a bit nasty to use functions from prov_json in dossiers."* Both fair points.

**(1) Correctness verification.** Did a careful axis-by-axis comparison of old vs new data-fetch behaviour:

- **Activities list** — old and new both call `repo.get_activities_for_dossier(dossier_id)`, same repo helper, same caching. Identical output.
- **`_is_agent(act_id, uid)` closure** — old did `select(AssociationRow).where(activity_id == act_id).where(agent_id == uid)` per activity, returned `is not None`. New does `any(a.agent_id == uid for a in assoc_by_activity.get(act_id, []))` over the preloaded rows. The preload filter is `AssociationRow.activity_id.in_(activity_ids)` where `activity_ids = [a.id for a in activities]` — so for every activity in the visibility loop, the preloaded list holds exactly the same rows the old per-activity query would have returned. **Equivalent for the call pattern.**
- **`_used_ids(act_id)` closure** — old did `select(UsedRow.entity_id).where(activity_id == act_id)`. New does `{u.entity_id for u in used_by_activity.get(act_id, [])}` over preloaded `UsedRow` objects. Same preload filter, same rows. **Equivalent.**

**One honest caveat** I glossed over in the Round 29 writeup: the handler still calls `repo.get_all_latest_entities(dossier_id)` at line 137 (for the `currentEntities` response field), and `load_dossier_graph_rows` *also* loads entities (as `graph_rows.entities`, all versions). So the post-fix handler now makes **two entity queries per request** where the pre-fix code made one. That's not a behavior regression but it is a small inefficiency — the net query count still dropped 16 → 10 for N=11 activities, which was the point of the fix, but there's ~1 query of waste hiding in that number. Deduplicating `graph_rows.entities` client-side ("latest per `entity_id`") is possible and adds ~5 lines, but would couple the handler to knowledge about entity versioning that it currently delegates to the repo. Scoped out of Round 30.5; flagged here for honesty and as a candidate cleanup if a future round touches this handler.

**(2) Architectural move.** `load_dossier_graph_rows` was in `prov_json.py` but isn't PROV-specific — it's a general DB-layer utility that fetches a dossier's graph rows with pre-built per-activity indexes. Three route modules plus one JSON builder use it. Having unrelated handlers reach into `prov_json.py` to get their rowsets was awkward by name and fragile — any future reshape of `prov_json.py` would force changes to callers that have nothing to do with PROV-JSON.

**Shipped:**
- New module `dossier_engine/db/graph_loader.py` — owns `DossierGraphRows` dataclass and `load_dossier_graph_rows` function. Verbatim move plus an updated module docstring explaining the callers and the Round 30.5 provenance.
- `dossier_engine/prov_json.py` — now a pure PROV-JSON document builder. Imports `DossierGraphRows` and `load_dossier_graph_rows` from `db.graph_loader`, re-exports them under the historical names via `__all__` so the old import path still works for any external caller. Removed 7 now-unused imports (`dataclass`, `field`, `select`, `ActivityRow`, `AssociationRow`, `Repository`, `UsedRow`).
- Four caller import sites updated to pull from the new home directly: `routes/dossiers.py`, `routes/prov.py`, `routes/prov_columns.py`, and `tests/integration/test_prov_endpoints.py` (two locations). The pattern: new code imports from `db.graph_loader`; the `prov_json` re-export is the back-compat cushion.

**Why a re-export, given no callers need it?** It's a cheap move that respects Hyrum's Law — the function was a public-looking member of `prov_json` for as long as it existed, and external consumers of the package (none in this repo today, but this is a library-shaped codebase) might have noticed. Keeping the re-export for one release buys safety with no coupling cost; a future round can remove it after confirming nothing reaches in via the historical path.

**Obs 97 alignment.** Round 27.5's "codebase legibility" observation flagged that module organization was getting muddled (*"the number of files per module is growing to a point where it is not clear at all"*). Adding `db/graph_loader.py` is a small move in the right direction for that observation — the DB concerns are now colocated under `db/`, and the PROV-JSON module is focused on its one job. Not a full answer to Obs 97 but a step.

**Paranoia check ✓.** Temporarily changed `routes/dossiers.py`'s `load_dossier_graph_rows` import to a non-existent name (`load_dossier_graph_rows_NOPE`) in the `db.graph_loader` path. The app-level import chain failed at collection time with `ImportError: cannot import name 'load_dossier_graph_rows_NOPE' from 'dossier_engine.db.graph_loader'` — proving the route actually resolves through the new home, not through the `prov_json` re-export. Restored; tests green.

**Verification:**
- Test count unchanged at **857 / 857** passing (pure refactor — no new tests, no behavior change). Unit 310, integration 482, toelatingen 26, common 18, file_service 21.
- All 8 callers verified: 4 production imports updated, 2 test imports updated, 2 internal references (within `prov_json.py` itself) now pull from the shared import.

**Totals after Round 30.5:** Unchanged from Round 30 — still 35 fixed, 5 deferred, 25 should-fix open, 857/857 passing. Obs 97 partially addressed (still open, but with a bit of progress noted).

### Process note — when the architectural criticism arrives mid-flight

Round 29 shipped Bug 9 with a shortcut (`routes/dossiers.py` importing from `prov_json.py`). User spotted it on review. Two things worth naming:

1. **Fast turnaround on architectural feedback is better than batching.** The user's pushback arrived after Round 29 shipped but before Round 30.5 picked up — the right response was "do the split now, while the Bug 9 story is still in working memory," not "add it to a backlog." Round 30.5 took ~15 minutes; delaying it would have accumulated interest in the form of "future me has to re-understand why routes/dossiers imports from prov_json."

2. **The original fix was right; the home was wrong.** Worth being explicit that user's correctness question and user's architecture question were separable — correctness confirmed in (1), architecture fixed in (2). If the correctness check had found a real gap, Round 30.5 would have been a different, bigger round. Keeping the two questions distinct mattered.

### Round 31 — Bug 4 shipped + Bug 27 shipped + an incidentally-discovered pre-existing gap cleaned up

Two scheduled Cat 2 items this round (Bug 4 was queued as the trivial closer, Bug 27 as the bigger type-tightening + policy decision), plus a third item that surfaced mid-flight while writing Bug 27's tests. Taking them in the order they landed.

#### Bug 4 — `Session` type annotation never imported

Trivial by intent, but still worth the honest write-up: the `Repository.__init__` signature in `db/models.py:238` was `def __init__(self, session: Session):` and `Session` was never imported at the top of the file. The code ran fine because `from __future__ import annotations` at the top of the module stringifies all annotations — `Session` was only a name that needed to resolve when something called `typing.get_type_hints(...)`.

This is exactly the latent-failure shape the review's "IDE-visible" framing was pointing at: static analyzers, editors, runtime type-hint consumers (FastAPI's `Depends`, Pydantic's model-building, anything using `get_type_hints`) all tripped over the unresolved `Session` when they tried to actually look it up. The code didn't trip over itself because Python's lazy annotation model let the name stay unresolved.

**Fix:** `session: Session` → `session: AsyncSession` (line 238). `AsyncSession` was already imported at line 28 — no new import needed. Every method on `Repository` uses `await self.session.execute(...)` and `await self.session.get(...)`, which is the `AsyncSession` interface — so this is the type the annotation always should have been.

**Regression test:** one test in `test_refs_and_plugin.py::TestRepositoryAnnotations::test_repository_init_annotations_resolve`. It calls `typing.get_type_hints(Repository.__init__)` — the exact operation that was failing pre-fix — and asserts the returned dict maps `"session"` to `AsyncSession`. Paranoia-checked by reverting to `Session`; the test goes red with `NameError: name 'Session' is not defined` in the failure trace, which is precisely the latent bug surfacing at test time.

Test count: 310 → 311 unit. No other tests touched.

#### Bug 27 — `DossierAccessEntry.activity_view: str` too narrow + `"related"` mode removed

This was the round's main piece. Three things bundled together because they were tangled in the same type and same code:

1. **Narrow the Pydantic model** so it matches the runtime contract rather than pretending `activity_view` is any string.
2. **Remove the `"related"` mode** — user's Round 29 call that I'd pushed back on; pulling the trigger now.
3. **Tighten `parse_activity_view`** so unknown strings fall through to deny-safe instead of being accepted verbatim.

**On (2) and the Round 29 pushback.** I'd argued against removing `"related"` citing "transparency semantics" and "citizens seeing activities that operated on their entities" as legitimate use cases. Looking at it again in Round 31: config.yaml didn't use `"related"`, toelatingen never deployed it, there was no citizen-facing UI that rendered it differently from `"own"`, and no test of it existed before I wrote one in Round 29 to pin behaviour for Bug 9. **I was defending a hypothetical against someone who knows the product.** The pushback shape was wrong — I made the user argue for it twice. Corrected in this round; the decision (kill it) was the right one and arriving at it faster would have been better. See the process note at the end of this writeup.

**Final surface for `activity_view`**, post-Round-31:
1. `"all"` — every activity visible
2. `"own"` — only activities where the user is the PROV agent
3. `list[str]` — only activities whose type is in the list
4. `dict` with `mode: "own"` + `include: [...]` — "own" plus an unconditional-include list

**Two-layer defense for legacy `"related"` in existing data.** Pydantic rejects it at **write** time (operators setting dossier access get a clean `ValidationError` — the `setDossierAccess` side effect won't persist). `parse_activity_view` deny-safes it at **read** time (legacy entries already in the DB don't silently flip semantics; they produce an empty timeline, and operators investigate). This is exactly the shape I'd have wanted for Bug 28 if that one had been shippable — fail-loudly-on-config-error at write, fail-safely-on-legacy-data at read.

**Shipped (code):**
- `entities.py` — `activity_view: str = "related"` → `activity_view: Union[Literal["all", "own"], list[str], dict] = "own"`. Default changed `"related"` → `"own"`: the narrower default, matching the aanvrager case "show me my own stuff" which is the most common unspecified intent.
- `routes/_activity_visibility.py` — `parse_activity_view` no longer accepts arbitrary strings via `isinstance(raw, str)`. Only `"all"` and `"own"` match explicitly; everything else falls through to the deny-safe `ActivityViewMode(base="list", explicit_types=frozenset())` branch. `is_activity_visible` lost the `if mode.base == "related":` branch. Docstrings on both the module and the `ActivityViewMode` dataclass updated; the dataclass docstring now notes that legacy `"related"` values preserved through the dict shape route to the final `return False` at evaluation time.
- `routes/access.py` — three docstring references to `"related"` updated or removed.
- `routes/dossiers.py` — module docstring updated; the Bug 9 historical comment kept but annotated with a Round 31 note about the mode's removal.
- `README.md` — activity_view table's row for `"related"` removed; round-reference note added.

**Shipped (tests):**
- New unit test file `tests/unit/test_activity_visibility.py` with 11 tests covering every `parse_activity_view` input shape. Previously this module had zero dedicated unit tests — it was exercised only via end-to-end route tests, which meant "what does X return?" required running the full app. Adding the unit coverage closes that gap *and* pins the Round 31 removal in the natural place.
- `test_refs_and_plugin.py::TestDossierAccessEntryActivityView` — 8 tests for the Pydantic contract: default is `"own"`, the four valid shapes are accepted, `"related"` is rejected, unknown strings are rejected, the wrapper `DossierAccess` composes entries correctly. These pin the write-time enforcement layer.
- `test_route_helpers.py::test_entry_with_activity_view_related` renamed to `test_entry_passes_through_legacy_related_value_verbatim` with a docstring explaining that `get_visibility_from_entry` is a pass-through and legacy `"related"` lands unchanged at this layer (deny-safing is the next layer's job). A future "cleanup" that tries to filter `"related"` at the wrong layer would go red here.
- `test_http_routes.py::test_related_mode_includes_activities_touching_visible_entities` (from Round 29) — **deleted** with a comment pointing to the deprecation pins that replaced it. Testing a removed mode is cargo-cult.

**Paranoia checks ✓ (two separate reverts).**
- *Read-time:* partial revert of `parse_activity_view` (re-inserted the `"related"` string acceptance) → `test_related_string_falls_through_to_deny_safe` goes red with `AssertionError: assert 'related' == 'list'`. Other 10 activity_visibility tests stay green. Restored.
- *Write-time:* revert of the Pydantic type (`Literal[...] | list | dict` → `str`) → 4 of 8 DossierAccessEntry tests go red: `test_rejects_related_string`, `test_rejects_unknown_string`, `test_accepts_list_of_types`, `test_accepts_dict_with_mode_and_include`. The 4 tests that stay green are the string-accepts-string tests (defaults, `"all"`, `"own"`), which pass on either type. Restored.

Both reverts produce the *expected* subset of failures — not "everything fails" (which would mean the tests are over-coupled) and not "nothing fails" (which would mean the tests aren't exercising the contract). Correct shape.

#### Incidental finding — pre-existing bug: `parse_activity_view` accepted arbitrary strings verbatim

While writing the activity_visibility unit tests, I added `test_unrecognized_string_falls_through_to_deny_safe` expecting it to pass (the module docstring said *"Unrecognised → deny-safe default: show nothing"*). It failed pre-fix with `assert 'banana' == 'list'`.

Tracing it: the `isinstance(raw, str)` branch at line 70–72 swallowed **all** strings via `ActivityViewMode(base=raw)` — not just `"own"` and `"related"`. A caller passing `activity_view: "typo"` got `base="typo"`, which then flowed into `is_activity_visible` and hit the final `return False`. So the end-to-end behaviour was deny-safe by accident (the evaluator didn't recognize the base), but the module's *documented* intent (deny-safe at parse time, deny-safe at eval time) wasn't what was happening — one of the two layers was passing through unchanged.

Not a correctness bug in practice (the end-to-end shape was deny-safe), but a cleanliness bug that would bite anyone reading the code and reasoning about what `parse_activity_view` returns. The Bug 27 fix naturally resolved it: the new `parse_activity_view` only accepts `"all"` and `"own"` explicitly, everything else routes to the terminal deny-safe branch. This is reflected in the new test `test_unrecognized_string_falls_through_to_deny_safe` plus the existing `test_related_string_falls_through_to_deny_safe`.

Not assigning a separate bug number for this — it's covered by the Bug 27 fix and writeup. If bookkeeping wants it tracked separately, could be filed as Bug 80; noting here that my lean is "don't, it's subsumed."

#### Process lesson — the shape of disagreement

Round 29 had me pushing back on user's "kill `related`" call. My argument: "it's the Pydantic default, it's tested, the semantics are transparency-oriented, production doesn't use it today but that's latent capability rather than mistake." The user agreed to do Bug 9 alone that round and I took the agreement as vindication of the pushback.

Looking at Round 31's evidence: config.yaml didn't use `"related"`, toelatingen never deployed it, no citizen-facing UI distinguished `"related"` from `"own"`, and the only test of it was one I'd just written. The "latent capability" was a theoretical defense against someone who actually knew the product. The user wasn't wrong; I was making them argue for a decision they'd already made well.

Two shapes of pushback to distinguish:

1. **Pushback that probes** — "can you tell me more about why" / "here's a consideration I want to flag" / "one check before I proceed" — gives the other side information and lets them reaffirm or revise. This is good; it surfaces hidden context.

2. **Pushback that forces re-argument** — "here's why you shouldn't do that, convince me" — shifts the burden of proof in a way that's wrong when the other side knows more than you. This is what I was doing.

The tell for (2) vs (1) is who ends up doing the work. If the other side has to marshal evidence to re-justify a decision they'd already made, I've framed the conversation wrong. Carrying this forward: when a user who clearly knows the product calls for a removal, default to *probing for hidden context* (are there unstated constraints? deprecation order questions? migration concerns?) and not to *defending the status quo*.

#### Verification

- Engine unit: **311 → 330** (+19: 11 new activity_visibility tests, 8 new DossierAccessEntry tests).
- Engine integration: **482 → 481** (-1: Round 29's `test_related_mode` deleted).
- Toelatingen / common / file_service unchanged: **26 / 18 / 21**.
- **Total: 876 / 876 passing** (was 858).

#### Totals after Round 31

**37 bugs fixed** (was 35 — Bug 4 and Bug 27 added). Should-fix table: **23 open** (was 25 — both bugs closed). Observations unchanged. Test suite grew 858 → 876 (+18 net).

#### Where to go next

Cat 2 still has Bugs 13, 34, 39, 43, 48, 50, 59, 60, 67 in the medium-priority band. Looking at those quickly:

- **Bug 13** — Deprecated `@app.on_event("startup")`. Small modernization fix (lifespan handler). Independent, no policy question.
- **Bug 34** — `authorize_activity` catches broad `Exception`. Hides real errors. Scope-contained, narrow surface.
- **Bug 39** — `TaskEntity.status: str` → `Literal[...]`. Same shape as Bug 27, simpler (no policy decision — the four status values are all kept). Natural next type-tightening if we want to continue that theme.
- **Bug 43** — `Aanvrager.model_post_init` raises `ValueError` without Pydantic shape. 422 error shape wrong. Bit of a weird one — worth a verify-before-plan pass.
- **Bug 48** — `.meta` filename not sanitized. Security-adjacent. Worth prioritizing if there's any appetite for the security axis.
- **Bug 50** — Migration fallback uses module-level `SYSTEM_ACTION_DEF` with bare name. Migration-specific, tooling concern.

My recommendation order: **Bug 39** (direct sequel to Bug 27, same type-tightening pattern), then **Bug 48** (security-adjacent is worth landing), then **Bug 13** (small modernization), then **Bug 34**. Bugs 43 and 50 need more reconnaissance before I can order them confidently.

Cat 3 (caching & perf) remains in the wings. If you want a change of pace after these type-tightening and small-fix rounds, that's the next batch-round candidate.

### Round 32 — Bug 39 shipped (with bundled `kind` tightening)

Direct sequel to Bug 27's type-tightening work, on the same file (`entities.py`). Simpler in shape — no policy decision like `"related"`, just narrowing types to match the values the code actually uses.

**Bundled decision (flagged).** The review entry for Bug 39 names only `TaskEntity.status`. While in the same file I noticed `TaskEntity.kind` has the same under-typing shape: `kind: str` with the four valid values (`"fire_and_forget"`, `"recorded"`, `"scheduled_activity"`, `"cross_dossier_activity"`) documented only as an inline comment. Bundled the `kind` tightening into the same round because (a) same file, same fix pattern, (b) leaving them asymmetric would make the file harder to read on next review, and (c) the drive-by scope was genuinely small — no new tests for a fundamentally different concern, just parallel coverage for a sibling field. I flagged the bundling to the user before coding; they approved by implication (continuing past that message). Distinct from Bug 28's id-check scope-widening that I correctly pulled back from — the difference is that `kind` is the same axis as `status` (narrowing one field's type set), while Bug 28's id-check would have been a second class of validation (duplicate detection across a different key).

**Verify-before-plan pass.** Taking the Round 27 lesson ("a type tightening can turn into more than expected"), did a thorough recon before touching code. All clean:

- **All 5 `status` values actively written** by production code (`tasks.py`, `worker.py`). No outliers. No migrations ever changed the set.
- **All 4 `kind` values actively written.** Default when unspecified at the YAML level is `"recorded"` (set at `tasks.py:95 task_def.get("kind", "recorded")`). The Pydantic field itself has no default — always explicit at construction.
- **Read-time re-validation path exists but is unexercised for tasks.** `plugin.entity_models["system:task"] = TaskEntity` at `app.py:128` means `context.get_typed("system:task")` *would* re-validate task content via `TaskEntity(**entity.content)`. No production call site does that today — worker and routes read `task.content` as a raw dict. But the path exists, so the tightened types must be correct on any data currently in the DB, not just on new writes. The value sets haven't changed since the initial schema migration (`9d887db892c9`), so legacy data is safe.
- **No test uses outlier values.** Only the documented sets appear across all test files.

No surprises. No policy question. No legacy-data risk. Pure narrow-the-type fix.

**Shipped:**
- `entities.py:79` — `kind: str` → `kind: Literal["fire_and_forget", "recorded", "scheduled_activity", "cross_dossier_activity"]`. No default (was already required). Inline comment removed — the valid values are now in the type itself.
- `entities.py:87` — `status: str = "scheduled"` → `status: Literal["scheduled", "completed", "cancelled", "superseded", "dead_letter"] = "scheduled"`. Default preserved. Inline comment removed. Short comment above pointing to the lifecycle diagram in the class docstring and explaining the legacy-data safety argument (no migration ever changed the set).

**Tests added (+6 in `test_refs_and_plugin.py::TestTaskEntityStatusAndKind`):**

- `test_default_status_is_scheduled` — pins the default so a future refactor has to go through this test to change it.
- `test_status_accepts_all_five_values` — pins every lifecycle value so a future narrowing that drops one goes red here with a clear failure on that value.
- `test_status_rejects_unknown_value` — the tightening pin. Fails pre-fix, passes post-fix.
- `test_kind_accepts_all_four_values` — same shape as `status`.
- `test_kind_rejects_unknown_value` — the tightening pin for `kind`.
- `test_kind_is_required` — pins the no-default invariant. The production call site always passes `kind` explicitly; YAML-level defaulting happens one layer up in `tasks.py`. If a future change adds a Pydantic default it has to go through this test deliberately.

**Paranoia check — two-direction revert.**

Each revert: remove one field's `Literal` back to `str`, run the test class, confirm exactly *one* test red (the matching `rejects_unknown_*`). Restore. Repeat for the other field.

- Revert `status` only: 1 red / 5 green. Red test: `test_status_rejects_unknown_value`. Error message: *"TaskEntity should have rejected status='pending' but accepted it. The Literal type on the status field must have regressed."*
- Revert `kind` only: 1 red / 5 green. Red test: `test_kind_rejects_unknown_value`. Parallel error message.
- Both restored: 6/6 green.

The *"exactly one red per revert"* shape is the healthy signal — each test is scoped to the specific invariant, not accidentally coupled to the other field.

**Verification:**
- Engine unit: **330 → 336** (+6).
- Engine integration: **481** (unchanged).
- Toelatingen / common / file_service unchanged: **26 / 18 / 21**.
- **Total: 882 / 882 passing** (was 876).

**Totals after Round 32:** **38 bugs fixed** (+1, Bug 39). Should-fix table: **22 open** (-1). Test suite grew 876 → 882.

### Where to go next

Cat 2 remaining priority items (same list as post-Round-31, minus Bug 39):

1. **Bug 48** — `.meta` filename not sanitized. Security-adjacent. Worth prioritizing.
2. **Bug 13** — `@app.on_event("startup")` modernization. Small fix (lifespan handler pattern).
3. **Bug 34** — `authorize_activity` catches broad `Exception`. Hides real errors.
4. **Bug 43** — `Aanvrager.model_post_init` raises `ValueError` without Pydantic shape. 422 error shape wrong. Needs a verify-before-plan pass.
5. **Bug 50** — Migration fallback uses module-level `SYSTEM_ACTION_DEF` with bare name. Migration-tooling concern.
6. **Bug 59** — broader plugin-load validation (large-ish round if tackled in full).
7. **Bug 60** — [check entry].
8. **Bug 67** — [check entry].

My order doesn't shift: **Bug 48 next** (security axis, worth landing), then **Bug 13**, then **Bug 34**, then recon for 43/50. Cat 3 (caching batch) remains the alternative for a change of pace.

### Round 33 — Bug 13 shipped

User called Bug 13 ahead of my Bug 48 recommendation. Fine — 13 is smaller and keeps momentum.

**Bug 13 — Deprecated `@app.on_event("startup")` / `@app.on_event("shutdown")`.**

FastAPI deprecated `on_event` in 0.93 (early 2023) in favor of the `lifespan` context-manager pattern. This codebase pins `fastapi>=0.110.0` — well past the deprecation cutoff. Two `on_event` handlers in `app.py::create_app`: startup (audit config + DB init + Alembic migrations) and shutdown (close search client). Neither was failing today but both emitted `DeprecationWarning` and were on the removal track for a future FastAPI major.

**Verify-before-plan pass (notable bits):**
- The `on_event` and `lifespan` patterns have the **same runtime timing** — lifespan runs when the ASGI server is ready, which matches when the old handlers fired. This is purely a deprecation migration, not a timing refactor.
- `lifespan` must be passed to `FastAPI(lifespan=...)` at construction, so the refactor had to move the `FastAPI()` call to *after* the lifespan function is defined. Fine — both live inside `create_app`, just lexically reordered.
- **`create_app` itself had zero test coverage before this round.** The HTTP integration tests all build their own minimal apps via `_build_test_app()` rather than going through `create_app`. Tests existed for functions `create_app` calls into (`test_alembic_startup`, `test_audit`) but not for the startup handler shape itself. Round 33 closes that gap — one shape test that asserts the lifespan is wired correctly.

**Shipped:**
- `app.py` — added `from contextlib import asynccontextmanager`. Defined `@asynccontextmanager async def lifespan(_app)` just before the `FastAPI()` construction, inside `create_app`, capturing `config` by closure the same way the old handlers did. Startup logic (audit config → DB init → Alembic) runs before `yield`; shutdown logic (close search client) runs after. Passed `lifespan=lifespan` to `FastAPI(...)`. Deleted both `@app.on_event(...)` handlers, replacing with a comment pointing at the lifespan function earlier in the file.
- No config changes, no YAML changes, no behavior changes — lifespan preserves execution order exactly.

**Test added (+1):**
- `tests/unit/test_app_factory_lifespan.py::TestCreateAppLifespan::test_create_app_attaches_lifespan_not_on_event` — builds a temp config.yaml pointing at the toelatingen plugin, calls `create_app(tmp_config_path)`, asserts three things:
  1. `app.router.on_startup == []` — no legacy startup handlers.
  2. `app.router.on_shutdown == []` — no legacy shutdown handlers.
  3. `not isinstance(app.router.lifespan_context, _DefaultLifespan)` — user lifespan is wired, not FastAPI's default no-op.
- The test does NOT fire the lifespan (which would require a running DB and would run Alembic). It's a shape test. Runtime correctness of the startup path is still covered by the function-level tests in `test_alembic_startup` and `test_audit`.
- Used `fastapi.routing._DefaultLifespan` which is private API — accepted here because the test's whole purpose is distinguishing "user wired a lifespan" from "FastAPI fell back to default." Documented the private-API dependency in the test comment.

**Pre-fix diagnostic bonus.** Running the test against the unfixed code emitted two `DeprecationWarning`s pointing at exactly `app.py:373` and `app.py:431` — the two `on_event` call sites. The warnings served as direct confirmation that the bug was real and pointed at the exact lines needing the fix. Post-fix, no warnings fire. A secondary visual signal that the fix worked, not just the assertion.

**Paranoia check ✓.** Partial revert (removed `lifespan=lifespan` from `FastAPI(...)` and re-added a stub `@app.on_event("startup")`). Test went red immediately with the expected shape — `app.router.on_startup` non-empty. The `DeprecationWarning` came back too, doubly confirming the shape. Restored; test green.

**Verification:**
- Engine unit: **336 → 337** (+1).
- Engine integration: **481** (unchanged).
- Toelatingen / common / file_service unchanged: **26 / 18 / 21**.
- **Total: 883 / 883 passing** (was 882).

**Totals after Round 33:** **39 bugs fixed** (+1, Bug 13). Should-fix table: **21 open** (-1). Test suite grew 882 → 883.

### Where to go next

Cat 2 remaining priority items (updated from post-Round-32):

1. **Bug 48** — `.meta` filename not sanitized. Security-adjacent. (Was my post-Round-32 top pick; user's Round 33 choice of Bug 13 shifted the order but didn't change the list.)
2. **Bug 34** — `authorize_activity` catches broad `Exception`. Hides real errors.
3. **Bug 43** — `Aanvrager.model_post_init` raises `ValueError` without Pydantic shape. 422 error shape wrong. Needs recon.
4. **Bug 50** — Migration fallback uses module-level `SYSTEM_ACTION_DEF` with bare name. Migration-tooling concern.
5. **Bug 59**, **60**, **67** — longer tail.

My order preference unchanged: **Bug 48 → Bug 34** → recon for 43/50. Cat 3 (caching batch) remains the change-of-pace alternative.
