<script setup lang="ts">
// The main dossier workspace.
//
// Two display modes:
// 1. **Existing dossier**: GET /dossiers/{id} succeeds, render the
//    full dossier shell with status, entities, allowedActivities,
//    and activity log. Picking an allowedActivity opens its form.
// 2. **Pre-creation**: query param ?init=<workflow>:<activity> means
//    NewDossierView routed here with a minted dossier_id but the
//    dossier doesn't exist yet. We skip the GET (which would 404)
//    and immediately render the chosen creation activity's form.
//    Submitting the form executes the activity, which creates the
//    dossier; we then transition to mode 1.
//
// The form-rendering UI itself lives in <ActivityForm/>; this view
// is the orchestrator.

import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { engineApi, type DossierDetail, type AllowedActivity } from '../composables/useApi'
import ActivityForm from '../components/ActivityForm.vue'
import EntityDetailModal from '../components/EntityDetailModal.vue'

// Currently-open entity in the detail modal. null = closed.
// We hold the full currentEntity object (not just an id) so the
// modal can render the headline content immediately and lazy-load
// version history in the background.
const selectedEntity = ref<any | null>(null)

const props = defineProps<{ wf: string; id: string }>()
const route = useRoute()
const router = useRouter()

const dossier = ref<DossierDetail | null>(null)
const error = ref<string | null>(null)
const loading = ref(true)

// Active form selection. Either an allowedActivity from the dossier
// (existing-dossier mode), or a synthesized "create this dossier"
// entry derived from the ?init= param (pre-creation mode).
//
// activeActivity: the activity type to render a form for (e.g.
// "oe:dienAanvraagIn"). null = no form open.
// preMintedActivityId: client-minted UUID for the activity. Pre-
// minted in pre-creation mode (passed via ?activityId query param);
// in existing-dossier mode we mint fresh per click.
const activeActivity = ref<string | null>(null)
const preMintedActivityId = ref<string | null>(null)

const initActivity = computed(() => route.query.init as string | undefined)
const initActivityId = computed(() => route.query.activityId as string | undefined)

const isPreCreation = computed(() => !!initActivity.value && !dossier.value)

// Workflow always comes from the route. The dossier object also
// carries its workflow once loaded, but the route is the canonical
// source — pre-creation mode has no dossier yet, and the route param
// is what got us to this URL in the first place.
const workflow = computed(() => dossier.value?.workflow ?? props.wf)

async function loadDossier() {
  loading.value = true
  error.value = null
  try {
    dossier.value = await engineApi.getDossier(props.id)
    // Track in localStorage for the home view's recent list.
    pushVisited(props.id)
  } catch (e: any) {
    if (e?.status === 404) {
      // Expected in pre-creation mode. Otherwise it's a real "not found".
      if (isPreCreation.value) {
        // Pre-creation: synthesize an active activity from the init param.
        if (initActivity.value && initActivityId.value) {
          activeActivity.value = initActivity.value
          preMintedActivityId.value = initActivityId.value
        }
      } else {
        error.value = `Dossier ${props.id} niet gevonden.`
      }
    } else {
      error.value = e?.detail
        ? (typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail))
        : 'Kon dossier niet laden.'
    }
  } finally {
    loading.value = false
  }
}

function pushVisited(id: string) {
  try {
    const KEY = 'dossier-ui:visited'
    const existing: string[] = JSON.parse(localStorage.getItem(KEY) || '[]')
    const next = [id, ...existing.filter(x => x !== id)].slice(0, 8)
    localStorage.setItem(KEY, JSON.stringify(next))
  } catch { /* ignore */ }
}

onMounted(loadDossier)
// Reload when the username changes — different roles see different
// allowedActivities, so we want fresh filtering.
watch(() => route.fullPath, () => { /* navigation handled by router */ })

function pickActivity(a: AllowedActivity) {
  activeActivity.value = a.type
  preMintedActivityId.value = crypto.randomUUID()
}

function closeForm() {
  // Leaving pre-creation mode without submitting goes back to the new-
  // dossier picker; otherwise just close the form.
  if (isPreCreation.value) {
    router.push({ name: 'new-dossier', params: { wf: props.wf } })
  } else {
    activeActivity.value = null
    preMintedActivityId.value = null
  }
}

