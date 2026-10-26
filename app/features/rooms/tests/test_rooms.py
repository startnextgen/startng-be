import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.db import Base, get_db
from app.features.rooms import core
from app.features.rooms.games import tictactoe
from app.live import create_ticket
from app.main import app
from app.models import Game, User
from app.routers.live import get_sessionmaker

GAME = "tic-tac-toe"


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
    monkeypatch.setattr(settings, "features", "rooms")
    monkeypatch.setattr(core, "rooms", {})
    monkeypatch.setattr(core, "user_room", {})
    monkeypatch.setattr(core, "queues", {})
    monkeypatch.setattr(core, "bad_codes", {})
    now = [1000.0]
    monkeypatch.setattr(core, "clock", lambda: now[0])
    with Session() as db:
        users = [User(email=f"{n}@example.com", name=n.title(), character={"name": n.title(), "style": {"body": "male"}}) for n in ("ada", "tunde", "chi")]
        db.add_all(users)
        db.flush()
        db.add(Game(name=GAME, owner_id=users[0].id, repo_url="https://github.com/ada/ttt"))
        db.add(Game(name="chess", owner_id=users[0].id, repo_url="https://github.com/ada/chess"))
        db.commit()
        ids = {u.name.lower(): u.id for u in users}
    with TestClient(app) as client:
        yield client, ids, now
    app.dependency_overrides.clear()


def url(user_id, room="new", game=GAME):
    return f"/rooms/live?game={game}&ticket={create_ticket(user_id, game)}&room={room}"


def test_it_is_off_until_the_flag_is_on(env, monkeypatch):
    client, ids, _ = env
    monkeypatch.setattr(settings, "features", "")
    assert client.get("/rooms/online?game=tic-tac-toe").status_code == 404
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url(ids["ada"])):
            pass


def test_a_bad_ticket_is_refused(env):
    client, ids, _ = env
    with client.websocket_connect(f"/rooms/live?game={GAME}&ticket=nope&room=new") as ws:
        with pytest.raises(WebSocketDisconnect) as err:
            ws.receive_json()
    assert err.value.code == core.BAD_TICKET


