<script setup lang="ts">
// Read-only entity detail modal. Renders the current content of the
// entity plus its version history, fetched lazily from the engine on
// open. Closes on backdrop click, Escape, or the X button.
//
// Why a modal rather than a sub-route: deep-linking to an entity
// matters less than deep-linking to a dossier, and this keeps the
// route surface simple. If we later want shareable entity URLs,
// this component lifts cleanly into a route by replacing the v-if
// gate with a route param.
//
// Note on cardinality: "current" is whatever appears in the
// dossier's currentEntities list. For revisable entities (cardinality
// 1, like aanvraag), current = the latest version of the one logical
// entity. For multi-cardinality entities (like behandelaar), each
// "current" entry is a separate logical entity_id; the version-history
// view shows the chain for the entity_id the user clicked. We don't
// surface a "this entity_id is one of N logical instances" hint
// here — the dossier-side sidebar groups by type and shows them all.

import { ref, watch, computed } from 'vue'
import { engineApi } from '../composables/useApi'

interface CurrentEntity {
  type: string
  entityId: string
  versionId: string
  content: any
  createdAt?: string
}

const props = defineProps<{
  dossierId: string
  entity: CurrentEntity | null  // null = closed
}>()

const emit = defineEmits<{ close: [] }>()

const versions = ref<any[] | null>(null)
const versionsLoading = ref(false)
const versionsError = ref<string | null>(null)

// Show a single version's content per row. Toggleable via the
// "expand" caret so the history list isn't a wall of JSON for
// dossiers with deep revision chains.
const expandedVersions = ref<Set<string>>(new Set())

watch(() => props.entity, async (entity) => {
  versions.value = null
  versionsError.value = null
  expandedVersions.value = new Set()
  if (!entity) return
  // External entities are platform-side references (e.g. an erfgoed
  // object IRI). They don't have versions in the platform's PROV
  // sense — the engine returns 404 if we ask. Skip the fetch and
  // show only the current content.
  if (entity.type === 'external') return

  versionsLoading.value = true
  try {
    const resp = await engineApi.entityVersions(
      props.dossierId, entity.type, entity.entityId,
    )
    versions.value = resp.versions
  } catch (e: any) {
    versionsError.value = e?.detail
      ? (typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail))
      : 'Kon versiegeschiedenis niet laden.'
  } finally {
    versionsLoading.value = false
  }
}, { immediate: true })

function close() { emit('close') }
function onBackdrop(e: MouseEvent) {
  // Click on the backdrop (not the dialog itself) closes.
  if (e.target === e.currentTarget) close()
}
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}

function toggleVersion(vid: string) {
  if (expandedVersions.value.has(vid)) expandedVersions.value.delete(vid)
  else expandedVersions.value.add(vid)
}

function isCurrentVersion(vid: string): boolean {
  return props.entity?.versionId === vid
}

function isTombstoned(v: any): boolean {
  // Engine marks tombstoned versions with invalidatedAt; content
  // is replaced by the platform with a redaction marker.
  return v?.invalidatedAt != null
}

const isExternal = computed(() => props.entity?.type === 'external')

// Pretty-print content as 2-space-indented JSON. Could swap for a
// bespoke per-type renderer later (e.g. show bijlage thumbnails for
// oe:aanvraag), but JSON is universal and readable for now.
function pretty(v: any): string {
  try { return JSON.stringify(v, null, 2) }
  catch { return String(v) }
}
</script>

