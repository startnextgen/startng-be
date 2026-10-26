import json

import pytest

from app.leaderboard import parse_description
from app.models import Game
from app.routers import pages
from tests.test_publish import env  # noqa: F401 (fixture)

SHELL = (
    '<!doctype html><html><head><!--seo-->\n    <title>start.ng</title>\n    <!--/seo--></head>'
    '<body><div id="root"></div><script src="/assets/app.js"></script></body></html>'
)


@pytest.fixture(autouse=True)
def shell(monkeypatch):
    monkeypatch.setattr(pages, "_shell", None)
    monkeypatch.setattr(pages, "_get_shell", lambda: SHELL)


def test_description_from_startng_json():
    assert parse_description(b'{"description": "  Dodge   the\\nlava.  "}') == "Dodge the lava."
    assert parse_description(b'{"description": "x"}') == "x"
    assert parse_description(b'{"description": 5}') is None
    assert parse_description(b'{"leaderboard": {}}') is None
    assert parse_description(b"not json") is None
    assert len(parse_description(json.dumps({"description": "a" * 1000}).encode())) == 300


def add_game(client_env, name="lane-dash", description=None):
    # The env fixture makes users but no games; add one straight to the database.
    from app.db import get_db
    from app.main import app

    db = next(app.dependency_overrides[get_db]())
    db.add(Game(name=name, owner_id=1, repo_url=f"https://github.com/ada/{name}", description=description))
    db.commit()


def test_game_page_is_rendered_for_crawlers(env):
    client, *_ = env
    add_game(env, description="Dodge the lava <b>fast</b>.")
    res = client.get("/pages/games/lane-dash")
    assert res.status_code == 200
    text = res.text
    assert "<title>Lane Dash · Play free on start.ng</title>" in text
    assert 'rel="canonical" href="https://start.ng/games/lane-dash"' in text
    assert 'property="og:title" content="Lane Dash · Play free on start.ng"' in text
    assert "Dodge the lava &lt;b&gt;fast&lt;/b&gt;." in text  # escaped, never raw HTML
    assert "<b>fast</b>" not in text
    assert '"@type": "VideoGame"' in text
    assert '<div id="root"><main><h1>Lane Dash</h1>' in text
    assert "/assets/app.js" in text  # the app still loads
    assert "index, follow" in text


def test_game_page_without_description_writes_one(env):
    client, *_ = env
    add_game(env)
    text = client.get("/pages/games/lane-dash").text
    assert "Play Lane Dash free in your browser on start.ng, a game by Ada." in text


def test_unknown_game_is_a_404_that_stays_out_of_search(env):
    client, *_ = env
    res = client.get("/pages/games/nope")
    assert res.status_code == 404
    assert "noindex" in res.text
    assert "Game not found" in res.text


def test_bad_names_are_refused(env):
    client, *_ = env
    assert client.get("/pages/games/Bad_Name").status_code == 422


def test_page_unavailable_when_the_website_cant_be_read(env, monkeypatch):
    client, *_ = env
    monkeypatch.setattr(pages, "_get_shell", lambda: None)
    assert client.get("/pages/games/lane-dash").status_code == 503


def test_sitemap_lists_pages_and_games(env):
    client, *_ = env
    add_game(env, "lane-dash")
    add_game(env, "moon-garden")
    res = client.get("/pages/sitemap.xml")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/xml")
    for loc in ["https://start.ng/", "https://start.ng/games", "https://start.ng/about", "https://start.ng/games/lane-dash", "https://start.ng/games/moon-garden"]:
        assert f"<loc>{loc}</loc>" in res.text


