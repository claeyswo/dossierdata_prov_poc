<script setup lang="ts">
// Generic activity form.
//
// Loads form-schema for the activity, renders three sections:
//
//   1. Description / metadata block (label, description, deadlines,
//      via-exception confirmation if applicable).
//   2. Used block — one <UsedSlotInput> per declared slot. Hidden if
//      the slot has auto_resolve set (engine fills these in).
//   3. Generated block — for each client-supplied generated entity
//      type, a <JsonForms> rendering the Pydantic-derived schema.
//      Handler-supplied types are listed as "engine-generated" with
//      no input.
//
// On submit: assembles { workflow, used, generated } and PUTs to
// the engine. 422 errors are surfaced inline with the raw detail
// because mapping engine-validator errors to JSONForms field paths
// would require parsing the detail's `loc` field — left for phase 2.
//
// "via exception" badging: if the parent passed an allowedActivity
// with `exempted_by_exception` set, we render a confirmation banner
// before the form so the user knows submitting will burn a single-
// use bypass. Phase 2 could add a typed-confirmation gate ("type
// CONSUME to proceed") for high-friction confirmation; for now a
// banner is enough.

import { ref, computed, onMounted, watch } from 'vue'
import {
  engineApi,
  type FormSchema,
  type AllowedActivity,
} from '../composables/useApi'
import UsedSlotInput from './UsedSlotInput.vue'

// JSONForms vue exports
import { JsonForms } from '@jsonforms/vue'
import { vanillaRenderers } from '@jsonforms/vue-vanilla'
import { mapContourRendererEntry } from './mapContourRenderer'
import { erfgoedobjectSearchEntry } from './erfgoedobjectSearchRenderer'

const props = defineProps<{
  workflow: string
  dossierId: string
  activityId: string
  activityType: string
  isCreation: boolean
  allowedActivity?: AllowedActivity
}>()

const emit = defineEmits<{
  success: []
  cancel: []
}>()

const formSchema = ref<FormSchema | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)
const submitting = ref(false)
const submitError = ref<any | null>(null)
const successResp = ref<any | null>(null)

// Existing entities of each client-supplied type already in the
// dossier. Populated alongside the form-schema. Used by the submit
// path to decide whether a generated entry is a creation or a
// revision, and by the template to render the multi-cardinality
// "revise existing or create new" picker.
//
// Keyed by entity type. Each value is an array of { entityId,
// versionId, content } — the latest version per logical entity_id,
// since that's what we'd derive from. We dedup here (unlike the
// used-block dropdown, which intentionally shows every version)
// because for the generated-block we ALWAYS derive from the latest
// version; older versions aren't valid revision parents.
const existingByType = ref<Record<string, Array<{
  entityId: string
  versionId: string
  content: any
}>>>({})

// User's choice for multi-cardinality types: which existing entity
// to revise, or null/empty = create new. Singletons don't appear
// in this map; their identity is determined automatically.
const chosenExistingByType = ref<Record<string, string>>({})  // type → entityId, '' = create new

// === Form state ===
// usedValues: keyed by slot index in formSchema.used (because a slot's
// type isn't a unique key — an activity could declare two slots of
// the same type, though it's unusual).
const usedValues = ref<Record<number, any>>({})
// generatedValues: keyed by entity type. Each value is the Pydantic
// content the user is filling in (only populated for client_supplied
// types).
const generatedValues = ref<Record<string, any>>({})

// === Derived ===
const visibleUsedSlots = computed(() =>
  formSchema.value?.used.filter(s => s.auto_resolve == null) ?? []
)

const hiddenUsedSlots = computed(() =>
  formSchema.value?.used.filter(s => s.auto_resolve != null) ?? []
)

const clientSuppliedGenerates = computed(() =>
  formSchema.value?.generates.filter(g => g.client_supplied) ?? []
)

const handlerSuppliedGenerates = computed(() =>
  formSchema.value?.generates.filter(g => !g.client_supplied) ?? []
)

// === Generated-block UI helpers ===

