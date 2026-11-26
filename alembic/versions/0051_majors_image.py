"""majors: a picture for the homepage card (majors.image_url)

Revision ID: 0051
Revises: 0050
(The open jams PR also numbers itself 0051: whichever merges second renumbers and points at the newest.)
"""
from alembic import op
import sqlalchemy as sa

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("majors", sa.Column("image_url", sa.String(300), nullable=True))


def downgrade() -> None:
    op.drop_column("majors", "image_url")
