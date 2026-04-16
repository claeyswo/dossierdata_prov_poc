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
        https://data.vlaanderen.be/id/dossier/{dossier_id}/

    Entity (versioned):
        dossier:entities/{type}/{entity_id}/{version_id}
        → https://data.vlaanderen.be/id/dossier/{did}/entities/oe:aanvraag/{eid}/{vid}

    Activity:
        dossier:activities/{activity_id}
        → https://data.vlaanderen.be/id/dossier/{did}/activities/{aid}

    Agent:
        dossier:agents/{agent_id}
        → https://data.vlaanderen.be/id/dossier/{did}/agents/{agent_id}

    Cross-dossier (full IRI, no prefix):
        https://data.vlaanderen.be/id/dossier/{other_did}/activities/{aid}

Namespace prefixes:
    prov:    http://www.w3.org/ns/prov#
    xsd:     http://www.w3.org/2001/XMLSchema#
    oe:      https://data.vlaanderen.be/ns/oe/
    dossier: https://data.vlaanderen.be/id/dossier/{dossier_id}/
"""

from __future__ import annotations

from uuid import UUID

# Base URI templates
DOSSIER_BASE = "https://data.vlaanderen.be/id/dossier/{dossier_id}/"
OE_NS = "https://data.vlaanderen.be/ns/oe/"


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