def test_publishing_saves_the_description_and_it_reaches_the_api(env):
    from tests.test_publish import GAME, make_zip

    client, _, tokens, archives = env
    manifest = json.dumps({"description": "Dodge the lava and climb as high as you can."})
    archives["zip"] = make_zip({**GAME, "startng.json": manifest})
    res = client.post("/games/publish", json={"repo_url": "https://github.com/ada/Lane-Dash"}, headers=tokens["ada"])
    assert res.status_code == 200, res.text
    listed = client.get("/games").json()
    assert listed[0]["description"] == "Dodge the lava and climb as high as you can."
    assert "Dodge the lava and climb as high as you can." in client.get("/pages/games/lane-dash").text

    # Publishing again without a description clears it: the repo is the source of truth.
    archives["zip"] = make_zip(GAME)
    client.post("/games/publish", json={"repo_url": "https://github.com/ada/Lane-Dash"}, headers=tokens["ada"])
    assert client.get("/games").json()[0]["description"] is None


def test_description_is_found_when_the_builder_didnt_write_one(env):
    from tests.test_publish import GAME, make_zip

    client, _, tokens, archives = env
    repo = {"repo_url": "https://github.com/ada/Lane-Dash"}

    # 1. the game's own <meta name="description">
    page = '<!doctype html><meta content="Dodge the lava, climb the tower &amp; beat your friends." name="description"><script type="module" src="src/game.js"></script>'
    archives["zip"] = make_zip({**GAME, "index.html": page})
    assert client.post("/games/publish", json=repo, headers=tokens["ada"]).status_code == 200
    assert client.get("/games").json()[0]["description"] == "Dodge the lava, climb the tower & beat your friends."

    # 2. otherwise the README's first real paragraph (not the title, badges or pictures)
    readme = "# Lane Dash\n\n![shot](shot.png)\n[![build](b.svg)](x)\n\nA **fast** runner: [dodge](https://x.y) the lava.\nIt gets harder.\n\n## Play\nOpen it.\n"
    archives["zip"] = make_zip({**GAME, "README.md": readme})
    client.post("/games/publish", json=repo, headers=tokens["ada"])
    assert client.get("/games").json()[0]["description"] == "A fast runner: dodge the lava. It gets harder."

    # 3. startng.json always wins over both
    archives["zip"] = make_zip({**GAME, "README.md": readme, "startng.json": json.dumps({"description": "From startng.json, written by the builder."})})
    client.post("/games/publish", json=repo, headers=tokens["ada"])
    assert client.get("/games").json()[0]["description"] == "From startng.json, written by the builder."

    # 4. nothing to go on: no description (the page writes a simple line)
    archives["zip"] = make_zip(GAME)
    client.post("/games/publish", json=repo, headers=tokens["ada"])
    assert client.get("/games").json()[0]["description"] is None


def add_indie(name="Moon Garden", description="Grow glowing plants on the moon before the sun comes up."):
    from app.db import get_db
    from app.main import app
    from app.models import IndieGame

    db = next(app.dependency_overrides[get_db]())
    game = IndieGame(owner_id=1, name=name, url=f"https://ada.itch.io/{name.lower().replace(' ', '-')}", description=description, cover=b"x")
    db.add(game)
    db.commit()
    return game.id


def test_indie_game_page_is_rendered_for_crawlers(env):
    client, *_ = env
    game_id = add_indie(description="Grow plants <i>fast</i> & win.")
    res = client.get(f"/pages/creators/{game_id}")
    assert res.status_code == 200
    text = res.text
    assert "<title>Moon Garden · Play free on start.ng</title>" in text
    assert f'rel="canonical" href="https://start.ng/creators/{game_id}-moon-garden"' in text
    assert f'content="https://api.start.ng/indie/{game_id}/cover.webp"' in text
    assert "Grow plants &lt;i&gt;fast&lt;/i&gt; &amp; win." in text and "<i>fast</i>" not in text
    assert "an independent creator" in text and '"@type": "VideoGame"' in text
    assert "/assets/app.js" in text


def test_unknown_indie_game_is_a_404(env):
    client, *_ = env
    res = client.get("/pages/creators/999")
    assert res.status_code == 404 and "noindex" in res.text
    assert client.get("/pages/creators/abc").status_code == 422


