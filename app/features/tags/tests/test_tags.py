import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.features.tags import service
from app.features.tags.models import GameTag
from app.main import app
from app.models import Game, User
from app.security import create_token

NAMES = ["lagos-apocalypto", "jollof-rush-3d", "danfo-make-way", "mama-put-rush", "budget-panic", "tower-stack"]


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

    monkeypatch.setattr(settings, "features", "tags")
    monkeypatch.setattr(service, "reviewed", lambda: {})  # the real list is for the real games; these tests have their own
    monkeypatch.setattr(settings, "super_admin_emails", "owner@example.com")
    app.dependency_overrides[get_db] = override_db
    with Session() as db:
        owner = User(email="owner@example.com", name="Owner", google_sub="g-owner")
        ada = User(email="ada@example.com", name="Ada", google_sub="g-ada")
        db.add_all([owner, ada])
        db.flush()
        db.add_all([Game(name=n, owner_id=ada.id, repo_url=f"https://github.com/ada/{n}") for n in NAMES])
        db.commit()
        tokens = {"owner": {"Authorization": f"Bearer {create_token(owner.id)}"}, "ada": {"Authorization": f"Bearer {create_token(ada.id)}"}}
    yield TestClient(app), tokens, Session
    app.dependency_overrides.clear()


def approve_all(client, tokens, slug, games):
    for g in games:
        current = client.get("/admin/tags/review?pending=false&limit=500", headers=tokens["owner"]).json()
        have = next((r["approved"] for r in current if r["game"] == g), [])
        assert client.put(f"/admin/games/{g}/tags", json={"tags": have + [slug]}, headers=tokens["owner"]).status_code == 200


# ---- the rules suggest from a game's name and words

def test_rules_suggest_from_the_name_and_description():
    assert "nigerian-themed" in service.suggest_for("lagos-apocalypto", None)
    assert "horror-and-spooky" in service.suggest_for("lagos-apocalypto", "A zombie survival game set in Lagos")
    assert "racing-and-driving" in service.suggest_for("danfo-make-way", None)
    assert "3d-games" in service.suggest_for("jollof-rush-3d", None)
    assert service.suggest_for("zzz", None) == []


def test_every_rule_compiles_and_every_slug_is_valid_and_unique():
    from app.features.tags.rules import GROUPS
    slugs = [s for tags in GROUPS.values() for s, *_ in tags]
    assert len(slugs) == len(set(slugs)) and all(s == s.lower() and " " not in s and len(s) <= 40 for s in slugs)


# ---- nothing is public until a person approves it

def test_suggestions_are_not_public_until_approved(env):
    client, tokens, Session = env
    assert client.get("/tags").json()["groups"][0]["tags"][0]["slug"] == "web-games"  # only the every-game tags at first
    r = client.post("/admin/tags/suggest", headers=tokens["owner"])
    assert r.status_code == 200 and r.json()["added"] > 0
    slugs = [t["slug"] for g in client.get("/tags").json()["groups"] for t in g["tags"]]
    assert "nigerian-themed" not in slugs  # suggested, not approved
    assert client.get("/tags/nigerian-themed").status_code == 404


def test_suggesting_again_adds_nothing_and_keeps_what_an_admin_decided(env):
    client, tokens, Session = env
    first = client.post("/admin/tags/suggest", headers=tokens["owner"]).json()["added"]
    client.put("/admin/games/lagos-apocalypto/tags", json={"tags": ["nigerian-themed"]}, headers=tokens["owner"])
    assert client.post("/admin/tags/suggest", headers=tokens["owner"]).json()["added"] == 0
    with Session() as db:
        rows = {(r.game_name, r.tag.slug if hasattr(r, "tag") else r.tag_id): r.status for r in db.scalars(select(GameTag)).all()}
    assert first > 0 and "approved" in rows.values() and "rejected" in rows.values()  # the others were turned down


def test_a_tag_gets_a_page_only_with_three_approved_games(env):
    client, tokens, Session = env
    approve_all(client, tokens, "nigerian-themed", NAMES[:2])
    assert client.get("/tags/nigerian-themed").status_code == 404
    approve_all(client, tokens, "nigerian-themed", NAMES[2:3])
    page = client.get("/tags/nigerian-themed")
    assert page.status_code == 200
    body = page.json()
    assert body["tag"]["games"] == 3 and {g["name"] for g in body["games"]} == set(NAMES[:3]) and body["pages"] == 1
    assert "nigerian-themed" in [t["slug"] for g in client.get("/tags").json()["groups"] for t in g["tags"]]


