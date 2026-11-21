"""persist live sync progress on sync_jobs"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "sync_jobs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("sync_jobs")}
    if "progress" not in existing:
        with op.batch_alter_table("sync_jobs") as batch:
            batch.add_column(sa.Column("progress", sa.Text(), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "sync_jobs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("sync_jobs")}
    if "progress" in existing:
        with op.batch_alter_table("sync_jobs") as batch:
            batch.drop_column("progress")
