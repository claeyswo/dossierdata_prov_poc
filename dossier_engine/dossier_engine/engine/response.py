"""
Response builder for idempotent activity replays.

When the engine sees a `PUT /activities/{activity_id}` whose `activity_id`
already exists, it doesn't re-execute the activity — it returns a
synthesized response describing the activity as it landed the first time.

This module owns that synthesis. The full execution pipeline builds its
response inline in the orchestrator (because it needs all the local
state produced along the way), but idempotent replays only have the
persisted activity row to work from.

The response shape returned here is a subset of what `execute_activity`
returns on a fresh execution — `used` and `generated` are empty because
this code path doesn't replay them. Clients receive enough to verify
that the activity exists and the dossier is in the expected state.
"""

from __future__ import annotations

from uuid import UUID

from ..db.models import Repository, ActivityRow
from ..plugin import Plugin
from ..auth import User
from .pipeline.eligibility import derive_allowed_activities
from .pipeline.status import derive_status


async def build_replay_response(
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    activity_row: ActivityRow,
    user: User,
) -> dict:
    """Build the response for an activity that already exists.

    The engine calls this when it detects an idempotent replay (the same
    activity_id has been PUT before). Returns the activity's identity,
    the dossier's current status, and the activities the calling user
    can run next.
    """
    current_status = await derive_status(repo, dossier_id)
    allowed = await derive_allowed_activities(plugin, repo, dossier_id, user)
    dossier = await repo.get_dossier(dossier_id)

    return {
        "activity": {
            "id": str(activity_row.id),
            "type": activity_row.type,
            "startedAtTime": activity_row.started_at.isoformat() if activity_row.started_at else None,
            "endedAtTime": activity_row.ended_at.isoformat() if activity_row.ended_at else None,
        },
        "used": [],
        "generated": [],
        "dossier": {
            "id": str(dossier_id),
            "workflow": dossier.workflow if dossier else "",
            "status": current_status,
            "allowedActivities": allowed,
        },
    }
