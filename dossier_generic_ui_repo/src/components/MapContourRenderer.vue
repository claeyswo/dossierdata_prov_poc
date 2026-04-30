<script setup lang="ts">
/**
 * Custom JSONForms renderer for fields tagged
 * ``format: "geojson-multipolygon"`` in the JSON Schema.
 *
 * Renders an OpenLayers map letting the user draw, edit, or clear a
 * MultiPolygon. The component reads & writes the standard JSONForms
 * data prop ({type, crs, coordinates}) so it composes seamlessly into
 * the rest of the activity form.
 *
 * == Why an in-flight pre-projected layer ==
 * The data we read & write is in EPSG:31370 (Lambert 72) — the
 * coordinate system Vlaamse government cartography uses. The
 * background tiles (OSM) are in EPSG:3857 (Web Mercator). OpenLayers
 * doesn't auto-reproject vector data; we hold the user's drawing in
 * a feature layer whose source coordinates are in 31370, and the View
 * is in 3857, with the layer's transformation handled by OL's
 * projection registry. proj4 supplies the transformation parameters.
 *
 * == Why we re-create on every prop change rather than diff ==
 * The JSONForms data prop is reactive but not a reliable signal of
 * "the user changed something" — initial load, version pre-fill, and
 * user edits all flow through the same channel. To avoid feedback
 * loops (we update data → data prop changes → we re-sync → loop) we
 * track a per-update generation counter and only sync from data → map
 * when the change came from outside this component.
 */

import { ref, watch, onMounted, onBeforeUnmount, computed } from 'vue'
import { rendererProps, useJsonFormsControl } from '@jsonforms/vue'
import { rankWith, type JsonFormsRendererRegistryEntry } from '@jsonforms/core'

// OpenLayers — full bundle imports. We're already paying the bundle
// cost for the SPA so importing what we need from `ol` is fine.
import 'ol/ol.css'
import Map from 'ol/Map'
import View from 'ol/View'
import TileLayer from 'ol/layer/Tile'
import VectorLayer from 'ol/layer/Vector'
import VectorSource from 'ol/source/Vector'
import OSM from 'ol/source/OSM'
import Feature from 'ol/Feature'
import { Polygon, MultiPolygon } from 'ol/geom'
import { Draw, Modify, Snap } from 'ol/interaction'
import { Style, Fill, Stroke } from 'ol/style'
import { fromLonLat } from 'ol/proj'
import { register as registerProjections } from 'ol/proj/proj4'
import { get as getProjection } from 'ol/proj'
import proj4 from 'proj4'

// === Projection setup ===
//
// Belgian Lambert 72 (EPSG:31370) isn't built into OpenLayers; we
// register it with proj4 and OL pulls the transformation from there.
// This block runs once per module load (not per component) which is
// what we want — re-registration is idempotent but wasteful.
//
// proj4 defs string sourced from epsg.io/31370.
proj4.defs(
  'EPSG:31370',
  '+proj=lcc +lat_0=90 +lon_0=4.36748666666667 +lat_1=51.1666672333333 ' +
  '+lat_2=49.8333339 +x_0=150000.013 +y_0=5400088.438 +ellps=intl ' +
  '+towgs84=-106.869,52.2978,-103.724,0.3366,-0.457,1.8422,-1.2747 ' +
  '+units=m +no_defs +type=crs',
)
registerProjections(proj4)

const PROJ_LAMBERT72 = 'EPSG:31370'
const PROJ_WEB_MERCATOR = 'EPSG:3857'

// Centre default view on Belgium when no geometry is set yet.
// Brussels-ish in WGS84; OL converts via fromLonLat.
const BELGIUM_CENTRE_LONLAT = [4.5, 50.85]

// === Component ===

const props = defineProps({
  ...rendererProps<any>(),
})

const { control, handleChange } = useJsonFormsControl(props)

// Source-of-truth refs for OpenLayers objects. Held outside the
// reactive system because OL mutates them internally and Vue's
// reactivity would chase its tail otherwise.
const mapEl = ref<HTMLDivElement | null>(null)
let mapInstance: Map | null = null
let vectorSource: VectorSource | null = null
let drawInteraction: Draw | null = null
let modifyInteraction: Modify | null = null
let snapInteraction: Snap | null = null

// Generation counter to avoid the "data → map → data" feedback loop.
// Incremented locally before publishing data; the data-change watcher
// skips re-syncing the map if its read of the counter matches the
// previous one. (Plain "are we currently writing" boolean works fine
// here because Vue batches reactive updates synchronously.)
let suppressDataWatcher = false

