<script setup lang="ts">
// One row in the used block of an activity form.
//
// Three flavors of used-slot from the form-schema endpoint:
//
//   1. external: true  → free-text URI input (e.g. heritage object IRI)
//      User pastes/types a URL; we validate format only.
//
//   2. external: false (default) → internal entity picker
//      Dropdown of existing entities of that type in the dossier.
//      Submits the selected version_id wrapped as the platform's
//      shorthand reference (the engine accepts a few formats; we
//      use the {entity, type} object form because it's unambiguous).
//
//   3. auto_resolve set → not rendered at all
//      The engine resolves it at execute time. The parent
//      ActivityForm filters these out before instantiating us, so
//      this component never receives one — but we still document
//      the case for clarity.
//
// In creation mode (no dossier exists yet) the internal-entity
// dropdown is impossible because we have nothing to pick from. We
// degrade gracefully: show a disabled dropdown with an explanatory
// message. In practice creation activities rarely have non-external
// used slots — they couldn't have anything to consume — so this is
// a corner case rather than a common path.

import { ref, watch, onMounted, computed } from 'vue'
import { engineApi, type UsedSlot } from '../composables/useApi'

const props = defineProps<{
  // Renamed from "slot" to dodge the Vue legacy attribute. Vue 3 allows
  // ``slot`` as a custom prop name (the v2 deprecated attribute is gone),
  // but linters and TS tooling sometimes flag it and the cognitive trap
  // for readers isn't worth the brevity. ``slotDef`` is unambiguous:
  // this is the slot's metadata from the form-schema, not Vue's
  // template-slot machinery.
  slotDef: UsedSlot
  dossierId: string
  isCreation: boolean
  modelValue: any  // current value: string IRI, or { entity, type } ref
}>()

const emit = defineEmits<{
  'update:modelValue': [value: any]
}>()

// === External slot ===
// We expose the bare string to the parent, who wraps it correctly
// in the request body.
const iriValue = ref<string>(typeof props.modelValue === 'string' ? props.modelValue : '')
watch(iriValue, v => emit('update:modelValue', v.trim() || null))

// === Internal-entity slot ===
const candidates = ref<any[]>([])
const loadingCandidates = ref(false)
const candidatesError = ref<string | null>(null)
// Selected version_id (the user picks a row; we send {entity, type}).
const selectedVersionId = ref<string>(
  typeof props.modelValue === 'object' && props.modelValue?.entity
    ? extractVersionId(props.modelValue.entity)
    : ''
)

function extractVersionId(ref: string): string {
  // Refs come in shapes like "type/<entity_id>@<version_id>" — we
  // care about the version_id portion only, since it's the unique
  // identifier of a row.
  const at = ref.lastIndexOf('@')
  return at >= 0 ? ref.slice(at + 1) : ref
}

async function loadCandidates() {
  if (props.isCreation) return
  loadingCandidates.value = true
  candidatesError.value = null
  try {
    const resp = await engineApi.entitiesByType(props.dossierId, props.slotDef.type)
    // Engine response shape:
    //   { dossier_id, entity_type, versions: [...] }
    // Defensive on the shape — accept either {versions: []} or a
    // bare array, since older engine versions or other plugins
    // could expose either. The plain `Array.isArray` check we had
    // before silently swallowed the wrapped shape and presented an
    // empty dropdown.
    let allVersions: any[] = []
    if (resp && typeof resp === 'object' && Array.isArray((resp as any).versions)) {
      allVersions = (resp as any).versions
    } else if (Array.isArray(resp)) {
      allVersions = resp
    } else {
      allVersions = []
    }

    // Don't deduplicate by entity_id. Each version is a distinct
    // PROV fact, and which one the user picks is part of what the
    // audit trail records. Hiding older versions would silently make
    // some valid choices unreachable — for example, signing an
    // earlier ``oe:beslissing`` after a revision still has meaningful
    // semantics, and the workflow rules (not the picker) decide
    // what's allowed.
    candidates.value = allVersions
  } catch (e: any) {
    candidatesError.value = e?.detail
      ? (typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail))
      : 'Kon entiteiten niet ophalen.'
  } finally {
    loadingCandidates.value = false
  }
}

