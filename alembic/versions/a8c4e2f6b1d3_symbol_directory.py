"""symbol directory: listed_symbols

Revision ID: a8c4e2f6b1d3
Revises: f7b3d5e9a2c4
Create Date: 2026-10-10 14:00:00.000000

Created empty. Until the first weekly refresh fills it, SYMBOL_DIRECTORY_MODE
=enforce behaves as warn, so a fresh deploy serves symbols as before.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'a8c4e2f6b1d3'
down_revision = 'f7b3d5e9a2c4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'listed_symbols',
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=400), nullable=True),
        sa.Column('exchange', sa.String(length=8), nullable=True),
        sa.Column('is_etf', sa.Boolean(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('refreshed_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('symbol'),
    )


def downgrade() -> None:
    op.drop_table('listed_symbols')
