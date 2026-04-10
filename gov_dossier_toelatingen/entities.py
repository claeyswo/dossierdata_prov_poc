"""
Entity models for the toelatingen beschermd erfgoed workflow.

These are the Pydantic models that validate entity content.
Generated from the JSON Schema definitions in the workflow template.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel

from gov_dossier_engine.file_refs import FileId


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
    object: str  # URI of the protected heritage object
    bijlagen: list[Bijlage] = []


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


