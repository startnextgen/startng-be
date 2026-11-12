from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import tournaments
from app.config import settings
from app.db import Base
from app.features.email import service, tournament_mail
from app.features.email.models import Email, Subscriber
from app.features.email.provider import LogProvider
from app.models import Game, TournamentScore, User

NOON = datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc)  # 12:00 in Nigeria
END = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)  # today's tournament ends (18:00) and the next starts
JUST_STARTED = datetime(2026, 10, 19, 17, 5, tzinfo=timezone.utc)  # 18:05 yesterday: today's tournament has just begun
BOARD = [{"key": "score", "label": "Score", "order": "highest", "format": "number"}]


class Clock:
    at = NOON


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(tournaments, "now", lambda: clock.at)
    monkeypatch.setattr(service, "now", lambda: clock.at)
    monkeypatch.setattr(settings, "features", "email")
    monkeypatch.setattr(settings, "email_merge_minutes", 10)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(service, "SessionLocal", Session)
    with Session() as db:
        users = {n: User(email=f"{n}@example.com", name=n.title()) for n in ("ada", "tunde", "chioma")}
        users["ada"].character = {"name": "Runner Ada"}
        db.add_all(users.values())
        db.flush()
        db.add_all([Game(name=f"lane-dash-{i}", owner_id=users["ada"].id, repo_url=f"https://github.com/x/g{i}", leaderboard=BOARD)
                    for i in range(3)])
        db.commit()
        ids = {n: u.id for n, u in users.items()}
    yield clock, Session, ids


def opt_in(Session, ids, who, topics):
    with Session() as db:
        service.set_my_topics(db, db.get(User, ids[who]), topics)


def emails(Session):
    """Every email so far: address -> the kinds of note in each of their emails, in order."""
    out: dict[str, list[str]] = {}
    with Session() as db:
        for r in db.scalars(select(Email).order_by(Email.id)).all():
            out.setdefault(r.to_email, []).extend(n["kind"] for n in r.notes)
    return out