def test_the_every_game_tags_list_every_visible_game_and_skip_hidden_ones(env):
    client, tokens, Session = env
    assert client.get("/tags/web-games").json()["tag"]["games"] == len(NAMES)
    with Session() as db:
        db.get(Game, "tower-stack").hidden = "admin"
        db.commit()
    body = client.get("/tags/web-games").json()
    assert body["tag"]["games"] == len(NAMES) - 1 and "tower-stack" not in {g["name"] for g in body["games"]}


def test_a_hidden_game_does_not_count_for_a_tag(env):
    client, tokens, Session = env
    approve_all(client, tokens, "nigerian-themed", NAMES[:3])
    with Session() as db:
        db.get(Game, NAMES[0]).hidden = "admin"
        db.commit()
    assert client.get("/tags/nigerian-themed").status_code == 404  # only 2 visible games are left


def test_a_games_own_tags_link_only_to_tags_that_have_a_page(env):
    client, tokens, Session = env
    approve_all(client, tokens, "nigerian-themed", NAMES[:3])
    approve_all(client, tokens, "lagos", NAMES[:1])  # only one game: no page yet
    slugs = [t["slug"] for t in client.get(f"/games/{NAMES[0]}/tags").json()]
    assert "nigerian-themed" in slugs and "web-games" not in slugs and "lagos" not in slugs  # the every-game search words are not chips


def test_related_tags_are_the_others_in_the_same_group(env):
    client, tokens, Session = env
    for slug in ("nigerian-themed", "african-themed"):
        approve_all(client, tokens, slug, NAMES[:3])
    related = client.get("/tags/nigerian-themed").json()["related"]
    assert [t["slug"] for t in related] == ["african-themed"]


# ---- the admin side

def test_only_admins_with_the_permission_can_review_and_set_tags(env):
    client, tokens, Session = env
    assert client.get("/admin/tags", headers=tokens["ada"]).status_code == 404  # not an admin
    assert client.put(f"/admin/games/{NAMES[0]}/tags", json={"tags": []}, headers=tokens["ada"]).status_code == 404
    assert client.post("/admin/tags/suggest", headers=tokens["ada"]).status_code == 404
    assert client.get("/admin/tags", headers=tokens["owner"]).status_code == 200


def test_setting_unknown_tags_or_a_missing_game_is_refused(env):
    client, tokens, Session = env
    assert client.put(f"/admin/games/{NAMES[0]}/tags", json={"tags": ["made-up"]}, headers=tokens["owner"]).status_code == 400
    assert client.put("/admin/games/nope/tags", json={"tags": []}, headers=tokens["owner"]).status_code == 404


def test_the_review_queue_lists_games_with_suggestions(env):
    client, tokens, Session = env
    client.post("/admin/tags/suggest", headers=tokens["owner"])
    rows = client.get("/admin/tags/review", headers=tokens["owner"]).json()
    row = next(r for r in rows if r["game"] == "lagos-apocalypto")
    assert "nigerian-themed" in row["suggested"] and row["approved"] == []
    client.put("/admin/games/lagos-apocalypto/tags", json={"tags": row["suggested"]}, headers=tokens["owner"])
    rows = client.get("/admin/tags/review", headers=tokens["owner"]).json()
    assert "lagos-apocalypto" not in [r["game"] for r in rows]  # nothing left to review


def test_every_game_tags_cannot_be_set_by_hand_and_are_always_there(env):
    client, tokens, Session = env
    assert client.put(f"/admin/games/{NAMES[0]}/tags", json={"tags": ["web-games", "lagos"]}, headers=tokens["owner"]).json() == ["lagos"]


# ---- the pages as the server writes them, and the sitemap

import sys  # noqa: E402

SERVER = sys.modules["app.features.tags.server_pages"]
SHELL = '<!doctype html><html><head><!--seo--><title>x</title><!--/seo--></head><body><div id="root"></div></body></html>'


@pytest.fixture
def shell(monkeypatch):
    monkeypatch.setattr(SERVER, "_get_shell", lambda: SHELL)


