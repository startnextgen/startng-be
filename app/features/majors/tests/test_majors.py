"""Majors: partner tournaments with prizes. An admin makes a draft and publishes it; players enter and their runs
count inside the window; the top places take the prizes; a major is the template for the next one."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import tournaments
from app.config import settings
from app.db import Base, get_db
from app.features.majors import service
from app.main import app
from app.models import Game, User
from app.security import create_token

BOARD = [{"key": "score", "label": "Score", "order": "highest", "format": "number"}]
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


class Clock:
    at = NOW


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(tournaments, "now", lambda: clock.at)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(settings, "features", "majors")
    monkeypatch.setattr(settings, "super_admin_emails", "boss@example.com")
    monkeypatch.setattr(service, "SessionLocal", Session)  # score events open their own session
    app.dependency_overrides[get_db] = override_db
    with Session() as db:
        users = {k: User(email=f"{k}@example.com", name=k.title(), google_sub=f"g-{k}") for k in ("boss", "ada", "tunde", "kofi", "zee")}
        db.add_all(users.values())
        db.flush()
        db.add_all([Game(name=f"g{i}", owner_id=users["ada"].id, repo_url=f"https://github.com/ada/g{i}", leaderboard=BOARD)
                    for i in range(3)])
        db.add(Game(name="no-board", owner_id=users["ada"].id, repo_url="https://github.com/ada/no-board"))
        db.commit()
        tokens = {k: {"Authorization": f"Bearer {create_token(u.id)}"} for k, u in users.items()}
    yield TestClient(app), tokens, clock
    app.dependency_overrides.clear()


def body(**extra):
    return {
        "slug": "techemberfest", "title": "Tech Ember Fest Major", "tagline": "Top 3 win tickets to Tech Ember Fest.",
        "about": "A day outdoors with games, music and food.", "partner_name": "Tech Ember Fest",
        "partner_url": "https://techemberfest.com", "partner_logo_url": None,
        "cta_label": "Get tickets", "cta_url": "https://techemberfest.com",
        "prizes": [{"place": 1, "prize": "Solo ticket"}, {"place": 2, "prize": "Solo ticket"}, {"place": 3, "prize": "Solo ticket"}],
        "games": ["g0", "g1"], "starts_at": (NOW + timedelta(days=1)).isoformat(), "ends_at": (NOW + timedelta(days=8)).isoformat(),
        **extra,
    }


def make(client, tokens, publish=True, **extra):
    r = client.post("/admin/majors", json=body(**extra), headers=tokens["boss"])
    assert r.status_code == 201, r.text
    if publish:
        assert client.post(f"/admin/majors/{r.json()['slug']}/publish", headers=tokens["boss"]).status_code == 200
    return r.json()


def play(client, tokens, who, game, score):
    assert client.post(f"/games/{game}/scores", json={"score": score}, headers=tokens[who]).status_code == 200


def enter(client, tokens, who, slug="techemberfest"):
    r = client.post(f"/majors/{slug}/enter", headers=tokens[who])
    assert r.status_code == 200, r.text
    return r.json()


def test_only_admins_make_them(env):
    client, tokens, clock = env
    assert client.post("/admin/majors", json=body(), headers=tokens["ada"]).status_code == 404  # the admin page doesn't exist for players
    assert client.post("/admin/majors", json=body()).status_code == 401
    made = make(client, tokens, publish=False)
    assert made["link"] == "https://start.ng/majors/techemberfest"
    assert made["published"] is False and made["state"] == "upcoming"


def test_a_draft_only_shows_to_admins_until_published(env):
    client, tokens, clock = env
    make(client, tokens, publish=False)
    assert client.get("/majors/techemberfest").status_code == 404
    assert client.get("/majors").json() == []
    assert client.post("/majors/techemberfest/enter", headers=tokens["ada"]).status_code == 404
    assert client.get("/admin/majors/techemberfest", headers=tokens["boss"]).json()["title"] == "Tech Ember Fest Major"  # the preview
    client.post("/admin/majors/techemberfest/publish", headers=tokens["boss"])
    page = client.get("/majors/techemberfest").json()
    assert page["partner"] == {"name": "Tech Ember Fest", "url": "https://techemberfest.com", "logo_url": None}
    assert [p["place"] for p in page["prizes"]] == [1, 2, 3]
    assert page["games"] == ["g0", "g1"] and page["entered"] is False and page["winners"] == []
    assert [m["slug"] for m in client.get("/majors").json()] == ["techemberfest"]


def test_what_is_refused(env):
    client, tokens, clock = env
    post = lambda **extra: client.post("/admin/majors", json=body(**extra), headers=tokens["boss"])
    assert post(games=["no-board"]).json()["errors"][0]["code"] == "game_not_allowed"
    assert post(ends_at=(NOW + timedelta(days=90)).isoformat()).json()["errors"][0]["code"] == "duration"
    assert post(partner_url="http://techemberfest.com").json()["errors"][0]["code"] == "link"
    assert post(prizes=[{"place": 1, "prize": "Ticket"}, {"place": 3, "prize": "Ticket"}]).json()["errors"][0]["code"] == "prizes"
    assert post(cta_label="Get tickets", cta_url=None).json()["errors"][0]["code"] == "cta"
    assert post(slug="Tech Ember").status_code == 422  # only lowercase letters, numbers and hyphens
    make(client, tokens)
    assert post().json()["errors"][0]["code"] == "slug_taken"


def test_only_runs_after_entering_and_inside_the_window_count(env):
    client, tokens, clock = env
    make(client, tokens)
    enter(client, tokens, "ada")  # entering early is fine
    play(client, tokens, "ada", "g0", 500)  # before it starts: doesn't count
    clock.at = NOW + timedelta(days=2)
    play(client, tokens, "tunde", "g0", 900)  # hasn't entered: doesn't count
    enter(client, tokens, "tunde")
    play(client, tokens, "ada", "g0", 100)
    play(client, tokens, "tunde", "g0", 80)
    play(client, tokens, "ada", "g0", 60)  # worse than her best: her 100 stays
    page = client.get("/majors/techemberfest", headers=tokens["ada"]).json()
    assert page["state"] == "running" and page["entrants"] == 2
    top = page["game_boards"][0]["top"]
    assert [(r["name"], r["stats"]["score"], r["points"]) for r in top] == [("Ada", 100, 10), ("Tunde", 80, 7)]
    assert page["me"]["place"] == 1 and page["me"]["points"] == 10
    assert page["played"] == 1 and page["game_boards"][0]["mine"]["rank"] == 1 and page["game_boards"][1]["mine"] is None
    tunde = client.get("/majors/techemberfest", headers=tokens["tunde"]).json()
    assert tunde["game_boards"][0]["mine"]["rank"] == 2 and tunde["game_boards"][0]["mine"]["stats"]["score"] == 80
    clock.at = NOW + timedelta(days=9)
    play(client, tokens, "tunde", "g0", 5000)  # after the end: doesn't count
    assert client.get("/majors/techemberfest").json()["game_boards"][0]["top"][0]["name"] == "Ada"
    assert client.post("/majors/techemberfest/enter", headers=tokens["kofi"]).json()["errors"][0]["code"] == "ended"


def test_the_top_places_take_the_prizes_when_it_ends(env):
    client, tokens, clock = env
    make(client, tokens)
    for who in ("ada", "tunde", "kofi", "zee"):
        enter(client, tokens, who)
    clock.at = NOW + timedelta(days=2)
    for who, s0, s1 in (("ada", 100, 10), ("tunde", 90, 40), ("kofi", 80, 30), ("zee", 70, 20)):
        play(client, tokens, who, "g0", s0)
        play(client, tokens, who, "g1", s1)
    # g0: ada 10, tunde 7, kofi 5, zee 3.  g1: tunde 10, kofi 7, zee 5, ada 3.  Totals: tunde 17, ada 13, kofi 12, zee 8.
    page = client.get("/majors/techemberfest").json()
    assert [(s["name"], s["points"]) for s in page["standings"]] == [("Tunde", 17), ("Ada", 13), ("Kofi", 12), ("Zee", 8)]
    assert page["winners"] == []  # not until it ends
    clock.at = NOW + timedelta(days=9)
    page = client.get("/majors/techemberfest").json()
    assert page["state"] == "ended"
    assert [(w["place"], w["name"], w["prize"]) for w in page["winners"]] == [(1, "Tunde", "Solo ticket"), (2, "Ada", "Solo ticket"), (3, "Kofi", "Solo ticket")]
    assert "email" not in page["winners"][0]  # players never see emails
    admin = client.get("/admin/majors", headers=tokens["boss"]).json()[0]
    assert [w["email"] for w in admin["winners"]] == ["tunde@example.com", "ada@example.com", "kofi@example.com"]


def test_an_admin_can_take_a_player_out_of_the_running(env):
    client, tokens, clock = env
    make(client, tokens)
    for who in ("ada", "tunde"):
        enter(client, tokens, who)
    clock.at = NOW + timedelta(days=2)
    play(client, tokens, "ada", "g0", 999999)
    play(client, tokens, "tunde", "g0", 80)
    rows = client.get("/admin/majors/techemberfest/entries", headers=tokens["boss"]).json()
    ada = next(r for r in rows if r["name"] == "Ada")
    r = client.post(f"/admin/majors/techemberfest/entries/{ada['player_id']}/remove", json={"note": "Impossible score"}, headers=tokens["boss"])
    assert r.status_code == 204
    page = client.get("/majors/techemberfest", headers=tokens["ada"]).json()
    assert [s["name"] for s in page["standings"]] == ["Tunde"] and page["removed"] is True and page["entrants"] == 1
    play(client, tokens, "ada", "g1", 50)  # her new runs don't count either
    assert client.get("/majors/techemberfest").json()["game_boards"][1]["top"] == []
    client.post(f"/admin/majors/techemberfest/entries/{ada['player_id']}/restore", headers=tokens["boss"])
    assert [s["name"] for s in client.get("/majors/techemberfest").json()["standings"]] == ["Ada", "Tunde"]


def test_a_major_is_the_template_for_the_next_one(env):
    client, tokens, clock = env
    made = make(client, tokens)
    assert made["template"]["games"] == ["g0", "g1"] and made["template"]["partner_name"] == "Tech Ember Fest"
    # The template is a ready body for a new major: change the words and post it.
    again = {**made["template"], "slug": "devfest-lagos", "title": "DevFest Lagos Major", "partner_name": "GDG Lagos"}
    assert client.post("/admin/majors", json=again, headers=tokens["boss"]).status_code == 201
    # Or copy one in a click, with a new address and dates.
    r = client.post("/admin/majors/techemberfest/copy", headers=tokens["boss"], json={
        "slug": "techemberfest-2027", "starts_at": (NOW + timedelta(days=300)).isoformat(), "ends_at": (NOW + timedelta(days=307)).isoformat()})
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["published"] is False and copy["games"] == ["g0", "g1"] and copy["prizes"] == made["prizes"]
    assert copy["entrants"] == 0 and copy["link"].endswith("/majors/techemberfest-2027")


def test_ready_made_templates(env):
    client, tokens, clock = env
    got = client.get("/admin/majors/templates", headers=tokens["boss"]).json()
    assert got[0]["key"] == "collab" and got[0]["major"]["games"] == []
    fest = next(t for t in got if t["key"] == "techemberfest")
    assert len(fest["major"]["prizes"]) == 3 and fest["major"]["partner_url"] == "https://techemberfest.com"
    assert len(fest["major"]["games"]) == len(set(fest["major"]["games"])) == 20
    # A template with its games swapped for ones on this start.ng is a valid major as it stands.
    ready = {**fest["major"], "games": ["g0", "g1"], "starts_at": (NOW + timedelta(days=1)).isoformat(), "ends_at": (NOW + timedelta(days=14)).isoformat()}
    assert client.post("/admin/majors", json=ready, headers=tokens["boss"]).status_code == 201
    assert client.get("/admin/majors/templates", headers=tokens["ada"]).status_code == 404


def test_games_and_start_are_locked_once_it_starts(env):
    client, tokens, clock = env
    make(client, tokens)
    put = lambda **extra: client.put("/admin/majors/techemberfest", json=body(**extra), headers=tokens["boss"])
    assert put(games=["g0", "g1", "g2"]).json()["games"] == ["g0", "g1", "g2"]  # before it starts: anything can change
    clock.at = NOW + timedelta(days=2)
    assert put(games=["g0"]).json()["errors"][0]["code"] == "started"
    r = put(games=["g0", "g1", "g2"], tagline="Now with 5 tickets.", ends_at=(NOW + timedelta(days=10)).isoformat())
    assert r.status_code == 200 and r.json()["tagline"] == "Now with 5 tickets."  # the words and the end still can


def test_leaving_and_unpublishing(env):
    client, tokens, clock = env
    make(client, tokens)
    enter(client, tokens, "ada")
    assert client.delete("/majors/techemberfest/entry", headers=tokens["ada"]).status_code == 204  # before the start
    assert client.get("/majors/techemberfest", headers=tokens["ada"]).json()["entered"] is False
    enter(client, tokens, "ada")
    clock.at = NOW + timedelta(days=2)
    assert client.delete("/majors/techemberfest/entry", headers=tokens["ada"]).json()["errors"][0]["code"] == "started"
    client.post("/admin/majors/techemberfest/unpublish", headers=tokens["boss"])
    assert client.get("/majors/techemberfest").status_code == 404
    play(client, tokens, "ada", "g0", 100)  # a draft counts nothing
    client.post("/admin/majors/techemberfest/publish", headers=tokens["boss"])
    assert client.get("/majors/techemberfest").json()["standings"] == []


def test_off_until_the_flag_is_on(env, monkeypatch):
    client, tokens, clock = env
    monkeypatch.setattr(settings, "features", "")
    assert client.get("/majors").status_code == 404
    assert client.post("/admin/majors", json=body(), headers=tokens["boss"]).status_code == 404


def _add_games(names):
    db = next(app.dependency_overrides[get_db]())
    owner = db.query(User).filter(User.email == "ada@example.com").one()
    db.add_all([Game(name=n, owner_id=owner.id, repo_url=f"https://github.com/ada/{n}", leaderboard=BOARD) for n in names])
    db.commit()
    db.close()


def test_the_admin_chooses_how_many_games(env):
    client, tokens, clock = env
    many = [f"extra-{i}" for i in range(service.MAX_GAMES)]
    _add_games(many)
    assert make(client, tokens, games=["g0"])["games"] == ["g0"]  # one game is fine
    r = client.post("/admin/majors", json=body(slug="big-one", games=many[:30]), headers=tokens["boss"])
    assert r.status_code == 201 and len(r.json()["games"]) == 30  # more than 20 is fine too
    r = client.post("/admin/majors", json=body(slug="too-big", games=[*many, "g0"]), headers=tokens["boss"])
    assert r.status_code == 422  # over the limit


def test_the_words_can_be_fixed_after_it_ends(env):
    client, tokens, clock = env
    make(client, tokens)
    clock.at = NOW + timedelta(days=20)  # well after the end
    r = client.put("/admin/majors/techemberfest", json=body(tagline="Congratulations to the winners!"), headers=tokens["boss"])
    assert r.status_code == 200, r.text
    assert r.json()["tagline"] == "Congratulations to the winners!"
    # Moving the end to another time in the past is still refused.
    r = client.put("/admin/majors/techemberfest", json=body(ends_at=(NOW + timedelta(days=9)).isoformat()), headers=tokens["boss"])
    assert r.json()["errors"][0]["code"] in ("ends_at", "started")


def test_a_removed_player_cannot_leave_and_enter_again(env):
    client, tokens, clock = env
    make(client, tokens)
    enter(client, tokens, "ada")
    ada = client.get("/admin/majors/techemberfest/entries", headers=tokens["boss"]).json()[0]
    client.post(f"/admin/majors/techemberfest/entries/{ada['player_id']}/remove", json={"note": "Second account"}, headers=tokens["boss"])
    r = client.delete("/majors/techemberfest/entry", headers=tokens["ada"])
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "removed"
    assert client.get("/majors/techemberfest", headers=tokens["ada"]).json()["removed"] is True


def test_runs_stop_counting_while_the_flag_is_off(env, monkeypatch):
    client, tokens, clock = env
    make(client, tokens)
    enter(client, tokens, "ada")
    clock.at = NOW + timedelta(days=2)
    monkeypatch.setattr(settings, "features", "")
    play(client, tokens, "ada", "g0", 100)  # the score itself still saves; the major just doesn't hear it
    monkeypatch.setattr(settings, "features", "majors")
    assert client.get("/majors/techemberfest").json()["standings"] == []
    play(client, tokens, "ada", "g0", 120)
    assert [s["name"] for s in client.get("/majors/techemberfest").json()["standings"]] == ["Ada"]
