"""majors: partner tournaments with prizes (app/features/majors)

Revision ID: 0050
Revises: 0049
"""
from alembic import op
import sqlalchemy as sa

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "majors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(40), nullable=False, unique=True),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("tagline", sa.String(160), nullable=False),
        sa.Column("about", sa.Text(), nullable=True),
        sa.Column("partner_name", sa.String(60), nullable=False),
        sa.Column("partner_url", sa.String(300), nullable=True),
        sa.Column("partner_logo_url", sa.String(300), nullable=True),
        sa.Column("cta_label", sa.String(40), nullable=True),
        sa.Column("cta_url", sa.String(300), nullable=True),
        sa.Column("prizes", sa.JSON(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_majors_ends_at", "majors", ["ends_at"])
    op.create_table(
        "major_games",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("major_id", sa.Integer(), sa.ForeignKey("majors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("game_name", sa.String(40), sa.ForeignKey("games.name", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("major_id", "game_name", name="uq_major_games"),
    )
    op.create_index("ix_major_games_major_id", "major_games", ["major_id"])
    op.create_index("ix_major_games_game_name", "major_games", ["game_name"])
    op.create_table(
        "major_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("major_id", sa.Integer(), sa.ForeignKey("majors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("removed_note", sa.String(200), nullable=True),
        sa.UniqueConstraint("major_id", "user_id", name="uq_major_entries"),
    )
    op.create_index("ix_major_entries_major_id", "major_entries", ["major_id"])
    op.create_index("ix_major_entries_user_id", "major_entries", ["user_id"])
    op.create_table(
        "major_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("major_id", sa.Integer(), sa.ForeignKey("majors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("game_name", sa.String(40), sa.ForeignKey("games.name", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("best", sa.BigInteger(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("major_id", "game_name", "user_id", name="uq_major_scores"),
    )
    op.create_index("ix_major_scores_major_id", "major_scores", ["major_id"])


def downgrade() -> None:
    op.drop_table("major_scores")
    op.drop_table("major_entries")
    op.drop_table("major_games")
    op.drop_table("majors")