def test_a_category_page_is_the_whole_page_with_one_h1_links_and_structured_data(env, shell):
    client, tokens, Session = env
    approve_all(client, tokens, "nigerian-themed", NAMES[:3])
    r = client.get("/pages/categories/nigerian-themed")
    assert r.status_code == 200
    assert r.text.count("<h1>") == 1 and "<h1>Nigerian themed games</h1>" in r.text
    assert "<title>Nigerian themed games: play free in your browser · start.ng</title>" in r.text
    assert '<link rel="canonical" href="' in r.text and "/categories/nigerian-themed" in r.text
    assert r.text.count('<article class="game">') == 3 and all(f'href="/games/{n}"' in r.text for n in NAMES[:3])
    assert '"@type": "CollectionPage"' in r.text and '"@type": "ItemList"' in r.text and '"@type": "BreadcrumbList"' in r.text
    assert "3 games" in r.text


def test_a_small_or_unknown_category_is_a_404_with_noindex(env, shell):
    client, tokens, Session = env
    approve_all(client, tokens, "nigerian-themed", NAMES[:2])  # only two games: no page yet
    for slug in ("nigerian-themed", "made-up"):
        r = client.get(f"/pages/categories/{slug}")
        assert r.status_code == 404 and "noindex" in r.text and "<h1>That category isn't here</h1>" in r.text


def test_the_hub_lists_only_categories_that_have_a_page(env, shell):
    client, tokens, Session = env
    approve_all(client, tokens, "nigerian-themed", NAMES[:3])
    approve_all(client, tokens, "lagos", NAMES[:1])
    r = client.get("/pages/categories")
    assert r.status_code == 200 and r.text.count("<h1>") == 1
    assert 'href="/categories/nigerian-themed"' in r.text and 'href="/categories/web-games"' in r.text and 'href="/categories/lagos"' not in r.text


def test_a_second_page_has_its_own_canonical_and_a_page_beyond_the_last_is_404(env, shell, monkeypatch):
    client, tokens, Session = env
    monkeypatch.setattr(service, "PER_PAGE", 2)
    r = client.get("/pages/categories/web-games?page=2")
    assert r.status_code == 200 and "?page=2" in r.text and 'rel="prev"' in r.text and 'rel="next"' in r.text and "(page 2)" in r.text
    assert client.get("/pages/categories/web-games?page=9").status_code == 404


def test_names_and_text_are_escaped(env, shell):
    client, tokens, Session = env
    with Session() as db:
        owner = db.query(User).filter_by(email="ada@example.com").one()
        owner.name = "<img src=x onerror=alert(1)>"
        db.commit()
    html = client.get("/pages/categories/web-games").text
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_the_pages_are_503_when_the_website_shell_is_down(env, monkeypatch):
    client, tokens, Session = env
    monkeypatch.setattr(SERVER, "_get_shell", lambda: None)
    assert client.get("/pages/categories").status_code == 503 and client.get("/pages/categories/web-games").status_code == 503


def test_the_sitemap_lists_categories_that_have_a_page_and_no_others(env):
    client, tokens, Session = env
    approve_all(client, tokens, "nigerian-themed", NAMES[:3])
    approve_all(client, tokens, "lagos", NAMES[:1])
    xml = client.get("/pages/sitemap.xml").text
    assert "/categories</loc>" in xml and "/categories/nigerian-themed</loc>" in xml and "/categories/web-games</loc>" in xml
    assert "/categories/lagos</loc>" not in xml


def test_the_sitemap_lists_nothing_from_a_feature_that_is_off(env, monkeypatch):
    client, tokens, Session = env
    monkeypatch.setattr(settings, "features", "")
    assert "/categories" not in client.get("/pages/sitemap.xml").text


def test_the_server_written_game_page_links_to_the_games_categories(env, monkeypatch):
    client, tokens, Session = env
    monkeypatch.setattr("app.routers.pages._get_shell", lambda: SHELL)
    approve_all(client, tokens, "nigerian-themed", NAMES[:3])
    approve_all(client, tokens, "lagos", NAMES[:1])  # one game only: no page, so no link
    html = client.get(f"/pages/games/{NAMES[0]}").text
    assert 'aria-label="Categories"' in html and 'href="/categories/nigerian-themed"' in html and ">Nigerian themed<" in html
    assert 'href="/categories/web-games"' not in html and "/categories/lagos" not in html


