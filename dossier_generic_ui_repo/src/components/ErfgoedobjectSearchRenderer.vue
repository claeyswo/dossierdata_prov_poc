<script setup lang="ts">
/**
 * Custom JSONForms renderer for fields tagged
 * ``format: "uri-erfgoedobject"`` in the JSON Schema.
 *
 * Replaces the generic free-text URI input with a search widget
 * that queries the Vlaamse onroerend-erfgoed inventaris API:
 *
 *   GET https://inventaris.onroerenderfgoed.be/erfgoedobjecten?tekst=<q>
 *
 * Stores the chosen object's ``uri`` field as the value, identical to
 * what the user would have typed by hand. The widget is purely a UI
 * upgrade — backends and downstream tooling never see a different
 * shape than they would have without the widget.
 *
 * == Selection-state design ==
 *
 * After selecting, we show a rich card (photo, name, gemeente, short
 * description, URI) instead of the bare IRI. To support reload — the
 * user comes back to revise an existing aanvraag and the form
 * pre-fills with just a URI string — we fetch the metadata for that
 * URI on mount. If the fetch fails or the URI doesn't follow the
 * inventaris IRI shape, we fall back to a "selected URI" card with
 * just the URI text and a "change" button. The data model never
 * carries anything but the URI; all the metadata is decorative.
 */

import { ref, watch, onMounted, computed } from 'vue'
import { rendererProps, useJsonFormsControl } from '@jsonforms/vue'

const props = defineProps({ ...rendererProps<any>() })
const { control, handleChange } = useJsonFormsControl(props)

// The URI shape the inventaris API mints for each object:
//   https://id.erfgoed.net/erfgoedobjecten/<numeric_id>
// We use this regex to extract the id from a stored URI on reload,
// so we can re-fetch the object's metadata to render the card. If a
// URI doesn't match, we don't fail — we just show the bare URI in a
// fallback card.
const ERFGOED_URI_RE = /^https:\/\/id\.erfgoed\.net\/erfgoedobjecten\/(\d+)$/

interface ErfgoedobjectResult {
  id: number
  naam: string
  uri: string
  korte_beschrijving?: string
  gemeente_samenvatting?: string
  locatie_samenvatting?: string
  primaire_foto?: string
  omvang?: { id: number; naam: string }
  disciplines?: Array<{ id: number; naam: string }>
}

// === State ===

const query = ref('')
const results = ref<ErfgoedobjectResult[]>([])
const loading = ref(false)
const errorMsg = ref<string | null>(null)

// The currently-selected object's metadata. ``null`` means either no
// selection (form is empty) or selection-is-set-but-metadata-not-loaded
// (we're mid-fetch on reload). Distinguish via ``loadingSelected``.
const selected = ref<ErfgoedobjectResult | null>(null)
const loadingSelected = ref(false)

// Keyboard-nav cursor through results. -1 = no cursor, 0..N-1 = on a
// result. Reset on every search.
const cursor = ref(-1)

// Debounce handle. setTimeout id; cleared on every keystroke.
let debounceTimer: ReturnType<typeof setTimeout> | null = null

// Result cap. The API itself returns a manageable first page; this
// extra cap is defensive for the rare case it returns a flood.
const MAX_RESULTS = 20

// Min query length before we fire a request. Shorter queries match
// too much; the API is likely happier too.
const MIN_QUERY_LEN = 3

// === Computed ===

const hasSelection = computed(() => {
  const v = control.value.data
  return typeof v === 'string' && v.length > 0
})

const showResults = computed(() =>
  query.value.length >= MIN_QUERY_LEN && (loading.value || results.value.length > 0 || errorMsg.value)
)

// === Initial selection — if the form pre-fills with a URI, fetch
// its metadata so we can render the rich card. ===

onMounted(async () => {
  const initial = control.value.data
  if (typeof initial === 'string' && initial.length > 0) {
    await loadSelectedFromUri(initial)
  }
})

// Watch external data changes (e.g. revision pre-fill happens after
// mount, or the user picked a different URI somewhere). We avoid
// re-fetching when the change came from our own click handler — see
// the suppress flag below.
let suppressDataWatcher = false
watch(() => control.value.data, async (uri) => {
  if (suppressDataWatcher) return
  if (typeof uri === 'string' && uri.length > 0) {
    if (selected.value && selected.value.uri === uri) return
    await loadSelectedFromUri(uri)
  } else {
    selected.value = null
  }
})

