"""Tournament emails (H1). One tournament ends as the next starts (6 PM, start.ng's time), so that's when we write:

  - "tournament_day" to everyone on the tournament topic (visitors too): who won the one that just ended, and the
    new one's games;
  - "tournament_result" to each player of the one that ended who chose results emails: their place and points.

A player who gets both gets one email (the 1-a-day cap merges them). The email sender's loop calls tick(), which also
settles the ended tournament and makes the new one, so this happens even when nobody opens start.ng at 6 PM.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ... import events, flags, tournaments
from ...config import settings
from ...models import Tournament, TournamentPoints
from ...routers.games import _cover_url, _player_name
from . import service
from .models import Announcement
from .service import Note, esc

log = logging.getLogger(__name__)

ANNOUNCE_WITHIN = timedelta(hours=3)  # a tournament that started longer ago than this isn't announced any more
REMIND_BEFORE = timedelta(minutes=10)  # the reminder goes out this long before the next tournament starts (Ziki #60)


def _title(name: str) -> str:
    return " ".join(w.capitalize() for w in name.split("-"))


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _local_time(when: datetime) -> str:
    zone = timezone(timedelta(hours=settings.tournament_utc_offset))
    local = tournaments.utc(when).astimezone(zone)
    sign = "+" if settings.tournament_utc_offset >= 0 else "-"
    return f"{local.strftime('%I:%M %p').lstrip('0')} (UTC{sign}{abs(settings.tournament_utc_offset)})"


def _button(link: str, label: str) -> str:
    return service.button(link, label, arrow=True)


def _eyebrow(text: str) -> str:
    return (f'<p style="margin:0 0 12px;font:700 11px/1.4 {service.MONO};letter-spacing:.18em;'
            f'text-transform:uppercase;color:{service.MUTED}">{esc(text)}</p>')


# The colours a game's card falls back to when it has no cover, in the same order and with the same hash as the
# website (src/games.ts): the same game always gets the same pair.
CARD_COLORS = [
    ("#ffd400", "#e10600"),
    ("#1dd1a1", "#0c6e5a"),
    ("#a55eea", "#4b7bec"),
    ("#ff6b6b", "#f7b731"),
    ("#45aaf2", "#2b2f4a"),
    ("#fd9644", "#eb3b5a"),
]


def _card_colors(slug: str) -> tuple[str, str]:
    hashed = 0
    for ch in slug:
        hashed = (hashed * 31 + ord(ch)) & 0xFFFFFFFF
    return CARD_COLORS[hashed % len(CARD_COLORS)]


def _game(g) -> dict:
    """A game in the day's email. Notes queued before covers were added hold plain titles, so accept those too."""
    if isinstance(g, str):
        return {"name": g, "slug": g.lower().replace(" ", "-"), "cover_url": None}
    return {"name": g.get("name") or _title(g.get("slug", "")), "slug": g.get("slug") or "", "cover_url": g.get("cover_url")}


def _thumb(game: dict) -> str:
    """The game's picture, or the website's coloured letter tile when it has none."""
    first, second = _card_colors(game["slug"] or game["name"])
    if game.get("cover_url"):
        inside = (f'<img src="{esc(game["cover_url"])}" alt="" width="56" height="56" '
                  'style="display:block;width:56px;height:56px;object-fit:cover;border:0;border-radius:10px">')
    else:
        letter = (game["name"] or "?")[:1].upper()
        inside = (f'<span style="font:900 24px/56px {service.FONT};color:rgba(255,255,255,.92);'
                  f'text-shadow:0 2px 8px rgba(0,0,0,.25)">{esc(letter)}</span>')
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate">'
        f'<tr><td width="56" height="56" align="center" valign="middle" bgcolor="{first}" '
        f'style="width:56px;height:56px;background:{first};background-image:linear-gradient(135deg,{first},{second});'
        f'border-radius:10px;overflow:hidden;text-align:center">{inside}</td></tr></table>'
    )


def _game_row(index: int, game: dict) -> str:
    """One game as a numbered row, with its cover, like the cards on the website."""
    number = f"{index:02d}"
    return (
        '<tr><td style="padding:0 0 8px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{service.BG}" '
        f'style="width:100%;background:{service.BG};border:1px solid {service.LINE};border-radius:12px;'
        'border-collapse:separate">'
        '<tr>'
        f'<td width="78" valign="middle" style="padding:9px 0 9px 9px">{_thumb(game)}</td>'
        '<td width="44" valign="middle" style="padding:10px 0">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate">'
        f'<tr><td width="28" height="28" align="center" bgcolor="{service.LIME}" style="width:28px;height:28px;'
        f'background:{service.LIME};border-radius:7px;font:700 11px/28px {service.MONO};color:{service.INK};'
        f'text-align:center">{number}</td></tr></table>'
        '</td>'
        f'<td valign="middle" style="padding:11px 14px 11px 0;font:700 15px/1.35 {service.FONT};'
        f'color:{service.INK}">{esc(game["name"])}</td>'
        '</tr></table></td></tr>'
    )