watch(selectedVersionId, vid => {
  if (!vid) {
    emit('update:modelValue', null)
    return
  }
  const c = candidates.value.find(c => (c.versionId || c.version_id || c.id) === vid)
  if (!c) {
    emit('update:modelValue', null)
    return
  }
  // Construct the platform's used-ref shape. The engine accepts
  // `{ entity: "type/<eid>@<vid>", type: "<type>" }` — the type field
  // is redundant if the entity ref carries the type prefix, but we
  // include both for explicitness.
  const eid = c.entityId || c.entity_id
  const ref = `${props.slotDef.type}/${eid}@${vid}`
  emit('update:modelValue', { entity: ref, type: props.slotDef.type })
})

onMounted(() => {
  if (!props.slotDef.external) loadCandidates()
})

function summariseCandidate(c: any): string {
  // Best-effort one-liner pulled from content. Same heuristic as
  // DossierView's sidebar — if we ever expose content shape via
  // form-schema we can do better.
  if (!c.content) return ''
  const candidateFields = ['onderwerp', 'naam', 'titel', 'label', 'reason', 'activity', 'gemeente']
  for (const k of candidateFields) {
    if (c.content[k]) return String(c.content[k]).slice(0, 80)
  }
  return JSON.stringify(c.content).slice(0, 80)
}
</script>

<template>
  <div class="control" style="background: var(--paper-deep); padding: var(--gap-3) var(--gap-4); border-left: 3px solid var(--teal);">
    <label>
      <span class="mono" style="text-transform: none; letter-spacing: 0; color: var(--olive); font-size: 12px; font-weight: 500;">
        {{ slotDef.type }}
      </span>
      <span v-if="slotDef.required" style="color: var(--crimson); margin-left: 6px;">*</span>
      <span v-if="slotDef.external" class="badge teal" style="margin-left: 8px; font-size: 9px;">extern</span>
      <span v-else class="badge plum" style="margin-left: 8px; font-size: 9px;">intern</span>
    </label>

    <div v-if="slotDef.description" class="faint" style="font-size: 12px; margin-top: -4px; margin-bottom: var(--gap-2); font-family: var(--font-display); font-style: italic;">
      {{ slotDef.description }}
    </div>

    <!-- External slot: free-text -->
    <input
      v-if="slotDef.external"
      type="text"
      v-model="iriValue"
      placeholder="https://id.erfgoed.net/erfgoedobjecten/12345"
      class="mono"
      style="font-size: 13px;"
    />

    <!-- Internal slot, pre-creation: explanatory disabled control -->
    <div v-else-if="isCreation" class="faint" style="font-size: 13px; padding: var(--gap-2) 0;">
      Bij aanmaakactiviteiten zijn nog geen interne entiteiten beschikbaar.
      Als de activiteit deze slot vereist is dat waarschijnlijk een werkstroomfout.
    </div>

    <!-- Internal slot, normal mode: dropdown of existing entities -->
    <div v-else>
      <div v-if="loadingCandidates" class="row gap-2" style="font-size: 13px; padding: var(--gap-2) 0;">
        <span class="spinner"></span>
        <span class="muted">Entiteiten laden…</span>
      </div>
      <div v-else-if="candidatesError" class="banner error" style="font-size: 13px;">
        {{ candidatesError }}
      </div>
      <select v-else v-model="selectedVersionId">
        <option value="">— kies —</option>
        <option
          v-for="c in candidates"
          :key="c.versionId || c.version_id || c.id"
          :value="c.versionId || c.version_id || c.id"
        >
          <!--
            Label combines (a) a content-summary if we can derive one
            and (b) the last 8 chars of the version_id. Two versions
            of the same logical entity may have nearly-identical
            content summaries, so the version_id suffix is what tells
            them apart at a glance. Cheap and works for any entity
            type without a per-type renderer.
          -->
          {{ summariseCandidate(c) || (c.entityId || c.entity_id || '?').slice(0, 8) }}
          ·
          v{{ (c.versionId || c.version_id || '').slice(-8) }}{{ c.derivedFrom ? ' (herziening)' : '' }}
        </option>
      </select>
    </div>
  </div>
</template>
