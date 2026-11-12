from datetime import datetime

from pydantic import BaseModel, Field


class PrizeIn(BaseModel):
    place: int = Field(ge=1, le=20)
    prize: str = Field(min_length=2, max_length=120)


class MajorIn(BaseModel):
    """Everything a major is. This is also the template: save it as JSON, change the words, and it's the next
    collaboration (see templates/collab.json)."""
    slug: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    title: str = Field(min_length=3, max_length=80)
    tagline: str = Field(min_length=3, max_length=160)
    about: str | None = Field(default=None, max_length=1200)
    partner_name: str = Field(min_length=2, max_length=60)
    partner_url: str | None = Field(default=None, max_length=300)
    partner_logo_url: str | None = Field(default=None, max_length=300)
    cta_label: str | None = Field(default=None, max_length=40)
    cta_url: str | None = Field(default=None, max_length=300)
    prizes: list[PrizeIn] = Field(min_length=1, max_length=10)
    games: list[str] = Field(min_length=1, max_length=50)  # service.MAX_GAMES
    starts_at: datetime
    ends_at: datetime


class CopyIn(BaseModel):
    """Use a major as the template for the next one: everything is copied, with a new address and new dates."""
    slug: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    title: str | None = Field(default=None, min_length=3, max_length=80)
    starts_at: datetime
    ends_at: datetime


class RemoveIn(BaseModel):
    note: str = Field(min_length=3, max_length=200)


class PrizeOut(BaseModel):
    place: int
    prize: str


class PartnerOut(BaseModel):
    name: str
    url: str | None
    logo_url: str | None


class RunOut(BaseModel):
    rank: int
    player_id: int
    name: str
    color: str | None
    stats: dict
    points: int


class GameOut(BaseModel):
    name: str
    url: str
    cover_url: str | None
    description: str | None
    leaderboard: list[dict]
    players: int
    top: list[RunOut]
    mine: RunOut | None = None  # the signed-in entrant's own best run here, wherever they stand


class StandingOut(BaseModel):
    place: int
    player_id: int
    name: str
    color: str | None
    points: int
    firsts: int
    games_played: int


class WinnerOut(BaseModel):
    place: int
    prize: str
    player_id: int
    name: str
    color: str | None
    points: int


class SummaryOut(BaseModel):
    """A major on a list."""
    slug: str
    title: str
    tagline: str
    partner: PartnerOut
    prizes: list[PrizeOut]
    starts_at: datetime
    ends_at: datetime
    state: str  # "upcoming", "running" or "ended"
    entrants: int
    games: list[str]


class MajorOut(SummaryOut):
    """A major's own page."""
    about: str | None
    cta_label: str | None
    cta_url: str | None
    published: bool
    entered: bool
    removed: bool  # an admin took this player out of the running
    played: int  # how many of its games the signed-in entrant has a run in
    game_boards: list[GameOut]
    standings: list[StandingOut]
    me: StandingOut | None
    winners: list[WinnerOut]  # empty until it ends


class AdminWinnerOut(WinnerOut):
    email: str  # to send the prize; only admins with majors.manage see it


class AdminEntryOut(BaseModel):
    player_id: int
    name: str
    email: str
    joined_at: datetime
    removed: bool
    removed_note: str | None
    points: int
    place: int | None


class AdminMajorOut(SummaryOut):
    about: str | None
    cta_label: str | None
    cta_url: str | None
    published: bool
    link: str
    winners: list[AdminWinnerOut]
    template: dict  # this major as a MajorIn body, to copy and change for the next collaboration
