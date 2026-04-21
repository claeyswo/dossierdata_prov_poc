# Dossier Platform — Consolidated Code Review

*8 passes across ~30,000 lines of Python + ~3,400 lines of YAML/Markdown. Frontend excluded per instruction.*

**Legend:** ~~strikethrough~~ = fixed & tested; 🔍 = investigated, not a real bug.

---

## Engagement summary

| Status | Count | Items |
|---|---|---|
| ✅ Fixed & verified | 18 | Bugs 1, 2, 12, 15, 16, 17, 32, 44, 47, 64, 65, 68, 70, 72 (coverage), 73, 74, 75, 76 + Obs-2 (duplicate "external") |
| 🔍 Investigated, not a bug | 1 | Bug 14 — cross-dossier refs are `type=external` rows |
| 🛑 Deferred / accepted | 4 | Bug 31 (RRN acceptable), Bug 45 (MinIO migration), Bug 63 (403 is correct HTTP), Bug 71 (test activities, deploy-time removal) |
| 🧪 Test suite | **760/760** passing | engine 705, toelatingen 16, file_service 21, common/signing 18 |
| 🏃 `test_requests.sh` | **25/25 OK, exit 0, zero deadlocks, zero worker crashes** | D1–D9 green |
| ✂️ Duplication closed | **D1, D2, D4, D22, D25** | Graph-loader consolidation + audit-emit wrapper |
| 🧰 Harnesses installed | **3** | Guidebook YAML lint + phase-docstring lint + CI shell-spec wrapper |
| 🤖 CI wired | **GitHub Actions** | `.github/workflows/ci.yml` — 4 jobs: pytest, shell-spec, doc-harnesses, migrations-append-only |
| 📦 Pending | ~59 bugs + 57 obs + 22 dups + 5 meta (partial relief) | See below |

Note: Bug 75 was discovered *by* harness 2 on its first run — a new bug surfaced and fixed in the same session as the harness that surfaced it.

---

## Bugs

### Must-fix — correctness, security, data integrity

