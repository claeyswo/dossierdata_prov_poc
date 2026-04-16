"""eligible_activities_text_to_jsonb

Revision ID: a3c1e7f29b01
Revises: 6226f68ae484
Create Date: 2026-04-16 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3c1e7f29b01'
down_revision: Union[str, Sequence[str], None] = '6226f68ae484'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Convert eligible_activities from Text (JSON-as-string) to native JSONB.

    The column previously held json.dumps()-encoded strings. USING casts
    existing text values to jsonb so no data is lost. NULL values stay NULL.
    """
    op.execute(
        "ALTER TABLE dossiers "
        "ALTER COLUMN eligible_activities "
        "TYPE JSONB USING eligible_activities::jsonb"
    )


def downgrade() -> None:
    """Revert eligible_activities from JSONB back to Text."""
    op.execute(
        "ALTER TABLE dossiers "
        "ALTER COLUMN eligible_activities "
        "TYPE TEXT USING eligible_activities::text"
    )
