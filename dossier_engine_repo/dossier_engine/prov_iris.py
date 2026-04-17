"""
IRI generation for PROV-JSON compliance.

Centralises the construction of W3C PROV-compliant qualified names
(QNames) and full IRIs for entities, activities, and agents. Used
by the PROV-JSON export route and the worker's cross-dossier URI
generation.

Internal format (`type/entity_id@version_id`) stays unchanged in
the database and the engine. This module only translates at the
PROV rendering boundary.

IRI structure (path segments match API routes for resolvability):

    Base namespace (per-dossier):
        https://id.erfgoed.net/dossiers/{dossier_id}/

    Entity (versioned):
        dossier:entities/{type}/{entity_id}/{version_id}
        → https://id.erfgoed.net/dossiers/{did}/entities/oe:aanvraag/{eid}/{vid}

    Activity:
        dossier:activities/{activity_id}
        → https://id.erfgoed.net/dossiers/{did}/activities/{aid}

    Agent:
        dossier:agents/{agent_id}
        → https://id.erfgoed.net/dossiers/{did}/agents/{agent_id}

    Cross-dossier (full IRI, no prefix):
        https://id.erfgoed.net/dossiers/{other_did}/activities/{aid}

Namespace prefixes:
    prov:    http://www.w3.org/ns/prov#
    xsd:     http://www.w3.org/2001/XMLSchema#
    oe:      https://id.erfgoed.net/vocab/ontology#
    dossier: https://id.erfgoed.net/dossiers/{dossier_id}/
"""

from __future__ import annotations

from uuid import UUID

# Base URI templates
DOSSIER_BASE = "https://id.erfgoed.net/dossiers/{dossier_id}/"
OE_NS = "https://id.erfgoed.net/vocab/ontology#"


def prov_prefixes(dossier_id: UUID | str) -> dict:
    """Standard PROV-JSON prefix block for a dossier."""
    return {
        "prov": "http://www.w3.org/ns/prov#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "oe": OE_NS,
        "dossier": DOSSIER_BASE.format(dossier_id=dossier_id),
    }


def _strip_ns(entity_type: str) -> str:
    """Strip the namespace prefix from an entity type.

    'oe:aanvraag' → 'aanvraag'
    'system:task' → 'task'
    'external'    → 'external'
    """
    if ":" in entity_type:
        return entity_type.split(":", 1)[1]
    return entity_type


def _type_ns(entity_type: str) -> str:
    """Return the namespace prefix for a type.

    'oe:aanvraag'   → 'oe'
    'system:task'    → 'system'
    'external'       → 'oe'  (default)
    """
    if ":" in entity_type:
        return entity_type.split(":", 1)[0]
    return "oe"


def entity_qname(entity_type: str, entity_id: UUID | str, version_id: UUID | str) -> str:
    """Build a PROV-JSON qualified entity key.

    Path segments match the API route structure so the expanded IRI
    is resolvable when served at the canonical base URL.

    Example:
        entity_qname("oe:aanvraag", eid, vid)
        → "dossier:entities/oe:aanvraag/{eid}/{vid}"
    """
    return f"dossier:entities/{entity_type}/{entity_id}/{version_id}"


def entity_full_iri(dossier_id: UUID | str, entity_type: str, entity_id: UUID | str, version_id: UUID | str) -> str:
    """Build a full (non-prefixed) entity IRI."""
    base = DOSSIER_BASE.format(dossier_id=dossier_id)
    return f"{base}entities/{entity_type}/{entity_id}/{version_id}"


def activity_qname(activity_id: UUID | str) -> str:
    """Build a PROV-JSON qualified activity key.

    Example: "dossier:activities/{aid}"
    """
    return f"dossier:activities/{activity_id}"


def activity_full_iri(dossier_id: UUID | str, activity_id: UUID | str) -> str:
    """Build a full (non-prefixed) activity IRI.

    Used for cross-dossier informed_by references.
    """
    base = DOSSIER_BASE.format(dossier_id=dossier_id)
    return f"{base}activities/{activity_id}"


def agent_qname(agent_id: str) -> str:
    """Build a PROV-JSON qualified agent key.

    Example: "dossier:agents/{agent_id}"
    """
    return f"dossier:agents/{agent_id}"


def prov_type_value(entity_type: str) -> dict:
    """Build a prov:type value with proper QName.

    'oe:aanvraag' → {"$": "oe:aanvraag", "type": "xsd:QName"}
    'system:task'  → {"$": "oe:task", "type": "xsd:QName"}
    """
    # Normalise: system: types get oe: prefix for the ontology
    ns = _type_ns(entity_type)
    bare = _strip_ns(entity_type)
    if ns == "system":
        qname = f"oe:{bare}"
    else:
        qname = f"{ns}:{bare}"
    return {"$": qname, "type": "xsd:QName"}


