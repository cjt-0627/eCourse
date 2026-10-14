"""persist the owner's interface language"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("owners")}
    if "locale" not in existing:
        with op.batch_alter_table("owners") as batch:
            batch.add_column(sa.Column("locale", sa.String(length=16), nullable=False, server_default="zh-Hant"))


def downgrade():
    with op.batch_alter_table("owners") as batch:
        batch.drop_column("locale")
