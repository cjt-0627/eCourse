"""add encrypted AI provider settings (historical)"""
from alembic import op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    if "ai_settings" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "ai_settings",
            sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("owners.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("api_base", sa.Text(), nullable=False),
            sa.Column("model", sa.String(length=200), nullable=False),
            sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )


def downgrade():
    if "ai_settings" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("ai_settings")
