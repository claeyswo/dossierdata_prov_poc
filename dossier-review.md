# Dossier Platform — Consolidated Code Review

*8 passes across ~30,000 lines of Python + ~3,400 lines of YAML/Markdown. Frontend excluded per instruction.*

**Legend:** ~~strikethrough~~ = fixed & tested; 🔍 = investigated, not a real bug.

---

## Session summary

| Status | Count | Items |
|---|---|---|
| ✅ Fixed & verified | 11 | Bugs 1, 2, 15, 16, 17, 44, 47, 68, 72 (coverage), 73, 74 |
| 🔍 Investigated, not a bug | 1 | Bug 14 — cross-dossier `used` refs are persisted as local `type=external` rows via `ensure_external_entity`; the raw-UUID cross-dossier case is rejected at `resolve_used` with 422 |
| 🛑 Consciously deferred / accepted | 3 | Bug 31 (RRN acceptable), Bug 45 (MinIO migration), Bug 71 (test activities accepted, removed at deploy time) |
| 🧪 Test suite | **705/705** passing | engine 668, file_service 19, common/signing 18 |
| 🏃 `test_requests.sh` | **25/25 OK, exit 0, zero deadlocks** | D1–D9 green |
| ✂️ Duplication | **D1, D2, D25 closed** | Graph-loader consolidation |
| 📦 Pending | 61 bugs + 57 obs + 24 dups + 6 meta | See below |

---

## Bugs

### Must-fix — correctness, security, data integrity (20)

| # | Pass | Summary | Location | Status |
|---|------|---------|----------|--------|
| ~~1~~ | 1 | ~~`remove_relations` — `r["relation_type"]` on frozen dataclass → `TypeError`.~~ | `engine/pipeline/relations.py:440-443` | ✅ |
| ~~2~~ | 1 | ~~Add-validator dispatch path also triggers on removes.~~ | `engine/pipeline/relations.py:442` | ✅ |
| 5 | 2 | `check_dossier_access` docstring claims default-deny but code asserts default-allow. | `routes/access.py:94-98` |  |
| 6 | 2 | Alembic failure fallback runs `create_tables()` — half-migrated schema risk. | `app.py:329-334` |  |
| 7 | 2 | Batch endpoint emits audit events per item before transaction commit. | `routes/activities.py` |  |
| 🔍 14 | 3 | **Investigated, not a bug.** Cross-dossier `used` refs are persisted as local `type=external` rows via `ensure_external_entity`; cross-dossier UUID refs are rejected at `resolve_used:89-92` with 422. The `if entity:` guard in `build_prov_graph` only drops rows on data-integrity violations (a UUID pointing at a row that no longer exists), not on cross-dossier cases. `_entity_key` already handles externals via the stored URI. | `routes/prov.py`→`prov_json.py` | Dropped from must-fix. |
| ~~15~~ | 3 | ~~Archive tempfile leak fills `/tmp` on heavy use.~~ | `routes/prov.py:752-755` | ✅ |
| ~~16~~ | 3 | ~~Duplicate PROV-JSON build between `/prov` and `/archive`.~~ | `routes/prov.py` | ✅ |
| ~~17~~ | 3 | ~~Hardcoded font paths break on non-Debian.~~ | `archive.py:240-243` | ✅ |
| 30 | 4 | `move_bijlagen_to_permanent` silently swallows per-file exceptions. | `dossier_toelatingen/tasks/__init__.py:139-150` |  |
| 📝 31 | 4 | Closed by product decision. | `dossier_toelatingen/handlers/__init__.py:37-42` | Decided. |
| ~~44~~ | 5 | ~~File service falls back to `temp/file_id` regardless of `dossier_id`.~~ | `file_service/app.py:156-212` | ✅ |
| 🛑 45 | 5 | Deferred — MinIO migration handles it. | `file_service/app.py` |  |
| ~~47~~ | 5 | ~~Upload tokens dossier-agnostic.~~ | `routes/files.py:62-67` | ✅ |
| 55 | 5 | `lineage.find_related_entity` doesn't filter by `dossier_id` defensively. | `lineage.py:76-77` |  |
| 57 | 6 | `routes/entities.py` three endpoints skip `inject_download_urls`. | `routes/entities.py:42-186` |  |
| 58 | 6 | `POST /{workflow}/validate/{name}` has no authentication. | `routes/reference.py:117-171` |  |
| 62 | 6 | `/entities/{type}/{eid}/{vid}` doesn't verify `entity_id` matches. | `routes/entities.py:141-146` |  |
| 63 | 7 | 404 before access check enables dossier-existence enumeration. | `routes/dossiers.py:79-81`, `routes/entities.py:203-205` |  |
| ~~68~~ | 7 | ~~Initial-schema Alembic migration mutated retroactively.~~ | `alembic/versions/` | ✅ |
| 🛑 71 | 8 | **Accepted.** Deploy-time removal of test activities from `workflow.yaml`; no framework flag needed. | `dossier_toelatingen/workflow.yaml:671-742` |  |
| ~~72~~ | 8 | ~~`bewerkRelaties` zero test coverage.~~ | `dossier_toelatingen/workflow.yaml:744+` | ✅ |

