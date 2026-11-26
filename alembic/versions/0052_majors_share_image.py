"""majors: a picture for link previews (majors.share_image_url)

Revision ID: 0052
Revises: 0051
(The event spotlight draft also numbers itself 0052: whichever merges second renumbers and points at the newest.)
"""
from alembic import op
import sqlalchemy as sa

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("majors", sa.Column("share_image_url", sa.String(300), nullable=True))


def downgrade() -> None:
    op.drop_column("majors", "share_image_url")
