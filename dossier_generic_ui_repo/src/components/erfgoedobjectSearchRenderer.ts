/**
 * JSONForms renderer registry entry for the erfgoedobject search
 * widget. Same pattern as mapContourRenderer.ts: SFC + entry split
 * across two files because Vite chokes on an SFC importing its own
 * default export inside its own ``<script>`` tag.
 *
 * Tester rank 10 — same shelf as the map renderer, no contention
 * because they match on different format strings.
 *
 * == Why ``formatIs`` rather than a hand-rolled ``schema.format`` check ==
 *
 * JSONForms hands the tester the whole *root* schema, not the leaf
 * field's schema, when the field is a top-level property. The tester
 * has to drill down via ``uischema.scope`` (e.g. ``#/properties/object``)
 * to find the field's own schema and inspect its ``format``. The
 * platform-provided ``formatIs`` helper does exactly this — and also
 * checks that the type is ``string``, which is the right gate for a
 * URI-string field. Hand-rolling the same logic would be a re-
 * implementation of ``schemaMatches`` + ``resolveSchema`` for no gain.
 *
 * The map renderer (mapContourRenderer.ts) gets away with a naive
 * ``schema.format`` check because its format annotation lives on the
 * Contour *object* schema, and JSONForms hands the tester that
 * object schema directly when walking into the nested model. Leaf
 * string fields at the root level need the scope-drilling that
 * ``formatIs`` provides.
 */

import { formatIs, rankWith, type JsonFormsRendererRegistryEntry } from '@jsonforms/core'
import ErfgoedobjectSearchRenderer from './ErfgoedobjectSearchRenderer.vue'

export const erfgoedobjectSearchTester = rankWith(
  10,
  formatIs('uri-erfgoedobject'),
)

export const erfgoedobjectSearchEntry: JsonFormsRendererRegistryEntry = {
  tester: erfgoedobjectSearchTester,
  renderer: ErfgoedobjectSearchRenderer as any,
}