def _stat(label: str, value: str, note: str = "") -> str:
    tail = (f' <span style="font:600 13px/1.4 {service.FONT};color:{service.MUTED}">{esc(note)}</span>'
            if note else "")
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{service.BG}" '
        f'style="width:100%;background:{service.BG};border:1px solid {service.LINE};border-radius:14px;'
        'border-collapse:separate">'
        '<tr><td style="padding:14px 16px">'
        f'<p style="margin:0 0 4px;font:700 10px/1.4 {service.MONO};letter-spacing:.16em;text-transform:uppercase;'
        f'color:{service.MUTED}">{esc(label)}</p>'
        f'<p style="margin:0;font:800 26px/1.1 {service.FONT};letter-spacing:-.03em;color:{service.INK}">'
        f'{esc(value)}{tail}</p>'
        '</td></tr></table>'
    )


def render_day(d: dict) -> Note:
    games = [_game(g) for g in d["games"]]
    winner = d.get("winner")
    subject = (f"{winner} won yesterday's tournament. Today: {len(games)} games" if winner
               else f"Today's tournament: {len(games)} games, 24 hours")
    others = f', ahead of {d["players"] - 1} other {"player" if d["players"] == 2 else "players"}' if d["players"] > 1 else ""
    won_html = ""
    if winner:
        won_html = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'bgcolor="{service.LIME_TINT}" style="width:100%;margin:0 0 26px;background:{service.LIME_TINT};'
            f'border:1px solid {service.LINE};border-radius:16px;border-collapse:separate">'
            '<tr><td style="padding:15px 18px">'
            f'<p style="margin:0 0 4px;font:700 10px/1.4 {service.MONO};letter-spacing:.16em;text-transform:uppercase;'
            f'color:{service.MUTED}">Last tournament</p>'
            f'<p style="margin:0;font:400 15px/1.55 {service.FONT};color:{service.INK}">'
            f'<b>{esc(winner)}</b> won the last tournament{esc(others)}.</p>'
            '</td></tr></table>'
        )
    won_text = f"{winner} won the last tournament{others}.\n\n" if winner else ""
    html = (
        f'{won_html}'
        f'{_eyebrow("Daily tournament")}'
        f'<h1 style="margin:0 0 12px;font:800 32px/1.1 {service.FONT};letter-spacing:-.045em;color:{service.INK}">'
        'Today\'s tournament is on</h1>'
        f'<p style="margin:0 0 16px;font:400 16px/1.6 {service.FONT};color:{service.MUTED}">'
        f'{len(games)} games, 24 hours, one winner.</p>'
        f'<p style="margin:0 0 26px;font:400 15px/1.6 {service.FONT};color:{service.INK}">It ends at '
        f'<span style="display:inline-block;background:{service.LIME};border-radius:6px;padding:4px 8px;'
        f'font:700 12px/1.4 {service.MONO};color:{service.INK}">{esc(d["ends"])}</span></p>'
        f'<p style="margin:0 0 10px;font:700 10px/1.4 {service.MONO};letter-spacing:.16em;text-transform:uppercase;'
        f'color:{service.MUTED}">Today\'s games</p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse">'
        f'{"".join(_game_row(i, g) for i, g in enumerate(games, 1))}'
        '</table>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{service.LIME_TINT}" style="width:100%;margin:22px 0 0;background:{service.LIME_TINT};'
        'border-radius:12px;border-collapse:separate">'
        f'<tr><td style="padding:14px 16px;font:400 14px/1.55 {service.FONT};color:{service.INK}">'
        '<b>Share it on X to enter</b>, then play.</td></tr></table>'
        f'{_button(d["link"], "Play today")}'
    )
    text = (f"{won_text}Today's tournament is on: {len(games)} games, 24 hours, one winner. It ends at {d['ends']}.\n\n"
            + "\n".join(f"- {g['name']}" for g in games) + f"\n\nPlay today: {d['link']}\nShare it on X to enter, then play.")
    return Note(subject, html, text)


