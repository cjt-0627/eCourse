"""group cross-platform courses by official course code

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "course_groups" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("course_groups")}
    if "course_code" not in columns:
        with op.batch_alter_table("course_groups") as batch:
            batch.add_column(sa.Column("course_code", sa.String(length=128), nullable=True))
            batch.create_index("ix_course_groups_course_code", ["course_code"], unique=False)
    op.execute("""
        UPDATE course_groups
        SET course_code = (
            SELECT cs.course_code
            FROM course_selections sel
            JOIN course_sources cs ON cs.id = sel.source_id
            WHERE sel.group_id = course_groups.id AND cs.course_code IS NOT NULL
            LIMIT 1
        )
        WHERE course_code IS NULL
    """)


def downgrade():
    if "course_groups" not in sa.inspect(op.get_bind()).get_table_names():
        return
    with op.batch_alter_table("course_groups") as batch:
        batch.drop_index("ix_course_groups_course_code")
        batch.drop_column("course_code")
