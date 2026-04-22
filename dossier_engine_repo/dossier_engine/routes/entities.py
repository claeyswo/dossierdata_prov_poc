"""
Entity read endpoints — three shapes for inspecting persisted entities.

* `GET /dossiers/{id}/entities/{type}` — every version of every logical
  entity of the given type, in creation order. Used for "show me all
  the aanvragen this dossier has ever had."
* `GET /dossiers/{id}/entities/{type}/{entity_id}` — every version of
  one specific logical entity. Used for inspecting the revision history
  of a single entity.
* `GET /dossiers/{id}/entities/{type}/{entity_id}/{version_id}` — a
  single entity version. The interesting case is tombstoned versions:
  rather than returning the redacted row directly, the endpoint emits
  a `301 Moved Permanently` to the URL of the live replacement. Per
  the deletion-scope decision (option a), the original row still
  exists with `content: null` and `tombstoned_by` set, so the
  redirect target is always findable via `get_latest_entity_by_id`.

All three endpoints share a dossier-load + visibility-check preamble.
That preamble lives in `_load_with_access_check`, called once per
endpoint. The bulk endpoints render their results through
`entity_version_dict` from `_serializers.py`; the single-version
endpoint produces a flatter dict inline because it doesn't have a
sibling list to drive the `redirectTo` / tombstone-reference machinery.

The single-version endpoint pipes its entity content through
`inject_download_urls` (Bug 57 fix) so file_id fields in the payload
are accompanied by short-lived signed download URLs — matching the
`dossiers.py` route's behaviour. The two bulk endpoints deliberately
do NOT inject URLs: they're "inspection / revision history" shaped,
clients follow up with a single-version fetch to actually download,
and minting one signed URL per file per version across every version
of every entity is waste in the common case. If a future client
actually needs URLs in the bulk responses, the fix is the same
per-entity inject call in the per-version loop.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from dossier_common.signing import sign_token, token_to_query_string

from ..auth import User
from ..db import Repository, get_session_factory
from ..file_refs import inject_download_urls
from ._serializers import entity_version_dict
from .access import check_dossier_access, get_visibility_from_entry


def register(app: FastAPI, *, get_user, global_access) -> None:
    """Register entity read endpoints on the FastAPI app."""

    @app.get(
        "/dossiers/{dossier_id}/entities/{entity_type}",
        tags=["entities"],
        summary="Get all versions of an entity type",
        description=(
            "Returns all versions of a given entity type in this "
            "dossier, ordered by creation time. Respects "
            "dossier_access visibility."
        ),
    )
    async def get_entity_versions(
        dossier_id: UUID,
        entity_type: str,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)
            await _load_with_access_check(
                repo, dossier_id, entity_type, user, global_access,
            )

            entities = await repo.get_entities_by_type(dossier_id, entity_type)
            if not entities:
                raise HTTPException(
                    404, detail=f"No entities of type '{entity_type}' found",
                )

            return {
                "dossier_id": str(dossier_id),
                "entity_type": entity_type,
                "versions": [
                    entity_version_dict(e, dossier_id, entity_type, entities)
                    for e in entities
                ],
            }

    @app.get(
        "/dossiers/{dossier_id}/entities/{entity_type}/{entity_id}",
        tags=["entities"],
        summary="Get all versions of a specific logical entity",
        description=(
            "Returns all versions of a specific logical entity "
            "(by entity_id), ordered by creation time."
        ),
    )
    async def get_logical_entity_versions(
        dossier_id: UUID,
        entity_type: str,
        entity_id: UUID,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)
            await _load_with_access_check(
                repo, dossier_id, entity_type, user, global_access,
            )

            entities = await repo.get_entity_versions(dossier_id, entity_id)
            # Filter by type — defensive, since entity_id is unique
            # but the URL also constrains the type.
            versions = [e for e in entities if e.type == entity_type]
            if not versions:
                raise HTTPException(404, detail="Entity not found")

            return {
                "dossier_id": str(dossier_id),
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "versions": [
                    entity_version_dict(
                        e, dossier_id, entity_type, versions,
                        include_entity_id=False,
                    )
                    for e in versions
                ],
            }

    @app.get(
        "/dossiers/{dossier_id}/entities/{entity_type}/{entity_id}/{version_id}",
        tags=["entities"],
        summary="Get a specific entity version",
        description="Returns a single entity version by its version ID.",
    )
    async def get_entity_version(
        dossier_id: UUID,
        entity_type: str,
        entity_id: UUID,
        version_id: UUID,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)
            await _load_with_access_check(
                repo, dossier_id, entity_type, user, global_access,
            )

            entity = await repo.get_entity(version_id)
            # 404 on any URL segment mismatch. Before the Bug 62 fix
            # the ``entity_id`` segment was not checked — the endpoint
            # happily returned the version as long as the version
            # existed in the right dossier with the right type, so a
            # client with a stale or wrong ``entity_id`` in the URL
            # would get a response whose actual ``entity_id`` field
            # differed from what they asked for (silent mis-attribution).
            # Fail fast on the mismatch instead; the URL
            # ``(dossier, type, entity_id, version_id)`` tuple must
            # address a single canonical row, not a set of rows where
            # ``entity_id`` is decorative.
            if (
                not entity
                or entity.dossier_id != dossier_id
                or entity.type != entity_type
                or entity.entity_id != entity_id
            ):
                raise HTTPException(404, detail="Entity version not found")

            # Tombstone redirect. If this version has been redacted,
            # 301 to the latest version of the same logical entity —
            # which by construction is the tombstone replacement, since
            # tombstones always generate a new revision. The original
            # row survives (content nulled, tombstoned_by stamped) so
            # the lookup is cheap.
            if entity.tombstoned_by is not None:
                latest = await repo.get_latest_entity_by_id(
                    dossier_id, entity.entity_id,
                )
                if latest is not None and latest.id != entity.id:
                    target = (
                        f"/dossiers/{dossier_id}/entities/{entity_type}/"
                        f"{entity.entity_id}/{latest.id}"
                    )
                    return RedirectResponse(url=target, status_code=301)
                # Defensive: no replacement found (shouldn't happen
                # under normal tombstone flow). Return 410 Gone.
                raise HTTPException(
                    410,
                    detail="Entity version was tombstoned and has no replacement",
                )

            # Bug 57 fix: inject download URLs for file_id fields in
            # the content. Resolve the plugin that owns this entity
            # type via registry stored on app.state (wired at app
            # startup — see app.py's ``app.state.registry = registry``),
            # then mint a per-request signer closure scoped to this
            # dossier and user (same pattern as ``routes/dossiers.py``;
            # tokens are dossier-scoped per Round 11 Bug 47 so a token
            # leaked from dossier A can't pull a file from dossier B).
            #
            # If no plugin owns this type (shouldn't happen for stored
            # entities — they went through validation — but defensive
            # fallback for the case of a temporarily unloaded plugin)
            # or if the resolved model class is None, inject_download_urls
            # returns the content unchanged.
            registry = app.state.registry
            plugin = None
            for p in registry.all_plugins():
                if entity_type in p.entity_models:
                    plugin = p
                    break
            model_class = (
                plugin.resolve_schema(entity_type, entity.schema_version)
                if plugin is not None else None
            )

            file_config = app.state.config.get("file_service", {})
            signing_key = file_config.get(
                "signing_key", "poc-signing-key-change-in-production",
            )
            file_service_url = file_config.get("url", "http://localhost:8001")

            def sign(file_id: str) -> str:
                token = sign_token(
                    file_id=file_id,
                    action="download",
                    signing_key=signing_key,
                    user_id=user.id,
                    dossier_id=str(dossier_id),
                )
                return (
                    f"{file_service_url}/download/{file_id}"
                    f"?{token_to_query_string(token)}"
                )

            content_with_urls = inject_download_urls(
                model_class, entity.content, sign,
            )

            return {
                "dossier_id": str(dossier_id),
                "entity_type": entity_type,
                "entity_id": str(entity.entity_id),
                "versionId": str(entity.id),
                "content": content_with_urls,
                "generatedBy": str(entity.generated_by),
                "derivedFrom": (
                    str(entity.derived_from) if entity.derived_from else None
                ),
                "attributedTo": entity.attributed_to,
                "createdAt": (
                    entity.created_at.isoformat() if entity.created_at else None
                ),
            }


async def _load_with_access_check(
    repo: Repository,
    dossier_id: UUID,
    entity_type: str,
    user: User,
    global_access: list[dict] | None,
) -> None:
    """Verify the dossier exists and the user can see this entity type.

    Raises 404 if the dossier doesn't exist, 403 if the user's
    `dossier_access` entry doesn't grant visibility into `entity_type`.
    Returns nothing — the caller does its own data fetching after the
    check passes.
    """
    dossier = await repo.get_dossier(dossier_id)
    if not dossier:
        raise HTTPException(404, detail="Dossier not found")

    access_entry = await check_dossier_access(
        repo, dossier_id, user, global_access,
    )
    visible_types, _ = get_visibility_from_entry(access_entry)
    if visible_types is not None and entity_type not in visible_types:
        raise HTTPException(
            403, detail=f"No access to entity type '{entity_type}'",
        )
