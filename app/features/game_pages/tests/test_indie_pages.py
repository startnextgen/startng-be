"""About and FAQ pages for games by independent creators (app/features/game_pages/indie.py)."""
import json
import re
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import IndieGame, User
from app.routers import pages as core_pages

INDIE = sys.modules["app.features.game_pages.indie"]
SHELL = '<!doctype html><html><head><!--seo--><title>x</title><!--/seo--></head><body><div id="root"></div></body></html>'
DESCRIPTION = "Grow glowing plants on the moon before the sun comes up."


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(settings, "features", "game_pages")
    monkeypatch.setattr(INDIE, "_get_shell", lambda: SHELL)
    monkeypatch.setattr(core_pages, "_get_shell", lambda: SHELL)
    app.dependency_overrides[get_db] = override_db
    with Session() as db:
        ada = User(email="ada@example.com", name="Ada", google_sub="g-ada")
        db.add(ada)
        db.flush()
        game = IndieGame(owner_id=ada.id, name="Moon Garden", url="https://www.ada.itch.io/moon-garden", description=DESCRIPTION, cover=b"x")
        db.add(game)
        db.commit()
        game_id = game.id
    yield TestClient(app), game_id, Session
    app.dependency_overrides.clear()


def ld(text: str) -> list[dict]:
    return [json.loads(m.replace("\\u003c", "<")) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)]


def test_the_about_page_says_only_what_we_know(env):
    client, game_id, _ = env
    res = client.get(f"/pages/creators/{game_id}/about")
    assert res.status_code == 200 and "text/html" in res.headers["content-type"]
    text = res.text
    assert "<title>About Moon Garden · start.ng</title>" in text
    assert f'rel="canonical" href="https://start.ng/creators/{game_id}-moon-garden/about"' in text
    assert "<h1>About Moon Garden</h1>" in text
    assert DESCRIPTION in text
    assert "Moon Garden was made by Ada, an independent creator." in text
    assert "hosted by its creator at ada.itch.io, outside start.ng" in text  # "www." is dropped
    assert "index, follow" in text
    # Nothing we can't know: not free, not mobile, nothing to install.
    for claim in ("free", "phone", "mobile", "install", "download"):
        assert claim not in text.lower().replace("freely", ""), claim
    kinds = {d["@type"] for d in ld(text)}
    assert {"BreadcrumbList", "VideoGame"} <= kinds


def test_the_faq_page_answers_questions_from_the_same_facts(env):
    client, game_id, _ = env
    text = client.get(f"/pages/creators/{game_id}/faq").text
    assert "<title>Moon Garden FAQ · start.ng</title>" in text
    faq = next(d for d in ld(text) if d["@type"] == "FAQPage")
    questions = [q["name"] for q in faq["mainEntity"]]
    assert questions == ["What is Moon Garden?", "Who made Moon Garden?", "Where can I play Moon Garden?", "Is Moon Garden made by start.ng?",
                         "When was Moon Garden added to start.ng?", "Does Moon Garden have a leaderboard on start.ng?"]
    answers = {q["name"]: q["acceptedAnswer"]["text"] for q in faq["mainEntity"]}
    assert answers["What is Moon Garden?"] == DESCRIPTION
    assert re.search(r"\bada\.itch\.io\b", answers["Where can I play Moon Garden?"])
    assert answers["Is Moon Garden made by start.ng?"].startswith("No.")
    assert re.fullmatch(r"Moon Garden was added to start.ng in [A-Z][a-z]+ \d{4}\.", answers["When was Moon Garden added to start.ng?"])
    assert answers["Does Moon Garden have a leaderboard on start.ng?"].startswith("No.")


def test_the_about_page_has_the_facts_at_a_glance(env):
    client, game_id, _ = env
    text = client.get(f"/pages/creators/{game_id}/about").text
    assert "<h2>Moon Garden at a glance</h2>" in text
    assert "Made by Ada, an independent creator" in text
    assert re.search(r"On start.ng since [A-Z][a-z]+ \d{4}", text)
    assert "Hosted by its creator at ada.itch.io, outside start.ng" in text
    assert "Opens in a new tab from its page on start.ng" in text


