"""job queue: jobs and prewarm_runs

Revision ID: c7d2e8f1a9b3
Revises: b3e1c9a4d7f2
Create Date: 2026-10-09 18:00:00.000000

The partial unique index on `jobs.dedupe_key` is what makes enqueue dedupe
safe under concurrency: at most one queued-or-running job per key, while
finished jobs keep their key as history.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c7d2e8f1a9b3'
down_revision = 'b3e1c9a4d7f2'
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
_ACTIVE = sa.text("status IN ('queued', 'running')")


def upgrade() -> None:
    op.create_table(
        'prewarm_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('trading_date', sa.Date(), nullable=False),
        sa.Column(
            'status',
            sa.Enum('running', 'finished', name='prewarm_run_status', native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('universe_size', sa.Integer(), nullable=False),
        sa.Column('movements_found', sa.Integer(), nullable=False),
        sa.Column('enrichments_queued', sa.Integer(), nullable=False),
        sa.Column('enrichments_deferred', sa.Integer(), nullable=False),
        sa.Column('enrichment_budget', sa.Integer(), nullable=False),
        sa.Column('enrichments_used', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('trading_date'),
    )
    op.create_table(
        'jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'kind',
            sa.Enum(
                'ingest_ticker', 'refresh_prices', 'prewarm_sector_macro',
                'enrich_movement', 'schedule_nightly',
                name='job_kind', native_enum=False, length=32,
            ),
            nullable=False,
        ),
        sa.Column('payload', _JSON, nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                'queued', 'running', 'succeeded', 'failed', 'dead',
                name='job_status', native_enum=False, length=16,
            ),
            nullable=False,
        ),
        sa.Column('dedupe_key', sa.String(length=255), nullable=False),
        sa.Column('blocked_by_key', sa.String(length=255), nullable=True),
        sa.Column('run_after', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('max_attempts', sa.Integer(), nullable=False),
        sa.Column('locked_by', sa.String(length=128), nullable=True),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column(
            'source',
            sa.Enum('interactive', 'scheduled', 'followup', name='job_source', native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column('progress', _JSON, nullable=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['prewarm_runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_jobs_claim', 'jobs', ['status', 'priority', 'run_after'], unique=False)
    op.create_index('ix_jobs_dedupe_key', 'jobs', ['dedupe_key'], unique=False)
    op.create_index('ix_jobs_run_id', 'jobs', ['run_id'], unique=False)
    op.create_index(
        'uq_jobs_active_dedupe_key', 'jobs', ['dedupe_key'], unique=True,
        postgresql_where=_ACTIVE, sqlite_where=_ACTIVE,
    )


def downgrade() -> None:
    op.drop_index('uq_jobs_active_dedupe_key', table_name='jobs')
    op.drop_index('ix_jobs_run_id', table_name='jobs')
    op.drop_index('ix_jobs_dedupe_key', table_name='jobs')
    op.drop_index('ix_jobs_claim', table_name='jobs')
    op.drop_table('jobs')
    op.drop_table('prewarm_runs')
