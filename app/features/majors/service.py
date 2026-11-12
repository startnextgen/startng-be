"""Majors: the rules, and what other features may call.

An admin makes one as a draft (a partner, prizes, 1 to 50 games that have leaderboards, a window of 1 hour to 45
days), looks at it, then publishes it. Players enter on its page; an entrant's runs on its games inside the window
count, and each game gives points by place (the daily tournament's 10/7/5/3/1). Most points wins, and the top
places take the prizes. Standings are worked out when someone looks, so nothing runs on a timer.

Runs arrive through the "score_submitted" event (every score a game sends), so games need no changes.
"""
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ... import events, flags, tournaments
from ... import leaderboard as boards
from ...db import SessionLocal
from ...models import Game, User
from .models import Major, MajorEntry, MajorGame, MajorScore

MAX_GAMES = 50  # the admin picks how many (1 to this); every page view works out all the standings, so keep it sane
MIN_LENGTH = timedelta(hours=1)
MAX_LENGTH = timedelta(days=45)
RESERVED_SLUGS = {"new", "admin", "template", "templates"}
_TEMPLATES = Path(__file__).parent / "templates"


def templates() -> list[dict]:
    """The ready-made majors in templates/*.json, "Blank collaboration" first: [{key, label, note, major}].
    `major` is a MajorIn body (the blank one has placeholders and no games or dates yet). To add one, add a file."""
    out = []
    for path in sorted(_TEMPLATES.glob("*.json"), key=lambda p: (p.stem != "collab", p.stem)):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({"key": path.stem, "label": data["label"], "note": data.get("note"), "major": data["major"]})
    return out


class Refused(Exception):
    """A rule was broken. `code` is for the website, `message` for the person."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def state(m: Major, at: datetime | None = None) -> str:
    at = at or tournaments.now()
    if tournaments.utc(m.ends_at) <= at:
        return "ended"
    return "running" if tournaments.utc(m.starts_at) <= at else "upcoming"


def find(db: Session, slug: str) -> Major | None:
    return db.scalar(select(Major).where(Major.slug == slug))


def listed(db: Session, include_drafts: bool = False) -> list[Major]:
    """Running first (ending soonest), then upcoming (starting soonest), then ended (newest first)."""
    query = select(Major)
    if not include_drafts:
        query = query.where(Major.published.is_(True))
    rows = db.scalars(query).all()
    now = tournaments.now()
    order = {"running": 0, "upcoming": 1, "ended": 2}

    def key(m: Major):
        s = state(m, now)
        when = tournaments.utc(m.ends_at if s == "running" else m.starts_at).timestamp()
        return (order[s], -tournaments.utc(m.ends_at).timestamp() if s == "ended" else when)

    return sorted(rows, key=key)


def entry(db: Session, m: Major, user_id: int) -> MajorEntry | None:
    return db.scalar(select(MajorEntry).where(MajorEntry.major_id == m.id, MajorEntry.user_id == user_id))


def entrant_count(db: Session, m: Major) -> int:
    return db.scalar(select(func.count()).select_from(MajorEntry).where(
        MajorEntry.major_id == m.id, MajorEntry.removed.is_(False))) or 0


# ---- making and changing one


def _http(url: str | None, what: str) -> str | None:
    url = (url or "").strip() or None
    if url and not url.startswith("https://"):
        raise Refused("link", f"The {what} must start with https://")
    return url


def _check(db: Session, fields: dict, games: list[str], at: datetime, current_end: datetime | None = None) -> tuple[dict, list[str]]:
    """The fields ready to save, and the game names in order. Raises Refused with what to fix. `current_end` is the
    saved end of a major being changed: keeping it is fine even once it has passed (fixing the words afterwards)."""
    f = dict(fields)
    if f["slug"] in RESERVED_SLUGS:
        raise Refused("slug", "That address is taken by start.ng. Pick another.")
    f["starts_at"], f["ends_at"] = tournaments.utc(f["starts_at"]), tournaments.utc(f["ends_at"])
    if not MIN_LENGTH <= f["ends_at"] - f["starts_at"] <= MAX_LENGTH:
        raise Refused("duration", "A major runs from 1 hour to 45 days.")
    if f["ends_at"] <= at and f["ends_at"] != current_end:
        raise Refused("ends_at", "The end can't be in the past.")
    for key, what in (("partner_url", "partner's link"), ("partner_logo_url", "logo"), ("cta_url", "button's link")):
        f[key] = _http(f.get(key), what)
    if bool(f.get("cta_label")) != bool(f.get("cta_url")):
        raise Refused("cta", "The button needs both its words and its link, or neither.")
    prizes = sorted(({"place": p["place"], "prize": p["prize"].strip()} for p in f["prizes"]), key=lambda p: p["place"])
    if [p["place"] for p in prizes] != list(range(1, len(prizes) + 1)):
        raise Refused("prizes", "Prizes go 1st, 2nd, 3rd and so on, with no place missing or twice.")
    f["prizes"] = prizes
    for key in ("title", "tagline", "partner_name"):
        f[key] = f[key].strip()
    f["about"] = (f.get("about") or "").strip() or None
    f["cta_label"] = (f.get("cta_label") or "").strip() or None
    names = list(dict.fromkeys(games))  # the same game twice is once
    if not 1 <= len(names) <= MAX_GAMES:
        raise Refused("games", f"Pick between 1 and {MAX_GAMES} games.")
    allowed = set(tournaments.eligible_games(db))
    bad = [n for n in names if n not in allowed]
    if bad:
        raise Refused("game_not_allowed", f"{bad[0]} can't be used: it needs a leaderboard and to be listed on start.ng.")
    return f, names


def create(db: Session, admin: User, fields: dict, games: list[str], at: datetime | None = None) -> Major:
    """A new major, as a draft: only admins see it until it's published."""
    at = at or tournaments.now()
    f, names = _check(db, fields, games, at)
    if find(db, f["slug"]):
        raise Refused("slug_taken", "There's already a major at that address. Pick another.")
    m = Major(**f, published=False, created_by=admin.id, games=[MajorGame(game_name=n, position=i) for i, n in enumerate(names)])
    db.add(m)
    try:
        db.commit()
    except IntegrityError:  # two admins took the address at once
        db.rollback()
        raise Refused("slug_taken", "There's already a major at that address. Pick another.")
    return m