/** Tells the template what mode each generated entry is in:
 *  'create' — fresh entity_id will be minted at submit time.
 *  'revise' — an existing entity_id will be reused; derivedFrom set.
 *  Mode flips when the user picks/un-picks an existing instance from
 *  the multi-cardinality picker, or is set automatically for
 *  singletons that have an existing instance in the dossier. */
function generatedMode(g: { type: string; cardinality: string }): 'create' | 'revise' {
  return chosenExistingByType.value[g.type] ? 'revise' : 'create'
}

/** One-line summary of an entity's content, for picker rows. Best-
 *  effort across types — picks the first content field that looks
 *  human-readable. Falls through to "" so the caller can use the
 *  entity_id prefix as a fallback. */
function summariseExisting(content: any): string {
  if (!content || typeof content !== 'object') return ''
  const candidateFields = ['onderwerp', 'naam', 'titel', 'label', 'beslissing', 'reason']
  for (const k of candidateFields) {
    if (typeof content[k] === 'string' && content[k]) return content[k]
  }
  return ''
}

/** Multi-cardinality picker change handler. Selecting an existing
 *  entity revises it; selecting "" (the "create new" option) goes
 *  back to creation mode. We also pre-fill / clear the form fields
 *  so the user sees the right starting state. */
function onExistingPicked(type: string, entityId: string) {
  if (entityId) {
    chosenExistingByType.value[type] = entityId
    const ex = (existingByType.value[type] ?? []).find(e => e.entityId === entityId)
    if (ex) {
      generatedValues.value[type] = JSON.parse(JSON.stringify(ex.content ?? {}))
    }
  } else {
    delete chosenExistingByType.value[type]
    generatedValues.value[type] = {}
  }
}

// Custom renderers prepend so they can outrank vanilla. Each one
// matches a specific ``format`` string set in the Pydantic schema:
//   - ``geojson-multipolygon`` → OpenLayers map for drawing contours
//   - ``uri-erfgoedobject`` → search widget against the inventaris API
// Vanilla still handles every other schema shape.
const renderers = [
  mapContourRendererEntry,
  erfgoedobjectSearchEntry,
  ...vanillaRenderers,
]