def test_one_indie_game_as_json_and_in_the_sitemap(env):
    client, _, tokens, _ = env
    game_id = add_indie()
    one = client.get(f"/indie/{game_id}", headers=tokens["ada"]).json()
    assert (one["name"], one["by"], one["mine"]) == ("Moon Garden", "Ada", True)
    assert client.get(f"/indie/{game_id}").json()["mine"] is False
    assert client.get("/indie/999").status_code == 404
    assert f"<loc>https://start.ng/creators/{game_id}-moon-garden</loc>" in client.get("/pages/sitemap.xml").text


def test_homepage_text_lists_every_game_a_to_z(env):
    client, *_ = env
    add_game(env, "moon-garden")
    add_game(env, "lane-dash")
    res = client.get("/pages/home")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    text = res.text
    assert not text.startswith("<!doctype") and "<html" not in text  # only what goes inside #root
    assert "<h1>Good vibes.<br><mark>Good games.</mark></h1>" in text
    assert "Every game on start.ng, A to Z (2)" in text
    lane, moon = text.index('<a href="/games/lane-dash">Lane Dash</a>'), text.index('<a href="/games/moon-garden">Moon Garden</a>')
    assert lane < moon  # A to Z
    assert "<span>by Ada</span>" in text
    assert "See all" not in text  # only past the 8 cards the homepage shows


def test_homepage_text_leaves_out_hidden_games_and_the_hangout(env):
    from app.db import get_db
    from app.main import app

    client, *_ = env
    add_game(env, "lane-dash")
    add_game(env, "hangout")
    db = next(app.dependency_overrides[get_db]())
    db.add(Game(name="copy-cat", owner_id=1, repo_url="https://github.com/ada/copy-cat", hidden="reported"))
    db.commit()
    text = client.get("/pages/home").text
    assert "/games/lane-dash" in text
    assert "/games/hangout" not in text and "/games/copy-cat" not in text
    assert "A to Z (1)" in text


def test_homepage_text_has_the_about_and_questions_and_escapes_names(env):
    from app.db import get_db
    from app.main import app
    from app.models import User

    client, *_ = env
    db = next(app.dependency_overrides[get_db]())
    db.get(User, 1).character = {"name": "Ada <b>Bold</b>"}
    db.commit()
    add_game(env, "lane-dash")
    text = client.get("/pages/home").text
    assert "by Ada &lt;b&gt;Bold&lt;/b&gt;" in text and "<b>Bold</b>" not in text
    assert "Nigeria&#x27;s home for browser games" in text  # escaped like every other text
    assert "<summary>Is it free?</summary>" in text
    assert '<a class="btn" href="/docs">Read the docs</a>' in text
    assert '<p><a href="/about">More about start.ng →</a></p><h3>Questions</h3>' in text  # the About on the homepage leads to the About page


def test_homepage_with_no_games_and_with_many(env):
    client, *_ = env
    assert "A to Z" not in client.get("/pages/home").text  # nothing to list yet
    for i in range(9):
        add_game(env, f"game-{i}")
    assert '<a class="btn" href="/games">See all 9 games →</a>' in client.get("/pages/home").text


def test_games_page_text_lists_the_hangout_then_newest_first(env):
    from datetime import datetime, timedelta, timezone

    from app.db import get_db
    from app.main import app

    client, *_ = env
    add_game(env, "moon-garden")
    add_game(env, "lane-dash")
    db = next(app.dependency_overrides[get_db]())
    now = datetime.now(timezone.utc)
    db.get(Game, "moon-garden").updated_at = now
    db.get(Game, "lane-dash").updated_at = now - timedelta(days=1)
    db.add(Game(name="copy-cat", owner_id=1, repo_url="https://github.com/ada/copy-cat", hidden="reported"))
    db.commit()
    res = client.get("/pages/games")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    text = res.text
    assert "<html" not in text  # only what goes inside #root
    assert "<h1>All games from the community</h1>" in text
    assert '<span class="count">3 games</span>' in text  # the Hangout counts, as on the real page
    hangout, moon, lane = (text.index(f'<a href="{href}">') for href in ("/hangout", "/games/moon-garden", "/games/lane-dash"))
    assert hangout < moon < lane
    assert "<p>moon-garden.start.ng</p>" in text and '<span class="by">by Ada</span>' in text
    assert "copy-cat" not in text
    assert "pagination" not in text  # one page only


