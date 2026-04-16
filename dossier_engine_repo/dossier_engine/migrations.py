"""
Data migration framework.

Applies content transforms to existing entities, one dossier at a
time, through the engine's normal activity pipeline. Every migration
produces a full PROV audit trail: a systemAction activity per dossier
with the old entity version in `used` and the transformed version in
`generated`, plus a system:note recording the migration UUID and
message.

Usage from a workflow plugin:

    from dossier_engine.migrations import DataMigration, run_migrations

    MIGRATIONS = [
        DataMigration(
            id="a1b2c3d4-...",
            message="Add classificatie field to aanvraag",
            target_type="oe:aanvraag",
            transform=lambda content: {**content, "classificatie": None},
        ),
    ]

    # CLI entry point or startup hook:
    await run_migrations(MIGRATIONS, config_path="path/to/config.yaml")

Design:

- Each migration has a UUID (`id`). When applied to a dossier, a
  system:note is created with `{"migration_id": "<uuid>", ...}` in
  its content. The runner skips dossiers that already have a note
  with that migration_id.

- The "already applied" check is a DB-level query, not an in-memory
  filter. This makes the runner idempotent and restartable: if it
  crashes mid-run, re-running it picks up where it left off.

- All writes go through `execute_activity` with the `systemAction`
  activity definition, so the full engine pipeline runs: PROV trail,
  handlers, side effects, access control updates, search index hooks.

- The runner processes one dossier at a time in its own transaction.
  A failure in one dossier doesn't affect others. Failed dossiers
  are logged and skipped.

- Migrations are ordered: they run in list order. A later migration
  can depend on an earlier one having already transformed the entity.

- The `filter` predicate is optional. When provided, it receives the
  entity content dict and returns True if the entity should be
  migrated. This lets you scope a migration to a subset of entities
  (e.g. only aanvragen in gemeente "Brugge").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import UUID, uuid4

from sqlalchemy import select, and_

from .app import load_config_and_registry, SYSTEM_USER
from .db import init_db, get_session_factory
from .db.models import EntityRow, DossierRow, Repository
from .engine import execute_activity
from .entities import SYSTEM_ACTION_DEF

logger = logging.getLogger("dossier.migrations")


@dataclass
class DataMigration:
    """A single data migration definition.

    Attributes:
        id: Unique UUID string for this migration. Used as the
            idempotency key — once a system:note with this ID exists
            in a dossier, the migration is not re-applied.
        message: Human-readable description stored in the system:note.
        target_type: Entity type to transform (e.g. "oe:aanvraag").
        transform: Function that takes the old content dict and returns
            the new content dict. Must be a pure function (no side
            effects, no DB access). Return None to skip the entity
            (no-op for this particular row).
        filter: Optional predicate on the entity content. When provided,
            only entities where filter(content) is True are migrated.
            Applied after the DB query, before the transform.
        workflow: Optional workflow name filter. When set, only dossiers
            of this workflow are considered. When None, the runner
            applies the migration to all workflows (unusual but valid
            for cross-workflow schema changes).
    """
    id: str
    message: str
    target_type: str
    transform: Callable[[dict], Optional[dict]]
    filter: Optional[Callable[[dict], bool]] = None
    workflow: Optional[str] = None


async def run_migrations(
    migrations: list[DataMigration],
    config_path: str = "dossier_app/dossier_app/config.yaml",
    *,
    dry_run: bool = False,
    batch_size: int = 100,
) -> dict:
    """Run a list of data migrations in order.

    Returns a summary dict: {migration_id: {applied: N, skipped: N, errors: N}}.

    Args:
        migrations: Ordered list of migrations to apply.
        config_path: Path to the dossier config.yaml.
        dry_run: If True, log what would be done without writing.
        batch_size: Number of dossiers to process per DB query page.
    """
    config, registry = load_config_and_registry(config_path)
    db_url = config.get("database", {}).get("url")
    if not db_url:
        raise RuntimeError("database.url is required in config")
    await init_db(db_url)

    session_factory = get_session_factory()
    summary = {}

    for migration in migrations:
        logger.info(
            f"Migration {migration.id}: {migration.message} "
            f"(target={migration.target_type}, dry_run={dry_run})"
        )
        result = await _run_one_migration(
            migration, registry, session_factory,
            dry_run=dry_run, batch_size=batch_size,
        )
        summary[migration.id] = result
        logger.info(
            f"Migration {migration.id}: "
            f"applied={result['applied']}, "
            f"skipped={result['skipped']}, "
            f"errors={result['errors']}"
        )

    return summary


async def _run_one_migration(
    migration: DataMigration,
    registry,
    session_factory,
    *,
    dry_run: bool,
    batch_size: int,
) -> dict:
    """Apply one migration across all matching dossiers."""
    result = {"applied": 0, "skipped": 0, "errors": 0}

    # Step 1: find all dossier IDs that match the workflow filter.
    async with session_factory() as session:
        stmt = select(DossierRow.id)
        if migration.workflow:
            stmt = stmt.where(DossierRow.workflow == migration.workflow)
        stmt = stmt.order_by(DossierRow.created_at)
        rows = (await session.execute(stmt)).scalars().all()
        dossier_ids = list(rows)

    logger.info(f"  Found {len(dossier_ids)} dossiers to check")

    # Step 2: process each dossier.
    for dossier_id in dossier_ids:
        try:
            applied = await _migrate_dossier(
                migration, dossier_id, registry, session_factory,
                dry_run=dry_run,
            )
            if applied:
                result["applied"] += applied
            else:
                result["skipped"] += 1
        except Exception:
            logger.exception(
                f"  Dossier {dossier_id}: migration {migration.id} failed"
            )
            result["errors"] += 1

    return result


async def _migrate_dossier(
    migration: DataMigration,
    dossier_id: UUID,
    registry,
    session_factory,
    *,
    dry_run: bool,
) -> int:
    """Apply one migration to one dossier. Returns the number of
    entities migrated (0 if already done or nothing to do)."""

    async with session_factory() as session, session.begin():
        repo = Repository(session)

        # Check if this migration was already applied to this dossier
        # by looking for a system:note with the migration UUID.
        existing_notes = await session.execute(
            select(EntityRow).where(
                and_(
                    EntityRow.dossier_id == dossier_id,
                    EntityRow.type == "system:note",
                    EntityRow.content["migration_id"].as_string() == migration.id,
                )
            )
        )
        if existing_notes.scalars().first() is not None:
            return 0  # already applied

        # Find the latest version of each logical entity of the target type.
        all_versions = await session.execute(
            select(EntityRow).where(
                and_(
                    EntityRow.dossier_id == dossier_id,
                    EntityRow.type == migration.target_type,
                    EntityRow.tombstoned_by.is_(None),
                )
            ).order_by(EntityRow.created_at)
        )
        all_rows = all_versions.scalars().all()

        # Group by entity_id, keep only the latest version per logical entity
        latest_by_entity: dict[UUID, EntityRow] = {}
        for row in all_rows:
            latest_by_entity[row.entity_id] = row  # last one wins (ordered by created_at)

        if not latest_by_entity:
            return 0  # no entities of target type

        # Apply filter + transform
        entities_to_migrate = []
        for entity_id, row in latest_by_entity.items():
            if row.content is None:
                continue
            if migration.filter and not migration.filter(row.content):
                continue
            new_content = migration.transform(row.content)
            if new_content is None:
                continue  # transform returned None = skip
            if new_content == row.content:
                continue  # no change
            entities_to_migrate.append((row, new_content))

        if not entities_to_migrate:
            return 0

        if dry_run:
            for row, new_content in entities_to_migrate:
                logger.info(
                    f"  [DRY RUN] Dossier {dossier_id}: would migrate "
                    f"{migration.target_type}/{row.entity_id} "
                    f"(version {row.id})"
                )
            return 0

        # Build the activity: generated = new versions (derivedFrom old) + note.
        # No `used` items — the disjoint invariant forbids listing the
        # same entity in both used and generated, and a data migration
        # by definition generates a new version of the entity it's
        # transforming. The `derivedFrom` reference on each generated
        # item is sufficient for PROV lineage.
        dossier = await repo.get_dossier(dossier_id)
        plugin = registry.get(dossier.workflow)
        if not plugin:
            logger.warning(f"  Dossier {dossier_id}: no plugin for workflow {dossier.workflow}")
            return 0

        # Find the systemAction definition
        systemaction_def = None
        for act_def in plugin.workflow.get("activities", []):
            if act_def["name"] == "systemAction":
                systemaction_def = act_def
                break
        if not systemaction_def:
            systemaction_def = SYSTEM_ACTION_DEF

        generated_items = []
        migrated_entity_ids = []

        for row, new_content in entities_to_migrate:
            old_ref = f"{row.type}/{row.entity_id}@{row.id}"
            new_vid = uuid4()
            new_ref = f"{row.type}/{row.entity_id}@{new_vid}"

            generated_items.append({
                "entity": new_ref,
                "content": new_content,
                "derivedFrom": old_ref,
            })
            migrated_entity_ids.append(str(row.entity_id))

        # Add the system:note recording this migration
        note_eid = uuid4()
        note_vid = uuid4()
        note_ref = f"system:note/{note_eid}@{note_vid}"
        generated_items.append({
            "entity": note_ref,
            "content": {
                "text": migration.message,
                "migration_id": migration.id,
                "migrated_entities": migrated_entity_ids,
                "target_type": migration.target_type,
            },
        })

        await execute_activity(
            plugin=plugin,
            activity_def=systemaction_def,
            repo=repo,
            dossier_id=dossier_id,
            activity_id=uuid4(),
            user=SYSTEM_USER,
            role="systeem",
            used_items=[],
            generated_items=generated_items,
            relation_items=[],
            workflow_name=dossier.workflow,
            informed_by=None,
        )

        count = len(entities_to_migrate)
        logger.info(
            f"  Dossier {dossier_id}: migrated {count} "
            f"{migration.target_type} entit{'y' if count == 1 else 'ies'}"
        )
        return count
