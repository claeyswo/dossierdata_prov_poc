"""add_agent_uri_column

Revision ID: 6226f68ae484
Revises: 9d887db892c9
Create Date: 2026-04-15 10:17:02.434090

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6226f68ae484'
down_revision: Union[str, Sequence[str], None] = '9d887db892c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable uri column to agents table.

    Stores the canonical external IRI for each agent. When present,
    the PROV-JSON export uses this IRI as the agent identifier instead
    of the internal dossier-scoped QName."""
    op.add_column('agents', sa.Column('uri', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('agents', 'uri')