def test_games_page_text_pages_like_the_app(env):
    client, *_ = env
    for i in range(44):  # 45 cards with the Hangout: 3 pages
        add_game(env, f"game-{i:02d}")
    first = client.get("/pages/games").text
    assert '<span class="count">1–20 of 45</span>' in first
    assert '<span class="off">← Previous</span>' in first
    assert '<a href="/games?page=2" rel="next" aria-label="Next page">Next →</a>' in first
    assert '<a href="/games" aria-label="Page 1" aria-current="page">1</a>' in first
    last = client.get("/pages/games?page=3").text
    assert '<span class="count">41–45 of 45</span>' in last
    assert '<a href="/games?page=2" rel="prev"' in last and "/hangout" not in last.split("<footer")[0]  # the footer links to the Hangout; the list must not
    assert client.get("/pages/games?page=4").status_code == 404  # past the end: nginx serves the 404 page
    assert client.get("/pages/games?page=0").status_code == 422


def test_games_page_text_with_no_games_says_so(env):
    client, *_ = env
    text = client.get("/pages/games").text
    assert "No community games yet" in text and '<a href="/hangout">The Hangout</a>' in text
    assert '<span class="count">1 game</span>' in text


def test_creators_page_text_lists_the_newest_first(env):
    client, *_ = env
    older = add_indie("Moon Garden", "Grow plants.")
    newer = add_indie("Sky <Hop>", "Jump.")
    res = client.get("/pages/creators")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    text = res.text
    assert "<html" not in text  # only what goes inside #root
    assert "<h1>Games by independent creators</h1>" in text
    assert '<span class="count">2 games</span>' in text
    assert text.index(f'href="/creators/{newer}-sky-hop"') < text.index(f'href="/creators/{older}-moon-garden"')
    assert "<h3>Sky &lt;Hop&gt;</h3>" in text and "<Hop>" not in text
    assert "<p>ada.itch.io</p>" in text and '<span class="by">by Ada</span>' in text
    assert f'src="https://api.start.ng/indie/{older}/cover.webp"' in text
    assert "pagination" not in text


def test_creators_page_text_pages_like_games(env):
    client, *_ = env
    assert "No games from independent creators yet" in client.get("/pages/creators").text
    for i in range(21):
        add_indie(f"Game {i}")
    first = client.get("/pages/creators").text
    assert '<span class="count">1–20 of 21</span>' in first
    assert '<a href="/creators?page=2" rel="next" aria-label="Next page">Next →</a>' in first
    last = client.get("/pages/creators?page=2").text
    assert '<span class="count">21–21 of 21</span>' in last and '<a href="/creators" rel="prev"' in last
    assert client.get("/pages/creators?page=3").status_code == 404  # past the end: nginx serves the 404 page
    assert client.get("/pages/creators?page=0").status_code == 422


def test_sitemap_lists_feature_pages_only_while_they_are_on(env, monkeypatch):
    from app.config import settings

    client, *_ = env
    monkeypatch.setattr(settings, "features", "")
    text = client.get("/pages/sitemap.xml").text
    assert all(p not in text for p in ("/spotlight", "/shop", "/friends", "<loc>https://start.ng/by</loc>"))
    monkeypatch.setattr(settings, "features", "spotlight,wardrobe,creator_hub,private_tournaments")
    text = client.get("/pages/sitemap.xml").text
    for p in ("/spotlight", "/shop", "/by", "/friends"):
        assert f"<loc>https://start.ng{p}</loc>" in text


def test_friends_page_text_only_while_the_feature_is_on(env, monkeypatch):
    from app.config import settings

    client, *_ = env
    monkeypatch.setattr(settings, "features", "")
    assert client.get("/pages/friends").status_code == 404
    monkeypatch.setattr(settings, "features", "private_tournaments")
    text = client.get("/pages/friends").text
    assert "<h1>Tournament with friends</h1>" in text and "Pick up to 5 games" in text
    assert "1 day, 3 days, 1 week, 2 weeks or 1 month" in text and "<html" not in text
