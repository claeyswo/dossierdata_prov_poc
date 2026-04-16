"""
Pydantic request/response models for the activity API.

Three layers:

1. **Item models** — the shape of individual entries inside a request:
   `UsedItem`, `GeneratedItem`, `RelationItem`. These appear inside
   `ActivityRequest.used`, `.generated`, `.relations` and inside the
   per-activity entries of a batch request.

2. **Request models** — `ActivityRequest` for single-activity calls,
   `BatchActivityRequest` (with `BatchActivityItem`) for batch calls.
   Single requests pull the activity type from the URL on typed
   endpoints; batch requests carry the type per-item.

3. **Response models** — `FullResponse` is the canonical activity
   response, composed of `ActivityResponse` (the activity itself),
   `UsedResponse`/`GeneratedResponse`/`RelationResponse` lists, and
   `DossierResponse` (post-execution dossier state). `DossierDetailResponse`
   is the GET /dossiers/{id} shape with `currentEntities` and the
   activity log appended.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class UsedItem(BaseModel):
    """Reference to an existing entity or external URI."""
    entity: str


class GeneratedItem(BaseModel):
    """New entity or new version of an existing entity.

    `content` is optional because the engine ignores it for external
    URIs in the generated block (externals get persisted as
    `type=external` rows with auto-generated `{"uri": ...}` content).
    Local entity references must supply content; the engine raises
    422 if they don't, so the validation is enforced one layer down."""
    entity: str
    content: Optional[dict[str, Any]] = None
    derivedFrom: Optional[str] = None


class RelationItem(BaseModel):
    """Generic activity→entity relation under a named type.

    Example for the `oe:neemtAkteVan` pattern — acknowledging newer
    versions of an entity the activity chose not to act on:

        {"entity": "oe:aanvraag/X@v3", "type": "oe:neemtAkteVan"}

    The `type` string is validated against the activity's YAML
    declaration of allowed relation types. Plugins register validators
    per type to enforce semantics (e.g. neemtAkteVan must cover every
    version between the declared used version and the current latest)."""
    entity: str
    type: str


class ActivityRequest(BaseModel):
    type: Optional[str] = None     # set from URL on typed endpoints
    workflow: Optional[str] = None  # only needed for first activity
    role: Optional[str] = None     # defaults to activity's default_role
    informed_by: Optional[str] = None  # local UUID or cross-dossier URI
    used: list[UsedItem] = []
    generated: list[GeneratedItem] = []
    relations: list[RelationItem] = []


class BatchActivityItem(BaseModel):
    """Single activity within a batch request."""
    activity_id: str               # client-generated UUID
    type: str
    role: Optional[str] = None
    informed_by: Optional[str] = None
    used: list[UsedItem] = []
    generated: list[GeneratedItem] = []
    relations: list[RelationItem] = []


class BatchActivityRequest(BaseModel):
    workflow: Optional[str] = None  # only needed if first activity creates dossier
    activities: list[BatchActivityItem]


class AssociatedWith(BaseModel):
    agent: str
    role: str
    name: str


class ActivityResponse(BaseModel):
    id: str
    type: str
    associatedWith: Optional[AssociatedWith] = None
    startedAtTime: Optional[str] = None
    endedAtTime: Optional[str] = None


class UsedResponse(BaseModel):
    entity: str
    type: str = "unknown"


class GeneratedResponse(BaseModel):
    entity: str
    type: str
    content: Optional[dict[str, Any]] = None
    schemaVersion: Optional[str] = None


class DossierResponse(BaseModel):
    id: str
    workflow: str
    status: str
    allowedActivities: list[dict[str, str]] = []


class RelationResponse(BaseModel):
    entity: str
    type: str


class FullResponse(BaseModel):
    activity: ActivityResponse
    used: list[UsedResponse] = []
    generated: list[GeneratedResponse] = []
    relations: list[RelationResponse] = []
    dossier: DossierResponse


class DossierDetailResponse(BaseModel):
    id: str
    workflow: str
    status: str
    allowedActivities: list[dict[str, str]] = []
    currentEntities: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []
