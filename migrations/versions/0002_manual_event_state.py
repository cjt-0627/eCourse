"""manual event handled state and revision

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("manual_events")}
    with op.batch_alter_table("manual_events") as batch:
        if "handled_at" not in existing:
            batch.add_column(sa.Column("handled_at", sa.DateTime(), nullable=True))
        if "revision" not in existing:
            batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    with op.batch_alter_table("manual_events") as batch:
        batch.drop_column("revision")
        batch.drop_column("handled_at")
