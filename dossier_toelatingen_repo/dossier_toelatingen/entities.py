"""
Entity models for the toelatingen beschermd erfgoed workflow.

These are the Pydantic models that validate entity content.
Generated from the JSON Schema definitions in the workflow template.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from dossier_engine.file_refs import FileId


# --- Geographic contour (GeoJSON MultiPolygon with embedded CRS) ---
#
# Aanvragen always carry a geographic component: the area on the map
# the request relates to. We store it as GeoJSON-shaped JSON so it
# composes naturally with PostGIS, mapping libraries, and downstream
# tooling (PROV-JSON exports preserve it as-is).
#
# The default projection is EPSG:31370 (Belgian Lambert 72) — the
# coordinate system used across Vlaamse government cartography. The
# CRS is embedded in the geometry per the GeoJSON 2008 convention
# (RFC 7946 deprecated it in favour of WGS84-only, but real-world
# Belgian data still flows in Lambert 72 and we'd rather record the
# projection explicitly than convert at the boundary and lose precision).

class ContourCrsProperties(BaseModel):
    name: str = "urn:ogc:def:crs:EPSG::31370"


class ContourCrs(BaseModel):
    type: str = "name"
    properties: ContourCrsProperties = Field(default_factory=ContourCrsProperties)


class Contour(BaseModel):
    """GeoJSON-shaped MultiPolygon with embedded CRS metadata.

    Coordinates are 4-deep: ``[polygon_index][ring_index][point_index][x_or_y]``.
    A polygon's first ring is its outer boundary; subsequent rings are
    holes. Each ring must close — first point equals last point.
    """
    # JSON Schema extension consumed by the generic UI: the custom
    # JSONForms tester matches on this format string and dispatches
    # the OpenLayers map renderer for the *entire* Contour object,
    # producing `{type, crs, coordinates}` together. Putting the format
    # on the wrapping object (rather than just `coordinates`) lets the
    # renderer manage all three fields atomically — drawing a polygon
    # produces the full GeoJSON, not just the array.
    model_config = {
        "json_schema_extra": {"format": "geojson-multipolygon"},
    }

    type: str = Field(default="MultiPolygon", description="Always 'MultiPolygon'.")
    crs: ContourCrs = Field(default_factory=ContourCrs)
    coordinates: list[list[list[list[float]]]] = Field(
        ...,
        description="GeoJSON MultiPolygon coordinates: array of polygons, each "
                    "an array of linear rings, each an array of [x, y] points.",
    )

    @field_validator("type")
    @classmethod
    def _type_must_be_multipolygon(cls, v: str) -> str:
        # Pydantic would let any string through; we require literally
        # "MultiPolygon" so generators downstream (PostGIS, OL parsing)
        # don't have to special-case heterogeneous geometry types in
        # this slot.
        if v != "MultiPolygon":
            raise ValueError(f"Contour.type must be 'MultiPolygon', got '{v}'")
        return v

    @field_validator("coordinates")
    @classmethod
    def _validate_coordinate_shape(
        cls,
        v: list[list[list[list[float]]]],
    ) -> list[list[list[list[float]]]]:
        # We rely on Pydantic's structural typing to enforce the four
        # levels of nesting and the float leaves. What it can't enforce
        # is the GeoJSON-specific invariants:
        #   1. Each ring has at least 4 points (3 distinct + closing copy)
        #   2. Each ring closes (first point == last point)
        #   3. Each point has exactly 2 coordinates (we use 2D, no z)
        # We check these here so the frontend doesn't submit a half-
        # drawn polygon and trigger an opaque database error later.
        if not v:
            raise ValueError("MultiPolygon must contain at least one polygon")
        for pi, polygon in enumerate(v):
            if not polygon:
                raise ValueError(f"Polygon {pi} has no rings")
            for ri, ring in enumerate(polygon):
                if len(ring) < 4:
                    raise ValueError(
                        f"Polygon {pi}, ring {ri} has {len(ring)} points; "
                        f"a closed ring needs at least 4 (3 distinct + closure)"
                    )
                for pti, point in enumerate(ring):
                    if len(point) != 2:
                        raise ValueError(
                            f"Polygon {pi}, ring {ri}, point {pti} has "
                            f"{len(point)} coordinates; expected exactly 2 (x, y)"
                        )
                if ring[0] != ring[-1]:
                    raise ValueError(
                        f"Polygon {pi}, ring {ri} is not closed: "
                        f"first point {ring[0]} != last point {ring[-1]}"
                    )
        return v


class Locatie(BaseModel):
    """Geographic location of the request. Holds the contour as a
    GeoJSON-shaped MultiPolygon. Modelled as a wrapping object rather
    than putting the contour directly on Aanvraag because future
    locatie attributes (address, parcel reference, gemeente lookup
    cache) compose more cleanly here than as siblings of bijlagen,
    aanvrager, etc."""
    contour: Contour


# --- Bijlage (file attachment) ---

class Bijlage(BaseModel):
    # `file_id` is a FileId — GET responses include `file_download_url` next to it.
    file_id: FileId
    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0


# --- Aanvraag ---

class AanvragerKBO(BaseModel):
    kbo: str


class AanvragerRRN(BaseModel):
    rrn: str


# Union type: aanvrager must have either kbo or rrn
# We model this with both optional + a validator
class Aanvrager(BaseModel):
    kbo: Optional[str] = None
    rrn: Optional[str] = None

    def model_post_init(self, __context) -> None:
        if not self.kbo and not self.rrn:
            raise ValueError("Aanvrager must have either 'kbo' or 'rrn'")
        if self.kbo and self.rrn:
            raise ValueError("Aanvrager must have either 'kbo' or 'rrn', not both")


class Aanvraag(BaseModel):
    onderwerp: str
    handeling: str
    aanvrager: Aanvrager
    gemeente: str
    # ``object`` is the URI of the protected heritage object the
    # request concerns. The ``format`` annotation drives the generic
    # UI's custom-renderer dispatch: a search widget that calls
    # the inventaris API and lets the user pick from real results
    # rather than typing the IRI by hand.
    object: str = Field(
        ...,
        description="URI van het beschermd erfgoed object",
        json_schema_extra={"format": "uri-erfgoedobject"},
    )
    bijlagen: list[Bijlage] = []
    # Geographic component. Optional because legacy data may not have
    # it; new submissions through the generic UI will carry it via the
    # OpenLayers map widget. Ground-truth shape: GeoJSON MultiPolygon
    # in EPSG:31370 (Lambert 72).
    locatie: Optional[Locatie] = None


class AanvraagV2(Aanvraag):
    """V2 adds optional classificatie/urgentie fields. Additive only —
    V1 content validates as V2 with the new fields left as None."""
    classificatie: Optional[str] = None
    urgentie: Optional[str] = None


# --- Beslissing ---

class BeslissingUitkomst(str, Enum):
    goedgekeurd = "goedgekeurd"
    afgekeurd = "afgekeurd"
    onvolledig = "onvolledig"


class Beslissing(BaseModel):
    beslissing: BeslissingUitkomst
    datum: str  # datetime string
    object: str  # URI
    # `brief` is a FileId — the signed decision letter PDF.
    # GET responses will include `brief_download_url` next to it.
    brief: FileId


# --- Handtekening ---

class Handtekening(BaseModel):
    getekend: bool


# --- Verantwoordelijke Organisatie ---
# Schema is just a URI string, but we wrap it for consistency

class VerantwoordelijkeOrganisatie(BaseModel):
    uri: str


# --- Behandelaar ---
# Schema is just a URI string, cardinality: multiple

class Behandelaar(BaseModel):
    uri: str


# --- System Fields ---

class SystemFields(BaseModel):
    datum: str  # datetime string
    aanmaker: str  # URI


