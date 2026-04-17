"""add domain_relations table

Revision ID: a3c1e7d4f890
Revises: 6226f68ae484
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a3c1e7d4f890"
down_revision = "6226f68ae484"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "domain_relations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dossier_id", UUID(as_uuid=True), sa.ForeignKey("dossiers.id"), nullable=False),
        sa.Column("relation_type", sa.Text(), nullable=False),
        sa.Column("from_ref", sa.Text(), nullable=False),
        sa.Column("to_ref", sa.Text(), nullable=False),
        sa.Column("created_by_activity_id", UUID(as_uuid=True), sa.ForeignKey("activities.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("superseded_by_activity_id", UUID(as_uuid=True), sa.ForeignKey("activities.id"), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_domain_rel_dossier", "domain_relations", ["dossier_id"])
    op.create_index("ix_domain_rel_type", "domain_relations", ["relation_type"])
    op.create_index("ix_domain_rel_from", "domain_relations", ["from_ref"])
    op.create_index("ix_domain_rel_to", "domain_relations", ["to_ref"])
    op.create_index("ix_domain_rel_active", "domain_relations", ["dossier_id", "superseded_at"])


def downgrade():
    op.drop_table("domain_relations")
