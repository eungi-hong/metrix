"""full-text search over articles (Postgres)

Revision ID: b7e3f1a9c5d2
Revises: a6d2e9c4b8f1
Create Date: 2026-10-19 03:00:00.000000

Ask Metrix's search_evidence tool matches questions against stored
articles with websearch_to_tsquery. A generated, stored tsvector over
title, summary and content (English configuration) keeps itself current
on every insert and update, and a GIN index serves the match. Postgres
only, and not on the models (alembic/env.py ignores it in the drift
check); SQLite falls back to LIKE.
"""

from __future__ import annotations

from alembic import op

revision = 'b7e3f1a9c5d2'
down_revision = 'a6d2e9c4b8f1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS search_vector tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english'::regconfig, coalesce(summary, '')), 'B') ||
            setweight(to_tsvector('english'::regconfig, left(coalesce(content, ''), 20000)), 'C')
        ) STORED
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_news_articles_search ON news_articles USING gin (search_vector)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_news_articles_search")
    op.execute("ALTER TABLE news_articles DROP COLUMN IF EXISTS search_vector")
