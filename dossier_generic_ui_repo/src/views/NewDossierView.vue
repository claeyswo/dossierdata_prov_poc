<script setup lang="ts">
// Pick a creation activity for the current workflow.
//
// The workflow comes from the route's :wf param, set by the parent
// route /workflows/:wf/dossiers/new. We hit /workflows once to find
// our workflow's creation_activities (they're inlined there to avoid
// a second round-trip per workflow). Picking one mints a fresh
// dossier UUID + activity UUID and routes to the dossier view in
// pre-creation mode (?init=<activity>&activityId=<uuid>).
//
// We do NOT pre-create the dossier here. The platform's contract is
// that the creation activity itself does that, atomically. Pre-creation
// would leave a half-state if the user navigates away.

import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { engineApi, type WorkflowEntry } from '../composables/useApi'

const props = defineProps<{ wf: string }>()
const router = useRouter()

const workflowEntry = ref<WorkflowEntry | null>(null)
const loading = ref(true)
const errorMsg = ref<string | null>(null)

async function loadWorkflow() {
  loading.value = true
  errorMsg.value = null
  try {
    const list = await engineApi.workflows()
    const entry = list.find(w => w.name === props.wf)
    if (!entry) {
      errorMsg.value = `Werkstroom "${props.wf}" niet gevonden op de motor.`
      return
    }
    workflowEntry.value = entry
  } catch (e: any) {
    errorMsg.value = e?.detail
      ? (typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail))
      : 'Kon werkstroomgegevens niet laden.'
  } finally {
    loading.value = false
  }
}

onMounted(loadWorkflow)

const creationActivities = computed(() =>
  workflowEntry.value?.creation_activities ?? []
)

function pickActivity(activity: { name: string; label: string }) {
  // Mint UUIDs client-side. The platform uses client-supplied UUIDs
  // as idempotency keys: replaying a PUT with the same activityId
  // returns the original response without re-running side effects.
  // Pre-minting also lets us route to the dossier view immediately
  // (the form needs an activity_id to render the activity_id badge).
  const dossierId = crypto.randomUUID()
  const activityId = crypto.randomUUID()
  router.push({
    name: 'dossier',
    params: { wf: props.wf, id: dossierId },
    query: {
      init: activity.name,
      activityId,
    },
  })
}
</script>

<template>
  <div>
    <span class="eyebrow">Nieuw dossier</span>
    <h1 class="page-title">Kies de aanleiding.</h1>
    <p class="muted" style="font-size: 16px; max-width: 640px;">
      Welke activiteit start dit dossier? Alleen activiteiten met
      <code>can_create_dossier: true</code> verschijnen in deze lijst —
      andere activiteiten kunnen pas uitgevoerd worden op een
      bestaand dossier.
    </p>

    <hr />

    <div v-if="loading" class="row" style="margin: var(--gap-5) 0;">
      <span class="spinner"></span>
      <span class="muted">Activiteiten ophalen…</span>
    </div>

    <div v-else-if="errorMsg" class="banner error">
      {{ errorMsg }}
    </div>

    <div v-else-if="!creationActivities.length" class="banner warn">
      Geen activiteiten met <code>can_create_dossier: true</code> beschikbaar
      in werkstroom <strong>{{ wf }}</strong>.
    </div>

    <div
      v-else
      style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--gap-4);"
    >
      <button
        v-for="a in creationActivities" :key="a.name"
        @click="pickActivity(a)"
        class="surface accent-left"
        style="
          text-align: left;
          cursor: pointer;
          background: white;
          color: inherit;
          border: 1px solid var(--line);
          border-left: 4px solid var(--plum);
          padding: var(--gap-5);
          display: flex;
          flex-direction: column;
          gap: var(--gap-2);
          font-weight: normal;
        "
      >
        <span class="eyebrow" style="margin: 0;">{{ a.name }}</span>
        <span style="font-family: var(--font-display); font-size: 20px; font-weight: 600; color: var(--plum);">
          {{ a.label }}
        </span>
        <span v-if="a.description" class="muted" style="font-size: 14px;">
          {{ a.description }}
        </span>
      </button>
    </div>
  </div>
</template>