async function loadSelectedFromUri(uri: string) {
  const m = uri.match(ERFGOED_URI_RE)
  if (!m) {
    // Doesn't match the inventaris IRI shape. Render a minimal fallback
    // card with just the URI; don't try to fetch.
    selected.value = { id: 0, naam: '', uri }
    return
  }
  // Fetch the canonical object by id. The inventaris API exposes
  // /erfgoedobjecten/<id> as the per-object endpoint with the same
  // response shape as one search-result entry.
  loadingSelected.value = true
  try {
    const url = `https://inventaris.onroerenderfgoed.be/erfgoedobjecten/${m[1]}`
    const resp = await fetch(url, { headers: { Accept: 'application/json' } })
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`)
    selected.value = await resp.json()
  } catch {
    // Network or shape failure. Don't blow up the form — show what we
    // have (the URI) and let the user re-pick if they want to.
    selected.value = { id: Number(m[1]), naam: '', uri }
  } finally {
    loadingSelected.value = false
  }
}

// === Search ===

function onQueryInput() {
  if (debounceTimer) clearTimeout(debounceTimer)
  errorMsg.value = null
  cursor.value = -1

  if (query.value.length < MIN_QUERY_LEN) {
    // Clear results immediately on too-short query — no point in
    // showing stale results from a prior longer query.
    results.value = []
    loading.value = false
    return
  }

  // Show the loading indicator immediately so the user sees the
  // search is in flight, even before the debounce fires. The actual
  // request still waits for the debounce window to close.
  loading.value = true
  debounceTimer = setTimeout(runSearch, 300)
}

async function runSearch() {
  const q = query.value.trim()
  if (q.length < MIN_QUERY_LEN) {
    loading.value = false
    return
  }
  try {
    const url = `https://inventaris.onroerenderfgoed.be/erfgoedobjecten?tekst=${encodeURIComponent(q)}`
    const resp = await fetch(url, { headers: { Accept: 'application/json' } })
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`)
    const data = await resp.json()
    // The API may return either a bare array or a paginated wrapper.
    // Defensively unwrap; cap to MAX_RESULTS for UI sanity.
    const arr: ErfgoedobjectResult[] = Array.isArray(data)
      ? data
      : Array.isArray((data as any)?.results)
        ? (data as any).results
        : []
    results.value = arr.slice(0, MAX_RESULTS)
  } catch (e: any) {
    errorMsg.value = `Zoekopdracht mislukt: ${e?.message ?? String(e)}`
    results.value = []
  } finally {
    loading.value = false
  }
}

function onKeydown(e: KeyboardEvent) {
  if (!showResults.value) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    cursor.value = Math.min(cursor.value + 1, results.value.length - 1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    cursor.value = Math.max(cursor.value - 1, -1)
  } else if (e.key === 'Enter') {
    if (cursor.value >= 0 && cursor.value < results.value.length) {
      e.preventDefault()
      pick(results.value[cursor.value])
    }
  } else if (e.key === 'Escape') {
    query.value = ''
    results.value = []
    cursor.value = -1
  }
}

function pick(item: ErfgoedobjectResult) {
  // Suppress our own watcher so the resulting data-change doesn't
  // re-trigger loadSelectedFromUri on the URI we just picked (we
  // already have the full result object in hand).
  suppressDataWatcher = true
  selected.value = item
  handleChange(control.value.path, item.uri)
  query.value = ''
  results.value = []
  cursor.value = -1
  setTimeout(() => { suppressDataWatcher = false }, 0)
}

function clearSelection() {
  suppressDataWatcher = true
  selected.value = null
  handleChange(control.value.path, '')
  setTimeout(() => { suppressDataWatcher = false }, 0)
}

// Truncate descriptions for compact result rows. The API returns
// 200-500 char descriptions; 140 is roughly two lines at our font.
function truncate(s: string | undefined, n = 140): string {
  if (!s) return ''
  return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + '…'
}

// The inventaris API returns ``primaire_foto`` URLs of the shape
//   https://id.erfgoed.net/afbeeldingen/<id>
// which 302-redirect to a JSON metadata endpoint on beeldbank — not
// the image bytes. We have to rewrite the URL client-side to point
// at the actual image: ``…/images/<id>/content/small``.
//
// We use the ``small`` variant in both the result-list thumbnails
// (72×72) and the selected card (also 72×72 in our layout); the
// shared variant keeps cache hit rates up. Other variants the
// beeldbank serves: ``medium``, ``full``. Bump the variant if the
// layout grows — at the small thumbnail size the difference isn't
// worth a separate request.
//
// Defensive on input: if the URL is already a direct image URL
// (contains ``/content/``) or doesn't match the afbeeldingen shape,
// pass it through unchanged. Lets the API evolve without us having
// to chase URL shapes.
const AFBEELDING_RE = /^https:\/\/id\.erfgoed\.net\/afbeeldingen\/(\d+)\/?$/

function photoUrl(raw: string | undefined): string | undefined {
  if (!raw) return undefined
  if (raw.includes('/content/')) return raw
  const m = raw.match(AFBEELDING_RE)
  if (!m) return raw
  return `https://beeldbank.onroerenderfgoed.be/images/${m[1]}/content/small`
}
</script>

<template>
  <div class="erfgoed-search">
    <!-- Selected state: rich card with photo/name/gemeente/URI + change button -->
    <div v-if="hasSelection && !loadingSelected" class="erfgoed-selected">
      <div v-if="selected?.primaire_foto" class="erfgoed-thumb">
        <img
          :src="photoUrl(selected.primaire_foto)"
          :alt="selected.naam || 'erfgoedobject'"
          referrerpolicy="no-referrer"
          crossorigin="anonymous"
        />
      </div>
      <div class="erfgoed-selected-body">
        <div class="row gap-2" style="align-items: baseline; flex-wrap: wrap;">
          <strong v-if="selected?.naam" style="font-size: 15px;">{{ selected.naam }}</strong>
          <span v-else class="faint">(geen naam beschikbaar)</span>
          <span v-if="selected?.gemeente_samenvatting" class="badge mute">
            {{ selected.gemeente_samenvatting }}
          </span>
          <span v-if="selected?.omvang?.naam" class="badge mute">{{ selected.omvang.naam }}</span>
        </div>
        <div v-if="selected?.locatie_samenvatting" class="faint" style="font-size: 12px; margin-top: 2px;">
          {{ selected.locatie_samenvatting }}
        </div>
        <div v-if="selected?.korte_beschrijving" style="font-size: 12px; margin-top: 4px; line-height: 1.45;">
          {{ truncate(selected.korte_beschrijving, 220) }}
        </div>
        <div class="mono faint" style="font-size: 10px; margin-top: 6px; word-break: break-all;">
          {{ control.data }}
        </div>
      </div>
      <div>
        <button type="button" class="subtle compact" @click="clearSelection">Wijzig</button>
      </div>
    </div>

    <!-- Loading-on-mount placeholder. Brief; metadata fetch is one round-trip. -->
    <div v-else-if="loadingSelected" class="erfgoed-selected">
      <div class="erfgoed-selected-body">
        <span class="spinner" style="margin-right: var(--gap-2);"></span>
        <span class="faint">Erfgoedobject ophalen…</span>
        <div class="mono faint" style="font-size: 10px; margin-top: 6px; word-break: break-all;">
          {{ control.data }}
        </div>
      </div>
    </div>

    <!-- Search state: input + result list. Shown when no selection.  -->
    <div v-else>
      <input
        type="search"
        v-model="query"
        @input="onQueryInput"
        @keydown="onKeydown"
        placeholder="Zoek een erfgoedobject (min. 3 letters)…"
        autocomplete="off"
        spellcheck="false"
      />

      <div v-if="showResults" class="erfgoed-results">
        <div v-if="loading" class="row gap-2" style="padding: var(--gap-3); align-items: center;">
          <span class="spinner"></span>
          <span class="faint" style="font-size: 13px;">Zoeken…</span>
        </div>
        <div v-else-if="errorMsg" class="banner error" style="margin: 0;">{{ errorMsg }}</div>
        <div v-else-if="results.length === 0" class="muted" style="padding: var(--gap-3);">
          Geen resultaten voor "{{ query }}".
        </div>
        <ul v-else style="list-style: none; margin: 0; padding: 0; max-height: 420px; overflow-y: auto;">
          <li
            v-for="(item, i) in results" :key="item.id"
            class="erfgoed-result"
            :class="{ 'erfgoed-result--cursor': i === cursor }"
            @click="pick(item)"
            @mouseenter="cursor = i"
          >
            <div v-if="item.primaire_foto" class="erfgoed-thumb">
              <img
                :src="photoUrl(item.primaire_foto)"
                :alt="item.naam"
                loading="lazy"
                referrerpolicy="no-referrer"
                crossorigin="anonymous"
              />
            </div>
            <div class="erfgoed-result-body">
              <div class="row gap-2" style="align-items: baseline; flex-wrap: wrap;">
                <strong style="font-size: 14px;">{{ item.naam }}</strong>
                <span v-if="item.gemeente_samenvatting" class="badge mute">
                  {{ item.gemeente_samenvatting }}
                </span>
                <span v-if="item.omvang?.naam" class="badge mute">{{ item.omvang.naam }}</span>
              </div>
              <div v-if="item.locatie_samenvatting" class="faint" style="font-size: 11px; margin-top: 2px;">
                {{ item.locatie_samenvatting }}
              </div>
              <div v-if="item.korte_beschrijving" style="font-size: 12px; margin-top: 4px; color: var(--ink-soft); line-height: 1.45;">
                {{ truncate(item.korte_beschrijving, 140) }}
              </div>
            </div>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>

<style scoped>
.erfgoed-search {
  margin-bottom: var(--gap-3);
}

.erfgoed-selected,
.erfgoed-result {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: var(--gap-3);
  padding: var(--gap-3);
  border: 1px solid var(--line);
  border-radius: 2px;
  background: var(--surface);
  align-items: start;
}

.erfgoed-result {
  grid-template-columns: auto 1fr;
  cursor: pointer;
  border-radius: 0;
  border-bottom: none;
  border-top: 1px solid var(--line);
  transition: background 100ms ease;
}
.erfgoed-result:first-child { border-top: none; }
.erfgoed-result:hover,
.erfgoed-result--cursor {
  background: var(--plum-soft);
}

.erfgoed-results {
  margin-top: var(--gap-2);
  border: 1px solid var(--line);
  border-radius: 2px;
  background: var(--surface);
}

.erfgoed-thumb {
  width: 72px;
  height: 72px;
  flex-shrink: 0;
  overflow: hidden;
  border-radius: 2px;
  background: var(--bg-deep);
}
.erfgoed-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.erfgoed-result-body,
.erfgoed-selected-body {
  min-width: 0;  /* allow text truncation in grid */
}
</style>