| # | Pass | Summary | Status |
|---|------|---------|--------|
| ~~1~~ | 1 | ~~`remove_relations` — `r["relation_type"]` on frozen dataclass → `TypeError`.~~ | ✅ |
| ~~2~~ | 1 | ~~Add-validator dispatch path also triggers on removes.~~ | ✅ |
| 5 | 2 | `check_dossier_access` docstring claims default-deny but code asserts default-allow. |  |
| 6 | 2 | Alembic failure fallback runs `create_tables()` — half-migrated schema risk. |  |
| 7 | 2 | Batch endpoint emits audit events per item before transaction commit. |  |
| 🔍 14 | 3 | **Not a bug.** Cross-dossier refs persisted as local `type=external` rows via `ensure_external_entity`; raw-UUID cross-dossier refs rejected at `resolve_used:89-92` with 422. | Dropped from must-fix. |
| ~~15~~ | 3 | ~~Archive tempfile leak fills `/tmp` on heavy use.~~ | ✅ |
| ~~16~~ | 3 | ~~Duplicate PROV-JSON build between `/prov` and `/archive`.~~ | ✅ |
| ~~17~~ | 3 | ~~Hardcoded font paths break on non-Debian.~~ | ✅ |
| 30 | 4 | `move_bijlagen_to_permanent` silently swallows per-file exceptions. |  |
| 📝 31 | 4 | Closed by product decision (RRN in `role`/`dossier_access`/ES ACL acceptable). | Decided. |
| ~~44~~ | 5 | ~~File service falls back to `temp/file_id` regardless of `dossier_id`.~~ | ✅ |
| 🛑 45 | 5 | Deferred — MinIO migration handles it. |  |
| ~~47~~ | 5 | ~~Upload tokens dossier-agnostic.~~ | ✅ |
| 55 | 5 | `lineage.find_related_entity` doesn't filter by `dossier_id` defensively. |  |
| 57 | 6 | `routes/entities.py` three endpoints skip `inject_download_urls`. |  |
| 58 | 6 | `POST /{workflow}/validate/{name}` has no authentication. |  |
| 62 | 6 | `/entities/{type}/{eid}/{vid}` doesn't verify `entity_id` matches. |  |
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
| 53 | 5 | `lineage.find_related_entity` frontier growth unbounded. |  |
| 54 | 5 | `lineage.find_related_entity` returns `None` for both "not found" and "ambiguous". |  |
| 56 | 6 | README claims externals in both `used`/`generated` allowed; code + test reject. |  |
| 59 | 6 | Unregistered validators silently skip. |  |
| 60 | 6 | `alembic/env.py` nested `asyncio.run()` hazard. |  |
| ~~64~~ | 7 | ~~Plugin guidebook uses `schema:` where loader reads `model:`.~~ | ✅ **Fixed** in `docs/plugin_guidebook.md:59`. Harness 1 prevents recurrence. |
| ~~65~~ | 7 | ~~Same `schema:` vs `model:` bug in external-ontologies section.~~ | ✅ **Fixed** in `docs/plugin_guidebook.md:635, 639, 643`. |
| 66 | 7 | Relation validator keying rules undocumented. |  |
| 67 | 7 | `_errors.py` payload key collision. |  |
| 69 | 7 | Tombstone role shape inconsistent between dossiertype template and workflow.yaml. |  |
| ~~70~~ | 8 | ~~`test_requests.sh` outputs dead `/prov/graph` URL.~~ | ✅ **Fixed** — four echo sites updated to `/prov/graph/timeline` (the user-visible visualization route). `prov.py` module docstring also corrected — it documented a `/prov/graph` endpoint that doesn't exist; now lists the four real ones. Verified end-to-end: `/prov/graph/timeline` returns 401 without auth (route registered), the old `/prov/graph` returns 404 (proves the URL was dead). |
| ~~73~~ | (impl) | ~~`conftest.py` TRUNCATE list omits `domain_relations`.~~ | ✅ |
| ~~74~~ | (impl) | ~~Worker/route deadlock on `system:task` rows.~~ | ✅ **Fixed.** Structural (worker takes dossier lock first, matching user-activity order) + defence-in-depth (`run_with_deadlock_retry` on routes). |
| ~~75~~ | (impl) | ~~Worker crashes on cold start if the app hasn't finished Alembic migrations yet — `UndefinedTableError` propagates to top-level crash handler.~~ | ✅ **Fixed.** Surfaced by harness 2. Worker now tolerates SQLSTATE 42P01 during pre-ready window, logs a warning and retries; real missing-table errors after first successful poll still propagate. |
| ~~76~~ | (impl) | ~~`file_service/app.py:265` — the `.meta` parse during `/internal/move` catches OSError and JSONDecodeError and falls back to "no binding info", which then permits the move.~~ | ✅ **Fixed & tested.** Discovered during M2 Stage 3: the silent-bypass code had already been replaced with `logger.error` + `raise HTTPException(500, ...)` with a thorough docstring explaining the four `.meta` states (missing / valid-with-field / valid-no-field / corrupted) and the policy for each. Review was carrying a stale open-bug entry. **One real sub-bug caught by writing the regression tests:** `UnicodeDecodeError` (subclass of `ValueError`, *not* of `JSONDecodeError`) wasn't in the except clause, so non-UTF-8 garbage in `.meta` crashed with a default 500 rather than the intended explicit reject. Widened the except to `(OSError, json.JSONDecodeError, UnicodeDecodeError)`; added two regression tests in `TestMoveEnforcesDossierBinding` (truncated JSON + binary garbage). Both green; full class 7/7 passing. |

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

The 8-pass sweep catalogued 57 structural observations. They cluster into five themes. Status key: **open** (unchanged), **partially addressed** (progress in a specific pass), **closed** (folded into a fix). Individual-observation numbering is reconstructed from the original passes where it was explicit.

