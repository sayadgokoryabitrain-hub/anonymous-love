"""Initial schema generated from the ORM metadata."""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from database.base import Base
    import database.models  # noqa: F401

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from database.base import Base
    import database.models  # noqa: F401

    Base.metadata.drop_all(bind=op.get_bind())
