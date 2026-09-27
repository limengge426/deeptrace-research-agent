"""Conversation threads: runs.thread_id links follow-up runs to the thread's first run.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("thread_id", sa.String(32)))
    op.execute("UPDATE runs SET thread_id = id WHERE thread_id IS NULL")  # every existing run starts its own thread
    op.create_index("runs_by_thread", "runs", ["thread_id", "created_at"])


def downgrade() -> None:
    op.drop_index("runs_by_thread", "runs")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("thread_id")
