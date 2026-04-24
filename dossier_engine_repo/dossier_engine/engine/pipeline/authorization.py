"""
Authorization and workflow rule validation.

Two phases live here:

* `authorize_activity` — checks whether the calling user has permission
  to run a given activity. Walks the activity's `authorization` block
  and tries each role entry in turn. Three role-matching patterns are
  supported (direct, scoped, entity-derived) — see the docstrings inline.

* `validate_workflow_rules` — checks the activity's structural
  preconditions (`requirements`, `forbidden`): which other activities
  must have already happened, which entities must already exist, which
  dossier statuses are required or forbidden. This is the workflow's
  state machine, encoded declaratively.

Both return `(ok: bool, error_message: str | None)` so callers can
decide whether to raise or just skip.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ...auth import User
from ...db.models import Repository
from ...plugin import Plugin
from ..lookups import lookup_singleton
from ._helpers.status import derive_status


async def authorize_activity(
    plugin: Plugin,
    activity_def: dict,
    user: User,
    repo: Repository,
    dossier_id: UUID | None,
) -> tuple[bool, str | None]:
    """Decide whether `user` may run `activity_def` on this dossier.

    Reads the activity's `authorization` block. Supported `access` types:

    * `everyone` — anyone (including unauthenticated callers) may run it.
    * `authenticated` — any authenticated user.
    * `roles` — the user must satisfy at least one of the role entries.

    Each role entry under `authorization.roles` is one of three shapes:

    1. **Direct match** (`{role: "behandelaar"}`) — the user must have
       this exact string in their roles list.

    2. **Scoped match** (`{role: "gemeente-toevoeger", scope: {from_entity:
       "oe:aanvraag", field: "content.gemeente"}}`) — the role string is
       composed at runtime from the base role plus a value resolved from
       an entity field, e.g. `gemeente-toevoeger:brugge`. Only usable on
       existing dossiers because the entity must already exist.

    3. **Entity-derived match** (`{from_entity: "oe:aanvraag", field:
       "content.aanvrager.rrn"}`) — the entity field value IS the role
       string. Used for dossier ownership checks where the dossier carries
       the owner's identifier directly.

    Returns `(True, None)` on success, `(False, error_message)` on failure.
    """
    auth = activity_def.get("authorization", {})
    access = auth.get("access", "authenticated")

    if access == "everyone":
        return True, None

    if access == "authenticated":
        if not user:
            return False, "Authentication required"
        return True, None

    if access == "roles":
        roles_config = auth.get("roles", [])
        if not roles_config:
            return True, None

        errors = []
        for role_entry in roles_config:
            if isinstance(role_entry, dict):
                if "role" in role_entry:
                    base_role = role_entry["role"]
                    scope = role_entry.get("scope")

                    if scope and dossier_id:
                        try:
                            entity_type = scope["from_entity"]
                            field_path = scope["field"]
                            entity = await lookup_singleton(plugin, repo, dossier_id, entity_type)
                            if not entity:
                                errors.append(f"{base_role} — entity '{entity_type}' not found")
                                continue
                            value = _resolve_field(entity.content, field_path)
                            if value is None:
                                errors.append(f"{base_role} — field '{field_path}' is null")
                                continue
                            resolved = f"{base_role}:{value}"
                        except Exception as e:
                            errors.append(f"{base_role} — scope resolution error: {e}")
                            continue
                    else:
                        resolved = base_role

                    if resolved in user.roles:
                        return True, None
                    errors.append(f"User does not have role '{resolved}'")

                elif "from_entity" in role_entry:
                    if not dossier_id:
                        errors.append("Entity-derived role check requires existing dossier")
                        continue
                    try:
                        entity_type = role_entry["from_entity"]
                        field_path = role_entry["field"]
                        entity = await lookup_singleton(plugin, repo, dossier_id, entity_type)
                        if not entity:
                            errors.append(f"Entity '{entity_type}' not found")
                            continue
                        resolved = _resolve_field(entity.content, field_path)
                        if resolved is None:
                            errors.append(f"Field '{field_path}' is null")
                            continue
                        if str(resolved) in user.roles:
                            return True, None
                        errors.append(f"User does not have role '{resolved}'")
                    except Exception as e:
                        errors.append(f"Entity-derived role error: {e}")
                        continue
            else:
                errors.append(
                    f"Invalid role entry shape (must be dict with "
                    f"'role' or 'from_entity'): {role_entry!r}"
                )

        return False, f"Authorization failed: {'; '.join(errors)}"

    return False, f"Unknown access type: {access}"


def _resolve_field(content: dict | Any, field_path: str) -> Any:
    """Resolve a dot-notation field path inside a content dict.

    Accepts paths like `content.aanvrager.kbo` or `aanvrager.kbo` —
    the leading `content.` segment is stripped if present, since this
    is always called with the entity's `content` dict already in hand.
    """
    if content is None:
        return None
    parts = field_path.split(".")
    if parts[0] == "content" and len(parts) > 1:
        parts = parts[1:]
    current = content
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


async def validate_workflow_rules(
    activity_def: dict,
    repo: Repository,
    dossier_id: UUID,
    known_status: str | None = None,
    known_activity_types: set[str] | None = None,
    plugin: Plugin | None = None,
    now: "datetime | None" = None,
) -> tuple[bool, str | None]:
    """Check the activity's structural preconditions.

    Reads `requirements` and `forbidden` blocks from the activity_def
    and validates that:

    * Required activities have already been completed in this dossier.
    * Required entities (by type) already exist.
    * The current dossier status is in the required set (if specified)
      and not in the forbidden set (if specified).
    * No forbidden activity has already been completed.
    * `requirements.not_before` (if declared) resolves to a time
      at-or-before `now` — i.e., the earliest legal time to run the
      activity has arrived.
    * `forbidden.not_after` (if declared) resolves to a time strictly
      after `now` — i.e., the deadline hasn't passed yet.

    Deadline rules accept three value shapes (see
    ``engine.scheduling.resolve_deadline``):
      - Absolute ISO 8601: ``"2026-12-31T23:59:59Z"``
      - Entity field reference: ``{from_entity, field}``
      - Entity field + offset: ``{from_entity, field, offset}``

    Entity references must point to a singleton type; the plugin
    validator rejects non-singletons at startup, and the resolver
    also defends against them at runtime. When a declared rule's
    anchor entity doesn't exist in the dossier, the resolver returns
    None and the rule is treated as inactive — that lets plugins
    compose the deadline with ``requirements.entities`` to gate the
    activity behind the anchor's existence.

    Pass `known_status` and `known_activity_types` to avoid redundant
    queries when the caller has already fetched them (e.g. inside the
    eligibility loop, which evaluates many activities against the same
    dossier state). Pass `plugin` whenever the activity might declare
    deadline rules that reference entities (the plugin gives us
    `is_singleton` and is needed by the singleton lookup); deadline
    rules are silently skipped if no plugin is provided, which lets
    narrow unit tests keep calling without it. ``now`` defaults to
    the current UTC time; callers on the execution path pass their
    ``state.now`` for consistency with other time-sensitive phases.

    Returns `(True, None)` on success, `(False, reason)` on failure.
    Malformed deadline declarations raise `ValueError` so the caller
    can wrap as 500 (it's a plugin-author bug, not a user error).
    """
    from datetime import datetime, timezone
    from ..scheduling import resolve_deadline

    if now is None:
        now = datetime.now(timezone.utc)

    requirements = activity_def.get("requirements", {})
    forbidden = activity_def.get("forbidden", {})

    if known_activity_types is not None:
        completed_types = known_activity_types
    else:
        activities = await repo.get_activities_for_dossier(dossier_id)
        completed_types = {a.type for a in activities}

    for req_activity in requirements.get("activities", []):
        if req_activity and req_activity not in completed_types:
            return False, f"Required activity '{req_activity}' not completed"

    for req_entity in requirements.get("entities", []):
        if req_entity and not await repo.entity_type_exists(dossier_id, req_entity):
            return False, f"Required entity type '{req_entity}' does not exist"

    req_statuses = requirements.get("statuses", [])
    forb_statuses = forbidden.get("statuses", [])

    current_status = known_status
    if current_status is None and (
        (req_statuses and any(s for s in req_statuses)) or
        (forb_statuses and any(s for s in forb_statuses))
    ):
        current_status = await derive_status(repo, dossier_id)

    if req_statuses and any(s for s in req_statuses):
        if current_status not in req_statuses:
            return False, f"Dossier status '{current_status}' not in required statuses {req_statuses}"

    for forb_activity in forbidden.get("activities", []):
        if forb_activity and forb_activity in completed_types:
            return False, f"Forbidden activity '{forb_activity}' already completed"

    if forb_statuses and any(s for s in forb_statuses):
        if current_status in forb_statuses:
            return False, f"Dossier is in forbidden status '{current_status}'"

    # Deadline rules. Resolved only when a plugin was supplied —
    # without a plugin, the singleton lookup path can't run, so we
    # skip the check entirely. Every production caller passes one;
    # test callers that don't exercise deadlines can omit it.
    if plugin is not None:
        not_before_decl = requirements.get("not_before")
        if not_before_decl is not None:
            not_before = await resolve_deadline(
                not_before_decl, plugin, repo, dossier_id,
                rule_name="not_before",
            )
            if not_before is not None and now < not_before:
                return False, (
                    f"Activity not yet available (not_before: "
                    f"{not_before.isoformat()})"
                )

        not_after_decl = forbidden.get("not_after")
        if not_after_decl is not None:
            not_after = await resolve_deadline(
                not_after_decl, plugin, repo, dossier_id,
                rule_name="not_after",
            )
            if not_after is not None and now >= not_after:
                return False, (
                    f"Activity deadline has passed (not_after: "
                    f"{not_after.isoformat()})"
                )

    return True, None
