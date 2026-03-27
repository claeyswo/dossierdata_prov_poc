"""
Entity models for the toelatingen beschermd erfgoed workflow.

These are the Pydantic models that validate entity content.
Generated from the JSON Schema definitions in the workflow template.
"""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional


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
    object: str  # URI of the protected heritage object


# --- Beslissing ---

class BeslissingUitkomst(str, Enum):
    goedgekeurd = "goedgekeurd"
    afgekeurd = "afgekeurd"
    onvolledig = "onvolledig"


class Beslissing(BaseModel):
    beslissing: BeslissingUitkomst
    datum: str  # datetime string
    object: str  # URI
    brief: str  # URI


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
