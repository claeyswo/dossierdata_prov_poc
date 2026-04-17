# Pipeline Architecture

*Internal developer guide. Not for plugin authors — see [Plugin Guidebook](plugin_guidebook.md) instead.*

This document explains how the activity execution pipeline works: what each phase does, why it runs in that specific order, and what breaks if you change it. Read this before modifying anything in `engine/pipeline/` or `engine/__init__.py`.

## The pipeline at a glance

Every state change in the system — whether a citizen submitting an application, a civil servant taking a decision, or a scheduled task firing — flows through the same pipeline. The entry point is `execute_activity()` in `engine/__init__.py`. It constructs an `ActivityState` object and passes it through twenty phases in sequence, all within a single database transaction.

The phases, in order:

```
 1. check_idempotency      →  replay detection
 2. ensure_dossier          →  create or fetch the dossier row
 3. authorize               →  can this user run this activity?
 4. resolve_role             →  determine the PROV agent role
 5. check_workflow_rules     →  status requirements, forbidden rules
 6. resolve_used             →  turn entity refs into DB rows
 7. enforce_disjoint         →  used ∩ generated = ∅
 8. process_generated        →  schema validation, derivation chains
 9. process_relations        →  process-control + domain relations
10. run_custom_validators    →  plugin-declared validation
11. validate_tombstone       →  tombstone shape check
12. create_activity_row      →  persist the activity + association
13. run_handler              →  plugin handler logic
14. persist_outputs          →  entities, used links, relations to DB
15. determine_status         →  compute the activity's status contribution
16. execute_side_effects     →  recursive child activities
17. process_tasks            →  schedule tasks (YAML + handler-appended)
18. cancel_matching_tasks    →  cancel tasks that this activity obsoletes
19. run_pre_commit_hooks     →  strict plugin hooks (can veto)
20. finalize_dossier         →  cache status, compute allowed activities
```

If any phase raises an `ActivityError`, the transaction rolls back. Nothing is persisted. The user sees an HTTP error with the phase's message. This is by design — the pipeline is atomic.

## Why this order matters

The ordering isn't arbitrary. Each phase depends on outputs from earlier phases and sets up inputs for later phases. Here are the critical dependencies and what breaks if you reorder.

### Phases 1–5: Security and preconditions

These run before the engine touches any entity data. The goal is to reject invalid or unauthorized requests as cheaply as possible.

**Idempotency check comes first** because if we've already executed this activity (same UUID), we can return the cached response immediately without doing any work. Moving it later would mean re-running authorization and workflow rules on every retry — wasteful and potentially inconsistent if the user's roles changed between the original and the retry.

**Authorization before workflow rules** because a 403 ("you can't do this") is a more useful response than a 422 ("the dossier isn't in the right status"). If you swap them, an unauthorized user learns which status the dossier is in, which is an information leak.

**Workflow rules after authorization** because the rules check dossier status, required prior activities, and forbidden-activity constraints. These all need the dossier row, which `ensure_dossier` loaded in phase 2. They don't need entity data yet.

### Phases 6–8: Data resolution

These phases turn the request's abstract references into concrete database rows.

**`resolve_used` before `process_generated`** because generated entities may declare `derivedFrom` pointing at a used entity. The engine needs the used entity's row (its `entity_id`, its content for schema migration) to set up the derivation chain. If you swap them, `derivedFrom` references resolve to nothing.

**`enforce_disjoint` between them** because it checks that no entity appears in both `used` and `generated`. This must happen after used-refs are resolved (so we know what was actually used) but before generated entities are processed (so we reject the conflict before doing schema validation work). The check is cheap — a set intersection — so placing it here costs nothing and catches errors early.

### Phase 9: Relations

Relations run after both used and generated are resolved. This matters because:

- **Process-control relations** (like `oe:neemtAkteVan`) reference existing entities by version ID. The entity must be resolvable, which means it must exist in the database. Used-resolution already confirmed the entities exist.
- **Domain relations** may reference entities being created in the same activity (`"from": "oe:aanvraag/e1@v1"` where `e1@v1` is in the `generated` block). The ref expansion (`expand_ref`) works on the string form, not the DB row, so it doesn't strictly need the entity to be persisted yet — but `from_types`/`to_types` validation uses `classify_ref` on the original string, which is stateless. If we ever add validation that checks the referenced entity exists in the DB, this phase would need to move after persistence.

### Phases 10–11: Validation

Custom validators and tombstone validation run after all data is resolved but before anything is persisted. This is the last chance to reject the request without side effects. The validators see the full picture: resolved used entities, pending generated entities, validated relations. They can cross-reference everything.

### Phase 12: Activity row creation

This is the point of no return for the activity's identity. The activity row and its `wasAssociatedWith` association are written to the database. From here on, the activity exists in the PROV graph even if later phases fail (though the transaction rollback would undo it).

**Why before the handler?** The handler may need the `activity_id` to set as `generated_by` on entities it creates, or to reference in tasks it schedules. The activity row must exist first so foreign key constraints are satisfied when the handler's outputs are persisted.

### Phase 13: Handler

The handler is plugin code that runs custom logic: computing a beslissing, setting access rules, generating system entities, appending tasks. It runs after the activity row exists but before persistence of the activity's outputs.

**Why after the activity row but before persistence?** The handler can modify `state.generated` (add entities), override `state.status` (change the dossier status), and append to `state.handler_result.tasks` (schedule work). All of these need to be picked up by the persistence and task-scheduling phases that follow. If the handler ran after persistence, its additional entities wouldn't be saved.

**Why not before the activity row?** The handler might query the database (e.g., to check existing entities or compute derived state). If the activity row doesn't exist yet, the handler's view of the dossier is inconsistent — it can see the dossier but not the activity that's supposedly happening.