async function onActivitySuccess() {
  // After a successful activity execution, refresh the dossier and
  // close the form. In pre-creation mode this transitions us to
  // existing-dossier mode (the GET will now succeed).
  activeActivity.value = null
  preMintedActivityId.value = null
  await loadDossier()
  // Clean up init query params so a refresh doesn't re-trigger the
  // pre-creation flow.
  if (initActivity.value) {
    router.replace({
      name: 'dossier',
      params: { wf: props.wf, id: props.id },
    })
  }
}

// ===== Display helpers =====
const statusBadgeClass = (status: string) => {
  if (status === 'concept') return 'olive'
  if (status === 'ingediend') return 'teal'
  if (status === 'aanvraag_onvolledig' || status === 'aanvraag_geweigerd') return 'crimson'
  if (status === 'toelating_verleend') return 'moss'
  if (status === 'aanvraag_ingetrokken') return 'mute'
  return 'plum'
}

function summariseContent(content: any): string {
  if (!content || typeof content !== 'object') return ''
  // Heuristic: pick the most "label-ish" field for a one-liner.
  const candidates = ['onderwerp', 'naam', 'titel', 'label', 'reason', 'activity']
  for (const k of candidates) {
    if (content[k]) return String(content[k]).slice(0, 80)
  }
  // Fallback: stringify the first 2-3 keys.
  const keys = Object.keys(content).slice(0, 3)
  return keys.map(k => `${k}: ${JSON.stringify(content[k]).slice(0, 30)}`).join(' · ')
}
</script>