def update(db: Session, m: Major, fields: dict, games: list[str], at: datetime | None = None) -> Major:
    """Change a major. Once it has started, its games, start and address stay as they are: players are already
    competing on them. The words, prizes and end can still change."""
    at = at or tournaments.now()
    f, names = _check(db, fields, games, at, current_end=tournaments.utc(m.ends_at))
    started = state(m, at) != "upcoming"
    if started:
        if names != [g.game_name for g in m.games]:
            raise Refused("started", "It has started, so its games can't change.")
        if f["starts_at"] != tournaments.utc(m.starts_at) or f["slug"] != m.slug:
            raise Refused("started", "It has started, so its start time and address can't change.")
    if f["slug"] != m.slug and find(db, f["slug"]):
        raise Refused("slug_taken", "There's already a major at that address. Pick another.")
    for key, value in f.items():
        setattr(m, key, value)
    if not started and names != [g.game_name for g in m.games]:
        m.games.clear()
        db.flush()  # the old rows go before the new ones arrive (a game can be in both)
        m.games = [MajorGame(game_name=n, position=i) for i, n in enumerate(names)]
    db.commit()
    return m


def as_template(m: Major) -> dict:
    """The major as a MajorIn body: copy it, change the words, and it's the next collaboration."""
    return {
        "slug": m.slug, "title": m.title, "tagline": m.tagline, "about": m.about,
        "partner_name": m.partner_name, "partner_url": m.partner_url, "partner_logo_url": m.partner_logo_url,
        "cta_label": m.cta_label, "cta_url": m.cta_url, "prizes": list(m.prizes),
        "games": [g.game_name for g in m.games],
        "starts_at": tournaments.utc(m.starts_at).isoformat(), "ends_at": tournaments.utc(m.ends_at).isoformat(),
    }


def copy(db: Session, admin: User, source: Major, slug: str, title: str | None, starts_at: datetime, ends_at: datetime,
         at: datetime | None = None) -> Major:
    """Use `source` as the template for a new draft. Games that have since left start.ng are dropped."""
    fields = as_template(source)
    allowed = set(tournaments.eligible_games(db))
    games = [g for g in fields.pop("games") if g in allowed]
    if not games:
        raise Refused("games", "None of that major's games can be used any more. Make a new one and pick games.")
    fields.update(slug=slug, title=title or source.title, starts_at=starts_at, ends_at=ends_at)
    return create(db, admin, fields, games, at)


# ---- players