// Are we in active drawing mode? Toggles the Draw interaction.
const drawing = ref(false)

const hasGeometry = computed(() => {
  const v = control.value.data
  return Array.isArray(v?.coordinates) && v.coordinates.length > 0
})

const featureCount = computed(() => {
  const v = control.value.data
  if (!Array.isArray(v?.coordinates)) return 0
  return v.coordinates.length
})

// === Style ===
//
// Translucent plum to match the Premies-style restyle (matches the
// rest of the app's accent colour). Stroke is darker plum for
// contrast against the green-grey OSM background.
const featureStyle = new Style({
  fill: new Fill({ color: 'rgba(120, 76, 138, 0.25)' }),
  stroke: new Stroke({ color: 'rgba(120, 76, 138, 0.95)', width: 2 }),
})

// === Initialisation ===

onMounted(() => {
  if (!mapEl.value) return

  vectorSource = new VectorSource()

  const vectorLayer = new VectorLayer({
    source: vectorSource,
    style: featureStyle,
  })

  // Verify the projection registered. If proj4 didn't pick it up for
  // any reason, fall back to web mercator and log — the map at least
  // renders something sensible rather than blowing up.
  const lambert = getProjection(PROJ_LAMBERT72)
  if (!lambert) {
    console.warn(
      '[MapContourRenderer] EPSG:31370 not registered with OpenLayers; ' +
      'coordinates will not transform correctly.'
    )
  }

  mapInstance = new Map({
    target: mapEl.value,
    layers: [
      new TileLayer({ source: new OSM() }),
      vectorLayer,
    ],
    view: new View({
      projection: PROJ_WEB_MERCATOR,
      center: fromLonLat(BELGIUM_CENTRE_LONLAT),
      zoom: 8,
    }),
  })

  // Wire interactions. We register Modify + Snap permanently (they
  // only act on existing features) and toggle Draw via the button.
  modifyInteraction = new Modify({ source: vectorSource })
  snapInteraction = new Snap({ source: vectorSource })
  mapInstance.addInteraction(modifyInteraction)
  mapInstance.addInteraction(snapInteraction)

  // Modify dispatches its 'modifyend' event after the user finishes
  // dragging; that's our cue to publish updated data upward.
  modifyInteraction.on('modifyend', publishFromMap)

  // Initial sync from data → map (handles pre-fill on revision).
  syncFromData()

  // Once features exist, re-fit the view to them so the user doesn't
  // start at Brussels-default when revising an existing geometry.
  fitToFeatures()
})

onBeforeUnmount(() => {
  if (mapInstance) {
    mapInstance.setTarget(undefined)
    mapInstance = null
  }
  vectorSource = null
  drawInteraction = null
  modifyInteraction = null
  snapInteraction = null
})

// === Data → map sync ===

watch(() => control.value.data, () => {
  if (suppressDataWatcher) return
  syncFromData()
}, { deep: true })

function syncFromData() {
  if (!vectorSource) return
  vectorSource.clear()

  const data = control.value.data
  if (!data || !Array.isArray(data.coordinates) || data.coordinates.length === 0) {
    return
  }

  // The data is in Lambert 72 ground coordinates. We construct the
  // geometry in 31370 then transform to the map's view projection.
  // OL's geometry.transform mutates in place, but we just constructed
  // it so the mutation is fine.
  try {
    const multi = new MultiPolygon(data.coordinates)
    multi.transform(PROJ_LAMBERT72, PROJ_WEB_MERCATOR)
    vectorSource.addFeature(new Feature(multi))
  } catch (e) {
    console.warn('[MapContourRenderer] could not parse coordinates:', e)
  }
}

function fitToFeatures() {
  if (!mapInstance || !vectorSource) return
  const extent = vectorSource.getExtent()
  // Empty source returns [Infinity, Infinity, -Infinity, -Infinity].
  if (!isFinite(extent[0])) return
  mapInstance.getView().fit(extent, {
    padding: [40, 40, 40, 40],
    maxZoom: 18,
    duration: 300,
  })
}

// === Map → data sync ===