### Should-fix — robustness (37)

| # | Pass | Summary | Location | Status |
|---|------|---------|----------|--------|
| 4 | 2 | `Session` type annotation never imported. | `db/models.py:238` |  |
| 9 | 2 | N+1 in dossier detail view. | `routes/dossiers.py:176-185` |  |
| 12 | 2 | `_parse_scheduled_for` silently returns None on unparseable dates. | `worker.py:58-69` |  |
| 13 | 2 | Deprecated `@app.on_event("startup")`. | `app.py:287` |  |
| — | 2 | Alembic subprocess has no timeout. | `app.py:322-326` |  |
| — | 2 | `file_service.signing_key` default accepted at startup. | `app.py`, `file_service/app.py:69` |  |
| — | 2 | No plugin-load cross-check that `handler:`/`validator:` names resolve. | `plugin.py` |  |
| — | 2 | Worker's recorded tasks don't pass `anchor_entity_id`/`anchor_type`. | `worker.py:587-594` |  |
| 20 | 3 | `_PendingEntity` missing several fields → `AttributeError`. | `engine/context.py:33-51` |  |
| 25 | 3 | `common_index.reindex_all` loads all dossiers into memory. | `search/common_index.py:135-136` |  |
| 27 | 3 | `DossierAccessEntry.activity_view: str` too narrow. | `entities.py:16` |  |
| 28 | 3 | `POCAuthMiddleware` silently overwrites on duplicate usernames. | `app.py:229-244` |  |
| 19 | 3 | `GET /dossiers` has no `response_model`. | `routes/dossiers.py:239-253` |  |
| — | 3 | Archive has no size cap. | `archive.py` |  |
| — | 3 | `app.py:69` appends `SYSTEM_ACTION_DEF` by reference. | `app.py:69` |  |
| 34 | 4 | `authorize_activity` catches broad `Exception`. | `authorization.py:102-103, 130-131` |  |
| 35 | 4 | `reindex_common_too` does 3N queries for N dossiers. | `dossier_toelatingen/search/__init__.py:165-194` |  |
| 38 | 4 | No per-user authorize cache. | `engine/pipeline/eligibility.py:64-89` |  |
| 39 | 4 | `TaskEntity.status: str` should be `Literal[...]`. | `entities.py:73` |  |
| 42 | 4 | Field validators take raw dict, no User context. | `field_validators.py` |  |
| 43 | 4 | `Aanvrager.model_post_init` raises `ValueError` without Pydantic shape. | `dossier_toelatingen/entities.py:44-48` |  |
| 46 | 5 | `POST /files/upload/request` accepts unbounded `request_body: dict`. | `routes/files.py:49` |  |
| 48 | 5 | `.meta` filename not sanitized. | `file_service/app.py:134-144` |  |
| 50 | 5 | Migration fallback uses module-level `SYSTEM_ACTION_DEF` with bare name. | `migrations.py:279-285` |  |
| 53 | 5 | `lineage.find_related_entity` frontier growth unbounded. | `lineage.py:65-103` |  |
| 54 | 5 | `lineage.find_related_entity` returns `None` for both "not found" and "ambiguous". | `lineage.py:89` |  |
| 56 | 6 | README claims externals in both `used`/`generated` allowed; code + test reject. | `README.md:1140` |  |
| 59 | 6 | Unregistered validators silently skip. | `engine/pipeline/validators.py:48-49` |  |
| 60 | 6 | `alembic/env.py` nested `asyncio.run()` hazard. | `alembic/env.py:99` |  |
| 64 | 7 | Plugin guidebook uses `schema:` where loader reads `model:`. | `docs/plugin_guidebook.md:59` |  |
| 65 | 7 | Same `schema:` vs `model:` bug repeated in external-ontologies section. | `docs/plugin_guidebook.md:635, 639, 643` |  |
| 66 | 7 | Relation validator keying rules undocumented. | `engine/pipeline/relations.py:355-412` |  |
| 67 | 7 | `_errors.py` payload key collision. | `routes/_errors.py:28` |  |
| 69 | 7 | Tombstone role shape inconsistent between dossiertype template and workflow.yaml. | `dossiertype_template.md:44-49` vs `workflow.yaml:148` |  |
| 70 | 8 | `test_requests.sh` outputs dead `/prov/graph` URL. | `test_requests.sh:216`, `routes/prov.py:5` |  |
| ~~73~~ | (impl) | ~~`conftest.py` TRUNCATE list omits `domain_relations`.~~ | `tests/conftest.py:181-189` | ✅ |
| ~~74~~ | (impl) | ~~Worker/route deadlock on `system:task` rows.~~ | `worker.py::_execute_claimed_task` + `routes/activities.py` | ✅ **Fixed.** Structural fix in the worker (acquire dossier lock before entity INSERTs — same order as user activities). Defence-in-depth: `run_with_deadlock_retry` helper in `db/session.py` detects SQLSTATE 40P01 and retries with a fresh transaction + exponential backoff + jitter. Both layers tested; shell spec now runs deadlock-free. |

