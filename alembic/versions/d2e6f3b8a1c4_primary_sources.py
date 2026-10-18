"""primary sources: filings, movement_filings, scorer v2 fields

Revision ID: d2e6f3b8a1c4
Revises: c5d8e2a7f9b1
Create Date: 2026-10-18 18:00:00.000000

Additive: two new tables and nullable columns. Downgrade drops them; the
filings can be fetched again from EDGAR.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd2e6f3b8a1c4'
down_revision = 'c5d8e2a7f9b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'filings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticker_id', sa.Integer(), nullable=False),
        sa.Column('cik', sa.Integer(), nullable=False),
        sa.Column('accession', sa.String(length=32), nullable=False),
        sa.Column('form', sa.String(length=32), nullable=False),
        sa.Column('filing_date', sa.Date(), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('items', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('primary_document', sa.String(length=255), nullable=True),
        sa.Column('url', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['ticker_id'], ['tickers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('accession'),
    )
    op.create_index('ix_filings_ticker_accepted', 'filings', ['ticker_id', 'accepted_at'])
    op.create_table(
        'movement_filings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('movement_id', sa.Integer(), nullable=False),
        sa.Column('filing_id', sa.Integer(), nullable=False),
        sa.Column('timing', sa.String(length=24), nullable=True),
        sa.Column('timing_precision', sa.String(length=8), nullable=True),
        sa.Column('relevance_tier', sa.String(length=16), nullable=True),
        sa.Column('relevance_score', sa.Float(), nullable=True),
        sa.Column('role', sa.String(length=16), nullable=True),
        sa.Column('rationale', sa.Text(), nullable=True),
        sa.Column('scored_by', sa.String(length=64), nullable=True),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['filing_id'], ['filings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['movement_id'], ['movements.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('movement_id', 'filing_id', name='uq_movement_filing'),
    )
    op.create_index('ix_movement_filings_movement_id', 'movement_filings', ['movement_id'])
    op.add_column('tickers', sa.Column('cik', sa.Integer(), nullable=True))
    op.add_column('movements', sa.Column('evidence_abstained', sa.Boolean(), nullable=True))
    op.add_column('movements', sa.Column('abstain_reason', sa.Text(), nullable=True))
    op.add_column('movement_news_links', sa.Column('role', sa.String(length=16), nullable=True))
    op.add_column('movement_news_links', sa.Column('scorer_version', sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column('movement_news_links', 'scorer_version')
    op.drop_column('movement_news_links', 'role')
    op.drop_column('movements', 'abstain_reason')
    op.drop_column('movements', 'evidence_abstained')
    op.drop_column('tickers', 'cik')
    op.drop_index('ix_movement_filings_movement_id', table_name='movement_filings')
    op.drop_table('movement_filings')
    op.drop_index('ix_filings_ticker_accepted', table_name='filings')
    op.drop_table('filings')
