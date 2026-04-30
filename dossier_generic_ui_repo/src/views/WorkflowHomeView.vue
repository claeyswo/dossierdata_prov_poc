<script setup lang="ts">
// Workflow-scoped landing page.
//
// Three things on this page:
//
//   1. Page header — workflow's label + description from /workflows.
//   2. "Nieuw dossier" CTA — links to the activity picker.
//   3. Search results / dossier list — calls the plugin's search
//      endpoint (the path comes from the workflow entry's search_path).
//      Optional query input runs a fuzzy filter on whatever the
//      plugin's search endpoint accepts (most use ?q=...).
//
// Empty-state distinction:
//   - search ran successfully, no hits → "Geen resultaten"
//   - search returned a `reason` field (typical: ES not configured /
//     not reachable) → render that reason as an info banner
//   - workflow has no search_path → render an explanatory note;
//     no search box, no list — the only action is "Nieuw dossier".

import { ref, watch, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import {
  engineApi,
  type WorkflowEntry,
  type SearchResult,
} from '../composables/useApi'

const props = defineProps<{ wf: string }>()
const router = useRouter()

const workflowEntry = ref<WorkflowEntry | null>(null)
const workflowLoading = ref(true)
const workflowError = ref<string | null>(null)

const query = ref('')
const result = ref<SearchResult | null>(null)
const searchLoading = ref(false)
const searchError = ref<string | null>(null)

async function loadWorkflow() {
  workflowLoading.value = true
  workflowError.value = null
  try {
    const list = await engineApi.workflows()
    const entry = list.find(w => w.name === props.wf)
    if (!entry) {
      workflowError.value = `Werkstroom "${props.wf}" niet gevonden op de motor.`
      return
    }
    workflowEntry.value = entry
    // Trigger an initial empty search so the page lands with whatever
    // the user has access to — no need to type to see anything.
    if (entry.search_path) {
      runSearch()
    }
  } catch (e: any) {
    workflowError.value = e?.detail
      ? (typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail))
      : 'Kon werkstroom niet laden.'
  } finally {
    workflowLoading.value = false
  }
}

async function runSearch() {
  if (!workflowEntry.value?.search_path) return
  searchLoading.value = true
  searchError.value = null
  try {
    // The plugin's search endpoint accepts ?q=... by convention. If
    // the field is empty we omit it — most plugins return "all
    // visible to caller" in that case (ACL-filtered when ES has the
    // index). Other params (gemeente, beslissing, etc.) are
    // plugin-specific and not exposed here in Phase 1.5; the user
    // types into a single search box.
    const params: Record<string, string | number> = { limit: 50 }
    if (query.value.trim()) params.q = query.value.trim()
    result.value = await engineApi.searchWorkflow(workflowEntry.value.search_path, params)
  } catch (e: any) {
    searchError.value = e?.detail
      ? (typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail))
      : 'Zoekopdracht mislukt.'
  } finally {
    searchLoading.value = false
  }
}

let queryDebounce: number | null = null
watch(query, () => {
  // Debounce the per-keystroke search to keep ES from getting hammered.
  // 220ms is a comfortable typing rhythm — fast enough to feel live,
  // slow enough that a sentence-length query waits for typing to settle.
  if (queryDebounce !== null) window.clearTimeout(queryDebounce)
  queryDebounce = window.setTimeout(() => {
    runSearch()
    queryDebounce = null
  }, 220)
})

watch(() => props.wf, loadWorkflow)
onMounted(loadWorkflow)

function openHit(hit: any) {
  if (!hit.dossier_id) return
  router.push({
    name: 'dossier',
    params: { wf: props.wf, id: hit.dossier_id },
  })
}

function startNew() {
  router.push({ name: 'new-dossier', params: { wf: props.wf } })
}

const hasSearch = computed(() => Boolean(workflowEntry.value?.search_path))
</script>

<template>
  <div>
    <!-- Workflow header. Label + description from /workflows. -->
    <div v-if="workflowLoading" class="row" style="padding: var(--gap-5) 0;">
      <span class="spinner"></span>
      <span class="muted">Werkstroom ophalen…</span>
    </div>

    <div v-else-if="workflowError" class="banner error">
      {{ workflowError }}
    </div>

    <div v-else-if="workflowEntry">
      <span class="eyebrow">Werkstroom</span>
      <div class="row between" style="align-items: flex-start; margin-bottom: var(--gap-3);">
        <h1 class="page-title" style="margin: 0;">
          {{ workflowEntry.label }}
        </h1>
        <button @click="startNew" style="white-space: nowrap;">
          + Nieuw dossier
        </button>
      </div>
      <p v-if="workflowEntry.description" class="muted" style="font-size: 15px; max-width: 640px; margin-top: 0;">
        {{ workflowEntry.description }}
      </p>

      <hr />

      <!-- ===== Search + results ===== -->
      <div v-if="hasSearch">
        <h2 class="section-title">Mijn dossiers</h2>

        <div class="row" style="margin-bottom: var(--gap-4);">
          <input
            type="search"
            v-model="query"
            placeholder="Zoek op onderwerp…"
            style="max-width: 480px;"
          />
          <span v-if="searchLoading" class="row gap-2" style="font-size: 13px;">
            <span class="spinner"></span>
            <span class="faint">zoeken…</span>
          </span>
        </div>

        <!-- Plugin-side error / empty-with-reason / results -->
        <div v-if="searchError" class="banner error">{{ searchError }}</div>

        <div v-else-if="result?.reason" class="banner info">
          <strong>Geen zoekresultaten beschikbaar.</strong>
          {{ result.reason }}
        </div>

        <div v-else-if="result && !result.hits.length" class="muted" style="padding: var(--gap-4) 0;">
          Geen dossiers gevonden.
        </div>

        <div v-else-if="result?.hits.length" class="col gap-3">
          <a
            v-for="hit in result.hits"
            :key="hit.dossier_id || hit.id || JSON.stringify(hit)"
            class="list-row"
            href="#"
            @click.prevent="openHit(hit)"
          >
            <div>
              <div class="primary">
                {{ hit.onderwerp || hit.title || hit.label || '— zonder onderwerp —' }}
              </div>
              <div class="secondary" v-if="hit.dossier_id">
                {{ hit.dossier_id }}
              </div>
            </div>
            <div class="row gap-2" style="justify-content: flex-end;">
              <span v-if="hit.status" class="badge plum">{{ hit.status }}</span>
              <span v-if="hit.beslissing" class="badge moss">{{ hit.beslissing }}</span>
              <span v-if="hit.gemeente" class="badge mute">{{ hit.gemeente }}</span>
            </div>
          </a>
        </div>

        <div v-if="result" class="faint" style="font-size: 12px; margin-top: var(--gap-3);">
          {{ result.total }} resulta{{ result.total === 1 ? 'at' : 'ten' }}
        </div>
      </div>

      <!-- ===== No-search fallback ===== -->
      <div v-else class="surface tinted-paper">
        <div class="eyebrow">Geen zoekindex</div>
        <p class="muted" style="margin: var(--gap-2) 0 0;">
          Deze werkstroom heeft geen zoekindex geregistreerd. Je kan
          een nieuw dossier aanmaken via <em>Nieuw dossier</em>, of
          een bestaand dossier rechtstreeks via UUID openen.
        </p>
      </div>
    </div>
  </div>
</template>
