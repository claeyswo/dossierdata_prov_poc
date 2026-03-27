"""
Common entity models provided by the engine.
These are shared across all workflow plugins.
"""

from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


class DossierAccessEntry(BaseModel):
    role: Optional[str] = None
    agents: list[str] = []
    view: list[str] = []
    activity_view: str = "related"  # "own", "related", "all"


class DossierAccess(BaseModel):
    access: list[DossierAccessEntry]
