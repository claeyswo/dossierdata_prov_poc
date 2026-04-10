"""
Database models and repository.

All activity/entity tables are append-only. No UPDATEs, no DELETEs.
Status is stored as computed_status on each activity row.
Content is stored as JSON, validated by Pydantic on write.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    Column, DateTime, ForeignKey, Text, Index, JSON,
)
from sqlalchemy.types import TypeDecorator, CHAR
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class UUID_DB(TypeDecorator):
    """Platform-independent UUID type. Uses CHAR(36) on SQLite, works on PostgreSQL too."""
    impl = CHAR(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return uuid_mod.UUID(str(value))
        return value

Base = declarative_base()


# =====================================================================
# Tables
# =====================================================================

class DossierRow(Base):
    __tablename__ = "dossiers"

    id = Column(UUID_DB(), primary_key=True)
    workflow = Column(Text, nullable=False)
    cached_status = Column(Text, nullable=True)  # denormalized, updated per activity
    eligible_activities = Column(Text, nullable=True)  # JSON list of activity names, updated per activity
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    activities = relationship("ActivityRow", back_populates="dossier", order_by="ActivityRow.started_at")
    entities = relationship("EntityRow", back_populates="dossier")


class ActivityRow(Base):
    __tablename__ = "activities"

    id = Column(UUID_DB(), primary_key=True)
    dossier_id = Column(UUID_DB(), ForeignKey("dossiers.id"), nullable=False)
    type = Column(Text, nullable=False)
    informed_by = Column(Text, nullable=True)  # local UUID or cross-dossier URI
    computed_status = Column(Text, nullable=True)  # stored when handler computes status
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dossier = relationship("DossierRow", back_populates="activities")
    associations = relationship("AssociationRow", back_populates="activity")
    used_entities = relationship("UsedRow", back_populates="activity")

    __table_args__ = (
        Index("ix_activities_dossier_id", "dossier_id"),
        Index("ix_activities_type", "type"),
        Index("ix_activities_dossier_type", "dossier_id", "type"),
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
    content = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dossier = relationship("DossierRow", back_populates="entities")

    __table_args__ = (
        Index("ix_entities_dossier_id", "dossier_id"),
        Index("ix_entities_entity_id", "entity_id"),
        Index("ix_entities_type", "type"),
        Index("ix_entities_dossier_type", "dossier_id", "type"),
        Index("ix_entities_dossier_type_created", "dossier_id", "type", "created_at"),
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


class TaskRow(Base):
    __tablename__ = "tasks"

    id = Column(UUID_DB(), primary_key=True)
    dossier_id = Column(UUID_DB(), ForeignKey("dossiers.id"), nullable=False)
    activity_id = Column(UUID_DB(), ForeignKey("activities.id"), nullable=False)
    result_activity_id = Column(UUID_DB(), ForeignKey("activities.id"), nullable=True)
    type = Column(Text, nullable=False)
    config = Column(JSON, nullable=False)
    status = Column(Text, nullable=False, default="scheduled")
    executed_at = Column(DateTime(timezone=True), nullable=True)
    attempt = Column(Text, default="0")
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AgentRow(Base):
    __tablename__ = "agents"

    id = Column(Text, primary_key=True)
    type = Column(Text, nullable=False)
    name = Column(Text, nullable=True)
    properties = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# =====================================================================
# Repository
# =====================================================================

class Repository:
    """Database operations. All writes are INSERTs (append-only)."""

    def __init__(self, session: Session):
        self.session = session

    # --- Dossier ---

    async def get_dossier(self, dossier_id: UUID) -> Optional[DossierRow]:
        result = await self.session.get(DossierRow, dossier_id)
        return result

    async def create_dossier(self, dossier_id: UUID, workflow: str) -> DossierRow:
        row = DossierRow(id=dossier_id, workflow=workflow)
        self.session.add(row)
        return row

    # --- Activity ---

    async def get_activity(self, activity_id: UUID) -> Optional[ActivityRow]:
        return await self.session.get(ActivityRow, activity_id)

    async def get_activities_for_dossier(self, dossier_id: UUID) -> list[ActivityRow]:
        from sqlalchemy import select
        result = await self.session.execute(
            select(ActivityRow)
            .where(ActivityRow.dossier_id == dossier_id)
            .order_by(ActivityRow.started_at)
        )
        return list(result.scalars().all())

    async def create_activity(
        self,
        activity_id: UUID,
        dossier_id: UUID,
        type: str,
        started_at: datetime,
        ended_at: datetime | None = None,
        informed_by: str | None = None,
        computed_status: str | None = None,
    ) -> ActivityRow:
        row = ActivityRow(
            id=activity_id,
            dossier_id=dossier_id,
            type=type,
            started_at=started_at,
            ended_at=ended_at,
            informed_by=informed_by,
            computed_status=computed_status,
        )
        self.session.add(row)
        return row

    # --- Association ---

    async def create_association(
        self,
        association_id: UUID,
        activity_id: UUID,
        agent_id: str,
        agent_name: str | None,
        agent_type: str | None,
        role: str,
    ) -> AssociationRow:
        row = AssociationRow(
            id=association_id,
            activity_id=activity_id,
            agent_id=agent_id,
            agent_name=agent_name,
            agent_type=agent_type,
            role=role,
        )
        self.session.add(row)
        return row

    # --- Entity ---

    async def get_entity(self, version_id: UUID) -> Optional[EntityRow]:
        return await self.session.get(EntityRow, version_id)

    async def get_singleton_entity(
        self, dossier_id: UUID, entity_type: str
    ) -> Optional[EntityRow]:
        """Return the latest (most recently created) entity of `entity_type`
        in the dossier. Intended for singleton-cardinality types — callers
        expecting a unique entity per type per dossier.

        NOTE: this method does NOT enforce the singleton invariant itself;
        cardinality enforcement happens at the engine layer via
        `plugin.cardinality_of(entity_type)`. See phase 1b."""
        from sqlalchemy import select
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
            .order_by(EntityRow.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_entity_by_id(
        self, dossier_id: UUID, entity_id: UUID
    ) -> Optional[EntityRow]:
        """Return the newest version row for a specific logical entity_id,
        or None if no versions of this entity exist in the dossier."""
        from sqlalchemy import select
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.entity_id == entity_id)
            .order_by(EntityRow.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_all_latest_entities(self, dossier_id: UUID) -> list[EntityRow]:
        from sqlalchemy import select, func, distinct
        # Get the latest version of each logical entity
        subq = (
            select(
                EntityRow.entity_id,
                func.max(EntityRow.created_at).label("max_created")
            )
            .where(EntityRow.dossier_id == dossier_id)
            .group_by(EntityRow.entity_id)
            .subquery()
        )
        result = await self.session.execute(
            select(EntityRow)
            .join(subq, (EntityRow.entity_id == subq.c.entity_id) & (EntityRow.created_at == subq.c.max_created))
            .where(EntityRow.dossier_id == dossier_id)
        )
        return list(result.scalars().all())

    async def get_entities_by_type(self, dossier_id: UUID, entity_type: str) -> list[EntityRow]:
        from sqlalchemy import select
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
            .order_by(EntityRow.created_at)
        )
        return list(result.scalars().all())

    async def get_entities_by_type_latest(
        self, dossier_id: UUID, entity_type: str
    ) -> list[EntityRow]:
        """Return the latest version of each distinct logical entity of this
        type in the dossier. For singleton types the list has at most one
        element. For multi-cardinality types, one element per entity_id."""
        from sqlalchemy import select, func
        # Subquery: max(created_at) per entity_id for this type
        subq = (
            select(
                EntityRow.entity_id,
                func.max(EntityRow.created_at).label("max_created"),
            )
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
            .group_by(EntityRow.entity_id)
            .subquery()
        )
        result = await self.session.execute(
            select(EntityRow)
            .join(
                subq,
                (EntityRow.entity_id == subq.c.entity_id)
                & (EntityRow.created_at == subq.c.max_created),
            )
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
            .order_by(EntityRow.created_at)
        )
        return list(result.scalars().all())

    async def get_entity_versions(self, dossier_id: UUID, entity_id: UUID) -> list[EntityRow]:
        """Get all versions of a specific logical entity, ordered by creation time."""
        from sqlalchemy import select
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.entity_id == entity_id)
            .order_by(EntityRow.created_at)
        )
        return list(result.scalars().all())

    async def entity_type_exists(self, dossier_id: UUID, entity_type: str) -> bool:
        from sqlalchemy import select, func
        result = await self.session.execute(
            select(func.count())
            .select_from(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
        )
        return result.scalar() > 0

    async def create_entity(
        self,
        version_id: UUID,
        entity_id: UUID,
        dossier_id: UUID,
        type: str,
        generated_by: UUID | None = None,
        content: dict | None = None,
        derived_from: UUID | None = None,
        attributed_to: str | None = None,
    ) -> EntityRow:
        row = EntityRow(
            id=version_id,
            entity_id=entity_id,
            dossier_id=dossier_id,
            type=type,
            generated_by=generated_by,
            content=content,
            derived_from=derived_from,
            attributed_to=attributed_to,
        )
        self.session.add(row)
        return row

    async def ensure_external_entity(self, dossier_id: UUID, uri: str) -> EntityRow:
        """Ensure an external entity exists for this URI in this dossier. Idempotent."""
        from sqlalchemy import select
        import uuid as uuid_mod
        # Deterministic UUID from URI + dossier_id so the same URI doesn't create duplicates
        entity_id = uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, f"{dossier_id}:{uri}")
        version_id = entity_id  # external entities have one "version"
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.id == version_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing
        return await self.create_entity(
            version_id=version_id,
            entity_id=entity_id,
            dossier_id=dossier_id,
            type="external",
            generated_by=None,
            content={"uri": uri},
        )

    # --- Used ---

    async def create_used(self, activity_id: UUID, entity_version_id: UUID):
        row = UsedRow(activity_id=activity_id, entity_id=entity_version_id)
        self.session.add(row)

    async def get_used_entity_ids_for_activity(self, activity_id: UUID) -> set[UUID]:
        """Get all entity version IDs used by an activity."""
        from sqlalchemy import select
        result = await self.session.execute(
            select(UsedRow.entity_id).where(UsedRow.activity_id == activity_id)
        )
        return {row[0] for row in result.all()}

    # --- Relations (generic activity→entity edges beyond used/generated) ---

    async def create_relation(
        self,
        activity_id: UUID,
        entity_version_id: UUID,
        relation_type: str,
    ):
        """Record an activity→entity relation under a named type. Idempotent
        at the (activity, entity, type) level: inserting the same triple
        twice is a no-op (caller should avoid it but we don't enforce it
        here beyond the PK constraint)."""
        row = RelationRow(
            activity_id=activity_id,
            entity_id=entity_version_id,
            relation_type=relation_type,
        )
        self.session.add(row)

    async def get_relations_for_activity(
        self, activity_id: UUID
    ) -> list[RelationRow]:
        """Return every relation row attached to this activity."""
        from sqlalchemy import select
        result = await self.session.execute(
            select(RelationRow).where(RelationRow.activity_id == activity_id)
        )
        return list(result.scalars().all())

    # --- Agent ---

    async def ensure_agent(self, agent_id: str, agent_type: str, name: str | None, properties: dict | None):
        existing = await self.session.get(AgentRow, agent_id)
        if existing:
            existing.name = name
            existing.properties = properties
            existing.updated_at = datetime.now(timezone.utc)
        else:
            row = AgentRow(id=agent_id, type=agent_type, name=name, properties=properties)
            self.session.add(row)

    # --- Task ---

    async def create_task(
        self,
        task_id: UUID,
        dossier_id: UUID,
        activity_id: UUID,
        type: str,
        config: dict,
    ) -> TaskRow:
        row = TaskRow(
            id=task_id,
            dossier_id=dossier_id,
            activity_id=activity_id,
            type=type,
            config=config,
        )
        self.session.add(row)
        return row
