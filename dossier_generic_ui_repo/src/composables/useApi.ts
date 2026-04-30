// API client for the dossier engine and file service.
//
// All requests carry the X-POC-User header (the platform's auth
// shortcut for the demo). In production this would be replaced by a
// real auth flow — bearer tokens, session cookies, whatever — but
// for the generic-UI proof of concept the X-POC-User header is the
// minimum viable handshake.
//
// Two prefixes:
//   /api/*       → engine (proxied to :8000 by Vite)
//   /file-svc/*  → file service (proxied to :8001 by Vite)
//
// We deliberately don't wrap in a fancy fetch library — fetch is
// fine for this scale. The wrapper exists to (1) inject the user
// header consistently, (2) parse JSON / handle errors uniformly,
// (3) surface a typed ActivityError for 422s so forms can map
// engine validation errors back to specific fields.

import { useUserStore } from '../stores/user'

export interface ApiError {
  status: number
  detail: any  // engine error bodies vary — sometimes string, sometimes structured
  raw: Response
}

async function request<T>(
  method: string, path: string, body?: any, init?: RequestInit,
): Promise<T> {
  const userStore = useUserStore()
  const headers: Record<string, string> = {
    'X-POC-User': userStore.username,
    ...((init?.headers as Record<string, string>) || {}),
  }
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
  }

  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined
      ? undefined
      : body instanceof FormData ? body : JSON.stringify(body),
    ...init,
  })

  if (res.status === 204) return undefined as T

  let data: any = null
  const text = await res.text()
  if (text) {
    try { data = JSON.parse(text) } catch { data = text }
  }

  if (!res.ok) {
    // Engine returns 422 for content-validation, 409 for workflow-rule,
    // 403 for auth — the SPA wants to handle them differently, so we
    // surface the raw status. The detail is whatever the server sent
    // (string or structured object); UI components decide rendering.
    const err: ApiError = { status: res.status, detail: data, raw: res }
    throw err
  }
  return data as T
}

export const api = {
  get:    <T = any>(p: string) => request<T>('GET', p),
  post:   <T = any>(p: string, body?: any) => request<T>('POST', p, body),
  put:    <T = any>(p: string, body?: any) => request<T>('PUT', p, body),
  delete: <T = any>(p: string) => request<T>('DELETE', p),
}

// ===== Domain calls =====

export interface ActivityNamesEntry {
  name: string
  label: string
  can_create_dossier: boolean
}

export interface UsedSlot {
  type: string
  external: boolean
  required: boolean
  description: string | null
  auto_resolve: string | null
}

export interface GeneratedEntry {
  type: string
  client_supplied: boolean
  /** "single" or "multiple". The frontend uses this to decide how to
   *  mint the entity_id at submit time:
   *    - single + existing instance → reuse entity_id, derive from latest
   *    - single + no instance → fresh entity_id (creation)
   *    - multiple → user picks revise-existing or create-new */
  cardinality: 'single' | 'multiple'
  schema: any | null  // JSON Schema; null when no Pydantic model registered
  description: string | null
}

export interface FormSchema {
  name: string
  label: string
  description: string | null
  client_callable: boolean
  allowed_roles: string[]
  default_role: string | null
  can_create_dossier: boolean
  used: UsedSlot[]
  generates: GeneratedEntry[]
  deadlines: { not_before: any; not_after: any }
  activity_names: ActivityNamesEntry[]
  reference_lists: string[]
}

export interface AllowedActivity {
  type: string
  label: string
  not_before?: string
  not_after?: string
  exempted_by_exception?: string  // version_id of the active exception
}

export interface DossierDetail {
  id: string
  workflow: string
  status: string
  allowedActivities: AllowedActivity[]
  currentEntities: any[]
  activities: any[]
  domainRelations: any[]
}

// === Workflow-meta endpoints ===

export interface WorkflowEntry {
  name: string
  label: string
  description: string | null
  version: string | null
  /** Path to the plugin's search endpoint (e.g. ``/toelatingen/search``).
   *  ``null`` if the plugin doesn't expose a search route — frontends
   *  should render the workflow home without a search box in that case
   *  and fall back to a "no search available" empty state. */
  search_path: string | null
  /** Activities flagged ``can_create_dossier: true`` in the workflow
   *  YAML, with their labels. Used by the new-dossier picker so the
   *  frontend doesn't need a second round-trip per workflow. Filtered
   *  to client-callable activities (system activities are excluded). */
  creation_activities: Array<{
    name: string
    label: string
    description: string | null
  }>
}