// Pydantic schemas use $ref + $defs for nested models — e.g.
// ``Aanvraag.aanvrager`` is ``{$ref: "#/$defs/Aanvrager"}`` and
// ``Aanvrager`` lives in ``$defs.Aanvrager``. JSONForms' default UI
// schema generator emits a ``Control`` for any property regardless
// of whether it's a leaf or an object reference; the vanilla
// renderers then have nothing that knows how to render an object
// $ref as a nested form, so the user sees "No applicable renderer
// found." Inlining the $ref turns the nested model into a plain
// object schema, which JSONForms recognises and renders as a
// nested fieldset (group-layout) with controls for each sub-field.
//
// Implementation notes:
// - We deep-clone before mutating so the cached form-schema response
//   isn't altered (a re-open of the form would otherwise see an
//   already-resolved schema with ``$defs`` removed and any
//   self-referential $defs broken).
// - Cycle detection via a Set of in-flight ref pointers; a Pydantic
//   model that recursively references itself (e.g. tree structures)
//   would otherwise infinite-loop. On cycle we leave the $ref in
//   place — JSONForms will still fail to render that subtree, but
//   the rest of the form works.
// - $defs is dropped from the inlined output since it's no longer
//   needed and JSONForms doesn't expect it on the root.
function resolveRefs(schema: any): any {
  if (!schema || typeof schema !== 'object') return schema

  // JSON-roundtrip clone, not ``structuredClone``, because the
  // schema arrives wrapped in Vue's reactive Proxy (it lives on a
  // ``ref<FormSchema>``). ``structuredClone`` rejects Proxies with
  // a ``DataCloneError``. JSON Schema is plain-JSON-shaped (no
  // Date, RegExp, Map, etc.) so the roundtrip is lossless and also
  // strips the Proxy wrapper as a useful side effect.
  const clone = (v: any): any => JSON.parse(JSON.stringify(v))

  const defs = (schema.$defs ?? schema.definitions ?? {}) as Record<string, any>
  const inFlight = new Set<string>()

  // Pydantic emits ``Optional[X]`` as
  //   ``anyOf: [{type: "X"}, {type: "null"}], default: null``
  // The vanilla renderers' control dispatcher matches on a single
  // ``type`` field, so the anyOf shape falls through to "no applicable
  // renderer." Flattening the binary-with-null case to a single
  // non-null type makes the field render as a plain optional input.
  // We don't lose anything semantically meaningful here for form
  // input — the user typing nothing into an optional field already
  // submits null/empty, regardless of what the JSON Schema says.
  function flattenAnyOfNull(node: any): any {
    if (!node || typeof node !== 'object') return node
    if (Array.isArray(node.anyOf) && node.anyOf.length === 2) {
      const [a, b] = node.anyOf
      const isNullA = a && a.type === 'null'
      const isNullB = b && b.type === 'null'
      if (isNullA !== isNullB) {
        const nonNull = isNullA ? b : a
        // Merge the non-null branch's keys into the parent, dropping
        // anyOf. Sibling annotations on the parent (title, default,
        // description) take precedence — they were authored at the
        // outer level and the user expects them.
        const { anyOf: _omit, ...siblings } = node
        return { ...nonNull, ...siblings }
      }
    }
    return node
  }

  function walk(node: any): any {
    if (Array.isArray(node)) return node.map(walk)
    if (!node || typeof node !== 'object') return node

    // First flatten anyOf-with-null so a $ref inside it gets seen.
    node = flattenAnyOfNull(node)

    // Resolve a $ref pointing at our own $defs. Other $ref shapes
    // (external URIs, nested pointers) are left alone — out of scope
    // for the generator but rare in Pydantic output.
    if (typeof node.$ref === 'string') {
      const ref = node.$ref
      const m = ref.match(/^#\/(?:\$defs|definitions)\/(.+)$/)
      if (m) {
        const name = m[1]
        if (inFlight.has(ref)) return node  // cycle: leave $ref intact
        const target = defs[name]
        if (target) {
          inFlight.add(ref)
          const resolved = walk(clone(target))
          inFlight.delete(ref)
          // Merge sibling keys onto the resolved object (e.g. a
          // ``description`` next to the $ref is preserved). Pydantic
          // doesn't usually emit siblings with $ref, but the spec
          // allows it and we honor it.
          const { $ref: _omit, ...siblings } = node
          return { ...resolved, ...siblings }
        }
      }
    }

    // Recurse into properties / items / etc.
    const out: Record<string, any> = {}
    for (const [k, v] of Object.entries(node)) {
      if (k === '$defs' || k === 'definitions') continue  // dropped from output
      out[k] = walk(v)
    }
    return out
  }

  return walk(clone(schema))
}

// Memoize per entity type so we don't re-resolve on every render.
// Keyed by type rather than schema object identity since the schema
// is the same instance across the form's lifetime — but a fresh
// loadFormSchema() replaces it, so we clear the cache there.
const resolvedSchemaCache = ref<Record<string, any>>({})

function schemaFor(type: string, raw: any): any {
  if (!raw) return null
  if (resolvedSchemaCache.value[type]) return resolvedSchemaCache.value[type]
  const resolved = resolveRefs(raw)
  resolvedSchemaCache.value[type] = resolved
  return resolved
}

// === Loading ===
async function loadFormSchema() {
  loading.value = true
  loadError.value = null
  try {
    formSchema.value = await engineApi.formSchema(props.workflow, props.activityType)
    // Reset the resolved-schema cache so a navigate-and-return to a
    // different activity (or a hot-reload during dev) sees fresh
    // resolution. Cheap to recompute; cheaper than reasoning about
    // cache invalidation when activityType changes.
    resolvedSchemaCache.value = {}

    // Initialize blank entity content per client-supplied type so
    // JSONForms doesn't crash on undefined input.
    for (const g of formSchema.value.generates) {
      if (g.client_supplied) {
        generatedValues.value[g.type] = {}
      }
    }
    // Initialize used slot values.
    formSchema.value.used.forEach((s, idx) => {
      if (s.auto_resolve == null) usedValues.value[idx] = s.external ? '' : null
    })

    // Fetch existing entities of each client-supplied generated type
    // in parallel. Used for the singleton-revise-latest decision and
    // for the multi-cardinality picker. We skip pre-creation (no
    // dossier exists yet, so nothing to revise) and skip handler-
    // supplied types (the engine resolves identity for those).
    existingByType.value = {}
    chosenExistingByType.value = {}
    if (!props.isCreation) {
      const types = Array.from(new Set(
        formSchema.value.generates
          .filter(g => g.client_supplied)
          .map(g => g.type)
      ))
      await Promise.all(types.map(async (t) => {
        try {
          const resp = await engineApi.entitiesByType(props.dossierId, t)
          const versions = (resp as any)?.versions ?? []
          // Dedup to latest-per-entity_id. For the generated-block
          // we only ever derive from the latest version of a logical
          // entity — engine-side ``derivedFrom`` validation enforces
          // this anyway. Older versions in the picker would only
          // produce a 422 at submit time.
          const latestByEid = new Map<string, any>()
          for (const v of versions) {
            const eid = v.entityId || v.entity_id
            if (!eid) continue
            const cur = latestByEid.get(eid)
            const a = v.createdAt || v.created_at || ''
            const b = cur ? (cur.createdAt || cur.created_at || '') : ''
            if (!cur || a >= b) {
              latestByEid.set(eid, {
                entityId: eid,
                versionId: v.versionId || v.version_id,
                content: v.content,
              })
            }
          }
          existingByType.value[t] = Array.from(latestByEid.values())
        } catch (e) {
          // 404 = no entities of this type yet. Treat as empty list,
          // which is the correct shape for "no revision target."
          existingByType.value[t] = []
        }
      }))

      // Auto-select for singletons: if a singleton type has exactly
      // one existing instance, the user has no choice — they're
      // revising it. We pre-fill chosenExistingByType so the submit
      // path doesn't need a separate singleton branch and the UI
      // can show "Herziening van" consistently.
      for (const g of formSchema.value.generates) {
        if (!g.client_supplied) continue
        if (g.cardinality !== 'single') continue
        const existing = existingByType.value[g.type] ?? []
        if (existing.length >= 1) {
          // Pick the latest (the list is sorted latest-first by virtue
          // of the >= comparison above; first entry is most recent).
          // For singletons it should be exactly one, but if a workflow
          // somehow ended up with more we still pick the most recent.
          chosenExistingByType.value[g.type] = existing[0].entityId
          // Pre-fill the form with the existing content so the user
          // edits a draft of "what's there" rather than starting from
          // empty fields. This matches user mental model of "revise"
          // and avoids accidentally blanking required fields.
          generatedValues.value[g.type] = JSON.parse(
            JSON.stringify(existing[0].content ?? {})
          )
        }
      }
    }
  } catch (e: any) {
    loadError.value = e?.detail
      ? (typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail))
      : 'Kon formuliergegevens niet laden.'
  } finally {
    loading.value = false
  }
}

