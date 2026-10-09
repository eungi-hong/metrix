"""identity: users, api_keys, job_requesters; owners on jobs, conversations, usage

Revision ID: f7b3d5e9a2c4
Revises: e5f9a1c3b7d2
Create Date: 2026-10-10 10:00:00.000000

Existing conversations get no owner (user_id and anonymous_key both null),
which makes them admin-only, and existing jobs no requesters, so only an
admin can see them. Nothing is guessed: there was no identity to recover.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'f7b3d5e9a2c4'
down_revision = 'e5f9a1c3b7d2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column(
            'plan',
            sa.Enum('anonymous', 'free', 'pro', 'internal', name='user_plan', native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('prefix', sa.String(length=16), nullable=False),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('prefix'),
    )
    op.create_index('ix_api_keys_user_id', 'api_keys', ['user_id'], unique=False)
    op.create_table(
        'job_requesters',
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('requester', sa.String(length=80), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('job_id', 'requester'),
    )
    op.add_column('jobs', sa.Column('user_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_jobs_user_id_users', 'jobs', 'users', ['user_id'], ['id'])
    op.add_column('conversations', sa.Column('user_id', sa.Integer(), nullable=True))
    op.add_column('conversations', sa.Column('anonymous_key', sa.String(length=80), nullable=True))
    op.create_foreign_key('fk_conversations_user_id_users', 'conversations', 'users', ['user_id'], ['id'])
    op.create_index(
        'ix_conversations_user_id_created_at', 'conversations', ['user_id', 'created_at'], unique=False
    )
    op.create_foreign_key('fk_usage_events_user_id_users', 'usage_events', 'users', ['user_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_usage_events_user_id_users', 'usage_events', type_='foreignkey')
    op.drop_index('ix_conversations_user_id_created_at', table_name='conversations')
    op.drop_constraint('fk_conversations_user_id_users', 'conversations', type_='foreignkey')
    op.drop_column('conversations', 'anonymous_key')
    op.drop_column('conversations', 'user_id')
    op.drop_constraint('fk_jobs_user_id_users', 'jobs', type_='foreignkey')
    op.drop_column('jobs', 'user_id')
    op.drop_table('job_requesters')
    op.drop_index('ix_api_keys_user_id', table_name='api_keys')
    op.drop_table('api_keys')
    op.drop_table('users')
