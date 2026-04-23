# Dossier Platform — Consolidated Code Review

*8 passes across ~30,000 lines of Python + ~3,400 lines of YAML/Markdown. Frontend excluded per instruction.*

**Legend:** ~~strikethrough~~ = fixed & tested; 🔍 = investigated, not a real bug.

---

## Engagement summary

| Status | Count | Items |
|---|---|---|
| ✅ Fixed & verified | 32 | Bugs 1, 2, 5, 6, 7, 12, 15, 16, 17, 30, 32, 44, 47, 53, 54, 55, 56, 57, 58, 62, 64, 65, 66, 68, 69, 70, 72 (coverage), 73, 74, 75, 76, 77 + Obs-2 (duplicate "external") |
| 🔍 Investigated, not a bug | 1 | Bug 14 — cross-dossier refs are `type=external` rows |
| 🛑 Deferred / accepted | 4 | Bug 31 (RRN acceptable), Bug 45 (MinIO migration), Bug 63 (403 is correct HTTP), Bug 71 (test activities, deploy-time removal) |
| 🧪 Test suite | **828/828** passing | engine 763, toelatingen 26, file_service 21, common/signing 18 |
| 🏃 `test_requests.sh` | **25/25 OK, exit 0, zero deadlocks, zero worker crashes** | D1–D9 green |
| ✂️ Duplication closed | **D1, D2, D4, D22, D25** | Graph-loader consolidation + audit-emit wrapper |
| 🧰 Harnesses installed | **3** | Guidebook YAML lint + phase-docstring lint + CI shell-spec wrapper |
| 🤖 CI wired | **GitHub Actions** | `.github/workflows/ci.yml` — 4 jobs: pytest, shell-spec, doc-harnesses, migrations-append-only |
| 🎯 Must-fix walk | **Complete** | All 17 fixable must-fix bugs closed; the 5 open rows are deferred/investigated by product decision (Bugs 14, 31, 45, 63, 71) |
| 📦 Pending | 26 should-fix + 16 lower-priority bugs + 31 observations + 21 dups + 5 meta (partial relief) | See below |

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

### Should-fix — robustness

