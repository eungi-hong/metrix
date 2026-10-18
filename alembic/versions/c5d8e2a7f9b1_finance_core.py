"""finance core: decomposition, analytics, detection trigger, link timing

Revision ID: c5d8e2a7f9b1
Revises: a8c4e2f6b1d3
Create Date: 2026-10-18 12:00:00.000000

Additive only. Every new column is nullable or has a server default, so the
previous code keeps working against this schema, and existing rows read as
before: movements qualify (`qualifies` true), links are current
(`superseded_at` NULL), no ticker is a benchmark. Nothing is backfilled here;
the queued `backfill_analytics` job computes the new values for stored
movements (no LLM or news call), and the nightly run keeps them current.

Downgrade drops the columns. Movements kept with `qualifies` false and links
with `superseded_at` set exist only because this version keeps paid work the
previous one deleted; after a downgrade they show as ordinary rows again.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'c5d8e2a7f9b1'
down_revision = 'a8c4e2f6b1d3'
branch_labels = None
depends_on = None

MOVEMENT_FLOATS = (
    'alpha',
    'beta_market',
    'beta_sector',
    'r_squared',
    'sigma_e',
    'market_component',
    'sector_component',
    'expected_return',
    'abnormal_return',
    'sar',
    'gap_return',
    'intraday_return',
    'relative_volume',
    'car_5',
    'car_20',
    'realized_vol',
    'realized_vol_percentile',
)
MOVEMENT_SHORT_STRINGS = (
    'news_window_mode',
    'trigger',
    'decomposition_model',
    'market_benchmark',
    'sector_benchmark',
    'dominant_component',
    'drift_5_label',
    'drift_20_label',
)


def upgrade() -> None:
    op.add_column(
        'tickers',
        sa.Column('is_benchmark', sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    for name in MOVEMENT_SHORT_STRINGS:
        op.add_column('movements', sa.Column(name, sa.String(length=16), nullable=True))
    op.add_column(
        'movements',
        sa.Column('qualifies', sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column('movements', sa.Column('estimation_sessions', sa.Integer(), nullable=True))
    for name in MOVEMENT_FLOATS:
        op.add_column('movements', sa.Column(name, sa.Float(), nullable=True))
    op.add_column('movements', sa.Column('decomposition_warning', sa.Text(), nullable=True))
    op.add_column(
        'movements',
        sa.Column('analytics_computed_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column('movement_news_links', sa.Column('timing', sa.String(length=24), nullable=True))
    op.add_column(
        'movement_news_links', sa.Column('timing_precision', sa.String(length=8), nullable=True)
    )
    op.add_column(
        'movement_news_links',
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('movement_news_links', 'superseded_at')
    op.drop_column('movement_news_links', 'timing_precision')
    op.drop_column('movement_news_links', 'timing')

    op.drop_column('movements', 'analytics_computed_at')
    op.drop_column('movements', 'decomposition_warning')
    for name in reversed(MOVEMENT_FLOATS):
        op.drop_column('movements', name)
    op.drop_column('movements', 'estimation_sessions')
    op.drop_column('movements', 'qualifies')
    for name in reversed(MOVEMENT_SHORT_STRINGS):
        op.drop_column('movements', name)

    op.drop_column('tickers', 'is_benchmark')