def test_a_friend_joins_by_code_and_they_play_to_a_win(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        hello = ada.receive_json()
        assert hello["type"] == "welcome" and hello["status"] == "waiting" and hello["you"] == 0
        code = hello["room"]
        assert len(code) == 6 and set(code) <= set(core.CODE_ALPHABET)
        with client.websocket_connect(url(ids["tunde"], code.lower())) as tunde:
            assert ada.receive_json()["type"] == "join"
            ada_start, tunde_start = ada.receive_json(), tunde.receive_json()
            assert ada_start["status"] == tunde_start["status"] == "playing"
            assert tunde_start["you"] == 1 and tunde_start["players"][0]["name"] == "Ada"
            assert tunde_start["turn"] == 0
            # X takes the top row; O plays elsewhere
            for who, cell in ((ada, 0), (tunde, 3), (ada, 1), (tunde, 4), (ada, 2)):
                who.send_json({"type": "move", "data": {"cell": cell}})
                a, t = ada.receive_json(), tunde.receive_json()
                assert a["type"] == t["type"] == "state"
            assert a["result"] == {"winner": 0, "line": [0, 1, 2], "reason": "line"} and a["status"] == "over"
            assert t["result"]["winner"] == 0


def test_bad_moves_only_tell_the_sender(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        code = ada.receive_json()["room"]
        with client.websocket_connect(url(ids["tunde"], code)) as tunde:
            ada.receive_json(), ada.receive_json(), tunde.receive_json()
            tunde.send_json({"type": "move", "data": {"cell": 0}})  # not O's turn
            assert tunde.receive_json()["message"] == "It's not your turn."
            ada.send_json({"type": "move", "data": {"cell": 99}})
            assert ada.receive_json()["code"] == "bad_move"
            ada.send_json({"type": "move", "data": {"cell": 0}})
            ada.receive_json(), tunde.receive_json()
            tunde.send_json({"type": "move", "data": {"cell": 0}})  # taken
            assert tunde.receive_json()["message"] == "That square is taken."


def test_a_wrong_code_and_a_full_room(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"], "ZZZZZZ")) as ws:
        assert ws.receive_json() == {"type": "closed", "reason": "not_found"}
        with pytest.raises(WebSocketDisconnect) as err:
            ws.receive_json()
    assert err.value.code == core.NOT_FOUND
    with client.websocket_connect(url(ids["ada"])) as ada:
        code = ada.receive_json()["room"]
        with client.websocket_connect(url(ids["tunde"], code)) as tunde:
            ada.receive_json(), ada.receive_json(), tunde.receive_json()
            with client.websocket_connect(url(ids["chi"], code)) as chi:
                with pytest.raises(WebSocketDisconnect) as err:
                    chi.receive_json()
            assert err.value.code == core.ROOM_FULL


def test_too_many_wrong_codes_slows_a_guesser_down(env):
    client, ids, _ = env
    for _ in range(core.BAD_CODES_PER_MINUTE):
        with client.websocket_connect(url(ids["ada"], "ZZZZZZ")) as ws:
            assert ws.receive_json()["reason"] == "not_found"
    with client.websocket_connect(url(ids["ada"], "ZZZZZZ")) as ws:
        with pytest.raises(WebSocketDisconnect) as err:
            ws.receive_json()
    assert err.value.code == core.TOO_MANY


def test_a_dropped_player_comes_back_to_the_same_seat(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        code = ada.receive_json()["room"]
        with client.websocket_connect(url(ids["tunde"], code)) as tunde:
            ada.receive_json(), ada.receive_json(), tunde.receive_json()
            ada.send_json({"type": "move", "data": {"cell": 4}})
            ada.receive_json(), tunde.receive_json()
            with client.websocket_connect(url(ids["tunde"], code)) as tunde_again:  # same player on a new connection
                back = tunde_again.receive_json()
                assert back["type"] == "welcome" and back["you"] == 1 and back["state"]["board"][4] == 0
                with pytest.raises(WebSocketDisconnect) as err:  # the old connection is replaced
                    for _ in range(5):
                        tunde.receive_json()
                assert err.value.code == core.REPLACED
    # "new" also returns you to the room you are in, instead of making a second one
    with client.websocket_connect(url(ids["ada"])) as ada2:
        assert ada2.receive_json()["room"] == code


def test_a_player_who_stays_away_forfeits_after_the_grace_time(env):
    client, ids, now = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        code = ada.receive_json()["room"]
        with client.websocket_connect(url(ids["tunde"], code)) as tunde:
            ada.receive_json(), ada.receive_json(), tunde.receive_json()
        away = ada.receive_json()
        assert away == {"type": "away", "seat": 1, "grace": core.SEAT_GRACE}
        now[0] += core.SEAT_GRACE - 1
        client.portal.call(core.sweep)
        now[0] += 2
        client.portal.call(core.sweep)
        end = ada.receive_json()
        assert end["status"] == "over" and end["result"] == {"winner": 0, "reason": "left"}


def test_rooms_close_when_their_time_is_up(env):
    client, ids, now = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        ada.receive_json()
        now[0] += core.WAIT_FOR_FRIEND + 1
        client.portal.call(core.sweep)
        assert ada.receive_json() == {"type": "closed", "reason": "nobody_came"}
    assert core.rooms == {} and core.user_room == {}


def test_an_empty_room_closes_after_the_rejoin_time(env):
    client, ids, now = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        code = ada.receive_json()["room"]
        ada.close(1006)  # dropped by the network (closing on purpose with 1000 cancels the room at once)
    for _ in range(100):  # the server notices the connection closing a moment later
        if core.rooms[code].empty_since is not None:
            break
        time.sleep(0.02)
    now[0] += core.EMPTY + 1
    client.portal.call(core.sweep)
    assert code not in core.rooms


def test_matchmaking_pairs_two_players(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"], "match")) as ada:
        assert ada.receive_json() == {"type": "waiting"}
        assert client.get("/rooms/online?game=tic-tac-toe").json()["online"] == 1
        with client.websocket_connect(url(ids["tunde"], "match")) as tunde:
            a, t = ada.receive_json(), tunde.receive_json()
            assert a["room"] == t["room"] and a["status"] == t["status"] == "playing"
            assert (a["you"], t["you"]) == (0, 1)
            assert client.get("/rooms/online?game=tic-tac-toe").json()["online"] == 2


def test_the_matchmaking_line_offers_a_bot_and_then_gives_up(env):
    client, ids, now = env
    with client.websocket_connect(url(ids["ada"], "match")) as ada:
        ada.receive_json()
        now[0] += core.MATCH_OFFER + 1
        client.portal.call(core.sweep)
        assert ada.receive_json() == {"type": "no_match"}
        now[0] += core.MATCH_WAIT
        client.portal.call(core.sweep)
        assert ada.receive_json() == {"type": "closed", "reason": "no_match"}


def test_playing_a_bot_from_the_matchmaking_line(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"], "match")) as ada:
        ada.receive_json()
        ada.send_json({"type": "bot"})
        assert ada.receive_json()["status"] == "waiting"  # the room with just them
        joined = ada.receive_json()
        assert joined["type"] == "join" and joined["player"]["bot"] is True
        start = ada.receive_json()
        assert start["status"] == "playing"
        ada.send_json({"type": "move", "data": {"cell": 0}})
        assert ada.receive_json()["type"] == "state"  # mine
        reply = ada.receive_json()  # the bot answers
        assert reply["type"] == "state" and reply["state"]["board"].count(1) == 1


def test_a_bot_can_be_added_to_a_room_waiting_for_a_friend(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        ada.receive_json()
        ada.send_json({"type": "bot"})
        assert ada.receive_json()["type"] == "join"
        assert ada.receive_json()["status"] == "playing"


def test_resigning_gives_the_win_to_the_other_player(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        code = ada.receive_json()["room"]
        with client.websocket_connect(url(ids["tunde"], code)) as tunde:
            ada.receive_json(), ada.receive_json(), tunde.receive_json()
            ada.send_json({"type": "resign"})
            assert ada.receive_json()["result"] == {"winner": 1, "reason": "resigned"}


def test_big_messages_are_refused(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        ada.receive_json()
        ada.send_text("x" * (core.MAX_MESSAGE + 1))
        assert ada.receive_json()["code"] == "too_big"


def test_tictactoe_rules():
    s = tictactoe.new_state(2)
    for seat, cell in ((0, 0), (1, 1), (0, 3), (1, 4), (0, 6)):
        s, err = tictactoe.apply(s, seat, {"cell": cell})
        assert err is None
    assert tictactoe.result(s) == {"winner": 0, "line": [0, 3, 6], "reason": "line"} and tictactoe.turn(s) is None
    draw = {"board": [0, 1, 0, 0, 1, 1, 1, 0, 0], "turn": 0}
    assert tictactoe.result(draw) == {"winner": None, "reason": "draw"}
    # the bot wins when it can and blocks when it must
    assert tictactoe.bot_move({"board": [1, 1, None, 0, 0, None, None, None, None], "turn": 1}, 1)["cell"] == 2
    assert tictactoe.bot_move({"board": [0, 0, None, 1, None, None, None, None, None], "turn": 1}, 1)["cell"] == 2


def test_the_game_list(env):
    client, _, _ = env
    from app.features.rooms.games import GAMES

    listed = client.get("/rooms/games").json()
    assert listed == [{"game": g.name, "min": g.min_players, "max": g.max_players} for g in GAMES.values()]
    assert {"game": GAME, "min": 2, "max": 2} in listed
    assert {"game": "chess", "min": 2, "max": 2} in listed


def test_after_a_game_ends_new_and_match_start_fresh(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        first = ada.receive_json()["room"]
        with client.websocket_connect(url(ids["tunde"], first)) as tunde:
            ada.receive_json(), ada.receive_json(), tunde.receive_json()
            ada.send_json({"type": "resign"})
            ada.receive_json(), tunde.receive_json()
    with client.websocket_connect(url(ids["ada"])) as ada2:
        again = ada2.receive_json()
        assert again["status"] == "waiting" and again["room"] != first
    with client.websocket_connect(url(ids["tunde"], "match")) as tunde2:
        assert tunde2.receive_json() == {"type": "waiting"}


def test_a_chess_game_in_a_room(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"], game="chess")) as ada:
        code = ada.receive_json()["room"]
        with client.websocket_connect(url(ids["tunde"], code, game="chess")) as tunde:
            ada.receive_json(), ada.receive_json(), tunde.receive_json()
            for who, (a, b) in ((ada, ("f2", "f3")), (tunde, ("e7", "e5")), (ada, ("g2", "g4")), (tunde, ("d8", "h4"))):
                who.send_json({"type": "move", "data": {"from": a, "to": b}})
                x, y = ada.receive_json(), tunde.receive_json()
            assert x["result"] == {"winner": 1, "reason": "checkmate"} and y["status"] == "over"  # fool's mate
            assert y["state"]["last"] == {"from": "d8", "to": "h4"} and y["state"]["check"] is True


def _finished_game(client, ids):
    """Ada makes a room, Tunde joins, Ada resigns. Returns the two open connections (a context manager pair)."""
    ada = client.websocket_connect(url(ids["ada"])).__enter__()
    code = ada.receive_json()["room"]
    tunde = client.websocket_connect(url(ids["tunde"], code)).__enter__()
    ada.receive_json(), ada.receive_json(), tunde.receive_json()
    ada.send_json({"type": "resign"})
    assert ada.receive_json()["status"] == tunde.receive_json()["status"] == "over"
    return ada, tunde, code


def test_a_rematch_starts_when_both_ask_and_the_other_player_goes_first(env):
    client, ids, _ = env
    ada, tunde, code = _finished_game(client, ids)
    try:
        ada.send_json({"type": "rematch"})
        assert ada.receive_json() == tunde.receive_json() == {"type": "rematch", "seats": [0]}
        tunde.send_json({"type": "rematch"})
        a, t = ada.receive_json(), tunde.receive_json()
        assert a["type"] == t["type"] == "welcome" and a["room"] == t["room"] == code
        assert a["status"] == "playing" and a["result"] is None and a["state"]["board"] == [None] * 9
        assert (a["you"], t["you"]) == (1, 0) and a["turn"] == 0  # seats moved round: Tunde goes first now
        tunde.send_json({"type": "move", "data": {"cell": 4}})
        assert ada.receive_json()["state"]["board"][4] is not None
    finally:
        tunde.__exit__(None, None, None)
        ada.__exit__(None, None, None)


def test_rematch_only_after_the_game_ends(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        code = ada.receive_json()["room"]
        with client.websocket_connect(url(ids["tunde"], code)) as tunde:
            ada.receive_json(), ada.receive_json(), tunde.receive_json()
            ada.send_json({"type": "rematch"})
            ada.send_json({"type": "move", "data": {"cell": 0}})
            assert ada.receive_json()["type"] == "state"  # the rematch was ignored: the next message is the move


def test_a_rematch_against_the_bot_starts_straight_away(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        ada.receive_json()
        ada.send_json({"type": "bot"})
        ada.receive_json(), ada.receive_json()  # the bot joins, then the game starts
        ada.send_json({"type": "resign"})
        assert ada.receive_json()["status"] == "over"
        ada.send_json({"type": "rematch"})
        again = ada.receive_json()
        while again["type"] != "welcome":
            again = ada.receive_json()
        assert again["status"] == "playing" and again["you"] == 1


def test_a_hosts_rematch_waits_again_and_is_not_closed_for_the_first_wait(env):
    client, ids, now = env
    room = core.make_room("quiz")
    room.seats = [core.Seat(user_id=ids["ada"], name="Ada", style=None), core.Seat(user_id=ids["tunde"], name="Tunde", style=None)]
    room.status, room.result, room.options = "over", {"winner": 0}, {"questions": []}
    now[0] += core.WAIT_FOR_FRIEND + 60  # a long quiz: well past the room's first 15 minutes
    host = core.Conn(user_id=ids["ada"], name="Ada", style=None, ws=None, game="quiz", seat=0)
    guest = core.Conn(user_id=ids["tunde"], name="Tunde", style=None, ws=None, game="quiz", seat=1)
    client.portal.call(core.rematch, room, guest)  # only the host can ask in a game with a host
    assert room.status == "over"
    client.portal.call(core.rematch, room, host)
    assert room.status == "waiting" and room.result is None and room.options == {"questions": []}
    client.portal.call(core.sweep)
    assert room.code in core.rooms  # still open: the wait starts again from the rematch
    now[0] += core.WAIT_FOR_FRIEND + 1
    client.portal.call(core.sweep)
    assert room.code not in core.rooms


def test_cancelling_a_room_nobody_joined_frees_the_player(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        first = ada.receive_json()["room"]
        ada.close(1000)  # the kit's leave(): Cancel
    assert first not in core.rooms and (GAME, ids["ada"]) not in core.user_room
    with client.websocket_connect(url(ids["ada"], "match")) as ada2:
        assert ada2.receive_json() == {"type": "waiting"}  # in the matchmaking line, not back in the old room


def test_a_dropped_connection_keeps_the_waiting_room(env):
    client, ids, _ = env
    with client.websocket_connect(url(ids["ada"])) as ada:
        first = ada.receive_json()["room"]
        ada.close(1006)  # the network dropped it
    assert first in core.rooms
    with client.websocket_connect(url(ids["ada"])) as ada2:
        assert ada2.receive_json()["room"] == first  # back in the same room, as before