function publishFromMap() {
  if (!vectorSource) return

  // Collect every polygon from every feature in the source. The user
  // may have drawn several disjoint polygons; each goes into the
  // MultiPolygon as a separate top-level entry.
  const polygons: number[][][][] = []

  vectorSource.getFeatures().forEach((feature) => {
    const geom = feature.getGeometry()
    if (!geom) return
    // Clone before transforming — the in-source geometry is in web
    // mercator and we mustn't mutate it (OL would re-render in 31370
    // coordinates, ruining the display).
    const cloned = geom.clone()
    cloned.transform(PROJ_WEB_MERCATOR, PROJ_LAMBERT72)

    if (cloned instanceof Polygon) {
      polygons.push(cloned.getCoordinates())
    } else if (cloned instanceof MultiPolygon) {
      for (const poly of cloned.getCoordinates()) {
        polygons.push(poly)
      }
    }
  })

  // Construct the GeoJSON-shaped value the backend Pydantic model
  // expects. CRS is fixed at Lambert 72; downstream tooling reads it
  // from here. Only publish if we actually have polygons — a publish
  // with empty coordinates would re-trigger required-field errors.
  if (polygons.length === 0) {
    suppressDataWatcher = true
    handleChange(control.value.path, undefined)
    setTimeout(() => { suppressDataWatcher = false }, 0)
    return
  }

  const value = {
    type: 'MultiPolygon',
    crs: {
      type: 'name',
      properties: { name: 'urn:ogc:def:crs:EPSG::31370' },
    },
    coordinates: polygons,
  }

  // Suppress our own watcher so the publish doesn't re-trigger
  // syncFromData (which would clear and re-add the same features,
  // losing the user's modify-in-progress state).
  suppressDataWatcher = true
  handleChange(control.value.path, value)
  // Reset on next tick so external changes still trigger sync.
  setTimeout(() => { suppressDataWatcher = false }, 0)
}

// === Draw mode controls ===

function startDrawing() {
  if (!mapInstance || !vectorSource) return
  if (drawInteraction) {
    mapInstance.removeInteraction(drawInteraction)
    drawInteraction = null
  }

  drawInteraction = new Draw({
    source: vectorSource,
    type: 'Polygon',
  })

  drawInteraction.on('drawend', () => {
    // The new feature is added to the source synchronously by OL
    // before this handler. Stop drawing and republish.
    stopDrawing()
    // Wait one tick so the feature is actually in the source.
    setTimeout(publishFromMap, 0)
  })

  mapInstance.addInteraction(drawInteraction)
  drawing.value = true
}

function stopDrawing() {
  if (drawInteraction && mapInstance) {
    mapInstance.removeInteraction(drawInteraction)
    drawInteraction = null
  }
  drawing.value = false
}

function clearAll() {
  if (!vectorSource) return
  vectorSource.clear()
  publishFromMap()
}
</script>

<template>
  <div class="map-contour-renderer">
    <div class="row gap-2" style="margin-bottom: var(--gap-2); align-items: center; flex-wrap: wrap;">
      <button
        type="button"
        :class="drawing ? '' : 'subtle'"
        class="compact"
        @click="drawing ? stopDrawing() : startDrawing()"
      >
        {{ drawing ? 'Tekenen stoppen' : 'Polygoon tekenen' }}
      </button>
      <button
        type="button"
        class="subtle compact"
        @click="clearAll"
        :disabled="!hasGeometry"
      >
        Alles wissen
      </button>
      <button
        type="button"
        class="subtle compact"
        @click="fitToFeatures"
        :disabled="!hasGeometry"
      >
        Inzoomen op selectie
      </button>
      <span class="faint" style="font-size: 12px; margin-left: auto;">
        <template v-if="hasGeometry">
          {{ featureCount }} polygon{{ featureCount === 1 ? '' : 'en' }} · Lambert 72 (EPSG:31370)
        </template>
        <template v-else>
          Klik <strong>Polygoon tekenen</strong> om een gebied af te bakenen
        </template>
      </span>
    </div>

    <!-- The map itself. Fixed height; full width of its container. -->
    <div ref="mapEl" class="map-canvas"></div>
  </div>
</template>

<style scoped>
.map-contour-renderer {
  margin-bottom: var(--gap-3);
}

.map-canvas {
  width: 100%;
  height: 360px;
  border: 1px solid var(--line);
  border-radius: 2px;
  background: var(--bg-deep);
}

/* OpenLayers ships some controls (zoom buttons, attribution) we want
   to keep readable against our muted-paper aesthetic. Light overrides
   only — leave their layout alone, just tweak chrome. */
:deep(.ol-zoom) {
  background: rgba(255, 255, 255, 0.85);
  border-radius: 2px;
}
:deep(.ol-zoom button) {
  background: white;
  border: 1px solid var(--line);
  color: var(--ink);
  font-family: var(--font-mono);
}
:deep(.ol-zoom button:hover) {
  background: var(--plum-soft);
  border-color: var(--plum);
}
:deep(.ol-attribution) {
  font-family: var(--font-ui);
  font-size: 10px;
}
</style>
