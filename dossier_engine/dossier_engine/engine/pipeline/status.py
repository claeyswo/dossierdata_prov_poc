"""
Dossier status derivation.

Status is not stored as a single column on the dossier — it's derived
by walking the activity history backwards and returning the first
non-null `computed_status`. This means status is always reproducible
from the activity log, and rolling back an activity rolls back the
status implicitly.

`derive_status` is the cheapest read in the engine and is called from
many places. It does one query (activities for the dossier) and then
walks in memory.
"""

from __future__ import annotations

from uuid import UUID

from ...db.models import Repository


async def derive_status(repo: Repository, dossier_id: UUID) -> str:
    """Return the current status of a dossier.

    Walks the activity history newest-first and returns the first
    `computed_status` it finds. If the dossier has no activities at all,
    returns the literal `"nieuw"`.
    """
    activities = await repo.get_activities_for_dossier(dossier_id)
    if not activities:
        return "nieuw"
    for activity in reversed(activities):
        if activity.computed_status:
            return activity.computed_status
    return "nieuw"
