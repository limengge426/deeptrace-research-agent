"""Initial schema: runs, events, leases, controls, tool_calls.

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
    )
    op.create_index("events_by_run", "events", ["run_id", "id"])
    op.create_table(
        "leases",
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("token", sa.Integer, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_table(
        "controls",
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.Float, nullable=False),
    )
    op.create_table(
        "tool_calls",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("args", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("finished_at", sa.Float),
    )
    op.create_index("tool_calls_by_run", "tool_calls", ["run_id", "status"])


def downgrade() -> None:
    for table in ("tool_calls", "controls", "leases", "events", "runs"):
        op.drop_table(table)
