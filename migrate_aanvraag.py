"""
Migration script: append ' - migratie' to all aanvraag onderwerpen.

Resumable: uses a unique note_id per migration. On resume, skips dossiers
that already have a system:note entity with this note_id.

Usage:
    python migrate_aanvraag.py --base-url http://localhost:8000 --config gov_dossier_app/config.yaml
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import sys
import time
from uuid import uuid4

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("migration")

BASE_URL = "http://localhost:8000"
SYSTEM_USER = "system"
NOTE_ID = "MIG-2026-001-aanvraag-migratie"


async def get_dossiers_without_note(config_path: str, note_id: str) -> list[str]:
    """Get dossier IDs that don't have a system:note with this note_id."""
    sys.path.insert(0, ".")
    from gov_dossier_engine.db import init_db, get_session_factory
    from gov_dossier_engine.db.models import DossierRow, EntityRow
    from sqlalchemy import select, func, and_
    import yaml

    with open(config_path) as f:
        config = yaml.safe_load(f)

    db_url = config.get("database", {}).get("url", "sqlite+aiosqlite:///./dossiers.db")
    await init_db(db_url)

    session_factory = get_session_factory()
    async with session_factory() as session:
        # Subquery: dossier IDs that have a note with this note_id
        done_subq = (
            select(EntityRow.dossier_id)
            .where(EntityRow.type == "system:note")
            .where(EntityRow.content["note_id"].as_string() == note_id)
            .distinct()
            .subquery()
        )

        # All dossiers NOT in that set
        result = await session.execute(
            select(DossierRow.id)
            .where(DossierRow.id.notin_(select(done_subq.c.dossier_id)))
            .order_by(DossierRow.created_at)
        )
        return [str(row[0]) for row in result.all()]


async def run_migration(base_url: str, config_path: str):
    dossier_ids = await get_dossiers_without_note(config_path, NOTE_ID)
    logger.info(f"Found {len(dossier_ids)} dossiers to migrate (already done skipped)")

    if not dossier_ids:
        logger.info("Nothing to do — all dossiers already migrated")
        return

    start = time.monotonic()
    migrated = 0
    skipped = 0
    errors = 0

    async with aiohttp.ClientSession() as session:
        for dossier_id in dossier_ids:
            # Get current dossier details
            async with session.get(
                f"{base_url}/dossiers/{dossier_id}",
                headers={"X-POC-User": SYSTEM_USER},
            ) as resp:
                if resp.status != 200:
                    skipped += 1
                    continue
                detail = await resp.json()

            # Find latest aanvraag
            aanvraag = None
            for ent in detail.get("currentEntities", []):
                if ent["type"] == "oe:aanvraag":
                    aanvraag = ent
                    break

            if not aanvraag or not aanvraag.get("content"):
                skipped += 1
                continue

            onderwerp = aanvraag["content"].get("onderwerp", "")
            if onderwerp.endswith(" - migratie"):
                skipped += 1
                continue

            # Build migrated content
            new_content = dict(aanvraag["content"])
            new_content["onderwerp"] = f"{onderwerp} - migratie"

            entity_id = aanvraag["entityId"]
            old_version_id = aanvraag["versionId"]
            new_version_id = str(uuid4())
            activity_id = str(uuid4())
            note_entity_id = str(uuid4())
            note_version_id = str(uuid4())

            body = {
                "generated": [
                    {
                        "entity": f"oe:aanvraag/{entity_id}@{new_version_id}",
                        "derivedFrom": f"oe:aanvraag/{entity_id}@{old_version_id}",
                        "content": new_content,
                    },
                    {
                        "entity": f"system:note/{note_entity_id}@{note_version_id}",
                        "content": {
                            "text": "Data migration: appended ' - migratie' to aanvraag onderwerp",
                            "note_id": NOTE_ID,
                        },
                    },
                ],
            }

            async with session.put(
                f"{base_url}/dossiers/{dossier_id}/activities/{activity_id}/systemAction",
                json=body,
                headers={"Content-Type": "application/json", "X-POC-User": SYSTEM_USER},
            ) as resp:
                if resp.status >= 400:
                    error_text = await resp.text()
                    logger.warning(f"Dossier {dossier_id}: HTTP {resp.status} — {error_text[:100]}")
                    errors += 1
                else:
                    migrated += 1

            if (migrated + skipped + errors) % 100 == 0:
                elapsed = time.monotonic() - start
                rate = (migrated + skipped + errors) / elapsed if elapsed > 0 else 0
                logger.info(f"  {migrated + skipped + errors}/{len(dossier_ids)} — {migrated} migrated, {skipped} skipped, {errors} errors — {rate:.0f}/sec")

    elapsed = time.monotonic() - start
    logger.info(f"Migration complete: {migrated} migrated, {skipped} skipped, {errors} errors in {elapsed:.1f}s")
    logger.info(f"Note ID: {NOTE_ID}")


def main():
    parser = argparse.ArgumentParser(description="Migrate aanvraag: append ' - migratie'")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--config", default="gov_dossier_app/config.yaml")
    args = parser.parse_args()
    asyncio.run(run_migration(args.base_url, args.config))


if __name__ == "__main__":
    main()