def test_names_places():
    assert [tournament_mail.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st"]


def test_the_day_email_and_results(env):
    clock, Session, ids = env
    clock.at = JUST_STARTED
    opt_in(Session, ids, "ada", ["tournament", "results"])  # gets both: one email
    opt_in(Session, ids, "tunde", ["results"])  # only how they did
    opt_in(Session, ids, "chioma", ["new_games"])  # played, but didn't ask for either
    with Session() as db:  # a visitor who confirmed the tournament topic
        sub = Subscriber(email="fan@example.com", unsubscribe_token="t", confirmed_at=clock.at)
        db.add(sub)
        db.flush()
        service.set_topics(db, sub, ["tournament"])
        db.commit()
        # today's tournament: announced once, the first time the loop sees it
        assert tournament_mail.tick(db) is True
        assert tournament_mail.tick(db) is False
        t = tournaments.running(db)
        for who, score in (("ada", 900), ("tunde", 500), ("chioma", 100)):
            db.add(TournamentScore(tournament_id=t.id, game_name="lane-dash-0", user_id=ids[who], best=score, stats={"score": score}))
        db.commit()
    assert emails(Session) == {"ada@example.com": ["tournament_day"], "fan@example.com": ["tournament_day"]}
    with Session() as db:
        service.send_due(db, LogProvider(), at=clock.at + timedelta(minutes=11))

    # 18:00: it ends, the next one starts. Results and the day's email go out together.
    clock.at = END + timedelta(minutes=1)
    with Session() as db:
        assert tournament_mail.tick(db) is True
    got = emails(Session)
    assert got["tunde@example.com"] == ["tournament_result"]
    assert got["fan@example.com"] == ["tournament_day", "tournament_day"]  # yesterday's (sent) + today's
    assert "chioma@example.com" not in got
    with Session() as db:
        sent = LogProvider()
        service.send_due(db, sent, at=clock.at + timedelta(minutes=11))
    by = {m.to: m for m in sent.sent}
    assert by["ada@example.com"].subject == "You won the start.ng tournament! (+1 more)"  # merged into one email
    assert "Runner Ada won the last tournament, ahead of 2 other players." in by["ada@example.com"].text
    assert by["tunde@example.com"].subject == "You came 2nd of 3 in the tournament"
    assert "Runner Ada won." in by["tunde@example.com"].text
    assert by["fan@example.com"].subject.startswith("Runner Ada won yesterday's tournament. Today: 3 games")
    assert "6:00 PM (UTC+1)" in by["fan@example.com"].text and "https://start.ng/tournament" in by["fan@example.com"].text


def test_the_day_email_shows_game_covers(env):
    clock, Session, ids = env
    clock.at = JUST_STARTED
    opt_in(Session, ids, "ada", ["tournament"])
    with Session() as db:
        game = db.scalar(select(Game).where(Game.name == "lane-dash-0"))
        game.cover, game.cover_at = "cover.png", datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        db.commit()
        assert tournament_mail.tick(db) is True
    with Session() as db:
        sent = LogProvider()
        service.send_due(db, sent, at=clock.at + timedelta(minutes=11))
    message = sent.sent[0]
    assert "https://lane-dash-0.start.ng/cover.png?v=" in message.html
    assert ">L</span>" in message.html  # a game without a cover keeps the website's letter tile
    assert "- Lane Dash 0" in message.text  # the plain-text alternative names every game


def test_a_day_note_queued_before_covers_still_renders():
    note = tournament_mail.render_day({
        "games": ["Lane Dash 0", "Pong"], "ends": "6:00 PM (UTC+1)", "link": "https://start.ng/tournament",
        "winner": None, "players": 0,
    })
    assert "Lane Dash 0" in note.html and "- Pong" in note.text
    assert note.subject == "Today's tournament: 2 games, 24 hours"


def test_card_colors_match_the_website():
    # Values from the website's hash (src/games.ts: hash * 31 + charCodeAt, >>> 0, mod 6). The same
    # game always gets the same tile in both places.
    assert tournament_mail._card_colors("lane-dash-0") == ("#a55eea", "#4b7bec")
    assert tournament_mail._card_colors("neon-drift-arcade") == ("#1dd1a1", "#0c6e5a")
    assert tournament_mail._card_colors("mami") == ("#ffd400", "#e10600")
    assert tournament_mail._card_colors("") == tournament_mail.CARD_COLORS[0]


def test_a_cover_is_shown_as_the_game_thumbnail():
    thumb = tournament_mail._thumb({
        "name": "Lane Dash 0", "slug": "lane-dash-0",
        "cover_url": "https://lane-dash-0.start.ng/cover.png?v=1",
    })
    assert '<img src="https://lane-dash-0.start.ng/cover.png?v=1"' in thumb
    assert "border-radius:10px" in thumb
    assert ">L</span>" not in thumb  # no letter tile when there is a cover


def test_a_game_without_a_cover_gets_the_websites_letter_tile():
    thumb = tournament_mail._thumb({"name": "neon drift arcade", "slug": "neon-drift-arcade", "cover_url": None})
    assert "<img" not in thumb
    assert ">N</span>" in thumb
    # the same gradient pair the website's card uses for this slug
    assert "linear-gradient(135deg,#1dd1a1,#0c6e5a)" in thumb


def test_cover_urls_are_escaped():
    note = tournament_mail.render_day({
        "games": [{
            "name": "Lane Dash 0", "slug": "lane-dash-0",
            "cover_url": 'https://lane-dash-0.start.ng/cover.png?v=1&next="x"',
        }],
        "ends": "6:00 PM (UTC+1)", "link": "https://start.ng/tournament", "winner": None, "players": 0,
    })
    assert '?v=1&amp;next=&quot;x&quot;' in note.html
    assert 'next="x"' not in note.html


def test_a_day_note_takes_old_and_new_games_together():
    note = tournament_mail.render_day({
        "games": ["Coin Dash Game", {"slug": "lane-dash-0", "cover_url": None}],
        "ends": "6:00 PM (UTC+1)", "link": "https://start.ng/tournament", "winner": None, "players": 0,
    })
    assert "Coin Dash Game" in note.html  # an old plain-title entry
    assert "Lane Dash 0" in note.html  # a new entry with no name falls back to its slug
    assert "- Coin Dash Game" in note.text and "- Lane Dash 0" in note.text


def test_not_announced_late_or_while_off(env, monkeypatch):
    clock, Session, ids = env
    opt_in(Session, ids, "ada", ["tournament"])
    monkeypatch.setattr(settings, "features", "")
    with Session() as db:
        tournament_mail.tick(db)  # the tournament started at 18:00 yesterday: too long ago to announce
    assert emails(Session) == {}
    monkeypatch.setattr(settings, "features", "email")
    clock.at = END + timedelta(hours=4)
    with Session() as db:
        assert tournament_mail.tick(db) is False


def test_the_reminder_goes_out_once_10_minutes_before(env):
    clock, Session, ids = env
    opt_in(Session, ids, "ada", ["reminder"])
    opt_in(Session, ids, "tunde", ["tournament"])  # the day's email only: no reminder
    clock.at = END - timedelta(minutes=11)  # 17:49: too early
    with Session() as db:
        assert tournament_mail.remind(db) is False
    clock.at = END - timedelta(minutes=9)  # 17:51
    with Session() as db:
        assert tournament_mail.remind(db) is True
        assert tournament_mail.remind(db) is False  # once per tournament
    assert emails(Session) == {"ada@example.com": ["tournament_reminder"]}
    with Session() as db:
        sent = LogProvider()
        service.send_due(db, sent, at=clock.at)  # outside the cap: it doesn't wait for the merge window
    (msg,) = sent.sent
    assert msg.to == "ada@example.com"
    assert msg.subject == "The start.ng tournament starts in 10 minutes"
    assert "6:00 PM (UTC+1)" in msg.text and "https://start.ng/tournament" in msg.text


def test_the_reminder_does_not_use_up_the_days_email(env):
    clock, Session, ids = env
    opt_in(Session, ids, "ada", ["reminder", "tournament"])
    clock.at = END - timedelta(minutes=5)
    with Session() as db:
        assert tournament_mail.remind(db) is True
        service.send_due(db, LogProvider(), at=clock.at)
    clock.at = END + timedelta(minutes=1)  # 18:00: the day's email still goes out
    with Session() as db:
        assert tournament_mail.tick(db) is True
        sent = LogProvider()
        service.send_due(db, sent, at=clock.at + timedelta(minutes=11))
    assert [m.to for m in sent.sent] == ["ada@example.com"]
    assert emails(Session)["ada@example.com"] == ["tournament_reminder", "tournament_day"]


def test_no_reminder_after_the_start_or_while_off(env, monkeypatch):
    clock, Session, ids = env
    opt_in(Session, ids, "ada", ["reminder"])
    clock.at = END + timedelta(minutes=1)  # just started: 23h59m to the next one
    with Session() as db:
        assert tournament_mail.remind(db) is False
    monkeypatch.setattr(settings, "features", "")
    clock.at = END - timedelta(minutes=5)
    with Session() as db:
        tournament_mail.remind(db)
    assert emails(Session) == {}
