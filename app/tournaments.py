"""Daily tournaments.

Every day a few random games (settings.tournament_games, 5) make up a tournament that runs for 24 hours and
ends in the evening (settings.tournament_end_hour, Nigeria time). Players enter by sharing it on X, then
play: while they're entered, every run a game sends to start.ng (POST /games/<name>/scores, the same call as
always, so games need no changes) also counts in the tournament, where only runs inside its 24 hours count.

When it ends, each game's tournament board gives points by place (POINTS, then 1 for everyone else who
played it). The player with the most points wins the day. Points only ever come from tournaments, so old
games can't be farmed, and the overall leaderboard adds them up (all time, or this week).

Nothing runs on a timer: the day's tournament is made the first time anyone asks for it, and a tournament
that has ended is settled (points saved, winner named) the next time anyone looks. Unique constraints make
both safe when two requests do it at once.
"""
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import leaderboard as boards
from . import events
from .activity import record as log_activity
from .config import settings
from .models import Game, Score, Tournament, TournamentEntry, TournamentGame, TournamentPoints, TournamentScore, User

POINTS = [10, 7, 5, 3]  # 1st to 4th on a tournament game; everyone else who played it gets ONE_POINT
ONE_POINT = 1
RECENT_TOURNAMENTS = 3  # a game isn't picked again until this many other tournaments have passed (if there are enough games)
NEVER_PICKED = {"hangout"}  # start.ng's own social space, not a game you win


def now() -> datetime:
    return datetime.now(timezone.utc)


def utc(when: datetime) -> datetime:
    """SQLite drops the zone; everything stored here is UTC."""
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when


def _local_zone() -> timezone:
    return timezone(timedelta(hours=settings.tournament_utc_offset))


