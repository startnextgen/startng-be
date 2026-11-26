import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app import live
from app.db import Base, get_db
from datetime import datetime, timedelta, timezone

from app.live import BAD_TICKET, REPLACED, create_ticket
from app.routers import live as live_router
from app.main import app
from app.models import Game, User
from app.routers.live import get_sessionmaker
from app.security import create_token

STYLE = {"primaryColor": "#a55eea", "body": "female"}


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

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_sessionmaker] = lambda: Session
    monkeypatch.setattr(live, "MOVE_EVERY", 0)  # don't drop quick test moves
    monkeypatch.setattr(live, "rooms", {})
    with Session() as db:
        ada = User(email="ada@example.com", name="Ada", character={"name": "Runner Ada", "style": STYLE})
        tunde = User(email="tunde@example.com", name="Tunde")
        db.add_all([ada, tunde])
        db.flush()
        db.add(Game(name="hangout", owner_id=ada.id, repo_url="https://github.com/ada/hangout"))
        db.commit()
        ids = {"ada": ada.id, "tunde": tunde.id}
    # One client for the whole test, entered, so every websocket shares its event loop (as in the rooms tests). Without it each
    # websocket gets its own loop and thread, a broadcast writes into another player's connection from the wrong loop, and
    # now and then that player never wakes up: the test hangs until CI's 15-minute limit.
    with TestClient(app) as client:
        yield client, ids
    app.dependency_overrides.clear()


def url(user_id, game="hangout"):
    return f"/games/{game}/live?ticket={create_ticket(user_id, game)}"


def test_ticket_needs_sign_in_and_a_real_game(env):
    client, ids = env
    auth = {"Authorization": f"Bearer {create_token(ids['ada'])}"}
    assert client.post("/games/hangout/ticket").status_code == 401
    assert client.post("/games/nope/ticket", headers=auth).status_code == 404
    res = client.post("/games/hangout/ticket", headers=auth)
    assert res.status_code == 200 and res.json()["expires_in"] == 60


def test_a_ticket_is_not_a_sign_in(env):
    client, ids = env
    ticket = create_ticket(ids["ada"], "hangout")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {ticket}"}).status_code == 401


@pytest.mark.parametrize("path", [
    "/games/hangout/live",
    "/games/hangout/live?ticket=nonsense",
    "/games/hangout/live?ticket={other_game}",
])
def test_bad_tickets_are_turned_away(env, path):
    client, ids = env
    path = path.format(other_game=create_ticket(ids["ada"], "lanedash"))
    with client.websocket_connect(path) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == BAD_TICKET


def test_sign_in_token_is_not_a_ticket(env):
    client, ids = env
    with client.websocket_connect(f"/games/hangout/live?ticket={create_token(ids['ada'])}") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == BAD_TICKET


def test_two_players_see_each_other(env):
    client, ids = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        welcome = ada.receive_json()
        assert welcome["type"] == "welcome" and welcome["you"] == ids["ada"]
        assert [(p["name"], p["style"]) for p in welcome["players"]] == [("Runner Ada", STYLE)]

        with client.websocket_connect(url(ids["tunde"])) as tunde:
            seen = tunde.receive_json()
            assert {p["name"] for p in seen["players"]} == {"Runner Ada", "Tunde"}
            joined = ada.receive_json()
            assert joined["type"] == "join" and joined["player"]["name"] == "Tunde"
            assert joined["player"]["style"] is None  # no character yet: games use the default look

            # Positions are kept inside the park, and junk is ignored.
            tunde.send_text("not json")
            tunde.send_json({"type": "move", "x": "far", "z": 0, "ry": 0})
            tunde.send_json({"type": "move", "x": 999, "z": -3.5, "ry": 1.2, "moving": True})
            moved = ada.receive_json()
            assert moved == {"type": "move", "id": ids["tunde"], "x": 60.0, "z": -3.5, "ry": 1.2, "moving": True}

            tunde.send_json({"type": "wave"})
            tunde.send_json({"type": "wave"})  # too soon after the first: dropped
            assert ada.receive_json() == {"type": "wave", "id": ids["tunde"]}

            ada.send_json({"type": "wave"})
            assert tunde.receive_json() == {"type": "wave", "id": ids["ada"]}

        left = ada.receive_json()
        assert left == {"type": "leave", "id": ids["tunde"], "seat": None}  # gone, seat and all


def test_joining_again_replaces_the_old_connection(env):
    client, ids = env
    with client.websocket_connect(url(ids["ada"])) as first:
        first.receive_json()
        with client.websocket_connect(url(ids["ada"])) as second:
            assert [p["id"] for p in second.receive_json()["players"]] == [ids["ada"]]
            with pytest.raises(WebSocketDisconnect) as closed:
                first.receive_json()
            assert closed.value.code == REPLACED


