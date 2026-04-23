"""
Field-level validators for the toelatingen workflow.

These are lightweight async callables exposed via
``POST /{workflow}/validate/{name}``. They run between activities
to give the frontend fast feedback on individual field values
without triggering the full activity pipeline.

Each validator receives the raw request body (dict) and returns
a result dict. Convention:

    {"valid": True, ...extra_data...}
    {"valid": False, "error": "Human-readable reason"}
"""

from __future__ import annotations

import re


# Fake inventaris data for POC. In production this would hit the
# real inventaris API or a cached mirror.
_KNOWN_ERFGOEDOBJECTEN = {
    "https://id.erfgoed.net/erfgoedobjecten/10001": {
        "label": "Stadhuis Brugge",
        "type": "monument",
        "gemeente": "Brugge",
    },
    "https://id.erfgoed.net/erfgoedobjecten/10002": {
        "label": "Sint-Baafskathedraal",
        "type": "monument",
        "gemeente": "Gent",
    },
    "https://id.erfgoed.net/erfgoedobjecten/10003": {
        "label": "Gravensteen",
        "type": "monument",
        "gemeente": "Gent",
    },
    "https://id.erfgoed.net/erfgoedobjecten/20001": {
        "label": "Begijnhof Leuven",
        "type": "landschap",
        "gemeente": "Leuven",
    },
}

_VALID_HANDELINGEN_PER_TYPE = {
    "monument": {
        "renovatie", "restauratie", "sloop_deel",
        "herbestemming", "onderhoud",
    },
    "landschap": {
        "renovatie", "onderhoud", "nieuwbouw",
    },
}


async def validate_erfgoedobject(body: dict) -> dict:
    """Check if an erfgoedobject URI resolves to a known object.

    Request:  {"uri": "https://id.erfgoed.net/erfgoedobjecten/10001"}
    Success:  {"valid": true, "label": "Stadhuis Brugge", "type": "monument", "gemeente": "Brugge"}
    Failure:  {"valid": false, "error": "Erfgoedobject niet gevonden: ..."}
    """
    uri = body.get("uri", "")
    if not uri:
        return {"valid": False, "error": "Veld 'uri' is vereist."}

    # Basic URI format check.
    if not uri.startswith("https://id.erfgoed.net/erfgoedobjecten/"):
        return {
            "valid": False,
            "error": f"Ongeldig formaat. Verwacht: "
                     f"https://id.erfgoed.net/erfgoedobjecten/{{id}}",
        }

    obj = _KNOWN_ERFGOEDOBJECTEN.get(uri)
    if obj is None:
        return {
            "valid": False,
            "error": f"Erfgoedobject niet gevonden: {uri}",
        }

    return {"valid": True, **obj}


async def validate_handeling(body: dict) -> dict:
    """Check if a handeling is valid for a given erfgoedobject type.

    Request:  {"erfgoedobject_uri": "https://...", "handeling": "sloop_deel"}
    Success:  {"valid": true}
    Failure:  {"valid": false, "error": "Sloop is niet toegelaten voor landschappen"}
    """
    uri = body.get("erfgoedobject_uri", "")
    handeling = body.get("handeling", "")

    if not uri or not handeling:
        return {
            "valid": False,
            "error": "Velden 'erfgoedobject_uri' en 'handeling' zijn vereist.",
        }

    obj = _KNOWN_ERFGOEDOBJECTEN.get(uri)
    if obj is None:
        return {
            "valid": False,
            "error": f"Erfgoedobject niet gevonden: {uri}",
        }

    obj_type = obj["type"]
    allowed = _VALID_HANDELINGEN_PER_TYPE.get(obj_type, set())

    if handeling not in allowed:
        return {
            "valid": False,
            "error": f"Handeling '{handeling}' is niet toegelaten "
                     f"voor type '{obj_type}' ({obj['label']}). "
                     f"Toegelaten: {sorted(allowed)}.",
        }

    return {"valid": True}


# =====================================================================
# Request / response models for OpenAPI documentation
# =====================================================================

from pydantic import BaseModel
from typing import Optional
from dossier_engine.plugin import FieldValidator


class ErfgoedobjectRequest(BaseModel):
    """Valideer of een erfgoedobject URI gekend is in de inventaris."""
    uri: str


class ErfgoedobjectResponse(BaseModel):
    """Resultaat van de erfgoedobject validatie."""
    valid: bool
    label: Optional[str] = None
    type: Optional[str] = None
    gemeente: Optional[str] = None
    error: Optional[str] = None


class HandelingRequest(BaseModel):
    """Valideer of een handeling toegelaten is voor een erfgoedobject."""
    erfgoedobject_uri: str
    handeling: str


class HandelingResponse(BaseModel):
    """Resultaat van de handeling validatie."""
    valid: bool
    error: Optional[str] = None


# Module-level FieldValidator bindings. Obs 95 / Round 28: these used
# to live in a ``FIELD_VALIDATORS = {...}`` dict that ``create_plugin()``
# passed to ``Plugin(...)``. Under the dotted-path migration the
# workflow YAML's ``field_validators:`` block resolves each URL key
# (e.g. ``erfgoedobject``) to its dotted path
# (``dossier_toelatingen.field_validators.erfgoedobject``) at plugin
# load time. The URL key itself stays user-facing — it ends up in
# ``POST /{workflow}/validate/{url_key}`` — so ``field_validators`` is
# the one registry whose key is NOT the dotted path.
erfgoedobject = FieldValidator(
    fn=validate_erfgoedobject,
    request_model=ErfgoedobjectRequest,
    response_model=ErfgoedobjectResponse,
    summary="Valideer erfgoedobject URI",
    description=(
        "Controleer of de URI verwijst naar een gekend "
        "erfgoedobject in de inventaris. Retourneert het "
        "label, type en gemeente bij succes."
    ),
)

handeling = FieldValidator(
    fn=validate_handeling,
    request_model=HandelingRequest,
    response_model=HandelingResponse,
    summary="Valideer handeling voor erfgoedobject",
    description=(
        "Controleer of een handeling (renovatie, sloop, ...) "
        "toegelaten is voor het type erfgoedobject (monument, "
        "landschap, ...) waarnaar de URI verwijst."
    ),
)