def enter(db: Session, m: Major, user: User, at: datetime | None = None) -> None:
    """Enter (before it starts, or while it runs). Entering twice is fine. Runs before entering don't count."""
    if state(m, at) == "ended":
        raise Refused("ended", "This major has ended.")
    if entry(db, m, user.id):
        return
    db.add(MajorEntry(major_id=m.id, user_id=user.id))
    try:
        db.commit()
    except IntegrityError:  # entered twice at once
        db.rollback()


def set_removed(db: Session, m: Major, user_id: int, removed: bool, note: str | None = None) -> MajorEntry:
    """An admin takes an entrant out of the running (their runs stop showing), or puts them back."""
    e = entry(db, m, user_id)
    if e is None:
        raise Refused("not_entered", "That player hasn't entered this major.")
    e.removed, e.removed_note = removed, (note if removed else None)
    db.commit()
    return e


# ---- runs and results


def record_run(db: Session, user_id: int, game: Game, stats: dict, schema: list[dict], at: datetime | None = None) -> None:
    """Count a run in every published, running major that uses this game and that the player entered. Keeps their best."""
    at = at or tournaments.now()
    first = stats[schema[0]["key"]]
    for m in db.scalars(select(Major).join(MajorGame).join(MajorEntry, MajorEntry.major_id == Major.id).where(
        MajorGame.game_name == game.name, MajorEntry.user_id == user_id, MajorEntry.removed.is_(False),
        Major.published.is_(True), Major.starts_at <= at, Major.ends_at > at,
    )).all():
        where = (MajorScore.major_id == m.id, MajorScore.game_name == game.name, MajorScore.user_id == user_id)
        row = db.scalar(select(MajorScore).where(*where))
        if row is None:
            db.add(MajorScore(major_id=m.id, game_name=game.name, user_id=user_id, best=first, stats=stats))
            try:
                db.commit()
                continue
            except IntegrityError:  # two first runs at once: compare with the one that won
                db.rollback()
                row = db.scalar(select(MajorScore).where(*where))
        if boards.is_better(schema, stats, boards.stats_of(schema, row.stats, row.best)):
            row.best, row.stats = first, stats
            db.commit()


@events.subscribe("score_submitted")
def on_score_submitted(payload: dict) -> None:
    if not flags.is_on("majors"):  # switched off: runs stop counting, like every other route of the feature
        return
    with SessionLocal() as db:
        game = db.get(Game, payload["game"])
        if game is None:
            return
        record_run(db, payload["user_id"], game, payload["stats"], game.leaderboard or boards.DEFAULT)


@dataclass
class Standing:
    user: User
    points: int = 0
    firsts: int = 0
    games_played: int = 0
    last_at: datetime | None = None
    place: int = 0


@dataclass
class Results:
    standings: list[Standing]
    boards: dict[str, list[MajorScore]]  # each game's runs, best first


def results(db: Session, m: Major) -> Results:
    """Each game's board and the standings (so far, or final once it has ended). Removed entrants don't count."""
    entrants = {e.user_id for e in m.entries if not e.removed}
    per_game: dict[str, list[MajorScore]] = defaultdict(list)
    for s in db.scalars(select(MajorScore).where(MajorScore.major_id == m.id)).all():
        if s.user_id in entrants:
            per_game[s.game_name].append(s)
    by_user: dict[int, Standing] = {}
    out_boards: dict[str, list[MajorScore]] = {}
    for mg in m.games:
        schema = mg.game.leaderboard or boards.DEFAULT
        ranked = sorted(per_game.get(mg.game_name, []),
                        key=lambda s: (boards.rank_key(schema, boards.stats_of(schema, s.stats, s.best)), tournaments.utc(s.updated_at)))
        out_boards[mg.game_name] = ranked
        for place, s in enumerate(ranked, start=1):
            st = by_user.setdefault(s.user_id, Standing(user=s.user))
            st.points += tournaments.points_for(place)
            st.firsts += place == 1
            st.games_played += 1
            when = tournaments.utc(s.updated_at)
            st.last_at = when if st.last_at is None or when > st.last_at else st.last_at
    # Most points, then most games won, then whoever got there first.
    standings = sorted(by_user.values(), key=lambda st: (-st.points, -st.firsts, st.last_at, st.user.id))
    for place, st in enumerate(standings, start=1):
        st.place = place
    return Results(standings=standings, boards=out_boards)


def winners(m: Major, res: Results, at: datetime | None = None) -> list[tuple[dict, Standing]]:
    """Once it has ended: each prize with the player who takes it. Empty while it runs."""
    if state(m, at) != "ended":
        return []
    return [(prize, st) for prize, st in zip(m.prizes, res.standings)]
