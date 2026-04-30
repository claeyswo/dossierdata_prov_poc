# dossier_generic_ui_repo

Generic Vue 3 frontend for the dossier platform. Reads workflow YAML
metadata via the engine's form-schema endpoint and renders activity
forms automatically — no plugin-specific code on the frontend.

## What it does

For every workflow plugin the engine loads:

- Lists the activities the user can run (via `GET /dossiers/{id}`'s
  `allowedActivities` — already filtered by role, status,
  workflow-rules, and exception-eligibility).
- For each picked activity, fetches `GET /{workflow}/activities/{name}/form-schema`
  to learn what the form should contain:
  - **Used block** — entities the activity consumes. The form-schema's
    `used[].external` flag drives the UI: external (free-text IRI) or
    internal (dropdown of existing entities of that type in the dossier).
    Auto-resolve slots are hidden and summarised in a "filled in by the
    engine" surface.
  - **Generated block** — entities the activity produces. Each
    client-supplied entry carries the Pydantic-derived JSON Schema, which
    JSONForms renders as a nested form (handling `$ref` for nested models
    and `array` items for lists naturally). Handler-supplied entries are
    listed but no form is rendered.
- On submit, mints UUIDs for the dossier (in pre-creation mode), the
  activity, and each generated entity's `entity_id`+`version_id`,
  assembles the body in the engine's expected shape
  (`used: [{entity}], generated: [{entity, content}]`), and PUTs.
- Surfaces exception bypass: `allowedActivities` entries with
  `exempted_by_exception` set get a "via uitzondering" badge in the
  picker and a confirmation banner on the form before submission.

## Stack

- Vue 3 + TypeScript (Composition API + `<script setup>`)
- Vue Router for `/`, `/new`, `/dossiers/:id`
- Pinia for the user store (X-POC-User dropdown)
- JSONForms + vanilla renderers for the Pydantic-schema-to-form rendering
- Vite for dev server + build

## Onroerend Erfgoed brand

Palette derives from the official Onroerend Erfgoed Office theme
(`#944EA1` purple anchor, `#37876D` moss, `#176D8A` teal, `#867B3D`
olive, `#4BCFA5` mint, `#C2B049` gold, `#EEECE1` cream paper). Type
pairing: Newsreader (variable serif with optical sizing) for display
and body, IBM Plex Sans for UI furniture, IBM Plex Mono for IDs.
The aesthetic is "archival editorial": square buttons, ALL-CAPS
eyebrows, paper-stripe fixed background, no rounded corners.

## Running

```bash
npm install
npm run dev    # Vite at :5173, proxies /api → engine :8000, /file-svc → :8001
```

Then visit `http://127.0.0.1:5173`.

## Phase-1 scope

This is Phase 1 of the generic-UI plan (see project notes). It
demonstrates that any workflow plugin with reasonable YAML can be
exercised end-to-end through the browser without writing UI code per
plugin. Limitations:

- Reference-list dropdowns aren't auto-wired (e.g. `handeling` renders
  as free-text, even though `handelingen` is a reference list). The
  form-schema exposes `reference_lists`; mapping them to fields needs
  a per-field convention or a `ui_schema:` annotation in the workflow
  YAML — Phase 2.
- File upload composable not wired; the `bijlagen` array form fields
  let you type metadata but don't open a file picker — Phase 2.
- No author preview mode, custom-component escape hatch, or admin
  dashboards yet — Phase 3.

## Files

```
src/
├── App.vue                       App shell, header, user picker
├── main.ts                       Pinia + router bootstrap
├── router.ts                     Three routes
├── style.css                     Design system (palette, type, JSONForms overrides)
├── env.d.ts                      Vite client types
├── views/
│   ├── HomeView.vue              Recent dossiers, lookup-by-ID
│   ├── NewDossierView.vue        Pick can_create_dossier activity
│   └── DossierView.vue           Existing-dossier OR pre-creation orchestrator
├── components/
│   ├── ActivityForm.vue          Used/generated/handler-supplied sections
│   └── UsedSlotInput.vue         External free-text vs internal dropdown
├── composables/
│   └── useApi.ts                 Typed API client (engine + file service)
└── stores/
    └── user.ts                   Pinia store for X-POC-User dropdown
```
