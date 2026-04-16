"""
Data migrations for the toelatingen workflow.

Each migration is a DataMigration instance with a unique UUID. Once
applied to a dossier, a system:note with that UUID is created so the
migration is never re-applied.

Migrations run in list order. Add new migrations to the END of the
MIGRATIONS list — never reorder or remove existing entries.

Usage:
    # Dry run (shows what would change, writes nothing)
    python -m dossier_toelatingen.data_migrations --dry-run

    # Apply all pending migrations
    python -m dossier_toelatingen.data_migrations

    # Custom config path
    python -m dossier_toelatingen.data_migrations --config path/to/config.yaml
"""

from dossier_engine.migrations import DataMigration


# ── Migration definitions ────────────────────────────────────────
#
# Add new migrations at the END. Each migration needs:
#   id          — unique UUID string (generate: python -c "import uuid; print(uuid.uuid4())")
#   message     — human-readable, stored in the system:note
#   target_type — entity type to transform
#   transform   — function(old_content) -> new_content (return None to skip)
#   filter      — optional predicate(content) -> bool
#   workflow    — "toelatingen" so only our dossiers are touched


def _add_classificatie(content: dict) -> dict | None:
    """Add classificatie + urgentie fields if missing (v1 → v2 shape)."""
    if "classificatie" in content:
        return None  # already has the field, no-op
    return {
        **content,
        "classificatie": None,
        "urgentie": None,
    }


MIGRATIONS = [
    DataMigration(
        id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
        message="Add classificatie and urgentie fields to aanvraag (v1→v2 backfill)",
        target_type="oe:aanvraag",
        transform=_add_classificatie,
        workflow="toelatingen",
    ),
]