| # | Pass | Summary | Status |
|---|------|---------|--------|
| 4 | 2 | `Session` type annotation never imported. |  |
| 9 | 2 | N+1 in dossier detail view. |  |
| ~~12~~ | 2 | ~~`_parse_scheduled_for` silently returns None on unparseable dates.~~ | ✅ **Already fixed & tested.** Discovered during M2 Stage 2 startup: `worker.py:_parse_scheduled_for` was already implementing log-and-defer via `datetime.max.replace(tzinfo=timezone.utc)` on malformed ISO, with a 12-case `TestParseScheduledFor` in `test_worker_helpers.py` including explicit regression guards. The review had been carrying a stale open-bug entry; verified end-to-end (parses valid forms, returns None for genuine-empty, returns aware `datetime.max` for malformed with logger.error). No code change this round — bookkeeping correction only. |
| 13 | 2 | Deprecated `@app.on_event("startup")`. |  |
| — | 2 | Alembic subprocess has no timeout. |  |
| — | 2 | `file_service.signing_key` default accepted at startup. |  |
| — | 2 | No plugin-load cross-check that `handler:`/`validator:` names resolve. |  |
| — | 2 | Worker's recorded tasks don't pass `anchor_entity_id`/`anchor_type`. |  |
| 20 | 3 | `_PendingEntity` missing several fields → `AttributeError`. |  |
| 25 | 3 | `common_index.reindex_all` loads all dossiers into memory. |  |
| 27 | 3 | `DossierAccessEntry.activity_view: str` too narrow. |  |
| 28 | 3 | `POCAuthMiddleware` silently overwrites on duplicate usernames. |  |
| 19 | 3 | `GET /dossiers` has no `response_model`. |  |
| — | 3 | Archive has no size cap. |  |
| — | 3 | `app.py:69` appends `SYSTEM_ACTION_DEF` by reference. |  |
| ~~32~~ | 4 | ~~`finalize_dossier`/`run_pre_commit_hooks` docstring documents reading `state.used_rows` — field doesn't exist.~~ | ✅ **Fixed** — docstring now reads `state.used_rows_by_ref` matching the code. Harness 3 prevents recurrence. |
| 34 | 4 | `authorize_activity` catches broad `Exception`. |  |
| 35 | 4 | `reindex_common_too` does 3N queries for N dossiers. |  |
| 38 | 4 | No per-user authorize cache. |  |
| 39 | 4 | `TaskEntity.status: str` should be `Literal[...]`. |  |
| 42 | 4 | Field validators take raw dict, no User context. |  |
| 43 | 4 | `Aanvrager.model_post_init` raises `ValueError` without Pydantic shape. |  |
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
- **Obs 85 — Dependency-override-friendly auth for tests.** Replace `POCAuthMiddleware` instance with FastAPI `dependency_overrides`. **Open.**
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
- **Obs 95 — Plugin surface: dotted-path resolution for all Callable registries.** Eight `dict[str, Callable]` fields on `Plugin` (`handlers`, `validators`, `task_handlers`, `status_resolvers`, `task_builders`, `side_effect_conditions`, `relation_validators`, `field_validators`) are built inside each plugin's `create_plugin()` function and keyed by short names the YAML references. Meanwhile `entity_models` and `entity_schemas` already do the cleaner thing: the YAML carries a dotted Python path (`model: dossier_toelatingen.entities.Aanvraag`) and `_import_dotted` resolves it at plugin load. The short-name-dict pattern has three footguns: (1) typos in YAML fail at runtime-of-first-lookup, not load time; (2) key-space collisions (Bug 66's "relation_validators uses both names and types as keys" issue); (3) no compile-time signal for what code runs when you read a YAML file — you have to find `create_plugin()` and trace the dict construction. Proposed migration: convert all eight registries to dotted-path resolution, mirroring the `entity_models` pattern. Blast radius is every plugin's `workflow.yaml` + every `create_plugin()`; natural fit for Cat 5 (plugin-surface tightening, already flagged as "needs design first" in Round 23 triage). Deferred from Round 26's Bug 78 work — that round will keep `relation_validators` as-is while killing the Style-3 lookup-by-type-name fallback; the broader migration awaits Cat 5's design discussion. **Open.**
- **Obs 96 — Existing load-time validators never invoked in production.** Three validators defined in `plugin.py` — `validate_workflow_version_references`, `validate_side_effect_conditions`, `validate_side_effect_condition_fn_registrations` — are unit-tested thoroughly (see `test_refs_and_plugin.py`) but never called by `app.py::load_config_and_registry` at startup. Only `_validate_plugin_prefixes` runs in production. Found while wiring up Bug 78's new `validate_relation_declarations` / `validate_relation_validator_registrations` in Round 26 — the Bug 78 validators are now called from `load_config_and_registry`, but the pre-existing three are silently unused. Fix is one-turn trivial: add three `for plugin in registry.all_plugins(): validate_*(plugin.workflow, ...)` calls next to the Bug 78 block. Unclear whether the omission was deliberate (perhaps the validators were considered too strict for early plugin development?) or accidental drift — worth a checkpoint before wiring them, because the production toelatingen YAML might have latent shape violations that only surface when the validators actually run. **Open.**

**Observation totals:** 47 catalogued, Obs 50-96. **14 closed** (Obs 61, 63, 66, 67, 68, 69, 70, 71, 72, 73, 74, 83, 90, 93), **1 partially addressed** (Obs 52), **1 deferred by product decision** (Obs 84), **31 open**. Two of the open observations are explicitly redundant with bugs (Obs 80 ↔ Bug 25; Obs 81 ↔ Bug 38) — the bug tables are authoritative for those; obs entries are cross-references. Round 24 closed six observations in the Cat 1 doc-fix batch, three of which were redundant with Bugs 56/66/69; Round 25 closed Obs 66 alongside Bugs 53/54; Round 26 opened Obs 95 (plugin-surface dotted-path migration, deferred to Cat 5) and Obs 96 (unwired load-time validators). Most of the non-redundant open ones are not acute — the pattern is "code works today but will decay without attention."

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

- **Bug 4** — `Session` type annotation never imported. *Surface: typing only, runtime-safe but IDE-visible.*
- **Bug 9** — N+1 in dossier detail view. *Sev 2 but directly user-visible as page-load latency.*
- **Bug 13** — Deprecated `@app.on_event("startup")`. *Modernization, small fix.*
- **Bug 20** — `_PendingEntity` missing several fields → `AttributeError`. *Sev 3, can crash on specific input shapes.*
- **Bug 27** — `DossierAccessEntry.activity_view: str` too narrow (should be Literal). *Type tightening.*
- **Bug 28** — `POCAuthMiddleware` silently overwrites on duplicate usernames. *Boot-time validation gap; should fail loudly.*
- **Bug 34** — `authorize_activity` catches broad `Exception`. *Hides real errors.*
- **Bug 39** — `TaskEntity.status: str` → `Literal[...]`. *Type tightening.*
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
