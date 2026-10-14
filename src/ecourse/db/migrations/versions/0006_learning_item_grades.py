"""persist remote grade labels

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("learning_items")}
    with op.batch_alter_table("learning_items") as batch:
        if "grade_text" not in columns:
            batch.add_column(sa.Column("grade_text", sa.Text(), nullable=True))
        if "grading_status_text" not in columns:
            batch.add_column(sa.Column("grading_status_text", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("learning_items") as batch:
        batch.drop_column("grading_status_text")
        batch.drop_column("grade_text")
