/**
 * JSONForms renderer registry entry for the GeoJSON-MultiPolygon
 * map widget. Lives in a separate module from the Vue SFC because
 * importing an SFC's default export inside its own `<script>` tag
 * creates a Vite circular-import that doesn't resolve cleanly.
 *
 * Tester rank is 10 — higher than vanilla's defaults (max ~3) so we
 * win unconditionally for any schema with
 * ``format: "geojson-multipolygon"``. Other custom renderers can
 * use rank 11+ to override us.
 */

import { rankWith, type JsonFormsRendererRegistryEntry } from '@jsonforms/core'
import MapContourRenderer from './MapContourRenderer.vue'

export const mapContourTester = rankWith(10, (_uischema, schema) => {
  if (!schema || typeof schema !== 'object') return false
  return (schema as any).format === 'geojson-multipolygon'
})

export const mapContourRendererEntry: JsonFormsRendererRegistryEntry = {
  tester: mapContourTester,
  renderer: MapContourRenderer as any,
}
