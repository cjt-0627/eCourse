"""remember when the owner finished or skipped the first-run tutorial"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("owners")}
    if "tutorial_completed_at" not in existing:
        with op.batch_alter_table("owners") as batch:
            batch.add_column(sa.Column("tutorial_completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("owners") as batch:
        batch.drop_column("tutorial_completed_at")
