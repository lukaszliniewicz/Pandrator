"""Initial authoritative web schema."""

from alembic import op

from pandrator.web.models import Base

revision = "0001_web_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())