def test_a_game_page_has_no_categories_block_while_the_flag_is_off(env, monkeypatch):
    client, tokens, Session = env
    monkeypatch.setattr("app.routers.pages._get_shell", lambda: SHELL)
    approve_all(client, tokens, "nigerian-themed", NAMES[:3])
    monkeypatch.setattr(settings, "features", "")
    assert 'aria-label="Categories"' not in client.get(f"/pages/games/{NAMES[0]}").text


# ---- tags work on their own

def _rows(Session, game):
    with Session() as db:
        return {r.tag_id: r.status for r in db.scalars(select(GameTag).where(GameTag.game_name == game)).all()}


def test_a_game_is_suggested_and_the_safe_tags_are_approved(env, monkeypatch):
    _, _, Session = env
    monkeypatch.setattr(settings, "tags_auto_approve", True)
    with Session() as db:
        approved = service.refresh_game(db, "jollof-rush-3d")
        again = service.refresh_game(db, "jollof-rush-3d")
        slugs = {t.id: t.slug for t in service.ensure_tags(db).values()}
    assert "3d-games" in approved and again == []
    assert {slugs[i]: s for i, s in _rows(Session, "jollof-rush-3d").items()}["3d-games"] == "approved"


def test_tags_about_a_place_wait_for_a_person(env, monkeypatch):
    _, _, Session = env
    monkeypatch.setattr(settings, "tags_auto_approve", True)
    with Session() as db:
        approved = service.refresh_game(db, "lagos-apocalypto")
        slugs = {t.id: t.slug for t in service.ensure_tags(db).values()}
    assert not set(approved) & service.NEEDS_A_PERSON
    rows = {slugs[i]: s for i, s in _rows(Session, "lagos-apocalypto").items()}
    assert rows["nigerian-themed"] == "suggested"


def test_a_rejected_tag_is_not_brought_back(env, monkeypatch):
    _, _, Session = env
    monkeypatch.setattr(settings, "tags_auto_approve", True)
    with Session() as db:
        tags = service.ensure_tags(db)
        db.add(GameTag(game_name="jollof-rush-3d", tag_id=tags["3d-games"].id, status="rejected"))
        db.commit()
        service.refresh_game(db, "jollof-rush-3d")
    assert _rows(Session, "jollof-rush-3d")[service.ensure_tags(Session()).get("3d-games").id] == "rejected"


def test_switched_off_only_suggests(env):
    _, _, Session = env  # tags_auto_approve is False in tests by default
    with Session() as db:
        assert service.refresh_game(db, "jollof-rush-3d") == []
    assert set(_rows(Session, "jollof-rush-3d").values()) == {"suggested"}


def test_the_job_covers_games_that_have_no_tags_and_respects_the_limit(env, monkeypatch):
    _, _, Session = env
    monkeypatch.setattr(settings, "tags_auto_approve", True)
    with Session() as db:
        assert service.refresh_missing(db, limit=2) == 2
    with Session() as db:
        assert service.refresh_missing(db, limit=50) >= 1


# ---- creators choose their own categories (startng.json); the games that were live first were reviewed

def test_categories_accept_slugs_and_names_and_refuse_the_rest():
    assert service.parse_categories(["endless-runners", "3D games", "Brainy games"]) == ["endless-runners", "3d-games", "brainy-games"]
    for bad in ["racing", ["racing"], ["web-games"], ["endless-runners"] * 1 + ["3d-games", "archery", "survival", "lagos", "educational"], [1]]:
        with pytest.raises(service.manifest.SchemaError):
            service.parse_categories(bad)
    assert service.parse_other("  Rhythm   games ") == "Rhythm games" and service.parse_other(5) is None and len(service.parse_other("x" * 90)) == 40


def test_the_creators_choice_is_approved_and_the_rules_stay_out(env):
    _, _, Session = env
    with Session() as db:
        service.suggest_game(db, "jollof-rush-3d")  # the rules had guessed
        game = db.get(Game, "jollof-rush-3d")
        service.apply_categories(db, game, ["brainy-games"])
        service.apply_other(db, game, "Rhythm")
        service.apply_other(db, game, "Rhythm")
        db.commit()
        assert [t.slug for t in service.tags_of(db, "jollof-rush-3d") if not t.everywhere] == ["brainy-games"]
        assert service.suggest_game(db, "jollof-rush-3d") == 0 and service.refresh_game(db, "jollof-rush-3d") == []
        assert service.review_queue(db, True, 50, 0)[0]["requested"] == ["Rhythm"]