def test_other_independent_games_are_linked_as_cards_read_live(env):
    client, game_id, Session = env
    with Session() as db:
        ada = db.query(User).filter_by(email="ada@example.com").one()
        tunde = User(email="tunde@example.com", name="Tunde", google_sub="g-tunde")
        db.add(tunde)
        db.flush()
        mine = IndieGame(owner_id=ada.id, name="Star Hop", url="https://ada.itch.io/star-hop", description="Hop between stars before they go out.", cover=b"x")
        theirs = IndieGame(owner_id=tunde.id, name="Dungeon Dave", url="https://tunde.itch.io/dave", description="A tiny dungeon crawler for one sitting.", cover=b"x")
        db.add_all([mine, theirs])
        db.commit()
        mine_id, theirs_id = mine.id, theirs.id
    page = client.get(f"/pages/creators/{game_id}/about").text
    assert "More games by Ada" in page and "More games by independent creators" in page
    assert f'class="game-link" href="/creators/{mine_id}-star-hop"' in page
    assert f'class="game-link" href="/creators/{theirs_id}-dungeon-dave"' in page
    assert f'class="game-link" href="/creators/{game_id}-moon-garden"' not in page  # not itself
    assert f"/indie/{mine_id}/cover.webp" in page


def test_what_a_creator_typed_is_never_written_as_html(env):
    client, game_id, Session = env
    with Session() as db:
        game = db.get(IndieGame, game_id)
        game.name = "Moon <b>Garden</b>"
        game.description = "First paragraph <script>alert(1)</script>.\n\nSecond one & more."
        db.commit()
    for kind in ("about", "faq"):
        text = client.get(f"/pages/creators/{game_id}/{kind}").text
        assert "<script>alert" not in text and "<b>Garden</b>" not in text
    about = client.get(f"/pages/creators/{game_id}/about").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in about
    assert "<p>Second one &amp; more.</p>" in about  # a blank line starts a new paragraph


def test_a_missing_game_is_a_real_404_that_stays_out_of_search(env):
    client, _, _ = env
    res = client.get("/pages/creators/9999/about")
    assert res.status_code == 404 and "noindex" in res.text and "Page not found" in res.text
    assert client.get("/pages/creators/1/walkthrough").status_code == 422  # only about and faq exist for these games
    assert client.get("/pages/creators/abc/about").status_code == 422


def test_unavailable_when_the_website_cant_be_read(env, monkeypatch):
    client, game_id, _ = env
    monkeypatch.setattr(INDIE, "_get_shell", lambda: None)
    assert client.get(f"/pages/creators/{game_id}/about").status_code == 503


def test_everything_answers_404_while_the_flag_is_off(env, monkeypatch):
    client, game_id, _ = env
    monkeypatch.setattr(settings, "features", "")
    assert client.get(f"/pages/creators/{game_id}/about").status_code == 404
    assert client.get(f"/pages/creators/{game_id}/faq").status_code == 404
    assert client.get(f"/indie/{game_id}/pages").status_code == 404
    page = client.get(f"/pages/creators/{game_id}").text  # the game's own page has no block, and the sitemap no addresses
    assert "More about this game" not in page
    sitemap = client.get("/pages/sitemap.xml").text
    assert "/about</loc>" in sitemap and f"/creators/{game_id}-moon-garden/about" not in sitemap  # the site's own About, but no game's


def test_the_pages_are_linked_and_listed(env):
    client, game_id, _ = env
    listed = client.get(f"/indie/{game_id}/pages").json()
    assert listed == [
        {"kind": "about", "label": "About Moon Garden", "path": f"/creators/{game_id}-moon-garden/about"},
        {"kind": "faq", "label": "Moon Garden FAQ", "path": f"/creators/{game_id}-moon-garden/faq"},
    ]
    assert client.get("/indie/9999/pages").json() == []
    page = client.get(f"/pages/creators/{game_id}").text
    assert 'aria-label="More about this game"' in page
    assert f'href="/creators/{game_id}-moon-garden/about"' in page and f'href="/creators/{game_id}-moon-garden/faq"' in page
    sitemap = client.get("/pages/sitemap.xml").text
    for kind in ("about", "faq"):
        assert f"<loc>https://start.ng/creators/{game_id}-moon-garden/{kind}</loc>" in sitemap