onMounted(loadFormSchema)
watch(() => props.activityType, loadFormSchema)

// === Submission ===
async function submit() {
  if (!formSchema.value) return
  submitError.value = null
  successResp.value = null
  submitting.value = true

  // Assemble used block. Each slot's value depends on its kind:
  //   - external: bare IRI string → wrap as { entity: <iri> }. The
  //     engine accepts the raw IRI as the entity ref; no type
  //     wrapper needed (the YAML's used-slot type declaration is
  //     informational only on this side).
  //   - internal: already an object { entity, type } from UsedSlotInput
  //   - auto_resolve: skip (engine fills in)
  // Empty values for required slots → let the engine raise; we pass
  // through whatever the user provided, so the validation error path
  // is the same one the engine returns programmatically too.
  const used: any[] = []
  formSchema.value.used.forEach((slot, idx) => {
    if (slot.auto_resolve != null) return
    const v = usedValues.value[idx]
    if (v === null || v === undefined || v === '') return
    if (slot.external) {
      used.push({ entity: v })
    } else {
      used.push(v)
    }
  })

  // Assemble generated block. Only client-supplied types contribute;
  // handler-supplied types are constructed server-side.
  //
  // Identity rules per entry, driven by ``cardinality`` from the
  // form-schema and the existing entities we fetched at load time:
  //
  //   * single + existing instance → revise
  //       reuse the existing entity_id, mint fresh version_id, set
  //       derivedFrom to the existing version_id. The user doesn't
  //       choose; there's only one instance ever and we always
  //       derive from its latest version. (Engine-side
  //       ``_validate_derivation`` enforces "must derive from latest"
  //       so anything else would 422.)
  //
  //   * single + no instance → create
  //       Mint fresh entity_id + version_id, no derivedFrom.
  //
  //   * multiple + user picked an existing instance → revise that
  //       Reuse the picked entity_id, mint fresh version_id, set
  //       derivedFrom to the latest version_id of that instance.
  //
  //   * multiple + user picked "create new" (or no choice) → create
  //       Mint fresh entity_id + version_id, no derivedFrom.
  //
  // Both UUIDs are client-minted in the platform's contract — they
  // serve as idempotency anchors (replay-safe by construction).
  const generated: any[] = []
  for (const g of formSchema.value.generates) {
    if (!g.client_supplied) continue
    const content = generatedValues.value[g.type] ?? {}

    let eid: string
    const vid = crypto.randomUUID()
    let derivedFrom: string | undefined

    const chosenExistingId = chosenExistingByType.value[g.type] || ''
    const existingList = existingByType.value[g.type] ?? []
    const chosenExisting = chosenExistingId
      ? existingList.find(e => e.entityId === chosenExistingId)
      : null

    if (chosenExisting) {
      // Revision path. Reuse the existing entity_id; derive from
      // its latest version. The engine validates the derivation
      // chain — if our cached "latest" is stale (someone else
      // revised the entity since we loaded), we'll get a 409 with
      // the current latest in the payload and the user can refresh.
      eid = chosenExisting.entityId
      derivedFrom = `${g.type}/${chosenExisting.entityId}@${chosenExisting.versionId}`
    } else {
      // Creation path. Fresh entity_id, no derivation.
      eid = crypto.randomUUID()
    }

    const entry: any = {
      entity: `${g.type}/${eid}@${vid}`,
      content,
    }
    if (derivedFrom !== undefined) entry.derivedFrom = derivedFrom
    generated.push(entry)
  }

  const body: any = {
    workflow: props.workflow,
    used,
    generated,
  }

  try {
    const resp = await engineApi.executeActivity(
      props.workflow, props.dossierId, props.activityId, props.activityType, body,
    )
    successResp.value = resp
    // Wait a tick so the user sees the success state before we close.
    setTimeout(() => emit('success'), 600)
  } catch (e: any) {
    submitError.value = e
  } finally {
    submitting.value = false
  }
}