def test_an_admin_choice_is_not_overwritten_by_the_creator(env):
    _, _, Session = env
    with Session() as db:
        service.approve(db, "tower-stack", ["stack-and-timing"])
        service.apply_categories(db, db.get(Game, "tower-stack"), ["survival"])
        assert [t.slug for t in service.tags_of(db, "tower-stack") if not t.everywhere] == ["stack-and-timing"]


def test_reviewed_games_get_their_reviewed_tags(env, monkeypatch):
    _, _, Session = env
    monkeypatch.setattr(service, "reviewed", lambda: {"budget-panic": ["simulation-and-management", "money-and-careers", "educational"]})
    with Session() as db:
        service.suggest_game(db, "budget-panic")  # old rule guesses exist
        assert service.refresh_missing(db, limit=50) >= 1
        assert {t.slug for t in service.tags_of(db, "budget-panic") if not t.everywhere} == {"simulation-and-management", "money-and-careers", "educational"}


def test_the_reviewed_file_is_clean():
    service.reviewed.cache_clear()
    data = json.loads(service.REVIEWED_FILE.read_text())
    assert data and all(isinstance(g, str) and 1 <= len(s) <= service.CREATOR_MAX and all(x in service._CHOOSABLE for x in s) and len(set(s)) == len(s) for g, s in data.items())


# ---- independent creators' games: categories on the game's own page only

def _indie(Session, name="Moon Garden", url="https://moon.example/play"):
    from app.models import IndieGame

    with Session() as db:
        owner = db.scalar(select(User).where(User.email == "ada@example.com"))
        g = IndieGame(owner_id=owner.id, name=name, url=url, description="Grow plants on the moon.", cover=b"x")
        db.add(g)
        db.commit()
        return g.id


def test_indie_categories_are_saved_and_replaced(env):
    _, _, Session = env
    gid = _indie(Session)
    with Session() as db:
        service.set_indie(db, gid, service.check_choice(["Brainy games", "cosy-and-relaxing", "brainy-games"]), "Gardening")
        service.set_indie(db, gid, ["cosy-and-relaxing"], "Gardening")
        db.commit()
        assert service.indie_labels(db, gid) == ["Cosy and relaxing"]
        assert service.set_indie(db, gid, ["archery"]) == ["archery"] and service.indie_labels(db, gid) == ["Archery"]


def test_indie_games_stay_out_of_the_category_counts(env):
    _, _, Session = env
    gid = _indie(Session)
    with Session() as db:
        before = service.counts(db)
        service.set_indie(db, gid, ["archery", "brainy-games"])
        db.commit()
        assert service.counts(db) == before


def test_old_independent_games_get_their_reviewed_categories(env, monkeypatch):
    _, _, Session = env
    gid = _indie(Session)
    monkeypatch.setattr(service, "indie_reviewed", lambda: {gid: ["brainy-games"], 9999: ["archery"]})
    with Session() as db:
        assert service.refresh_indie(db) == 1 and service.refresh_indie(db) == 0
        assert service.indie_labels(db, gid) == ["Brainy games"]


def test_choices_endpoint_and_reviewed_indie_file(env):
    client, _, _ = env
    groups = client.get("/tags/choices").json()
    assert "Words people search" not in [g["name"] for g in groups] and groups[0]["tags"][0]["slug"]
    service.indie_reviewed.cache_clear()
    data = json.loads(service.INDIE_REVIEWED_FILE.read_text())
    assert data and all(1 <= len(s) <= service.CREATOR_MAX and all(x in service._CHOOSABLE for x in s) for s in data.values())


# ---- the footer on server-written pages

def test_the_footer_is_the_new_one_on_server_pages(env):
    client, _, _ = env
    html = client.get("/pages/categories").text
    assert "Built by the HNG community" not in html
    assert 'class="container site-footer"' in html and 'href="/leaderboard"' in html and 'href="/categories">Categories<' in html and "Docs for your AI" in html
