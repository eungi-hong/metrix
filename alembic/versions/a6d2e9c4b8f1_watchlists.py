"""watchlists and their holdings

Revision ID: a6d2e9c4b8f1
Revises: f3c9a7d1e5b2
Create Date: 2026-10-19 01:00:00.000000

Additive: two new tables.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'a6d2e9c4b8f1'
down_revision = 'f3c9a7d1e5b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'watchlists',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('anonymous_key', sa.String(length=80), nullable=True),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_watchlists_user_id', 'watchlists', ['user_id'])
    op.create_index('ix_watchlists_anonymous_key', 'watchlists', ['anonymous_key'])
    op.create_table(
        'watchlist_holdings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('watchlist_id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('weight', sa.Float(), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['watchlist_id'], ['watchlists.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('watchlist_id', 'symbol', name='uq_watchlist_symbol'),
    )
    op.create_index('ix_watchlist_holdings_watchlist_id', 'watchlist_holdings', ['watchlist_id'])


def downgrade() -> None:
    op.drop_index('ix_watchlist_holdings_watchlist_id', table_name='watchlist_holdings')
    op.drop_table('watchlist_holdings')
    op.drop_index('ix_watchlists_anonymous_key', table_name='watchlists')
    op.drop_index('ix_watchlists_user_id', table_name='watchlists')
    op.drop_table('watchlists')
