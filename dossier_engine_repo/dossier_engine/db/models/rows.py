"""
Row dataclasses — the SQLAlchemy-mapped table definitions.

All tables are append-only: no UPDATEs, no DELETEs. Status and other
mutable properties get new rows, not in-place modifications.

Columns use Postgres-native ``UUID`` (via ``UUID_DB``) and ``JSONB``
(via ``JSON_DB``) — the earlier POC's SQLite support was removed
during the worker production-readiness pass.

``Base`` is the declarative base shared with other SQLAlchemy models
in this package. Import it from here, not from a separate base module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Text,
    distinct, func, select, update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


# Column type aliases. `UUID_DB` is kept as the name for minimum churn
# in the existing Column definitions below — it's now a thin shim over
# Postgres's native UUID type. `JSON_DB` is a similar shim over JSONB.
UUID_DB = lambda: PGUUID(as_uuid=True)
JSON_DB = JSONB

Base = declarative_base()
# =====================================================================
# Tables
# =====================================================================

class DossierRow(Base):
    __tablename__ = "dossiers"

    id = Column(UUID_DB(), primary_key=True)
    workflow = Column(Text, nullable=False)
    cached_status = Column(Text, nullable=True)  # denormalized, updated per activity
    eligible_activities = Column(Text, nullable=True)  # JSON list of dicts (name + optional exempted_by_exception), updated per activity
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    activities = relationship("ActivityRow", back_populates="dossier", order_by="ActivityRow.started_at")
    entities = relationship("EntityRow", back_populates="dossier")


class ActivityRow(Base):
    __tablename__ = "activities"

    id = Column(UUID_DB(), primary_key=True)
    dossier_id = Column(UUID_DB(), ForeignKey("dossiers.id"), nullable=False)
    type = Column(Text, nullable=False)
    # Split `informed_by` into two typed columns with disjoint semantics:
    # - informed_by_activity_id: local (same-dossier) activity UUID
    # - informed_by_uri:         full IRI, used for cross-dossier references
    # At most one of the two is set per row (enforced by check constraint).
    # Readers should use the `informed_by` property for the stringified
    # value when they don't care about the discriminator, or access the
    # columns directly when they need the type back. There's no single
    # `informed_by: Text` column any more — the column was removed
    # because every reader had to split on "is it a UUID or a URI?"
    # anyway, which meant stringly-typed conditionals scattered
    # everywhere. Split columns push that decision to the writers
    # (one place) and let readers stay typed.
    informed_by_activity_id = Column(UUID_DB(), nullable=True)
    informed_by_uri = Column(Text, nullable=True)
    computed_status = Column(Text, nullable=True)  # stored when handler computes status
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dossier = relationship("DossierRow", back_populates="activities")
    associations = relationship("AssociationRow", back_populates="activity")
    used_entities = relationship("UsedRow", back_populates="activity")

    @property
    def informed_by(self) -> str | None:
        """Display form of the informant. Returns the URI for
        cross-dossier references, the stringified UUID for local
        references, or None. Back-compat read shim for call sites
        that just want the old behaviour."""
        if self.informed_by_uri is not None:
            return self.informed_by_uri
        if self.informed_by_activity_id is not None:
            return str(self.informed_by_activity_id)
        return None

    __table_args__ = (
        Index("ix_activities_dossier_id", "dossier_id"),
        Index("ix_activities_type", "type"),
        Index("ix_activities_dossier_type", "dossier_id", "type"),
        CheckConstraint(
            "(informed_by_activity_id IS NULL) OR (informed_by_uri IS NULL)",
            name="ck_activities_informed_by_one_of",
        ),
    )


class AssociationRow(Base):
    __tablename__ = "associations"

    id = Column(UUID_DB(), primary_key=True)
    activity_id = Column(UUID_DB(), ForeignKey("activities.id"), nullable=False)
    agent_id = Column(Text, nullable=False)
    agent_name = Column(Text, nullable=True)
    agent_type = Column(Text, nullable=True)
    role = Column(Text, nullable=False)  # functional role
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    activity = relationship("ActivityRow", back_populates="associations")

    __table_args__ = (
        Index("ix_associations_activity_id", "activity_id"),
    )


class EntityRow(Base):
    __tablename__ = "entities"

    id = Column(UUID_DB(), primary_key=True)  # version UUID
    entity_id = Column(UUID_DB(), nullable=False)  # logical entity UUID
    dossier_id = Column(UUID_DB(), ForeignKey("dossiers.id"), nullable=False)
    type = Column(Text, nullable=False)
    generated_by = Column(UUID_DB(), ForeignKey("activities.id"), nullable=True)
    derived_from = Column(UUID_DB(), ForeignKey("entities.id"), nullable=True)
    attributed_to = Column(Text, nullable=True)
    content = Column(JSON_DB, nullable=True)
    schema_version = Column(Text, nullable=True)  # e.g. "v1", "v2"; NULL = unversioned/legacy
    tombstoned_by = Column(UUID_DB(), ForeignKey("activities.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dossier = relationship("DossierRow", back_populates="entities")

    __table_args__ = (
        Index("ix_entities_dossier_id", "dossier_id"),
        Index("ix_entities_entity_id", "entity_id"),
        Index("ix_entities_type", "type"),
        Index("ix_entities_dossier_type", "dossier_id", "type"),
        Index("ix_entities_dossier_type_created", "dossier_id", "type", "created_at"),
        Index("ix_entities_dossier_entity", "dossier_id", "entity_id"),
        Index("ix_entities_generated_by", "generated_by"),
    )


class UsedRow(Base):
    __tablename__ = "used"

    activity_id = Column(UUID_DB(), ForeignKey("activities.id"), primary_key=True)
    entity_id = Column(UUID_DB(), ForeignKey("entities.id"), primary_key=True)

    activity = relationship("ActivityRow", back_populates="used_entities")


class RelationRow(Base):
    """Generic activity→entity relation under a named type.

    Used for PROV-style annotations beyond `used` and `wasGeneratedBy` — e.g.
    `oe:neemtAkteVan` ("takes note of") for explicitly acknowledging newer
    entity versions the activity chose not to act on. Plugins can register
    their own relation types; the engine stores and returns them uniformly
    but delegates validation to plugin-registered validators."""
    __tablename__ = "activity_relations"

    activity_id = Column(UUID_DB(), ForeignKey("activities.id"), primary_key=True)
    entity_id = Column(UUID_DB(), ForeignKey("entities.id"), primary_key=True)
    relation_type = Column(Text, primary_key=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_relations_activity", "activity_id"),
        Index("ix_relations_entity", "entity_id"),
        Index("ix_relations_type", "relation_type"),
    )


class AgentRow(Base):
    __tablename__ = "agents"

    id = Column(Text, primary_key=True)
    type = Column(Text, nullable=False)
    name = Column(Text, nullable=True)
    uri = Column(Text, nullable=True)  # canonical external IRI for this agent
    properties = Column(JSON_DB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class DomainRelationRow(Base):
    """A semantic relationship between two things (entity→entity,
    entity→URI, dossier→dossier, etc.) established by an activity.

    Distinct from `activity_relations` (process-control edges like
    oe:neemtAkteVan where the activity is one end of the relation).
    Here neither endpoint is the activity — the activity is the
    *provenance* of the relation (who established it and when).

    Superseded relations stay in the table for history — they're
    never hard-deleted. Active relations have superseded_at IS NULL.
    """
    __tablename__ = "domain_relations"

    id = Column(UUID_DB(), primary_key=True, default=uuid.uuid4)
    dossier_id = Column(UUID_DB(), ForeignKey("dossiers.id"), nullable=False)
    relation_type = Column(Text, nullable=False)
    from_ref = Column(Text, nullable=False)
    to_ref = Column(Text, nullable=False)
    created_by_activity_id = Column(UUID_DB(), ForeignKey("activities.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    superseded_by_activity_id = Column(UUID_DB(), ForeignKey("activities.id"), nullable=True)
    superseded_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_domain_rel_dossier", "dossier_id"),
        Index("ix_domain_rel_type", "relation_type"),
        Index("ix_domain_rel_from", "from_ref"),
        Index("ix_domain_rel_to", "to_ref"),
        Index("ix_domain_rel_active", "dossier_id", "superseded_at"),
    )

