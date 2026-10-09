"""news freshness: PARTIAL status, window close time, attempt count

Revision ID: b3e1c9a4d7f2
Revises: 71f4c36396e8
Create Date: 2026-10-09 12:00:00.000000

`news_status` is a plain VARCHAR(16) with no CHECK constraint (SQLAlchemy only
emits one for a non-native enum when `create_constraint=True`), so the new
'partial' value needs no DDL.

The backfill uses the news-window settings in force when the migration runs,
the same values the application would have computed for these rows. Movements
already marked complete by an enrichment that ran before their window closed
are the rows the old code got wrong; they are moved to partial so the next run
re-enriches them.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.core.config import settings

revision = 'b3e1c9a4d7f2'
down_revision = '71f4c36396e8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'movements',
        sa.Column('news_window_closes_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'movements',
        sa.Column('news_attempts', sa.Integer(), server_default='0', nullable=False),
    )

    # search_window() ends at the last microsecond of (date + days_after), in
    # UTC; the window closes NEWS_WINDOW_GRACE_HOURS after that.
    op.execute(
        sa.text(
            """
            UPDATE movements
            SET news_window_closes_at =
                ((date + CAST(:days_after AS integer) + 1)::timestamp AT TIME ZONE 'UTC')
                - interval '1 microsecond'
                + make_interval(hours => CAST(:grace_hours AS integer))
            """
        ).bindparams(
            days_after=settings.news_window_days_after,
            grace_hours=settings.news_window_grace_hours,
        )
    )
    op.alter_column('movements', 'news_window_closes_at', nullable=False)

    op.execute(
        """
        UPDATE movements
        SET news_status = 'partial'
        WHERE news_status = 'complete'
          AND news_fetched_at < news_window_closes_at
        """
    )
    # The true count is unknown; every enriched row had at least one attempt.
    op.execute("UPDATE movements SET news_attempts = 1 WHERE news_status <> 'pending'")


def downgrade() -> None:
    # The previous code cannot load 'partial'; it treated these rows as complete.
    op.execute("UPDATE movements SET news_status = 'complete' WHERE news_status = 'partial'")
    op.drop_column('movements', 'news_attempts')
    op.drop_column('movements', 'news_window_closes_at')