def test_only_people_in_the_room_are_shown(env, monkeypatch):
    client, ids = env
    monkeypatch.setattr(live, "MAX_SEATS", 2)
    Session = app.dependency_overrides[get_sessionmaker]()
    now = datetime.now(timezone.utc)
    with Session() as db:
        # Active on start.ng a moment ago, but not in the Hangout: not shown, no seat.
        chioma = User(email="chioma@example.com", name="Chioma", last_seen_at=now)
        musa = User(email="musa@example.com", name="Musa")
        db.add_all([chioma, musa])
        db.commit()
        ids["chioma"], ids["musa"] = chioma.id, musa.id

    with client.websocket_connect(url(ids["ada"])) as ada:
        welcome = ada.receive_json()
        assert [p["name"] for p in welcome["players"]] == ["Runner Ada"]
        assert welcome["seats"] == [{"id": ids["ada"], "name": "Runner Ada", "style": STYLE, "seat": 0}]
        me = welcome["players"][0]
        assert me["seat"] == 0 and me["x"] is None and me["z"] is None  # standing at their seat

        with client.websocket_connect(url(ids["tunde"])) as tunde:
            assert {r["name"]: r["seat"] for r in tunde.receive_json()["seats"]} == {"Runner Ada": 0, "Tunde": 1}
            ada.receive_json()  # Tunde joined
            with client.websocket_connect(url(ids["musa"])) as musa:
                musa_me = next(p for p in musa.receive_json()["players"] if p["id"] == ids["musa"])
                assert musa_me["seat"] is None and musa_me["x"] is not None  # no seat left: starts mid-park
                ada.receive_json()  # Musa joined
            assert ada.receive_json() == {"type": "leave", "id": ids["musa"], "seat": None}
        assert ada.receive_json() == {"type": "leave", "id": ids["tunde"], "seat": None}

        # Tunde's seat is free again, and people who left aren't listed.
        with client.websocket_connect(url(ids["chioma"])) as chioma:
            welcome = chioma.receive_json()
            assert {p["name"] for p in welcome["players"]} == {"Runner Ada", "Chioma"}
            assert {r["name"]: r["seat"] for r in welcome["seats"]} == {"Runner Ada": 0, "Chioma": 1}


def test_activity_is_recorded(env):
    client, ids = env
    auth = {"Authorization": f"Bearer {create_token(ids['tunde'])}"}
    Session = app.dependency_overrides[get_sessionmaker]()

    def seen():
        with Session() as db:
            return db.get(User, ids["tunde"]).last_seen_at

    assert seen() is None
    client.get("/auth/me", headers=auth)
    first = seen()
    assert first is not None
    client.get("/auth/me", headers=auth)
    assert seen() == first  # not rewritten on every request
    client.post("/games/hangout/ticket", headers=auth)
    assert seen() > first  # coming into the Hangout always counts

def test_chat_reaches_everyone_and_late_joiners_see_it(env):
    client, ids = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        assert ada.receive_json()["chat"] == []
        with client.websocket_connect(url(ids["tunde"])) as tunde:
            tunde.receive_json()
            ada.receive_json()  # tunde joined

            tunde.send_json({"type": "chat", "text": "  hello\x00world  "})
            said = ada.receive_json()
            assert tunde.receive_json() == said  # the sender gets it too
            assert said["type"] == "chat" and said["id"] == ids["tunde"]
            assert said["name"] == "Tunde" and said["text"] == "hello world"
            assert isinstance(said["at"], int)
        left = ada.receive_json()
        assert left["type"] == "leave" and left["id"] == ids["tunde"]

        with client.websocket_connect(url(ids["tunde"])) as again:
            history = again.receive_json()["chat"]
            assert [(m["name"], m["text"]) for m in history] == [("Tunde", "hello world")]


def test_chat_is_cleaned_and_rate_limited(env):
    client, ids = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        ada.receive_json()
        with client.websocket_connect(url(ids["tunde"])) as tunde:
            tunde.receive_json()
            ada.receive_json()

            tunde.send_json({"type": "chat", "text": "   "})  # nothing to say
            tunde.send_json({"type": "chat", "text": 123})  # not text
            tunde.send_json({"type": "chat"})
            tunde.send_json({"type": "chat", "text": "x" * 300})  # cut to the limit
            tunde.send_json({"type": "chat", "text": "too soon"})  # right after the last: dropped
            tunde.send_json({"type": "wave"})  # marks where the dropped ones would have been
            said = ada.receive_json()
            assert said["type"] == "chat" and said["text"] == "x" * live.MAX_CHAT
            assert ada.receive_json() == {"type": "wave", "id": ids["tunde"]}


def test_a_room_remembers_only_its_last_messages(env, monkeypatch):
    monkeypatch.setattr(live, "CHAT_EVERY", 0)
    monkeypatch.setattr(live, "CHAT_HISTORY", 2)
    client, ids = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        ada.receive_json()
        for text in ("one", "two", "three"):
            ada.send_json({"type": "chat", "text": text})
            assert ada.receive_json()["text"] == text
    with client.websocket_connect(url(ids["tunde"])) as tunde:
        assert [m["text"] for m in tunde.receive_json()["chat"]] == ["two", "three"]
