"""spend ledger: usage_events, spend_daily, jobs.hold_reason

Revision ID: e5f9a1c3b7d2
Revises: d4a6b2c8e1f5
Create Date: 2026-10-09 22:00:00.000000

No backfill: spend before this migration was only logged, so the ledger
starts empty and today's spend_daily row is created by the first call.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'e5f9a1c3b7d2'
down_revision = 'd4a6b2c8e1f5'
branch_labels = None
depends_on = None

MONEY = sa.Numeric(14, 6)


def upgrade() -> None:
    op.create_table(
        'usage_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('operation', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_read_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_write_tokens', sa.Integer(), nullable=True),
        sa.Column('cost_usd', MONEY, nullable=False),
        sa.Column('cost_estimated', sa.Boolean(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column(
            'call_class',
            sa.Enum('interactive', 'background', name='call_class', native_enum=False, length=16),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_usage_events_created_at', 'usage_events', ['created_at'], unique=False)
    op.create_index(
        'ix_usage_events_user_id_created_at', 'usage_events', ['user_id', 'created_at'], unique=False
    )
    op.create_table(
        'spend_daily',
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('spent_usd', MONEY, nullable=False),
        sa.Column('background_spent_usd', MONEY, nullable=False),
        sa.Column('reserved_usd', MONEY, nullable=False),
        sa.Column('background_reserved_usd', MONEY, nullable=False),
        sa.Column('alert_logged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('interactive_cap_logged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('background_cap_logged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('day'),
    )
    op.add_column('jobs', sa.Column('hold_reason', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'hold_reason')
    op.drop_table('spend_daily')
    op.drop_index('ix_usage_events_user_id_created_at', table_name='usage_events')
    op.drop_index('ix_usage_events_created_at', table_name='usage_events')
    op.drop_table('usage_events')