### Lower-priority (16)

| # | Pass | Summary |
|---|------|---------|
| 18 | 3 | `/prov/graph/timeline` uses local dict lookups; shares logic with `dossiers.py:176-185` which hits the DB. |
| 21 | 3 | `inject_download_urls` skips `list[FileId]`. |
| 22 | 3 | `classify_ref` misclassifies bare URLs without scheme. |
| 23 | 3 | `path` vs `DOSSIER_AUDIT_LOG_PATH` env precedence undocumented. |
| 24 | 3 | `emit_audit` swallows all exceptions. |
| 26 | 3 | `recreate_index` doesn't refresh between delete/create. |
| 29 | 3 | `configure_iri_base` mutates module globals; test-order landmine. |
| 32 | 4 | `finalize_dossier` docstring documents reading `state.used_rows` — field doesn't exist. |
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

## Structural observations (57)

### Code organization
- Worker split into `poll.py`, `execute.py`, `retry.py`, `requeue.py`, `signals.py`
- Unify relation shape in `ActivityState` (3 typed lists + 1 dict)
- Split `prov.py` (792 → ~490 after this round's refactor)
- Extract `prov_columns_layout.py` — 270 lines of pure algorithm inside a route registration
- Untangle import-inside-function cycles
- Rationalize `namespaces.py` singleton + scattered fallbacks

### Plugin surface
- Centralize plugin validation — 3 load-time validators exist, 5 more missing
- Plugin interface table promises 15 field validations; only 3 are actually checked
- `authorize_activity` pre-creation vs post-creation modes — split into two functions
- Load-time validation for `status:` dict-form shape
- `eligible_activities` column: `Text` → `JSONB`
- `set_dossier_access` — write-on-change only; dedup 6 copies of the view list
- Remove legacy `handle_beslissing` (marked "kept for backward compatibility")
- Back-compat "behandelaar" role needs an owner + removal deadline
- `systemAction` sub-types
- Document `systeemgebruiker` role grants
- Lineage walker needs per-walk cache + distinguishable return values

### Documentation drift
- Pipeline doc's "UPDATE must happen after persistence" is factually wrong
- Pipeline doc's `ActivityState` field table is ~⅓ of actual fields, presented as complete
- README claims external-overlap is allowed; code + tests reject
- Guidebook uses wrong YAML key (`schema:` vs `model:`)
- Dossiertype template's tombstone block shape doesn't match production workflow
- Template's endpoint docs omit the workflow-name prefix
- Relation validator keying rules (three styles) undocumented

### Performance / observability
- Cache `SearchSettings()` at module load
- `is_singleton` should cache
- `derive_status` should prefer `dossier.cached_status` first
- `check_workflow_rules` should pass `known_status`
- Archive size cap/warning
- Reindex pagination
- No per-user eligibility cache

### Test / deployment concerns
- Test fixtures use direct `Repository` instances against real Postgres — no unit isolation story documented
- `test_requests.sh` is an executable spec that isn't in CI *(passes clean after Bug 74 fix)*
- Schema-versioning tests require declaring test-only activities in production YAML
- Dependency-override-friendly auth for tests
- Signing key rotation support
- Migration framework needs top-level audit log
- `DataMigration.transform` signature should widen
- Cross-workflow task permission model

---

## Duplication (24 remaining; 3 closed this engagement)

| # | What | Status |
|---|------|--------|
| ~~D1~~ | Four copies of "load dossier graph data" | ✅ Closed — `load_dossier_graph_rows` in `prov_json.py`. |
| ~~D2~~ | Two copies of PROV-JSON build | ✅ Closed — `build_prov_graph` in `prov_json.py`. |
| D3 | `prov_type_value`/`agent_type_value` helpers under-used |  |
| D4 | Audit emission boilerplate (~15 sites) |  |
| D5 | 4 copies of latest-version-per-entity_id subquery |  |
| D6 | Repository cache returns list directly — caller mutation corrupts cache |  |
| D7 | `reindex_all` vs `reindex_common_too` 90% identical loops |  |
| D8 | `get_typed` vs `get_singleton_typed` share 80% body |  |
| D9 | `set_dossier_access` 6 copies of behandelaar/beheerder view list |  |
| D10 | 3 `reindex_*` loops share structure |  |
| D11 | `upload_file`/`download_file` repeat 7-param token extraction |  |
| D12 | `informed_by` normalization in 4 places |  |
| D13 | `_supersede_matching` + `cancel_matching_tasks` share latest-by-type pattern |  |
| D14 | Tombstone tests share structure differing from regular version tests |  |
| D15 | `DossierAccessEntry` fields duplicate what `access.py` narrates |  |
| D16 | Validator-fn registration pattern repeated without shared helper |  |
| D17 | Three endpoints in `routes/entities.py` repeat access-check preamble |  |
| D18 | Plugin load sequence repeated per plugin |  |
| D19 | `scheduled_for` parsing could be in one helper |  |
| D20 | `_activity_visibility.parse_activity_view` usage split across 3 route files |  |
| D21 | Four routes hand-roll a "filter activities by user access" loop |  |
| D22 | `emit_audit` boilerplate with the same 7 fields per call site |  |
| D23 | "Find systemAction activity def" pattern in 2 places |  |
| D24 | Alembic initial schema indices duplicated by Python model `__table_args__` — drift risk |  |
| ~~D25~~ | Both archive.py and prov.py do their own PROV-JSON prefix building | ✅ Closed — both use `build_prov_graph` which handles prefixes once. |
| D26 | `sign_token` + `verify_token` share payload-string building logic |  |
| D27 | Test setup helpers exist in 4+ test files with slight variations |  |

---

## Meta-patterns (6)

**M1. Docstring "Reads/Writes" drift has no enforcement.** Lint would catch `finalize_dossier:52`'s claim of `state.used_rows` (not a real field).

**M2. "Silent skip" as a default policy.** Unregistered validators skip, unrecognized activity_view modes skip, missing systemAction falls back to bare-name stale copy, failures swallowed across many places.

**M3. Hardcoded paved-path values.** One instance closed this engagement (Bug 17, fonts); others remain — `systeemgebruiker` in `entities.py:105`, signing-key default in `app.py:41`, `id.erfgoed.net` in `prov_iris.py:53-54`.

**M4. Documentation drift across README, plugin guidebook, dossiertype template, pipeline architecture doc.** None subject to a test.

**M5. Executable specs that don't execute.** `test_requests.sh` and guidebook YAML examples drift because they're outside the automated test suite. Partial relief: `TestProvJsonSharedBuilder` and `TestSharedGraphLoader` guard against graph-loader / PROV-JSON re-divergence.

**M6. "Test" is a namespace, not a load-time gate.** Bug 71 accepted — the deploy checklist, rather than a framework flag, keeps test activities out of production.

---

## What was shipped in this engagement

### Round 1 — Bug 1/2 (remove_relations TypeError)
Field access, 7 new tests in `TestProcessRemoveRelations`. `tests/conftest.py` TRUNCATE list extended (Bug 73).

### Round 2 — Bug 44/47 (file service security)
Dossier-binding minted into upload tokens + stamped into `.meta`; file_service rejects moves whose target doesn't match the stamped binding. 5 `TestMoveEnforcesDossierBinding` + 2 `TestDownloadNoLongerFallsBackToTemp` tests. `test_requests.sh` upload helper + 13 call sites updated.

### Round 3 — Bug 68 (Alembic consolidation)
Pre-deploy: three migrations folded into one initial. `scripts/check_migrations_append_only.py` guard + README section explaining the rule.

### Round 4 — Bug 31 (product decision)
No code change. RRN in `role`, `oe:dossier_access`, and the ES ACL is acceptable for this deployment (none are externally queryable). `agent_id`/`agent.uri` already use `user.id`/`user.uri` correctly at `persistence.py:63-84`.

### Round 5 — Archive cluster (Bugs 15, 16, 17) + Duplication D1/D2/D25
- **`dossier_engine/fonts.py`** — new. Candidate paths for five platforms + `DOSSIER_FONT_DIR` env override + actionable error message. `check_fonts_available()` for optional startup fail-fast.
- **`dossier_engine/prov_json.py`** — new. `load_dossier_graph_rows` returns a dataclass with four rowsets + pre-built indexes + agent lookup. `build_prov_graph` assembles PROV-JSON from it.
- **`routes/prov.py`** — 792 → 506 lines. /prov and /archive both 1-line calls to the builder; archive uses in-memory `Response` (no tempfile, no `FileResponse`). Timeline uses the loader + applies `visible_types` filter post-load.
- **`routes/prov_columns.py`** — columns endpoint uses the shared loader; layout algorithm unchanged.
- 16 new tests (8 font, 3 shared-builder, 3 archive-endpoint, 2 shared-loader).

### Round 6 — Bug 74 (worker/route deadlock)
Root cause: lock-order inversion. User activities take `dossiers FOR UPDATE` then INSERT entities (`FOR KEY SHARE` via FK). Workers took `entities FOR UPDATE SKIP LOCKED` via `_claim_one_due_task`, then read the dossier non-locking and later called `get_dossier_for_update` inside the pipeline — reverse order, deadlock under concurrency.

**Structural fix (primary).** In `worker.py::_execute_claimed_task`, replaced the non-locking `repo.get_dossier(dossier_id)` with `repo.get_dossier_for_update(dossier_id)` at the top of the function. Workers and user activities now acquire locks in the same order: **dossier → entities**. The pipeline's subsequent `get_dossier_for_update` is a no-op (Postgres is idempotent about re-locking within a transaction). Docstring at the lock acquisition explains the bug so the fix isn't accidentally reverted.

**Defence-in-depth (secondary).** New `run_with_deadlock_retry(work, max_attempts=3, base_backoff_seconds=0.05)` in `db/session.py`. Detects SQLSTATE 40P01 via `.orig.sqlstate` or `__cause__.sqlstate` (robust across driver versions). Retries with a fresh transaction + exponential backoff + ±25% jitter. Non-deadlock exceptions bubble out unchanged — the wrapper is strictly a deadlock safety net, not a generic retry mechanism. All three `_handle_*` methods in `routes/activities.py` converted to use it; batch handler retries the whole batch on deadlock, preserving atomicity.

11 unit tests (`tests/unit/test_deadlock_retry.py`) cover both the detection helper and the retry wrapper's contract (first-try success, retry-then-succeed, give-up-after-max, non-deadlock not retried, application errors not retried, single-attempt still raises, backoff grows exponentially, cause-chain detection).

### Round 7 — Bug 14 investigated, dropped
Traced `resolve_used` at `engine/pipeline/used.py:72-92`: external URIs are persisted via `ensure_external_entity` as local `type=external` rows; cross-dossier UUID refs are rejected at line 89-92 with 422. The `if entity:` guard in `build_prov_graph` only drops rows if the `UsedRow.entity_id` points at a non-existent version — a data-integrity violation, not a cross-dossier case. `_entity_key` already handles `type=external` rows by returning the stored URI. Bug reclassified as 🔍 investigated, not a real bug.

### Verification performed
- **Test suite:** **705/705** (engine 668, signing 18, file_service 19). Engine grew by 27 tests across the engagement.
- **Shell spec:** `bash test_requests.sh` → 25 `OK:` assertions, 5 summary-pass lines, exit 0, **zero deadlocks** in the PG log for the current run (historical deadlocks are from runs before Bug 74 was fixed).
- **Bug 74 regression guard:** 11 unit tests covering detection + retry policy; structural fix prevents the known inversion from occurring at all.

### Where to go next (in priority order)
1. **Meta M4 + M5 — make docs executable.** Three small harnesses (guidebook YAML through plugin loader, `test_requests.sh` in CI, docstring field-name lint) catch the next round of drift automatically.
2. **Bug 70** — dead `/prov/graph` URL in `test_requests.sh` output. One-line fix after Harness 2 is in place (CI run would flag it).
3. **Duplication D4 + D22** — `emit_audit` boilerplate. High frequency (~15 sites), mechanical refactor.
4. **Bug 63** — 404-before-access-check enumeration. Security but not urgent.
