"""Majors' own tables. Migration: alembic/versions/0050_majors.py."""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...db import Base
from ...models import Game, User


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Major(Base):
    __tablename__ = "majors"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)  # its page: start.ng/majors/<slug>
    title: Mapped[str] = mapped_column(String(80))
    tagline: Mapped[str] = mapped_column(String(160))  # one line under the title
    about: Mapped[str | None] = mapped_column(Text)  # a short paragraph about the partner or the event
    # The partner this is run with. The logo is a picture address (https), shown beside start.ng's.
    partner_name: Mapped[str] = mapped_column(String(60))
    partner_url: Mapped[str | None] = mapped_column(String(300))
    partner_logo_url: Mapped[str | None] = mapped_column(String(300))
    # The button that sends players to the partner ("Get tickets").
    cta_label: Mapped[str | None] = mapped_column(String(40))
    cta_url: Mapped[str | None] = mapped_column(String(300))
    prizes: Mapped[list] = mapped_column(JSON)  # [{"place": 1, "prize": "..."}], best place first
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False)  # a draft only shows to admins
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    games: Mapped[list["MajorGame"]] = relationship(order_by="MajorGame.position", cascade="all, delete-orphan")
    entries: Mapped[list["MajorEntry"]] = relationship(cascade="all, delete-orphan")


class MajorGame(Base):
    __tablename__ = "major_games"
    __table_args__ = (UniqueConstraint("major_id", "game_name", name="uq_major_games"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    major_id: Mapped[int] = mapped_column(ForeignKey("majors.id", ondelete="CASCADE"), index=True)
    game_name: Mapped[str] = mapped_column(ForeignKey("games.name", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)

    game: Mapped[Game] = relationship()


class MajorEntry(Base):
    """A player who entered. An admin can take a player out of the running (cheating, a second account)."""

    __tablename__ = "major_entries"
    __table_args__ = (UniqueConstraint("major_id", "user_id", name="uq_major_entries"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    major_id: Mapped[int] = mapped_column(ForeignKey("majors.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    removed_note: Mapped[str | None] = mapped_column(String(200))

    user: Mapped[User] = relationship()


class MajorScore(Base):
    """An entrant's best run in one of the major's games, inside its window."""

    __tablename__ = "major_scores"
    __table_args__ = (UniqueConstraint("major_id", "game_name", "user_id", name="uq_major_scores"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    major_id: Mapped[int] = mapped_column(ForeignKey("majors.id", ondelete="CASCADE"), index=True)
    game_name: Mapped[str] = mapped_column(ForeignKey("games.name", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    best: Mapped[int] = mapped_column(BigInteger)
    stats: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    user: Mapped[User] = relationship()