/** Generic search-result shape. Plugin search endpoints return their
 *  own field set; we type the bits the generic frontend cares about
 *  and keep the rest open. ``onderwerp`` is a toelatingen convention
 *  but we treat it as the canonical "human-readable label" field for
 *  the result list. Plugins with different conventions will see their
 *  results render with whatever fields ``onderwerp`` falls back to.
 *
 *  When ES is down or unavailable, the search endpoint typically
 *  returns ``{hits: [], total: 0, reason: "..."}`` — surfaced via the
 *  optional ``reason`` so the frontend can render an explanatory
 *  empty state instead of "no matches." */
export interface SearchResult {
  hits: Array<{
    dossier_id?: string
    onderwerp?: string
    workflow?: string
    [k: string]: any
  }>
  total: number
  reason?: string
}

export const engineApi = {
  /** List every workflow plugin loaded in the engine. Used by the
   *  HomeView's workflow picker. */
  workflows: () => api.get<WorkflowEntry[]>('/api/workflows'),

  /** Hit a workflow's plugin-declared search endpoint. The path comes
   *  from ``WorkflowEntry.search_path`` so the frontend follows
   *  whatever route the plugin chose. Query params are forwarded
   *  verbatim — different plugins accept different filters; the
   *  caller is responsible for sending the right ones (or none, in
   *  which case the search returns recent dossiers in ACL scope).
   *  Returns ``null``-on-no-search if ``search_path`` is null. */
  searchWorkflow: (search_path: string, params: Record<string, string | number> = {}) => {
    const query = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== '' && v !== undefined && v !== null) query.set(k, String(v))
    }
    const qs = query.toString()
    return api.get<SearchResult>(`/api${search_path}${qs ? '?' + qs : ''}`)
  },

  /** Versions of one logical entity within a dossier, oldest-first
   *  (the engine returns them in insertion order, which is also
   *  creation order). Used by the entity-detail modal to render
   *  version history alongside the current content. */
  entityVersions: (dossierId: string, entityType: string, entityId: string) =>
    api.get<{
      dossier_id: string
      entity_type: string
      entity_id: string
      versions: Array<{
        versionId: string
        content: any
        generatedBy: string | null
        derivedFrom: string | null
        invalidatedAt?: string | null
        createdAt?: string
      }>
    }>(
      `/api/dossiers/${encodeURIComponent(dossierId)}` +
      `/entities/${encodeURIComponent(entityType)}/${encodeURIComponent(entityId)}`
    ),

  formSchema: (workflow: string, activityName: string) =>
    api.get<FormSchema>(`/api/${workflow}/activities/${encodeURIComponent(activityName)}/form-schema`),

  getDossier: (id: string) =>
    api.get<DossierDetail>(`/api/dossiers/${id}`),

  /** Execute an activity (creating-or-updating a dossier).
   *  PUT /{workflow}/dossiers/{dossierId}/activities/{activityId}/{activityType}
   *  Returns the FullResponse. The caller passes pre-minted UUIDs for
   *  dossier and activity (the platform uses client-minted IDs as the
   *  idempotency keys — replaying with the same activityId returns the
   *  original response without re-running side effects). */
  executeActivity: (
    workflow: string, dossierId: string, activityId: string, activityType: string,
    body: any,
  ) =>
    api.put(
      `/api/${workflow}/dossiers/${dossierId}/activities/${activityId}/${encodeURIComponent(activityType)}`,
      body,
    ),

  /** Versions of every logical entity of a given type within a
   *  dossier. Used by UsedSlotInput's internal-entity dropdown. The
   *  engine returns ``{dossier_id, entity_type, versions: [...]}``;
   *  callers need to dedupe by entity_id and pick the latest per
   *  logical entity for picker UI. */
  entitiesByType: (dossierId: string, type: string) =>
    api.get<{
      dossier_id: string
      entity_type: string
      versions: Array<{
        versionId: string
        entityId: string
        content: any
        createdAt?: string
        generatedBy?: string | null
        derivedFrom?: string | null
      }>
    }>(
      `/api/dossiers/${dossierId}/entities/${encodeURIComponent(type)}`
    ),

  /** Fetch a workflow's reference list. Used to populate enum-dropdowns
   *  in entity content forms when a JSON Schema string field has no
   *  enum but the plugin author has named a relevant reference list. */
  referenceList: (workflow: string, name: string) =>
    api.get<any[]>(`/api/${workflow}/reference/${encodeURIComponent(name)}`),
}
