"""ticker data version for the read path's ETag and response cache

Revision ID: e8b4c1d6f2a7
Revises: d2e6f3b8a1c4
Create Date: 2026-10-18 23:00:00.000000

Additive: one column with a server default, so existing rows start at 0.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'e8b4c1d6f2a7'
down_revision = 'd2e6f3b8a1c4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tickers', sa.Column('data_version', sa.BigInteger(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('tickers', 'data_version')
