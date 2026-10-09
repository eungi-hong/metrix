"""demand tracking: ticker_demand, tickers.ingest_error_permanent

Revision ID: d4a6b2c8e1f5
Revises: c7d2e8f1a9b3
Create Date: 2026-10-09 20:00:00.000000

Existing failed tickers get ingest_error_permanent = false: whether their old
error was permanent was never recorded. They stay in the universe until their
next ingestion fails and records it, which costs at most one attempt each.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'd4a6b2c8e1f5'
down_revision = 'c7d2e8f1a9b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ticker_demand',
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('popularity', sa.Float(), nullable=False),
        sa.Column('popularity_updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('request_count', sa.Integer(), nullable=False),
        sa.Column('first_requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('symbol'),
    )
    op.create_index(
        op.f('ix_ticker_demand_last_requested_at'), 'ticker_demand', ['last_requested_at'], unique=False
    )
    op.add_column(
        'tickers',
        sa.Column('ingest_error_permanent', sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('tickers', 'ingest_error_permanent')
    op.drop_index(op.f('ix_ticker_demand_last_requested_at'), table_name='ticker_demand')
    op.drop_table('ticker_demand')
