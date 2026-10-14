"""credential account blind index"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("credential_secrets")}
    if "account_fingerprint" not in existing:
        with op.batch_alter_table("credential_secrets") as batch:
            batch.add_column(sa.Column("account_fingerprint", sa.String(length=64), nullable=True))
            batch.create_index("ix_credential_secrets_account_fingerprint", ["account_fingerprint"])


def downgrade():
    with op.batch_alter_table("credential_secrets") as batch:
        batch.drop_index("ix_credential_secrets_account_fingerprint")
        batch.drop_column("account_fingerprint")