def window(at: datetime) -> tuple[datetime, datetime]:
    """The 24 hours of the tournament running at `at`: from the last end-of-day hour (local) to the next."""
    local = utc(at).astimezone(_local_zone())
    start = local.replace(hour=settings.tournament_end_hour, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    start = start.astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def week_start(at: datetime) -> datetime:
    """Monday 00:00 local time of the week `at` is in, in UTC."""
    local = utc(at).astimezone(_local_zone())
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.astimezone(timezone.utc)


# ---- picking games and making the day's tournament


def eligible_games(db: Session) -> list[str]:
    """Games a tournament can use: they keep a leaderboard (a startng.json, or players have sent scores),
    they don't fail the standards (None = published before the standards existed, allowed for now),
    and they aren't the Hangout."""
    has_scores = exists().where(Score.game_name == Game.name)
    rows = db.scalars(
        select(Game.name)
        .where(Game.meets_standards.is_not(False))
        .where(Game.hidden.is_(None))  # reported, flagged or blocked (app/admin.py)
        .where(Game.leaderboard.is_not(None) | has_scores)
        .order_by(Game.name)
    ).all()
    return [name for name in rows if name not in NEVER_PICKED]


def _recently_used(db: Session, before: datetime) -> set[str]:
    recent = select(Tournament.id).where(Tournament.starts_at < before).order_by(Tournament.starts_at.desc()).limit(RECENT_TOURNAMENTS)
    return set(db.scalars(select(TournamentGame.game_name).where(TournamentGame.tournament_id.in_(recent))).all())


def pick_games(db: Session, starts_at: datetime, rng: random.Random | None = None) -> list[str]:
    """settings.tournament_games random games, avoiding the last few tournaments' games when there are enough others."""
    rng = rng or random.SystemRandom()
    games = eligible_games(db)
    used = _recently_used(db, starts_at)
    fresh = [g for g in games if g not in used]
    want = settings.tournament_games
    picked = rng.sample(fresh, min(want, len(fresh)))
    if len(picked) < want:
        reused = [g for g in games if g in used]
        picked += rng.sample(reused, min(want - len(picked), len(reused)))
    return picked


# Features can pause the daily tournament for a while (majors do, so a partner tournament has the stage to itself). Each check
# gets (db, start, end) and says whether anything that pauses it overlaps that time; a moment is start == end.
_pauses: list[Callable[[Session, datetime, datetime], bool]] = []


def pause_when(check: Callable[[Session, datetime, datetime], bool]) -> Callable[[Session, datetime, datetime], bool]:
    """Decorator for a feature's check: while it says yes, there is no daily tournament (none is served, none is made)."""
    _pauses.append(check)
    return check


def paused(db: Session, start: datetime, end: datetime) -> bool:
    return any(check(db, start, end) for check in _pauses)


def paused_throughout(db: Session, start: datetime, end: datetime) -> bool:
    """Paused for the whole window (at its first and its last second): no point making that day's tournament at all.
    A window that is only partly paused is still made; it just isn't served while the pause lasts."""
    last = end - timedelta(seconds=1)
    return paused(db, start, start) and paused(db, last, last)


def running(db: Session, at: datetime | None = None) -> Tournament | None:
    """The tournament running now, if it has been made (never makes one). None while the daily tournament is paused."""
    at = at or now()
    found = db.scalar(select(Tournament).where(Tournament.starts_at <= at, Tournament.ends_at > at))
    return None if found is not None and paused(db, at, at) else found


def current(db: Session, at: datetime | None = None) -> Tournament | None:
    """Today's tournament, made now if it doesn't exist yet. None when fewer than
    settings.tournament_min_games games qualify. Also settles tournaments that have ended."""
    at = at or now()
    settle_due(db, at)
    found = running(db, at)
    if found:
        return found
    starts_at, ends_at = window(at)
    if paused(db, at, at):  # a major is on: no daily tournament until it ends
        return None
    if paused_throughout(db, starts_at, ends_at):
        return None
    games = pick_games(db, starts_at)
    if len(games) < settings.tournament_min_games:
        return None
    t = Tournament(starts_at=starts_at, ends_at=ends_at, games=[TournamentGame(game_name=g, position=i) for i, g in enumerate(games)])
    db.add(t)
    log_activity(db, None, "system", "tournament.created", starts_at.date().isoformat(), games=games)
    try:
        db.commit()
    except IntegrityError:  # another request made it first: use theirs
        db.rollback()
        return running(db, at)
    return t


def ahead(db: Session, at: datetime | None = None) -> list[Tournament]:
    """For admins: the current tournament and the one after it, made now if they don't exist yet, so admins see
    (and can change) the games before players do. Nothing changes for players: the next one stays out of sight
    until it starts. Fewer games than settings.tournament_min_games: what could be made."""
    at = at or now()
    out = []
    t = current(db, at)
    if t:
        out.append(t)
    next_start, next_end = window(window(at)[1])
    nxt = db.scalar(select(Tournament).where(Tournament.starts_at == next_start))
    if nxt is None and not paused_throughout(db, next_start, next_end):
        games = pick_games(db, next_start)
        if len(games) >= settings.tournament_min_games:
            nxt = Tournament(starts_at=next_start, ends_at=next_end,
                             games=[TournamentGame(game_name=g, position=i) for i, g in enumerate(games)])
            db.add(nxt)
            log_activity(db, None, "system", "tournament.created", next_start.date().isoformat(), games=games)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                nxt = db.scalar(select(Tournament).where(Tournament.starts_at == next_start))
    if nxt:
        out.append(nxt)
    return out


def started(t: Tournament, at: datetime | None = None) -> bool:
    return utc(t.starts_at) <= (at or now())


def set_games(db: Session, t: Tournament, names: list[str]) -> None:
    """Replace the games of a tournament that hasn't started (an admin chose them). The caller checks them."""
    t.games.clear()
    db.flush()
    t.games.extend(TournamentGame(game_name=g, position=i) for i, g in enumerate(names))
    db.commit()


def replace_hidden(db: Session, game_name: str, at: datetime | None = None) -> None:
    """A game was hidden (app/admin.py). Tournaments that haven't started get another game in its place. The one that's
    running drops it: it's no longer shown, its runs no longer count, and it gives no points (no new game joins halfway
    through the day, so everyone plays the same games). Finished tournaments keep their results."""
    at = at or now()
    for t in db.scalars(select(Tournament).join(TournamentGame).where(
        TournamentGame.game_name == game_name, Tournament.finished_at.is_(None),
    )).all():
        names = [tg.game_name for tg in t.games if tg.game_name != game_name]
        if utc(t.starts_at) > at:
            spare = [g for g in pick_games(db, utc(t.starts_at)) if g not in names]
            names += spare[:1]
        set_games(db, t, names)


def enter(db: Session, t: Tournament, user: User) -> None:
    """Enter a player (they shared the tournament). Entering twice is fine."""
    if db.scalar(select(TournamentEntry.id).where(TournamentEntry.tournament_id == t.id, TournamentEntry.user_id == user.id)):
        return
    db.add(TournamentEntry(tournament_id=t.id, user_id=user.id))
    log_activity(db, user, "player", "tournament.entered", utc(t.starts_at).date().isoformat())
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def is_entered(db: Session, t: Tournament, user_id: int) -> bool:
    return db.scalar(select(TournamentEntry.id).where(TournamentEntry.tournament_id == t.id, TournamentEntry.user_id == user_id)) is not None


def entrants(db: Session, t: Tournament) -> int:
    return db.scalar(select(func.count()).select_from(TournamentEntry).where(TournamentEntry.tournament_id == t.id)) or 0


# ---- scores


def record(db: Session, game: Game, user: User, stats: dict, schema: list[dict], at: datetime | None = None) -> bool:
    """Count a run in the running tournament, if the game is in it and the player has entered.
    Keeps the player's best run for the game. Returns whether it counted (as a new best or not)."""
    at = at or now()
    t = running(db, at)
    if t is None or t.finished_at is not None or game.name not in {g.game_name for g in t.games}:
        return False
    if not is_entered(db, t, user.id):
        return False
    first = stats[schema[0]["key"]]
    where = (TournamentScore.tournament_id == t.id, TournamentScore.game_name == game.name, TournamentScore.user_id == user.id)
    row = db.scalar(select(TournamentScore).where(*where))
    if row is None:
        db.add(TournamentScore(tournament_id=t.id, game_name=game.name, user_id=user.id, best=first, stats=stats))
        try:
            db.commit()
            return True
        except IntegrityError:  # two first runs at once: compare with the one that won
            db.rollback()
            row = db.scalar(select(TournamentScore).where(*where))
    if boards.is_better(schema, stats, boards.stats_of(schema, row.stats, row.best)):
        row.best, row.stats = first, stats
        db.commit()
    return True


def remove_scores(db: Session, game_name: str, user_id: int) -> None:
    """A game's owner removed a player's score: take it out of tournaments that haven't finished too."""
    unfinished = select(Tournament.id).where(Tournament.finished_at.is_(None))
    db.execute(delete(TournamentScore).where(
        TournamentScore.game_name == game_name, TournamentScore.user_id == user_id, TournamentScore.tournament_id.in_(unfinished),
    ))
    db.commit()


# ---- results


@dataclass
class Standing:
    user: User
    points: int = 0
    firsts: int = 0
    last_at: datetime | None = None  # their latest counted run: ties go to whoever got there first
    place: int = 0


@dataclass
class Results:
    standings: list[Standing] = field(default_factory=list)
    boards: dict[str, list[TournamentScore]] = field(default_factory=dict)  # each game's runs, best first


def points_for(place: int) -> int:
    return POINTS[place - 1] if place <= len(POINTS) else ONE_POINT


def results(db: Session, t: Tournament) -> Results:
    """Each game's board and the day's standings (from the runs so far, or the final ones)."""
    out = Results()
    by_user: dict[int, Standing] = {}
    scores = db.scalars(select(TournamentScore).where(TournamentScore.tournament_id == t.id)).all()
    per_game: dict[str, list[TournamentScore]] = defaultdict(list)
    for s in scores:
        per_game[s.game_name].append(s)
    for tg in t.games:
        schema = tg.game.leaderboard or boards.DEFAULT
        ranked = sorted(per_game.get(tg.game_name, []),
                        key=lambda s: (boards.rank_key(schema, boards.stats_of(schema, s.stats, s.best)), utc(s.updated_at)))
        out.boards[tg.game_name] = ranked
        for place, s in enumerate(ranked, start=1):
            st = by_user.setdefault(s.user_id, Standing(user=s.user))
            st.points += points_for(place)
            st.firsts += place == 1
            when = utc(s.updated_at)
            st.last_at = when if st.last_at is None or when > st.last_at else st.last_at
    out.standings = sorted(by_user.values(), key=lambda st: (-st.points, -st.firsts, st.last_at, st.user.id))
    for place, st in enumerate(out.standings, start=1):
        st.place = place
    return out


def settle(db: Session, t: Tournament, at: datetime | None = None) -> None:
    """Save the points of an ended tournament and name its winner (once)."""
    if t.finished_at is not None or utc(t.ends_at) > (at or now()):
        return
    res = results(db, t)
    for st in res.standings:
        db.add(TournamentPoints(tournament_id=t.id, user_id=st.user.id, points=st.points, firsts=st.firsts, place=st.place))
    t.winner_id = res.standings[0].user.id if res.standings else None
    t.finished_at = at or now()
    log_activity(db, None, "system", "tournament.finished", utc(t.starts_at).date().isoformat(),
           winner=res.standings[0].user.email if res.standings else None, players=len(res.standings))
    try:
        db.commit()
    except IntegrityError:  # settled by another request at the same moment
        db.rollback()
        return
    events.publish("tournament_finished", {"tournament_id": t.id, "winner_id": t.winner_id, "players": len(res.standings)})


def settle_due(db: Session, at: datetime | None = None) -> None:
    at = at or now()
    for t in db.scalars(select(Tournament).where(Tournament.ends_at <= at, Tournament.finished_at.is_(None))).all():
        settle(db, t, at)


def recent(db: Session, limit: int, at: datetime | None = None) -> list[Tournament]:
    """Finished tournaments, newest first."""
    settle_due(db, at)
    return list(db.scalars(
        select(Tournament).where(Tournament.finished_at.is_not(None)).order_by(Tournament.starts_at.desc()).limit(limit)
    ).all())


@dataclass
class OverallRow:
    user: User
    points: int
    wins: int
    firsts: int


def overall(db: Session, period: str, limit: int, at: datetime | None = None) -> list[OverallRow]:
    """Points from every finished tournament (period "all") or the ones that ended this week ("week")."""
    at = at or now()
    settle_due(db, at)
    query = (
        select(
            TournamentPoints.user_id,
            func.sum(TournamentPoints.points).label("points"),
            func.sum(TournamentPoints.firsts).label("firsts"),
            func.count().filter(TournamentPoints.place == 1).label("wins"),
            func.min(Tournament.ends_at).label("since"),
        )
        .join(Tournament, Tournament.id == TournamentPoints.tournament_id)
        .group_by(TournamentPoints.user_id)
    )
    if period == "week":
        query = query.where(Tournament.ends_at >= week_start(at))
    rows = db.execute(query).all()
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([r.user_id for r in rows]))).all()}
    rows = sorted(rows, key=lambda r: (-r.points, -r.wins, -r.firsts, utc(r.since), r.user_id))[:limit]
    return [OverallRow(user=users[r.user_id], points=r.points, wins=r.wins, firsts=r.firsts) for r in rows]