### Phase 14: Persistence

Everything goes to the database: generated entities, used links, activity-relation rows, domain-relation rows, domain-relation removals. This is a single batch of writes, all within the same transaction.

**Why after the handler?** Because the handler may have added entities to `state.generated` or modified entity content. Persisting before the handler runs would miss those additions.

**Why before status determination?** Because `determine_status` reads the activity's `computed_status` from the YAML and stamps it on the activity row. The activity row must already be persisted (phase 12) for this UPDATE to work.

### Phases 15–18: Post-persistence effects

These phases create secondary effects based on the now-persisted activity.

**Status determination (15)** stamps the status on the activity row. Must happen after persistence so the row exists.

**Side effects (16)** are recursive child activities (e.g., `setDossierAccess` fires automatically after `dienAanvraagIn`). They run through the full pipeline themselves, so they need the parent activity's entities to be persisted and visible. A `session.flush()` runs before side effects to ensure the parent's writes are visible within the transaction.

**Task scheduling (17)** processes both YAML-declared and handler-appended tasks. It runs after the handler (which may append tasks) and after persistence (because task entities need the activity's generated entities to exist for anchor resolution). The supersession logic (`_supersede_matching`) queries existing `system:task` entities — if we scheduled tasks before persisting the current activity's entities, the anchor resolution would fail.

**Task cancellation (18)** cancels existing scheduled tasks whose `cancel_if_activities` includes the activity we just ran. This must run after the current activity's tasks are scheduled (phase 17), otherwise we might cancel a task and then immediately re-create it.

### Phase 19: Pre-commit hooks

These are the last plugin code to run before the transaction commits. They see the fully persisted state — all entities, relations, tasks, side effects. They can veto the entire activity by raising an exception, which rolls back everything.

**Why so late?** Because hooks may need to validate the final state, including side-effect outputs and scheduled tasks. A PKI signature hook, for example, needs to verify the signed entity actually exists in the database before approving the commit.

**Why before finalization?** Because finalization computes the cached status and allowed-activities list on the dossier row. If a hook vetoes the activity, we don't want stale cache values from a half-computed finalization.

### Phase 20: Finalization

Derives the current dossier status from all activities, computes which activities are now eligible, caches both on the dossier row, and runs the `post_activity_hook` (search index updates). This is advisory — the `post_activity_hook` can't veto the activity. Errors are swallowed.

## The ActivityState object

`ActivityState` is a mutable dataclass that flows through every phase. Each phase reads some fields and writes others. The fields are documented with "set by phase X" comments, but there's no compile-time enforcement. Here's the lifecycle:

| Field | Set by | Read by |
|---|---|---|
| `dossier` | ensure_dossier | authorize, workflow_rules, finalization |
| `used_refs` | resolve_used | enforce_disjoint, persistence |
| `resolved_entities` | resolve_used | handlers, task anchor resolution |
| `used_rows_by_ref` | resolve_used | relation validators |
| `generated` | process_generated, handler | persistence, validators, task scheduling |
| `validated_relations` | process_relations | persistence |
| `validated_domain_relations` | process_relations | persistence |
| `validated_remove_relations` | process_relations | persistence |
| `handler_result` | run_handler | persistence, task scheduling |
| `computed_status` | determine_status | finalization |

The risk: a phase reads a field that a later phase sets. This is always a bug, but Python won't catch it — the field exists (with its default value) and the read silently gets stale or empty data. When adding a new phase, trace every field it reads and confirm the setter phase runs earlier.

## Relations: two kinds, one pipeline

The `process_relations` phase handles both process-control relations (`oe:neemtAkteVan`) and domain relations (`oe:betreft`, `oe:gerelateerd_aan`). They share a pipeline because they both arrive in the same `relations` field on the request, but they diverge at persistence:

- **Process-control**: persisted to `activity_relations` (activity→entity edge).
- **Domain**: IRI-expanded via `expand_ref`, validated against `from_types`/`to_types`, persisted to `domain_relations` (entity→entity/URI edge with provenance).
- **Removals**: `remove_relations` entries are also IRI-expanded and validated, then applied as supersessions on `domain_relations`.

Validator dispatch is per-operation: the `add` validator fires for add-entries, the `remove` validator fires for remove-entries, and either can be omitted. For process-control relations, only the type-level validator fires (there is no remove operation).

## Transaction boundaries

The entire pipeline runs in one `async with session.begin()` block. If any phase raises, the transaction rolls back and nothing is persisted. This means:

- Side effects (phase 16) are atomic with the parent — if a side effect fails, the parent activity is also rolled back.
- Pre-commit hooks (phase 19) can veto even after side effects and tasks are scheduled — the rollback undoes everything.
- The `post_activity_hook` (phase 20) runs inside the transaction but its exceptions are swallowed — a failing search index update doesn't roll back the activity.

The worker uses a separate transaction per task execution. If a scheduled task fails, only that task's activity is rolled back — the original scheduling activity is unaffected (it committed in a previous transaction).

## Adding a new phase

If you need to add a new phase to the pipeline:

1. Decide where it goes by tracing its read/write dependencies against the table above.
2. Create a new module in `engine/pipeline/` with a single async function taking `ActivityState`.
3. Document which `state` fields it reads and writes, in the function's docstring.
4. Add the call to `execute_activity()` in `engine/__init__.py` at the correct position.
5. Write a unit test that constructs an `ActivityState` fixture and calls your phase function directly — no HTTP, no database needed for the phase's own logic.
6. Write an integration test that exercises the phase through the full HTTP path.
7. Update this document.