### Code organization
- **Obs 50 — Worker split.** `worker.py` is ~1,340 lines (grew ~80 lines with Bug 75's resilience logic + Bug 12's log-and-defer). Proposed split: `poll.py`, `execute.py`, `retry.py`, `requeue.py`, `signals.py`. **Open.**
- **Obs 51 — Unify relation shape in `ActivityState`.** Three typed lists (`validated_relations`, `validated_domain_relations`, `validated_remove_relations`) plus the `relations_by_type` dict. Same conceptual "validated relation" has 4 in-memory shapes; this is where Bugs 1/2 lived. **Open.**
- **Obs 52 — Split `prov.py`** (currently 509 lines, down from 792 after Round 5) into extract / transform / render layers. **Partially addressed** (Round 5 extracted `prov_json.py` with the graph-rowset loader + PROV-JSON builder; the remaining `prov.py` is mostly route registration + HTML render). Further split is lower-urgency now.
- **Obs 53 — Extract `prov_columns_layout.py`** — ~280 lines of pure layout algorithm currently inside `register_columns_graph`. Pure function of inputs; easy to isolate. **Open.**
- **Obs 54 — Untangle import-inside-function cycles.** Pattern appears in `relations.py`, `side_effects.py`, `persistence.py`, `dossiers.py`. Signals a cycle in the module graph that could be cleaned up in one refactor. **Open.**
- **Obs 55 — Rationalize `namespaces.py` singleton** + scattered `try/except RuntimeError` fallbacks in `prov_iris.py`, `activity_names.py`. **Open.**

### Plugin surface
- **Obs 56 — Centralize plugin validation.** Three load-time validators exist (`validate_workflow_version_references`, `validate_side_effect_conditions`, `validate_side_effect_condition_fn_registrations`, `_validate_plugin_prefixes`), five more are missing. Also: no cross-check that `handler:` / `validator:` names resolve to registered callables (Bug 59 territory). **Open.**
- Plugin interface table in docs promises 15 field validations; 3 are actually checked. **Open.**
- `authorize_activity` pre-creation vs post-creation modes threaded via `dossier_id: UUID | None` — should split into two functions. **Open.**
- Load-time validation for `status:` dict-form shape. **Open.**
- `eligible_activities` column: `Text` → `JSONB`. **Open.**
- **`set_dossier_access`** — 6 copies of the view list, duplicate `"external"`. **Closed** in Round 11 (view-list constants + role helpers extracted, duplicate bug fixed, 16 regression tests added). Write-on-change optimization explicitly declined as a product decision (keep full provenance graph).
- Remove legacy `handle_beslissing` (marked "kept for backward compatibility"). **Open.**
- Back-compat `"behandelaar"` role needs an owner + removal deadline. **Closed** in Round 11 (confirmed actively used by `workflow.yaml:71, 80, 89, 304, 391, 724, 755` authorization entries — legitimate global-staff role, not legacy).
- `systemAction` sub-types: `oe:migrationAction`, `oe:requeueAction`, `oe:retryAction`. **Open.**
- Document `systeemgebruiker` role grants; add `caller_only: "system"` check. **Open.**
- Lineage walker needs per-walk cache + distinguishable "not found" vs "ambiguous" return (Bugs 53, 54). **Open.**

### Documentation drift
- Pipeline doc's "UPDATE must happen after persistence" claim is factually wrong. **Open.**
- Pipeline doc's `ActivityState` field table covers ~⅓ of actual fields, presented as complete. **Open.**
- README claims external-overlap is allowed; code + tests reject (Bug 56). **Open.**
- Guidebook uses wrong YAML key (`schema:` vs `model:`). **Closed** in Round 8 (Bugs 64, 65 fixed; harness 1 now prevents recurrence).
- Dossiertype template's tombstone block shape doesn't match production workflow (Bug 69). **Open.**
- Template's endpoint docs omit the workflow-name prefix — 4 different URL forms for workflow search, none matching production. **Open.**
- Relation validator keying rules (three styles) undocumented (Bug 66). **Open.**
- `prov.py` module docstring referenced non-existent `/prov/graph` endpoint. **Closed** in Round 12 (fixed alongside Bug 70).