<template>
  <div v-if="loading" class="row" style="padding: var(--gap-7) 0;">
    <span class="spinner"></span>
    <span class="muted">Dossier laden…</span>
  </div>

  <div v-else-if="error" class="banner error">
    {{ error }}
  </div>

  <div v-else>
    <!-- ======= Header strip ======= -->
    <div class="row between" style="align-items: flex-end; margin-bottom: var(--gap-5);">
      <div>
        <span class="eyebrow">{{ workflow }}</span>
        <h1 class="page-title" style="margin: 0;">
          <span v-if="dossier">Dossier <em>{{ dossier.id.slice(0, 8) }}</em></span>
          <span v-else>Nieuw dossier</span>
        </h1>
        <div class="mono faint" style="font-size: 12px; margin-top: 4px;">
          {{ id }}
        </div>
      </div>
      <div v-if="dossier" class="col gap-2" style="align-items: flex-end;">
        <span class="badge" :class="statusBadgeClass(dossier.status)">
          {{ dossier.status }}
        </span>
        <span class="faint" style="font-size: 12px;">
          {{ dossier.activities.length }} activiteit{{ dossier.activities.length === 1 ? '' : 'en' }}
          · {{ dossier.currentEntities.length }} entiteit{{ dossier.currentEntities.length === 1 ? '' : 'en' }}
        </span>
      </div>
    </div>

    <hr class="rule-strong" />

    <!-- ======= Pre-creation mode: just the form ======= -->
    <div v-if="isPreCreation && activeActivity">
      <div class="banner info">
        <strong>Aanmaakmodus.</strong>
        Dit dossier bestaat nog niet. Wanneer je deze activiteit
        verzendt, wordt het dossier <em>en</em> de eerste activiteit
        atomair gecreëerd.
      </div>
      <ActivityForm
        :workflow="workflow"
        :dossier-id="id"
        :activity-id="preMintedActivityId!"
        :activity-type="activeActivity"
        :is-creation="true"
        @success="onActivitySuccess"
        @cancel="closeForm"
      />
    </div>

    <!-- ======= Existing dossier ======= -->
    <div v-else-if="dossier" style="display: grid; grid-template-columns: 1fr 360px; gap: var(--gap-6);">
      <!-- Main column -->
      <div>
        <!-- Active activity form takes priority -->
        <div v-if="activeActivity">
          <ActivityForm
            :workflow="workflow"
            :dossier-id="id"
            :activity-id="preMintedActivityId!"
            :activity-type="activeActivity"
            :is-creation="false"
            :allowed-activity="dossier.allowedActivities.find(a => a.type === activeActivity)"
            @success="onActivitySuccess"
            @cancel="closeForm"
          />
        </div>

        <!-- Activity picker -->
        <div v-else>
          <h2 class="section-title">Beschikbare activiteiten</h2>
          <p v-if="!dossier.allowedActivities.length" class="muted">
            Geen activiteiten beschikbaar in deze status voor de huidige gebruiker.
          </p>
          <div v-else style="display: grid; gap: var(--gap-3);">
            <button
              v-for="a in dossier.allowedActivities"
              :key="a.type"
              class="ghost"
              @click="pickActivity(a)"
              style="
                padding: var(--gap-4);
                text-align: left;
                background: white;
                border: 1px solid var(--paper-dark);
                display: flex;
                flex-direction: column;
                align-items: flex-start;
                gap: 4px;
                font-weight: normal;
              "
            >
              <div class="row between" style="width: 100%;">
                <span style="font-family: var(--font-display); font-size: 18px; font-weight: 500; color: var(--ink);">
                  {{ a.label }}
                </span>
                <span v-if="a.exempted_by_exception" class="badge gold">
                  via uitzondering
                </span>
              </div>
              <span class="mono faint" style="font-size: 11px;">{{ a.type }}</span>
              <span v-if="a.not_after" class="faint" style="font-size: 12px;">
                Verloopt: {{ a.not_after }}
              </span>
            </button>
          </div>

          <h2 class="section-title">Activiteiten</h2>
          <div v-if="!dossier.activities.length" class="muted">Nog geen activiteiten.</div>
          <ol v-else class="surface tinted-paper" style="list-style: none; padding: var(--gap-4); margin: 0;">
            <li
              v-for="act in dossier.activities" :key="act.id"
              style="padding: var(--gap-2) 0; border-bottom: 1px dashed var(--paper-dark);"
            >
              <div class="row between">
                <span style="font-family: var(--font-display); font-size: 15px;">{{ act.type }}</span>
                <span class="mono faint" style="font-size: 11px;">
                  {{ (act.startedAtTime || '').slice(0, 19).replace('T', ' ') }}
                </span>
              </div>
              <div class="faint mono" style="font-size: 11px;">{{ act.id }}</div>
            </li>
          </ol>
        </div>
      </div>

      <!-- Sidebar: current entities + meta -->
      <aside class="col gap-5">
        <div class="surface tinted-paper">
          <span class="eyebrow">Status</span>
          <div style="font-family: var(--font-display); font-size: 22px; font-weight: 500; margin-top: 4px;">
            {{ dossier.status }}
          </div>
        </div>

        <div>
          <h3 style="margin-top: 0;">Huidige entiteiten</h3>
          <div v-if="!dossier.currentEntities.length" class="muted">Nog geen entiteiten.</div>
          <ul v-else style="list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--gap-2);">
            <li
              v-for="e in dossier.currentEntities"
              :key="e.versionId || e.entityId || e.entity"
            >
              <!-- Click opens the read-only detail modal. Button instead
                   of a link because there's no URL — the modal lives
                   in the same route. Keyboard-friendly via the native
                   button focus + activation. -->
              <button
                type="button"
                class="entity-card"
                @click="selectedEntity = e"
              >
                <div class="mono" style="font-size: 11px; color: var(--olive);">{{ e.type }}</div>
                <div class="mono faint" style="font-size: 10px; margin-top: 2px;">
                  {{ (e.entityId || '').slice(0, 8) || '—' }}
                </div>
                <div v-if="e.content" style="margin-top: var(--gap-2); font-family: var(--font-display); font-size: 13px;">
                  {{ summariseContent(e.content) }}
                </div>
              </button>
            </li>
          </ul>
        </div>
      </aside>
    </div>

    <!-- Read-only entity detail modal. Lives at the dossier-view
         level so any inner component can request it via the
         selectedEntity ref. -->
    <EntityDetailModal
      :dossier-id="props.id"
      :entity="selectedEntity"
      @close="selectedEntity = null"
    />
  </div>
</template>