// Format the submit-error detail for display. Engine returns 422
// with structured Pydantic errors (`detail: [{loc, msg, type}, ...]`),
// 409 with workflow-rule failures (`detail: "..."`), 403 with auth
// strings. We render all three reasonably without per-type markup.
function formatSubmitError(err: any): string {
  if (!err) return ''
  if (typeof err.detail === 'string') return err.detail
  if (Array.isArray(err.detail)) {
    return err.detail.map((d: any) =>
      `${(d.loc || []).join('.')}: ${d.msg}`
    ).join('\n')
  }
  if (err.detail && typeof err.detail === 'object') {
    return JSON.stringify(err.detail, null, 2)
  }
  return `HTTP ${err.status}`
}
</script>

<template>
  <div>
    <!-- Loading / error states first -->
    <div v-if="loading" class="row" style="padding: var(--gap-5) 0;">
      <span class="spinner"></span>
      <span class="muted">Formuliergegevens ophalen…</span>
    </div>

    <div v-else-if="loadError" class="banner error">
      {{ loadError }}
    </div>

    <!-- Main form -->
    <div v-else-if="formSchema">
      <!-- Header -->
      <div class="row between" style="align-items: flex-start; margin-bottom: var(--gap-4);">
        <div>
          <span class="eyebrow">Activiteit</span>
          <h2 class="section-title" style="margin-top: 0;">{{ formSchema.label }}</h2>
          <div class="mono faint" style="font-size: 12px;">{{ formSchema.name }}</div>
          <p v-if="formSchema.description" class="muted" style="font-family: var(--font-display); font-size: 15px; max-width: 640px; margin-top: var(--gap-2);">
            {{ formSchema.description }}
          </p>
        </div>
        <button class="ghost compact" @click="emit('cancel')">Annuleer</button>
      </div>

      <!-- Via-exception warning -->
      <div v-if="allowedActivity?.exempted_by_exception" class="banner warn">
        <strong>Met uitzondering.</strong>
        Deze activiteit is enkel uitvoerbaar dankzij een actieve uitzondering
        (versie <span class="mono">{{ allowedActivity.exempted_by_exception.slice(0,8) }}</span>).
        De motor zal deze uitzondering automatisch consumeren — de status
        verandert van <em>active</em> naar <em>consumed</em> en de uitzondering
        kan daarna niet meer gebruikt worden zonder opnieuw verleend te worden.
      </div>

      <!-- Deadline indicators -->
      <div v-if="allowedActivity?.not_after || allowedActivity?.not_before" class="row gap-3" style="margin-bottom: var(--gap-4);">
        <span v-if="allowedActivity.not_before" class="badge teal">
          Niet voor: {{ allowedActivity.not_before }}
        </span>
        <span v-if="allowedActivity.not_after" class="badge gold">
          Niet na: {{ allowedActivity.not_after }}
        </span>
      </div>

      <!-- Hidden auto-resolve summary -->
      <div v-if="hiddenUsedSlots.length" class="surface tinted-paper" style="margin-bottom: var(--gap-5);">
        <div class="eyebrow" style="margin-bottom: var(--gap-2);">Automatisch ingevuld door de motor</div>
        <ul style="list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 4px;">
          <li v-for="(s, idx) in hiddenUsedSlots" :key="idx" class="row gap-2">
            <span class="mono faint" style="font-size: 12px;">{{ s.type }}</span>
            <span class="badge mute">{{ s.auto_resolve }}</span>
          </li>
        </ul>
      </div>

      <!-- Used block -->
      <section v-if="visibleUsedSlots.length">
        <h3 style="margin-top: 0;">Gebruikt</h3>
        <p class="muted" style="font-size: 13px; margin-bottom: var(--gap-3);">
          Entiteiten die deze activiteit nodig heeft.
        </p>
        <div class="col gap-3">
          <UsedSlotInput
            v-for="(s, idx) in formSchema.used.filter(x => x.auto_resolve == null)"
            :key="idx"
            :slot-def="s"
            :dossier-id="dossierId"
            :is-creation="isCreation"
            :model-value="usedValues[formSchema.used.indexOf(s)]"
            @update:model-value="usedValues[formSchema.used.indexOf(s)] = $event"
          />
        </div>
      </section>

      <!-- Generated block - client-supplied -->
      <section v-if="clientSuppliedGenerates.length" style="margin-top: var(--gap-6);">
        <h3 style="margin-top: 0;">Gegenereerd</h3>
        <p class="muted" style="font-size: 13px; margin-bottom: var(--gap-3);">
          Inhoud die deze activiteit produceert. Velden uit het Pydantic-model.
        </p>

        <div v-for="g in clientSuppliedGenerates" :key="g.type" class="surface" style="margin-bottom: var(--gap-4);">
          <!-- Header: shows whether this is a creation or a revision.
               For singletons, the choice is automatic and we just
               surface "herziening van …" or "nieuwe entiteit" so the
               user knows which is happening. For multi-cardinality,
               we render a picker letting the user choose. -->
          <div class="row between" style="margin-bottom: var(--gap-3); align-items: flex-start;">
            <div>
              <span class="eyebrow" style="margin: 0; color: var(--moss);">
                {{ generatedMode(g) === 'revise' ? 'Herziening van bestaande entiteit' : 'Nieuwe entiteit' }}
              </span>
              <div class="mono" style="font-size: 13px; color: var(--ink-soft);">{{ g.type }}</div>
            </div>
            <div class="row gap-2" style="flex-wrap: wrap; justify-content: flex-end;">
              <span class="badge mute">{{ g.cardinality === 'single' ? 'singleton' : 'meervoudig' }}</span>
              <span class="badge moss">client-supplied</span>
            </div>
          </div>

          <!-- Multi-cardinality picker: revise existing or create new.
               Only shown when the type is multi AND there's at least
               one existing instance to choose from. For singletons
               the identity is automatic; for multi+empty the only
               option is "create new" so the picker is redundant. -->
          <div
            v-if="g.cardinality === 'multiple' && (existingByType[g.type]?.length ?? 0) > 0"
            class="surface tinted-paper"
            style="margin-bottom: var(--gap-3); padding: var(--gap-3);"
          >
            <label style="margin-bottom: var(--gap-2);">Bestaand of nieuw?</label>
            <select
              :value="chosenExistingByType[g.type] || ''"
              @change="onExistingPicked(g.type, ($event.target as HTMLSelectElement).value)"
              style="font-family: var(--font-ui);"
            >
              <option value="">— Nieuwe entiteit aanmaken —</option>
              <option
                v-for="ex in existingByType[g.type]" :key="ex.entityId"
                :value="ex.entityId"
              >
                Herzien: {{ summariseExisting(ex.content) || ex.entityId.slice(0, 8) }}
                · v{{ ex.versionId.slice(-8) }}
              </option>
            </select>
          </div>

          <!-- Singleton revision hint: for read-context, when we've
               auto-selected the existing instance to revise. -->
          <div
            v-else-if="g.cardinality === 'single' && chosenExistingByType[g.type]"
            class="banner info"
            style="margin-bottom: var(--gap-3);"
          >
            Deze activiteit herziet de bestaande
            <code>{{ g.type }}</code>. De velden hieronder zijn voor-ingevuld
            met de huidige inhoud — pas aan wat je wilt wijzigen.
          </div>

          <div v-if="!g.schema" class="banner info">
            Geen Pydantic-schema bekend voor type <code>{{ g.type }}</code>.
            De inhoud moet vrij ingevoerd worden.
            <textarea
              v-model="generatedValues[g.type]"
              placeholder="JSON-inhoud"
              style="margin-top: var(--gap-2); font-family: var(--font-mono); font-size: 12px;"
            ></textarea>
          </div>

          <JsonForms
            v-else
            :data="generatedValues[g.type]"
            :schema="schemaFor(g.type, g.schema)"
            :renderers="renderers"
            @change="(e: any) => { generatedValues[g.type] = e.data }"
          />
        </div>
      </section>

      <!-- Generated block - handler-supplied -->
      <section v-if="handlerSuppliedGenerates.length" style="margin-top: var(--gap-5);">
        <div class="surface tinted-paper">
          <div class="eyebrow">Door de motor aangemaakt</div>
          <ul style="list-style: none; padding: 0; margin: var(--gap-2) 0 0; display: flex; flex-direction: column; gap: 4px;">
            <li v-for="g in handlerSuppliedGenerates" :key="g.type" class="row gap-2">
              <span class="mono" style="font-size: 12px; color: var(--olive);">{{ g.type }}</span>
              <span class="faint" style="font-size: 12px;">— door handler ingevuld</span>
            </li>
          </ul>
        </div>
      </section>

      <!-- Submit area -->
      <div style="margin-top: var(--gap-6); border-top: 2px solid var(--ink); padding-top: var(--gap-4);">
        <div v-if="submitError" class="banner error">
          <strong>HTTP {{ submitError.status }}</strong>
          <pre>{{ formatSubmitError(submitError) }}</pre>
        </div>

        <div v-if="successResp" class="banner success">
          <strong>Activiteit uitgevoerd.</strong>
          Status: <em>{{ successResp.dossier?.status ?? '—' }}</em>
        </div>

        <div class="row between">
          <span class="faint mono" style="font-size: 11px;">
            activity_id · {{ activityId.slice(0, 8) }}
          </span>
          <div class="row gap-3">
            <button class="ghost" @click="emit('cancel')" :disabled="submitting">Annuleer</button>
            <button @click="submit" :disabled="submitting">
              <span v-if="submitting" class="row gap-2">
                <span class="spinner"></span> Verzenden…
              </span>
              <span v-else>↑ Verzend</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