<template>
  <div
    v-if="entity"
    class="modal-backdrop"
    @click="onBackdrop"
    @keydown="onKeydown"
    tabindex="-1"
    ref="backdropEl"
  >
    <div class="modal-dialog" role="dialog" :aria-label="`Entiteit ${entity.type}`">
      <button class="modal-close" @click="close" aria-label="Sluiten">×</button>

      <span class="eyebrow" style="margin-bottom: var(--gap-2);">{{ entity.type }}</span>
      <h2 class="display-2" style="margin: 0 0 var(--gap-3);">
        Entiteit
      </h2>

      <dl class="dl" style="margin-bottom: var(--gap-5);">
        <dt>entity_id</dt>
        <dd class="mono" style="word-break: break-all;">{{ entity.entityId }}</dd>
        <dt>version_id (huidig)</dt>
        <dd class="mono" style="word-break: break-all;">{{ entity.versionId }}</dd>
        <dt v-if="entity.createdAt">aangemaakt</dt>
        <dd v-if="entity.createdAt">{{ entity.createdAt }}</dd>
        <dt>type</dt>
        <dd>
          <code>{{ entity.type }}</code>
          <span v-if="isExternal" class="badge teal" style="margin-left: var(--gap-2);">
            extern
          </span>
        </dd>
      </dl>

      <h3 style="margin-top: 0;">Huidige inhoud</h3>
      <pre class="content-pre">{{ pretty(entity.content) }}</pre>

      <!-- Version history. External entities don't have versions in
           the platform sense, so we omit the section for them. -->
      <template v-if="!isExternal">
        <h3>Versiegeschiedenis</h3>

        <div v-if="versionsLoading" class="row gap-2" style="font-size: 13px;">
          <span class="spinner"></span>
          <span class="faint">versies laden…</span>
        </div>

        <div v-else-if="versionsError" class="banner error">
          {{ versionsError }}
        </div>

        <div v-else-if="versions && versions.length === 0" class="muted">
          Geen versiegeschiedenis beschikbaar.
        </div>

        <ol v-else-if="versions" class="version-list">
          <li
            v-for="(v, i) in versions" :key="v.versionId"
            class="version-item"
            :class="{ 'version-item--current': isCurrentVersion(v.versionId) }"
          >
            <div class="row gap-3" style="align-items: center;">
              <button
                type="button" class="subtle compact"
                @click="toggleVersion(v.versionId)"
                style="min-width: 2em;"
                :aria-expanded="expandedVersions.has(v.versionId)"
              >
                {{ expandedVersions.has(v.versionId) ? '▾' : '▸' }}
              </button>
              <span class="mono" style="font-size: 11px; flex: 1;">
                v{{ i + 1 }} · {{ v.versionId }}
              </span>
              <span v-if="isCurrentVersion(v.versionId)" class="badge plum">huidig</span>
              <span v-if="isTombstoned(v)" class="badge crimson">tombstoned</span>
              <span v-if="v.derivedFrom" class="badge mute" :title="`afgeleid van ${v.derivedFrom}`">
                herzien
              </span>
            </div>

            <div v-if="expandedVersions.has(v.versionId)" style="margin-top: var(--gap-3);">
              <dl class="dl" style="grid-template-columns: max-content 1fr; gap: 4px var(--gap-4); font-size: 12px; margin-bottom: var(--gap-3);">
                <dt v-if="v.generatedBy">aangemaakt door</dt>
                <dd v-if="v.generatedBy" class="mono">{{ v.generatedBy }}</dd>
                <dt v-if="v.derivedFrom">herziening van</dt>
                <dd v-if="v.derivedFrom" class="mono">{{ v.derivedFrom }}</dd>
                <dt v-if="v.invalidatedAt">tombstoned op</dt>
                <dd v-if="v.invalidatedAt">{{ v.invalidatedAt }}</dd>
              </dl>
              <pre class="content-pre" style="font-size: 11px;">{{ pretty(v.content) }}</pre>
            </div>
          </li>
        </ol>
      </template>
    </div>
  </div>
</template>

<style scoped>
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(31, 26, 42, 0.45);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 56px 24px 24px;
  z-index: 1000;
  overflow-y: auto;
}

.modal-dialog {
  position: relative;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 3px;
  padding: var(--gap-6);
  max-width: 720px;
  width: 100%;
  max-height: calc(100vh - 80px);
  overflow-y: auto;
  box-shadow: 0 12px 40px rgba(0,0,0,0.18);
}

.modal-close {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 32px;
  height: 32px;
  padding: 0;
  border-radius: 2px;
  font-size: 22px;
  line-height: 1;
  background: transparent;
  border: 1px solid transparent;
  color: var(--ink-faint);
}
.modal-close:hover {
  background: var(--bg-deep);
  border-color: var(--line);
  color: var(--ink);
}

.content-pre {
  font-family: var(--font-mono);
  font-size: 12px;
  background: var(--bg-deep);
  padding: var(--gap-3) var(--gap-4);
  border-left: 3px solid var(--plum-line);
  margin: 0 0 var(--gap-5);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.5;
}

.version-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--gap-3);
}
.version-item {
  background: var(--bg-deep);
  padding: var(--gap-3) var(--gap-4);
  border-radius: 2px;
  border-left: 3px solid var(--line);
}
.version-item--current {
  border-left-color: var(--plum);
  background: var(--plum-soft);
}
</style>