def render_result(d: dict) -> Note:
    place, players, points = d["place"], d["players"], d["points"]
    pts = f'{points} {"point" if points == 1 else "points"}'
    if place == 1:
        subject = "You won the start.ng tournament!"
        line = f"You won, with {pts}. Your name is on the past winners list."
    else:
        subject = f"You came {ordinal(place)} of {players} in the tournament"
        line = f"You came {ordinal(place)} of {players}, with {pts}. {d['winner']} won."
    html = (
        f'{_eyebrow("Tournament result")}'
        f'<h1 style="margin:0 0 12px;font:800 31px/1.15 {service.FONT};letter-spacing:-.045em;color:{service.INK}">'
        f'{esc(subject)}</h1>'
        f'<p style="margin:0;font:400 15px/1.6 {service.FONT};color:{service.MUTED}">{esc(line)}</p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;margin:24px 0 0;border-collapse:separate"><tr>'
        '<td width="50%" valign="top" style="padding:0 6px 0 0">'
        f'{_stat("Place", ordinal(place), f"of {players}")}'
        '</td>'
        '<td width="50%" valign="top" style="padding:0 0 0 6px">'
        f'{_stat("Points", str(points))}'
        '</td>'
        '</tr></table>'
        f'{_button(d["link"], "Try again today")}'
    )
    return Note(subject, html, f"{subject}\n\n{line}\n\nTry again today: {d['link']}")


def render_reminder(d: dict) -> Note:
    subject = "The start.ng tournament starts in 10 minutes"
    line = f"A new tournament starts at {d['starts']}: new games, 24 hours, one winner. Be there when it opens."
    html = (
        f'{_eyebrow("Tournament reminder")}'
        f'<h1 style="margin:0 0 12px;font:800 31px/1.15 {service.FONT};letter-spacing:-.045em;color:{service.INK}">'
        f'{esc(subject)}</h1>'
        f'<p style="margin:0;font:400 15px/1.6 {service.FONT};color:{service.MUTED}">{esc(line)}</p>'
        f'{_button(d["link"], "Go to the tournament")}'
    )
    return Note(subject, html, f"{subject}\n\n{line}\n\nGo to the tournament: {d['link']}")


service.register_kind("tournament_day", "tournament", render_day)
service.register_kind("tournament_result", "results", render_result)
service.register_kind("tournament_reminder", "reminder", render_reminder, capped=False)  # at once: it's about a time


def _link() -> str:
    return settings.site_url.rstrip("/") + "/tournament"


# ---- results: when a tournament is settled


def _on_finished(payload: dict) -> None:
    if not flags.is_on(service.NAME):
        return
    with service.SessionLocal() as db:
        rows = db.scalars(select(TournamentPoints).where(TournamentPoints.tournament_id == payload["tournament_id"])).all()
        winner = next((r.user for r in rows if r.place == 1), None)
        for r in rows:
            service.notify(r.user_id, "tournament_result", {
                "place": r.place, "players": payload["players"], "points": r.points, "link": _link(),
                "winner": _player_name(winner, "Someone") if winner else "Someone",
            }, db=db)
        db.commit()


events.subscribe("tournament_finished")(_on_finished)


# ---- the day's email: once per tournament, when it starts


def tick(db: Session, at: datetime | None = None) -> bool:
    """Settle the tournament that ended (results go out through tournament_finished), make today's, and announce
    it if it hasn't been. Returns whether it announced one."""
    at = at or tournaments.now()
    t = tournaments.current(db, at)  # also settles ended ones
    if t is None or at - tournaments.utc(t.starts_at) > ANNOUNCE_WITHIN:
        return False
    key = f"tournament:{t.id}"
    if db.get(Announcement, key) is not None:
        return False
    db.add(Announcement(key=key))
    try:
        db.commit()
    except IntegrityError:  # another server announced it
        db.rollback()
        return False
    before = db.scalar(select(Tournament).where(Tournament.ends_at == t.starts_at, Tournament.finished_at.is_not(None)))
    players = 0
    winner = None
    if before is not None and before.winner is not None:
        players = len(db.scalars(select(TournamentPoints.id).where(TournamentPoints.tournament_id == before.id)).all())
        winner = _player_name(before.winner, "Someone")
    service.broadcast("tournament", "tournament_day", {
        "games": [{"name": _title(g.game_name), "slug": g.game_name, "cover_url": _cover_url(g.game)} for g in t.games],
        "ends": _local_time(t.ends_at), "link": _link(), "winner": winner, "players": players,
    }, db=db)
    db.commit()
    return True


# ---- the reminder: once per tournament, 10 minutes before it starts (Ziki #60)


def remind(db: Session, at: datetime | None = None) -> bool:
    """Email everyone on the reminder topic once, in the 10 minutes before the next tournament starts. Each tournament
    starts as the one before ends (6 PM, start.ng's time), so this works whether or not the next one has been made.
    Returns whether it sent one."""
    at = at or tournaments.now()
    starts = tournaments.window(at)[1]
    if not timedelta() < starts - tournaments.utc(at) <= REMIND_BEFORE:
        return False
    key = f"reminder:{starts.isoformat()}"
    if db.get(Announcement, key) is not None:
        return False
    db.add(Announcement(key=key))
    try:
        db.commit()
    except IntegrityError:  # another server sent it
        db.rollback()
        return False
    service.broadcast("reminder", "tournament_reminder", {"starts": _local_time(starts), "link": _link()}, db=db)
    db.commit()
    return True
