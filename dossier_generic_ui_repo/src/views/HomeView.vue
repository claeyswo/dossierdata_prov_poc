<script setup lang="ts">
// Top-level landing page. Lists the workflow plugins the engine has
// loaded, each as a clickable card. Picking one routes to that
// workflow's home (search results + new-dossier button).
//
// Single-workflow shortcut: if the engine only has one workflow
// loaded, we redirect immediately to its home — no point showing
// a picker with one option. Behavior is opt-in via a guard, so the
// picker is still reachable (e.g. for admin users who want to see
// metadata) by visiting /?picker=1.

import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { engineApi, type WorkflowEntry } from '../composables/useApi'

const route = useRoute()
const router = useRouter()

const workflows = ref<WorkflowEntry[]>([])
const loading = ref(true)
const errorMsg = ref<string | null>(null)

async function load() {
  loading.value = true
  errorMsg.value = null
  try {
    const list = await engineApi.workflows()
    workflows.value = list
    // Single-workflow shortcut. Only redirect if the user didn't
    // explicitly request the picker via ?picker=1.
    if (list.length === 1 && !route.query.picker) {
      router.replace({ name: 'workflow-home', params: { wf: list[0].name } })
    }
  } catch (e: any) {
    errorMsg.value = e?.detail
      ? (typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail))
      : 'Kon werkstromen niet laden.'
  } finally {
    loading.value = false
  }
}

onMounted(load)

function open(wf: WorkflowEntry) {
  router.push({ name: 'workflow-home', params: { wf: wf.name } })
}
</script>

<template>
  <div>
    <span class="eyebrow">Werkruimte</span>
    <h1 class="page-title">
      <em>Een open register</em><br />
      voor het Vlaams onroerend erfgoed.
    </h1>
    <p class="muted" style="font-size: 16px; line-height: 1.55; max-width: 640px; margin-top: var(--gap-4);">
      Kies een werkstroom om aanvragen te bekijken, dossiers te raadplegen
      of een nieuwe procedure op te starten. De interface leest werkstroomdefinities
      rechtstreeks van de motor en genereert formulieren op basis van Pydantic-schema's —
      niets is hardgecodeerd.
    </p>

    <hr />

    <div v-if="loading" class="row" style="margin: var(--gap-5) 0;">
      <span class="spinner"></span>
      <span class="muted">Werkstromen ophalen…</span>
    </div>

    <div v-else-if="errorMsg" class="banner error">
      {{ errorMsg }}
    </div>

    <div v-else-if="!workflows.length" class="banner warn">
      Geen werkstromen geladen op de motor.
    </div>

    <div v-else>
      <h2 class="section-title">Werkstromen</h2>
      <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: var(--gap-4);">
        <button
          v-for="w in workflows" :key="w.name"
          class="surface accent-left"
          @click="open(w)"
          style="
            text-align: left;
            cursor: pointer;
            border: 1px solid var(--line);
            border-left: 4px solid var(--plum);
            background: white;
            color: inherit;
            display: flex;
            flex-direction: column;
            gap: var(--gap-2);
            padding: var(--gap-5);
            font-weight: normal;
          "
        >
          <span class="eyebrow" style="margin: 0;">{{ w.name }}</span>
          <span style="font-family: var(--font-display); font-size: 22px; font-weight: 600; color: var(--plum);">
            {{ w.label }}
          </span>
          <span v-if="w.description" class="muted" style="font-size: 14px;">
            {{ w.description }}
          </span>
          <div class="row gap-2" style="margin-top: var(--gap-2);">
            <span v-if="w.search_path" class="badge teal">zoeken beschikbaar</span>
            <span v-else class="badge mute">zonder zoekindex</span>
            <span v-if="w.version" class="badge mute mono">v{{ w.version }}</span>
          </div>
        </button>
      </div>
    </div>
  </div>
</template>
