"""connection-level course preference generation"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("platform_connections")}
    if "preferences_version" not in existing:
        with op.batch_alter_table("platform_connections") as batch:
            batch.add_column(sa.Column("preferences_version", sa.Integer(), nullable=False, server_default="1"))

def downgrade():
    with op.batch_alter_table("platform_connections") as batch:
        batch.drop_column("preferences_version")
