"""Shared access control utilities for routes."""

from __future__ import annotations

from uuid import UUID
from fastapi import HTTPException
from ..db.models import Repository
from ..auth import User


async def check_dossier_access(repo: Repository, dossier_id: UUID, user: User) -> dict | None:
    """Check if user has access to this dossier. Returns the matched access entry.
    
    Returns:
        None — no dossier_access entity exists (no restrictions)
        dict — the matched access entry (with role, view, activity_view)
    
    Raises:
        HTTPException 403 if user has no access
    """
    access_entity = await repo.get_latest_entity(dossier_id, "oe:dossier_access")
    if not access_entity or not access_entity.content:
        return None  # no access entity = no restrictions

    for entry in access_entity.content.get("access", []):
        entry_role = entry.get("role")
        if entry_role and entry_role in user.roles:
            return entry
        entry_agents = entry.get("agents", [])
        if user.id in entry_agents:
            return entry

    raise HTTPException(403, detail="No access to this dossier")


def get_visibility_from_entry(entry: dict | None) -> tuple[set[str] | None, str]:
    """Extract visible types and activity_view mode from an access entry.
    
    Returns:
        (visible_types, activity_view_mode)
        visible_types is None if no restrictions, otherwise a set of entity type strings
    """
    if entry is None:
        return None, "all"
    
    visible = set(entry.get("view", []))
    activity_view = entry.get("activity_view", "all")
    return visible, activity_view
