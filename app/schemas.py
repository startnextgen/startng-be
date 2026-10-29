from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

Hex = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str | None = None
    remember: bool = False  # stay signed in


class LoginIn(BaseModel):
    email: EmailStr
    password: str
    remember: bool = False  # stay signed in


class GoogleIn(BaseModel):
    id_token: str
    remember: bool = False  # stay signed in


class CharacterStyle(BaseModel):
    # The look games build against. Colours are #rrggbb.
    primaryColor: str = Hex
    secondaryColor: str = Hex
    skinColor: str = Hex
    hairColor: str = Hex
    body: Literal["female", "male"] = "female"
    hair: Literal["short", "afro", "braids", "bun", "ponytail", "dreads", "bald"]
    hat: Literal["none", "cap", "beanie", "tophat", "crown"]
    accessory: Literal["none", "glasses", "earrings", "beard", "scarf", "backpack"]


class Character(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    style: CharacterStyle


class UserOut(BaseModel):
    id: int
    email: str
    name: str | None = None
    username: str | None = None
    character: Character | None = None

    model_config = {"from_attributes": True}


class UsernameIn(BaseModel):
    username: str


class UsernameCheck(BaseModel):
    username: str
    available: bool
    reason: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class PublishIn(BaseModel):
    repo_url: str = Field(max_length=300)
    # The game's name and address (<name>.start.ng). Defaults to the repo name.
    name: str | None = Field(default=None, max_length=60)
    branch: str | None = Field(default=None, max_length=100)


class ProblemOut(BaseModel):
    """Something start.ng found while checking a game. code is for scripts, message for people."""

    code: str
    message: str
    file: str | None = None


class CheckOut(BaseModel):
    """POST /games/check: the game would go live like this."""

    ok: bool = True
    name: str
    url: str
    repo_url: str
    update: bool  # true when the repo is already live: publishing updates that game
    folder: str
    files: int
    size_bytes: int
    skipped: list[str]
    warnings: list[ProblemOut] = []


class UpdateProblem(BaseModel):
    """Why the latest push to a live game's repo didn't go live (the game kept its last version)."""

    commit: str
    at: datetime
    detail: str
    errors: list[ProblemOut] = []


class PublishOut(BaseModel):
    name: str
    url: str
    repo_url: str
    created: bool  # false when an existing game was updated
    folder: str  # where in the repo the game was found ("" = the top)
    files: int
    size_bytes: int
    skipped: list[str]  # files left out because the games host doesn't serve their type
    warnings: list[ProblemOut] = []  # things worth fixing that didn't stop the game going live
    # Why it isn't on start.ng's lists yet, if it isn't: "in_review" (an admin looks at every new game first; its
    # address works meanwhile). None: listed.
    hidden: str | None = None


class PendingOut(BaseModel):
    """POST /games/publish without signing in, for a repo that isn't live yet."""

    status: str = "pending"
    name: str
    repo_url: str
    approve_url: str  # open it, sign in with start.ng, approve: the game goes live on your account
    expires_at: datetime
    folder: str
    files: int
    size_bytes: int
    skipped: list[str]
    warnings: list[ProblemOut] = []
    message: str


class PendingInfo(BaseModel):
    """What the approve page shows."""

    name: str
    repo_url: str
    expires_at: datetime
    available: bool  # false if the name was taken since; approving would fail


class GameOut(BaseModel):
    name: str
    url: str
    repo_url: str
    by: str
    updated_at: datetime
    cover_url: str | None = None  # the card picture; None means show the letter tile
    description: str | None = None  # from the game's startng.json; None means the page writes its own
    # Only in /games/mine: the latest push didn't go live, and why. None when it's all fine.
    update_problem: UpdateProblem | None = None
    # Only in /games/mine: how start.ng is keeping the game up to date from its repo.
    checked_at: datetime | None = None  # when the repo was last checked for a new push (None: not yet)
    live_commit: str | None = None  # the first 7 characters of the commit that's live (None: not known yet)
    updates_paused_until: datetime | None = None  # set while GitHub's limit has paused all checking
    # The game's leaderboard columns, or None when the game has no leaderboard (no startng.json, no scores)
    leaderboard: list[dict] | None = None
    # How many people opened it from start.ng in the last 7 days (the homepage shows the most played)
    plays: int = 0
    # Only in /games/mine: why it's hidden from start.ng ("reported", "flagged", "blocked"; app/admin.py), or None
    hidden: str | None = None
    # Whether the creator has published more than 5 high-quality games
    is_verified: bool = False


# Highest score a game can save; anything above is treated as a bad request.
MAX_SCORE = 1_000_000_000


class ScoreIn(BaseModel):
    # A plain score, for games without a startng.json ...
    score: int | None = Field(default=None, ge=0, le=MAX_SCORE)
    # ... or the game's own stats, exactly as its startng.json lists them: {"stage": 8, "kills": 100}
    stats: dict[str, Any] | None = None  # checked exactly by app/leaderboard.check_stats (numbers only)


class ScoreOut(BaseModel):
    best: int  # the player's best score in this game, after this one
    improved: bool  # true when this score is their new best
    rank: int  # their place on the game's leaderboard
    stats: dict[str, int | None]  # the best run's stats (games with a startng.json)
    # Awards this run just earned (items and badges). Only present while the "progress" feature is on.
    unlocked: list[dict[str, str]] | None = None


class LeaderboardEntry(BaseModel):
    rank: int
    player_id: int  # lets a game highlight the signed-in player's row
    name: str  # the player's character name
    color: str | None  # their character's top colour, for a dot next to the name
    score: int  # the first stat (the plain score for games without a startng.json)
    stats: dict[str, int | None]  # every stat, keyed like the game's startng.json


class PlayerOut(BaseModel):
    """What a game knows about its player: never the email."""

    id: int
    name: str
    style: CharacterStyle | None = None


class GameSessionOut(BaseModel):
    session: str  # send as "Authorization: Bearer <session>" to this game's player API
    expires_in: int  # seconds
    player: PlayerOut


# ---- Indie Spotlight: games by independent creators, hosted elsewhere and listed on start.ng


class IndieGameIn(BaseModel):
    name: str
    url: str
    description: str
    cover: str  # the picture as a data URL: data:image/png;base64,... (PNG, JPEG or WebP, 2 MB or less)
    categories: list[str] = []  # up to 5 of start.ng's categories (GET /tags/choices); used only while the tags feature is on
    other_category: str | None = None  # a category of the creator's own, when none fit: an admin reads it


class IndieGameOut(BaseModel):
    id: int
    name: str
    url: str
    description: str
    by: str
    cover_path: str  # served by this API, e.g. /indie/3/cover.webp
    created_at: datetime
    mine: bool = False  # the signed-in user submitted it (they can remove it)
    plays: int = 0  # opened from start.ng in the last 7 days
    categories: list[str] = []  # the names of its categories; filled on the game's own page (GET /indie/{id}), not in the list


# ---- Daily tournaments (app/tournaments.py)


class TournamentRun(BaseModel):
    """One player's best run on a tournament game."""

    rank: int
    player_id: int
    name: str
    color: str | None
    stats: dict[str, int | None]
    points: int  # what this place is worth (10, 7, 5, 3, then 1)


class TournamentGameOut(BaseModel):
    name: str
    url: str
    cover_url: str | None = None
    description: str | None = None
    leaderboard: list[dict]  # the game's columns, for showing the runs
    players: int  # entered players who have a run on it
    top: list[TournamentRun]  # the best runs so far (top 3)


class TournamentStanding(BaseModel):
    place: int
    player_id: int
    name: str
    color: str | None
    points: int
    firsts: int  # games they're first in


class TournamentOut(BaseModel):
    id: int
    starts_at: datetime
    ends_at: datetime
    finished: bool
    games: list[TournamentGameOut]
    entrants: int
    entered: bool = False  # the signed-in player has entered (always false when signed out)
    standings: list[TournamentStanding]  # the day's points so far (top 20); final once finished
    me: TournamentStanding | None = None  # the signed-in player's own row, when they have points
    winner: TournamentStanding | None = None  # once finished
    # When the next tournament starts, for the countdown. Its games stay hidden from players until then.
    next_starts_at: datetime | None = None


class TournamentSummary(BaseModel):
    """A finished tournament, for the list of past winners."""

    id: int
    starts_at: datetime
    ends_at: datetime
    games: list[str]
    winner: TournamentStanding | None
    podium: list[TournamentStanding]  # the top 3


class OverallEntry(BaseModel):
    rank: int
    player_id: int
    name: str
    color: str | None
    points: int
    wins: int  # days won
    firsts: int  # tournament games won


# ---- Reports and the admin page (app/admin.py, app/routers/admin.py)

# Why a player reports a game, with the words the website shows
REPORT_REASONS = {
    "broken": "It's broken or buggy",
    "obscene": "Obscene or sexual content",
    "offensive": "Hateful or offensive",
    "harmful": "Scam, malware or phishing",
    "stolen": "It copies someone else's game",
    "other": "Something else",
}
ReportReason = Literal["broken", "obscene", "offensive", "harmful", "stolen", "other"]


class ReportIn(BaseModel):
    reason: ReportReason
    details: str | None = Field(default=None, max_length=1000)  # needed when the reason is "other"


class ReportOut(BaseModel):
    id: int
    game: str
    reason: str
    details: str | None
    by_admin: bool
    reporter_id: int | None
    reporter: str | None  # their name or email, for admins
    status: str
    created_at: datetime
    closed_at: datetime | None
    closed_by: str | None
    note: str | None


class AdminMe(BaseModel):
    email: str
    is_super: bool
    role: str | None
    permissions: list[str]


class Permission(BaseModel):
    key: str
    label: str


class RoleIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    permissions: list[str]


class RoleOut(BaseModel):
    id: int
    name: str
    permissions: list[str]
    admins: int  # how many admins have it


class AdminIn(BaseModel):
    email: EmailStr
    role_id: int | None = None


class AdminRoleIn(BaseModel):
    role_id: int | None


class AdminOut(BaseModel):
    id: int | None  # None for super admins (from settings, not the table)
    email: str
    is_super: bool
    role_id: int | None
    role: str | None
    added_by: str | None
    created_at: datetime | None
    signed_up: bool  # they've signed in to start.ng with Google at least once


class FlagIn(BaseModel):
    reason: ReportReason
    details: str | None = Field(default=None, max_length=1000)


class CloseIn(BaseModel):
    outcome: Literal["approved", "dismissed"]  # approved hides the game
    note: str | None = Field(default=None, max_length=1000)


class RestoreIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class ContactIn(BaseModel):
    issue: str = Field(min_length=3, max_length=2000)
    game_name: str | None = None
    report_id: int | None = None


class ContactOut(BaseModel):
    id: int
    issue: str
    game_name: str | None
    report_id: int | None
    admin_email: str
    created_at: datetime


class DeveloperOut(BaseModel):
    id: int
    email: str
    name: str | None
    blocked_at: datetime | None
    games: list[str]
    contacts: list[ContactOut]
    contacts_needed: int  # contacts before they can be blocked or deleted
    can_block: bool


class GameCard(BaseModel):
    """A game as the admin page shows it: a card with its picture."""
    name: str
    url: str
    cover_url: str | None
    by: str
    description: str | None
    hidden: str | None  # why it's hidden from start.ng, or None


class ReviewGameOut(GameCard):
    hidden_at: datetime | None
    developer_id: int
    developer: str
    developer_contacts: int
    open_reports: int  # reports waiting for a decision
    reports: list[ReportOut]


class AdminTournamentOut(BaseModel):
    id: int
    starts_at: datetime
    ends_at: datetime
    started: bool
    games: list[GameCard]


class TournamentGamesIn(BaseModel):
    games: list[str]


class AdminTournamentsOut(BaseModel):
    tournaments: list[AdminTournamentOut]  # the current one, then the next
    eligible: list[GameCard]  # games a tournament can use (they keep a leaderboard, meet the standards, aren't hidden)
    min_games: int
    max_games: int


class LogOut(BaseModel):
    id: int
    actor: str | None  # their email; None for start.ng itself
    kind: str  # player, developer, admin or system
    action: str
    target: str | None
    detail: dict | None
    at: datetime


class LogPage(BaseModel):
    """One page of the activity log: the entries, and how many there are in all for the search."""
    items: list[LogOut]
    total: int
    page: int
    pages: int
    page_size: int


class DayCount(BaseModel):
    day: str
    n: int


class TopGame(BaseModel):
    name: str
    title: str
    plays: int


class AnalyticsOut(BaseModel):
    """How start.ng is doing: the numbers behind the admin overview."""
    players: int
    new_players_7: int
    new_players_30: int
    active_1: int  # players seen in the last day
    active_7: int
    active_30: int
    games: int  # live games
    new_games_7: int
    plays_7: int
    plays_30: int
    plays_per_day: list[DayCount]  # the last 30 days, oldest first, zeros included
    signups_per_day: list[DayCount]
    top_games_7: list[TopGame]


class OverviewOut(BaseModel):
    open_reports: int
    games_waiting: int  # games with reports waiting for a decision
    hidden_games: int
    blocked_developers: int
    admins: int