"""
Revert migration: remove ' - migratie' from all aanvraag onderwerpen.

Resumable: uses a unique note_id per revert. On resume, skips dossiers
that already have a system:note entity with the revert note_id.

Usage:
    python revert_migrate_aanvraag.py --base-url http://localhost:8000 --config gov_dossier_app/config.yaml
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
logger = logging.getLogger("revert")

BASE_URL = "http://localhost:8000"
SYSTEM_USER = "system"
MIGRATION_NOTE_ID = "MIG-2026-001-aanvraag-migratie"
REVERT_NOTE_ID = "REVERT-MIG-2026-001-aanvraag-migratie"


async def get_dossiers_to_revert(config_path: str) -> list[str]:
    """Get dossier IDs that have the migration note but not the revert note."""
    sys.path.insert(0, ".")
    from gov_dossier_engine.db import init_db, get_session_factory
    from gov_dossier_engine.db.models import DossierRow, EntityRow
    from sqlalchemy import select
    import yaml

    with open(config_path) as f:
        config = yaml.safe_load(f)

    db_url = config.get("database", {}).get("url", "sqlite+aiosqlite:///./dossiers.db")
    await init_db(db_url)

    session_factory = get_session_factory()
    async with session_factory() as session:
        # Dossiers with migration note
        migrated_subq = (
            select(EntityRow.dossier_id)
            .where(EntityRow.type == "system:note")
            .where(EntityRow.content["note_id"].as_string() == MIGRATION_NOTE_ID)
            .distinct()
            .subquery()
        )

        # Dossiers with revert note (already reverted)
        reverted_subq = (
            select(EntityRow.dossier_id)
            .where(EntityRow.type == "system:note")
            .where(EntityRow.content["note_id"].as_string() == REVERT_NOTE_ID)
            .distinct()
            .subquery()
        )

        # Migrated but not yet reverted
        result = await session.execute(
            select(DossierRow.id)
            .where(DossierRow.id.in_(select(migrated_subq.c.dossier_id)))
            .where(DossierRow.id.notin_(select(reverted_subq.c.dossier_id)))
            .order_by(DossierRow.created_at)
        )
        return [str(row[0]) for row in result.all()]


async def run_revert(base_url: str, config_path: str):
    dossier_ids = await get_dossiers_to_revert(config_path)
    logger.info(f"Found {len(dossier_ids)} dossiers to revert (already reverted skipped)")

    if not dossier_ids:
        logger.info("Nothing to do — all migrated dossiers already reverted")
        return

    start = time.monotonic()
    reverted = 0
    skipped = 0
    errors = 0

    async with aiohttp.ClientSession() as session:
        for dossier_id in dossier_ids:
            async with session.get(
                f"{base_url}/dossiers/{dossier_id}",
                headers={"X-POC-User": SYSTEM_USER},
            ) as resp:
                if resp.status != 200:
                    skipped += 1
                    continue
                detail = await resp.json()

            aanvraag = None
            for ent in detail.get("currentEntities", []):
                if ent["type"] == "oe:aanvraag":
                    aanvraag = ent
                    break

            if not aanvraag or not aanvraag.get("content"):
                skipped += 1
                continue

            onderwerp = aanvraag["content"].get("onderwerp", "")
            if not onderwerp.endswith(" - migratie"):
                skipped += 1
                continue

            new_content = dict(aanvraag["content"])
            new_content["onderwerp"] = onderwerp[: -len(" - migratie")]

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
                            "text": "Revert migration: removed ' - migratie' from aanvraag onderwerp",
                            "note_id": REVERT_NOTE_ID,
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
                    reverted += 1

            if (reverted + skipped + errors) % 100 == 0:
                elapsed = time.monotonic() - start
                rate = (reverted + skipped + errors) / elapsed if elapsed > 0 else 0
                logger.info(f"  {reverted + skipped + errors}/{len(dossier_ids)} — {reverted} reverted, {skipped} skipped, {errors} errors — {rate:.0f}/sec")

    elapsed = time.monotonic() - start
    logger.info(f"Revert complete: {reverted} reverted, {skipped} skipped, {errors} errors in {elapsed:.1f}s")
    logger.info(f"Note ID: {REVERT_NOTE_ID}")


def main():
    parser = argparse.ArgumentParser(description="Revert aanvraag migration")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--config", default="gov_dossier_app/config.yaml")
    args = parser.parse_args()
    asyncio.run(run_revert(args.base_url, args.config))


if __name__ == "__main__":
    main()
