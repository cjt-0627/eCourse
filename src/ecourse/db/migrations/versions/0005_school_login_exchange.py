"""school credential login exchanges"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # Never batch-rebuild owners on SQLite. With foreign_keys=ON, dropping the
    # temporary parent during Alembic's batch operation cascades through every
    # owner child table. Legacy NOT NULL schemas use an unusable auth sentinel.
    credential_columns = {column["name"] for column in inspector.get_columns("credential_secrets")}
    if "verified_at" not in credential_columns:
        with op.batch_alter_table("credential_secrets") as batch:
            batch.add_column(sa.Column("verified_at", sa.DateTime(), nullable=True))
        op.execute("UPDATE credential_secrets SET verified_at = CURRENT_TIMESTAMP")
    if "school_login_exchanges" in inspector.get_table_names():
        exchange_columns = {column["name"] for column in inspector.get_columns("school_login_exchanges")}
        if "credential_id" not in exchange_columns:
            with op.batch_alter_table("school_login_exchanges") as batch:
                batch.add_column(sa.Column("credential_id", sa.String(length=36), nullable=True))
                batch.create_foreign_key("fk_school_login_exchange_credential", "credential_secrets", ["credential_id"], ["id"], ondelete="CASCADE")
        return
    op.create_table(
        "school_login_exchanges",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("owners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), sa.ForeignKey("login_attempts.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("credential_id", sa.String(length=36), sa.ForeignKey("credential_secrets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("device_label", sa.String(length=160), nullable=True),
    )
    op.create_index("ix_school_login_exchanges_owner_id", "school_login_exchanges", ["owner_id"])
    op.create_index("ix_school_login_exchanges_expires_at", "school_login_exchanges", ["expires_at"])


def downgrade():
    op.drop_table("school_login_exchanges")
    # Deliberately keep additive credential metadata. Rebuilding either owners
    # or credential_secrets on SQLite can activate child-table cascades.
