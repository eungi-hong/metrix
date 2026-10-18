"""indexes for symbol search: symbol prefixes, and company names by trigram (Postgres)

Revision ID: f3c9a7d1e5b2
Revises: e8b4c1d6f2a7
Create Date: 2026-10-19 00:30:00.000000

GET /symbols/search matches company names with ILIKE '%...%', which a
pg_trgm GIN index serves. Creating the extension needs a privilege some
managed databases withhold, so both steps are attempted and skipped with a
notice if refused: search still works, by sequential scan over a table of
about 12,000 rows. Nothing on SQLite.
"""

from __future__ import annotations

from alembic import op

revision = 'f3c9a7d1e5b2'
down_revision = 'e8b4c1d6f2a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Symbol prefixes (LIKE 'NV%'): a pattern-ops btree, which needs no
    # extension. With it both halves of the search's OR are indexable.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_listed_symbols_symbol_pattern ON listed_symbols (symbol text_pattern_ops)"
    )
    op.execute(
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
        EXCEPTION WHEN insufficient_privilege OR feature_not_supported OR undefined_file THEN
            RAISE NOTICE 'pg_trgm unavailable (%); symbol search runs without a trigram index', SQLERRM;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
                CREATE INDEX IF NOT EXISTS ix_listed_symbols_name_trgm
                    ON listed_symbols USING gin (lower(name) gin_trgm_ops);
                CREATE INDEX IF NOT EXISTS ix_tickers_company_name_trgm
                    ON tickers USING gin (lower(company_name) gin_trgm_ops);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_tickers_company_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_listed_symbols_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_listed_symbols_symbol_pattern")
    # The extension is left in place: other objects may use it.