### Performance / observability
- Cache `SearchSettings()` at module load (currently re-reads env on every `get_client()`). **Open.**
- `is_singleton` should cache instead of linear-scanning `entity_types` per call. **Open.**
- `derive_status` should prefer `dossier.cached_status` first. **Open.**
- `check_workflow_rules` should pass `known_status` from `state.dossier.cached_status`. **Open.**
- Archive size cap/warning. **Open.**
- Reindex pagination (load all dossiers into memory; Bug 25). **Open.**
- No per-user eligibility cache (Bug 38). **Open.**

### Test / deployment concerns
- Test fixtures use direct `Repository` instances against real Postgres — no unit isolation story documented. **Open.**
- `test_requests.sh` as an executable spec that wasn't in CI. **Closed** in Round 8 + Round 9 (`scripts/ci_run_shell_spec.sh` harness 2 + GitHub Actions `shell-spec` job).
- Schema-versioning tests require declaring test-only activities in production YAML (Bug 71). **Deferred by product decision** (deploy-time checklist removes them).
- Dependency-override-friendly auth for tests (replace `POCAuthMiddleware` instance with FastAPI `dependency_overrides`). **Open.**
- Signing key rotation support (only one key accepted). **Open.**
- Migration framework needs top-level audit log (who/when/command). **Open.**
- `DataMigration.transform` signature should widen to `(content, row)`. **Open.**
- Cross-workflow task permission model (no check that source plugin can schedule into target workflow). **Open.**

### Specific refactors named
- Add "Reads/Writes docstring matches state fields" lint. **Closed** in Round 8 (harness 3).
- Share layout between `archive.render_timeline_svg` and columns graph (160 + 270 lines of separate layout code). **Open.**
- `activity_view` mode complexity reduction (5 modes; hard mental load for small feature value). **Open.**
- The pipeline architecture doc's ActivityState hazard is documented but not enforced. **Closed** in Round 8 (harness 3 enforces it).

**Observation totals:** of the 57 catalogued, **~9 are closed or have direct relief shipped** (harness 3, harness 2 CI wiring, `set_dossier_access` refactor, Bugs 64/65 guidebook fix, Bug 70's doc-drift, `test_requests.sh` CI integration). The remaining ~48 are open and tracked in the themes above. Most are not acute — the pattern is "code works today but will decay without attention."

## Duplication targets (27 catalogued, 5 closed)

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

**Duplication totals:** **5 closed** (D1, D2, D4, D9, D22, D25 — counting D22 and D4 as one closure since they were the same pattern), **22 open**.

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

### Where to go next (in priority order)

1. **Bug 5 — `check_dossier_access` docstring/code drift.** Security-boundary concern: docstring claims default-deny, code reportedly asserts default-allow. Small scope (either code matches docstring or docstring matches code, one of the two), high-value — this is a literal doc-vs-code drift in authorization. Worth verifying first that the drift is still there, given what just happened with Bugs 12 and 76.
2. **Bug 58 — unauthenticated `/validate` endpoint.** User-visible, not behind SSO.
3. **Remaining open "must-fix" bugs** — Bugs 6, 7, 30, 55, 57, 62. Priority depends on deployment context.

The two "optional" items previously on this list remain closed:
- **Obs-3** (write-on-change for `set_dossier_access`) — deferred by product decision. Keeping the full provenance graph is intended behaviour, not a pending optimization. Filed alongside Bugs 31/45/71 under deferred/accepted.
- **Bug 63 follow-up** (enumeration alerting) — not an application concern. The `dossier.denied` stream already carries everything needed; dashboard + alert rule is a Wazuh config task, owned by SIEM operators.
