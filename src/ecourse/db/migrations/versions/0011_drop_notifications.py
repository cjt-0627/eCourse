"""drop notification, reminder, digest, push and announcement-read tables"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# Children first so foreign keys never block a drop.
TABLES = (
    "push_deliveries",
    "push_subscriptions",
    "notifications",
    "notification_events",
    "notification_dedupe",
    "digest_snapshots",
    "reminder_rules",
    "announcement_reads",
)


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in TABLES:
        if table in existing:
            op.drop_table(table)


def downgrade():
    raise NotImplementedError("notification tables were removed permanently")