def agent_type_value(agent_type: str) -> dict:
    """Build a prov:type value for an agent.

    'persoon'               → {"$": "oe:persoon", "type": "xsd:QName"}
    'natuurlijk_persoon'    → {"$": "oe:natuurlijk_persoon", "type": "xsd:QName"}
    'systeem'               → {"$": "oe:systeem", "type": "xsd:QName"}
    """
    if ":" in agent_type:
        qname = agent_type  # already prefixed
    else:
        qname = f"oe:{agent_type}"
    return {"$": qname, "type": "xsd:QName"}


# =====================================================================
# Domain relation ref expansion
# =====================================================================
#
# Domain relations store their endpoints as full IRIs so they're
# self-describing, resolvable, and consistent with external URIs.
# But the API accepts shorthand refs for convenience — callers
# shouldn't have to construct full IRIs when they already know the
# dossier context. This section translates between the two.
#
# Accepted shorthand formats:
#
#   oe:type/eid@vid                   → local entity in current dossier
#   dossier:did/oe:type/eid@vid       → entity in a different dossier
#   dossier:did                        → dossier itself
#   https://...                        → external URI (returned as-is)
#
# Expanded IRIs follow the same structure as the PROV rendering:
#
#   https://id.erfgoed.net/dossiers/{did}/entities/{type}/{eid}/{vid}
#   https://id.erfgoed.net/dossiers/{did}/
#   https://... (external, unchanged)

def expand_ref(ref: str, dossier_id: UUID | str) -> str:
    """Expand a shorthand domain-relation ref to a full IRI.

    If the ref is already a full IRI (starts with http:// or https://),
    it's returned unchanged. Otherwise the shorthand is parsed and
    expanded using the canonical IRI structure from this module.

    Examples::

        expand_ref("oe:aanvraag/e1@v1", "d1")
        → "https://id.erfgoed.net/dossiers/d1/entities/oe:aanvraag/e1/v1"

        expand_ref("dossier:d2/oe:aanvraag/e1@v1", "d1")
        → "https://id.erfgoed.net/dossiers/d2/entities/oe:aanvraag/e1/v1"

        expand_ref("dossier:d2", "d1")
        → "https://id.erfgoed.net/dossiers/d2/"

        expand_ref("https://id.erfgoed.net/erfgoedobjecten/10001", "d1")
        → "https://id.erfgoed.net/erfgoedobjecten/10001"
    """
    # Already a full IRI — pass through.
    if ref.startswith("https://") or ref.startswith("http://"):
        return ref

    # dossier: prefix — either a dossier ref or a cross-dossier entity.
    if ref.startswith("dossier:"):
        remainder = ref[len("dossier:"):]
        # dossier:did/oe:type/eid@vid → cross-dossier entity
        if "/" in remainder:
            did_str, entity_part = remainder.split("/", 1)
            return _expand_entity_ref(entity_part, did_str)
        # dossier:did → dossier IRI
        return DOSSIER_BASE.format(dossier_id=remainder)

    # No prefix → local entity in the current dossier.
    return _expand_entity_ref(ref, str(dossier_id))


def _expand_entity_ref(ref: str, dossier_id_str: str) -> str:
    """Expand an entity ref (type/eid@vid) to a full entity IRI
    within the given dossier. Falls back to returning the ref
    unchanged if it doesn't parse as a valid EntityRef."""
    # Import here to avoid circular dependency (EntityRef is in
    # engine/refs.py which doesn't depend on prov_iris).
    from .engine.refs import EntityRef

    parsed = EntityRef.parse(ref)
    if parsed:
        return entity_full_iri(
            dossier_id_str,
            parsed.type,
            parsed.entity_id,
            parsed.version_id,
        )
    # Doesn't parse — return as-is and let downstream validation
    # catch it if needed.
    return ref


def classify_ref(ref: str) -> str:
    """Classify a domain-relation endpoint reference.

    Returns one of:
    * ``"entity"`` — an entity within the platform (local or
      cross-dossier). The IRI contains ``/entities/``.
    * ``"dossier"`` — a dossier itself. The IRI matches the
      dossier base pattern without ``/entities/``.
    * ``"external_uri"`` — anything outside the platform.

    Works on both expanded IRIs and shorthand refs::

        classify_ref("https://id.erfgoed.net/dossiers/d1/entities/...")
        → "entity"

        classify_ref("oe:aanvraag/e1@v1")
        → "entity"

        classify_ref("dossier:d2")
        → "dossier"

        classify_ref("https://id.erfgoed.net/erfgoedobjecten/10001")
        → "external_uri"
    """
    # Shorthand forms.
    if ref.startswith("dossier:"):
        remainder = ref[len("dossier:"):]
        return "entity" if "/" in remainder else "dossier"

    # Full IRIs.
    _DOSSIER_PREFIX = "https://id.erfgoed.net/dossiers/"
    if ref.startswith(_DOSSIER_PREFIX):
        return "entity" if "/entities/" in ref else "dossier"

    # Not a platform IRI and not a shorthand → external.
    if ref.startswith("https://") or ref.startswith("http://"):
        return "external_uri"

    # No prefix, no scheme → local entity shorthand.
    return "entity"
