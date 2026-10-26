"""The rooms core: rooms with a short code, matchmaking, bots, and reconnecting, for games start.ng referees (Referee mode).

A game never holds the player's sign-in: it gets a one-minute ticket (POST /games/{name}/ticket, like the Hangout) and opens
the WebSocket /rooms/live?game=<name>&ticket=...&room=new|match|<CODE>. The server reads the ticket and attaches the
player's real name and look. Rooms live in this process's memory (like the Hangout's), so they are lost on a restart.

Messages (JSON text):
  you -> server    {type: "move", data: <the game's move>} | {type: "resign"} | {type: "bot"} | {type: "ping"}
                   {type: "rematch"}  after a game ends: play again in the same room (see rematch())
                   hosted games (a quiz, bingo; the creator of the room is the host, seat 0): {type: "setup", data: {...}} (up to 64 KB,
                   before the game starts) and {type: "start"}
  server -> you    {type: "welcome", you: seat, room: CODE, game, players: [player], status, state, turn, result, host, now}
                   {type: "state", state, turn, result, status, now}  after every move (state is that player's own view)
                   {type: "setup_ok"}                                 the host's setup was accepted
                   {type: "rematch", seats: [seat]}                   who has asked to play again so far
                   (now is the server's clock in milliseconds, so a page can show a countdown that matches the server)
                   {type: "join", player} | {type: "away", seat, grace} | {type: "back", seat}
                   {type: "waiting"}  {type: "no_match"}             matchmaking: no one yet / offer to play a bot
                   {type: "error", code, message}  {type: "closed", reason}  {type: "pong"}
  player           {seat, id, name, style, online, bot}   (id is null for a bot)
  status           "waiting" (for the other player), "playing", "over"

Rules for the player (see the proposal): a room waits 15 minutes for a friend, matchmaking 60 seconds (a bot is offered after
15); a dropped player keeps their seat for 10 minutes (after that, in a game against a person, the other player wins); a room
closes after 30 minutes without a move, 10 minutes with nobody in it, 5 minutes after the game ends, or 4 hours at most.
"""
import asyncio
import json
import random
import time
from collections import deque
from dataclasses import dataclass, field

from starlette.websockets import WebSocket

from .games import GAMES

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I/L, so a code read aloud or typed is hard to get wrong
CODE_LENGTH = 6
MAX_MESSAGE = 2048  # bytes
MESSAGES_PER_SECOND = 30
BAD_CODES_PER_MINUTE = 10

WAIT_FOR_FRIEND = 15 * 60
MATCH_OFFER = 15
MATCH_WAIT = 60
SEAT_GRACE = 10 * 60
IDLE = 30 * 60
EMPTY = 10 * 60
OVER_KEEP = 5 * 60
MAX_AGE = 4 * 3600

# WebSocket close codes the game understands (the Hangout's, plus two).
BAD_TICKET = 4401
ROOM_FULL = 4403
NOT_FOUND = 4404
REPLACED = 4409
TOO_MANY = 4429

clock = time.monotonic  # tests replace this
MAX_SETUP = 64 * 1024  # bytes: what a host may send to set a game up (a quiz's questions)
MAX_SHARED = 8 * 1024  # bytes: the shared state of a Relay room
RELAY_SEATS = (2, 8)  # how many players a Relay room may be made for


def wall_ms() -> int:
    """The server's clock in milliseconds since 1970: deadlines inside a game and the clock pages count down against."""
    return int(time.time() * 1000)


def startable(rules) -> bool:
    """A game where the creator of the room (seat 0) starts it: a quiz or bingo (hosted), or a card or board game for 2 to 4 (start_mode "start")."""
    return getattr(rules, "start_mode", "full") in ("host", "start")


class RelayRules:
    """The rules object of a Relay room: start.ng does not know the game's rules, it only carries messages between the players.
    Used for any published game that is not one of the refereed ones (games/). The room is open from the start: people come and
    go until it is full, and the game decides when it begins."""

    start_mode = "open"
    min_players = 1
    forfeit = False

    def __init__(self, game: str, max_players: int):
        self.name, self.max_players = game, max_players

    def new_state(self, players, options=None):
        return {}

    def turn(self, state):
        return None

    def apply(self, state, seat, move):
        return state, "This game keeps its own rules."

    def result(self, state):
        return None

    def view(self, state, seat):
        return state

    def bot_move(self, state, seat, level):
        return None


def relay(rules) -> bool:
    return isinstance(rules, RelayRules)


def hosted(rules) -> bool:
    """A game with a host (a quiz, bingo): whoever makes the room is the host (seat 0) and starts it, instead of it starting when it is full."""
    return getattr(rules, "start_mode", "full") == "host"


@dataclass
class Conn:
    """One signed-in player's open connection."""
    user_id: int
    name: str
    style: dict | None
    ws: WebSocket
    game: str
    room: "Room | None" = None
    seat: int = -1
    queued_at: float | None = None
    offered: bool = False
    window: deque = field(default_factory=deque)  # when the last messages arrived, for the rate limit
    left: bool = False  # closed on purpose (code 1000, the kit's leave()), not dropped by the network


@dataclass
class Seat:
    user_id: int | None  # None: a bot
    name: str
    style: dict | None
    conn: Conn | None = None
    away_since: float | None = None
    bot: bool = False


@dataclass
class Room:
    code: str
    game: str
    rules: object
    seats: list[Seat] = field(default_factory=list)
    state: object = None
    status: str = "waiting"
    result: dict | None = None
    created: float = 0.0
    last_move: float = 0.0
    finished_at: float | None = None
    empty_since: float | None = None
    options: object = None  # what the host set up (hosted games)
    shared: object = None  # a Relay room's shared state, and its version (see set_state)
    version: int = 0
    rematch: set = field(default_factory=set)  # seats that asked to play again after the game ended


rooms: dict[str, Room] = {}
user_room: dict[tuple[str, int], str] = {}  # (game, player) -> the room they're in
queues: dict[str, list[Conn]] = {}
bad_codes: dict[int, deque] = {}


# ---- small helpers


def new_code() -> str:
    for _ in range(100):
        code = "".join(random.SystemRandom().choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if code not in rooms:
            return code
    raise RuntimeError("no free room code")


def online(game: str) -> int:
    """Distinct signed-in players connected to this game's rooms or waiting for a match."""
    people = {s.user_id for r in rooms.values() if r.game == game for s in r.seats if s.conn is not None}
    people |= {c.user_id for c in queues.get(game, [])}
    return len(people)


async def send(conn: Conn | None, message: dict) -> None:
    if conn is None:
        return
    try:
        await conn.ws.send_text(json.dumps(message))
    except Exception:  # gone: its own loop will notice
        pass


def players_of(room: Room) -> list[dict]:
    return [
        {"seat": i, "id": s.user_id, "name": s.name, "style": s.style, "online": s.bot or s.conn is not None, "bot": s.bot,
         **({"host": True} if (startable(room.rules) or relay(room.rules)) and i == 0 else {})}
        for i, s in enumerate(room.seats)
    ]


def turn_of(room: Room) -> int | None:
    return room.rules.turn(room.state) if room.state is not None else None


async def tell_state(room: Room, only: Conn | None = None) -> None:
    for i, seat in enumerate(room.seats):
        if seat.conn is None or (only is not None and seat.conn is not only):
            continue
        view = room.rules.view(room.state, i) if room.state is not None else None
        await send(seat.conn, {"type": "state", "state": view, "turn": turn_of(room), "result": room.result, "status": room.status, "now": wall_ms()})


async def tell_all(room: Room, message: dict, skip: Conn | None = None) -> None:
    for seat in room.seats:
        if seat.conn is not None and seat.conn is not skip:
            await send(seat.conn, message)


async def welcome(room: Room, conn: Conn) -> None:
    view = room.rules.view(room.state, conn.seat) if room.state is not None else None
    await send(conn, {
        "type": "welcome", "you": conn.seat, "room": room.code, "game": room.game, "players": players_of(room),
        "status": room.status, "state": view, "turn": turn_of(room), "result": room.result,
        "host": 0 if startable(room.rules) or relay(room.rules) else None, "now": wall_ms(),
        **({"max": room.rules.max_players, "shared": {"version": room.version, "data": room.shared}} if relay(room.rules) else {}),
    })


# ---- rooms


def make_room(game: str, max_players: int | None = None) -> Room:
    now = clock()
    if game in GAMES:
        room = Room(code=new_code(), game=game, rules=GAMES[game], created=now, last_move=now)
    else:  # a Relay room
        room = Room(code=new_code(), game=game, rules=RelayRules(game, max_players or 4), created=now, last_move=now,
                    status="playing", state={})
    rooms[room.code] = room
    return room


def seat_human(room: Room, conn: Conn) -> int:
    room.seats.append(Seat(user_id=conn.user_id, name=conn.name, style=conn.style, conn=conn))
    conn.room, conn.seat = room, len(room.seats) - 1
    user_room[(room.game, conn.user_id)] = room.code
    room.empty_since = None
    return conn.seat


async def attach(room: Room, seat_index: int, conn: Conn) -> None:
    """A player (back) at their seat: their new connection replaces any old one."""
    seat = room.seats[seat_index]
    old = seat.conn
    seat.conn, seat.away_since, room.empty_since = conn, None, None
    conn.room, conn.seat = room, seat_index
    if old is not None and old is not conn:
        old.room = None
        try:
            await old.ws.close(REPLACED, "Joined again from somewhere else")
        except Exception:
            pass
    await welcome(room, conn)
    await tell_all(room, {"type": "back", "seat": seat_index}, skip=conn)


async def start_if_ready(room: Room) -> None:
    if room.status != "waiting" or len(room.seats) < room.rules.max_players or startable(room.rules):
        return
    room.status = "playing"
    room.state = room.rules.new_state(len(room.seats))
    room.last_move = clock()
    for seat in room.seats:
        if seat.conn is not None:
            await welcome(room, seat.conn)
    await bots_play(room)


async def finish(room: Room, result: dict) -> None:
    room.status, room.result, room.finished_at = "over", result, clock()
    await tell_state(room)


async def close_room(room: Room, reason: str) -> None:
    rooms.pop(room.code, None)
    for seat in room.seats:
        if seat.user_id is not None and user_room.get((room.game, seat.user_id)) == room.code:
            del user_room[(room.game, seat.user_id)]
        if seat.conn is not None:
            await send(seat.conn, {"type": "closed", "reason": reason})
            seat.conn.room = None
            try:
                await seat.conn.ws.close(1000, reason)
            except Exception:
                pass
            seat.conn = None
    room.status = "closed"


async def bots_play(room: Room) -> None:
    """While it's a bot's turn, it plays (off the event loop, so a slow bot can't hold up the rest of the server)."""
    loop = asyncio.get_running_loop()
    while room.status == "playing":
        who = turn_of(room)
        if who is None or not room.seats[who].bot:
            return
        move = await loop.run_in_executor(None, room.rules.bot_move, room.state, who, "medium")
        if move is None:
            return
        room.state, error = room.rules.apply(room.state, who, move)
        if error:
            return
        room.last_move = clock()
        result = room.rules.result(room.state)
        if result:
            await finish(room, result)
            return
        await tell_state(room)


async def relay_send(room: Room, conn: Conn, msg: dict) -> None:
    """A Relay room: pass the message on to the others (or to one seat, with "to")."""
    if "data" not in msg:
        return
    out = {"type": "message", "from": conn.seat, "data": msg["data"]}
    to = msg.get("to")
    room.last_move = clock()
    if to is None:
        await tell_all(room, out, skip=conn)
    elif isinstance(to, int) and not isinstance(to, bool) and 0 <= to < len(room.seats) and to != conn.seat:
        await send(room.seats[to].conn, out)


async def relay_set_state(room: Room, conn: Conn, msg: dict) -> None:
    """A Relay room's shared state (up to 8 KB). A write must say which version it was made from; if someone else wrote in between,
    it is refused and the writer is sent the newer state, so two players can never overwrite each other without knowing."""
    if "data" not in msg:
        return
    if len(json.dumps(msg["data"]).encode()) > MAX_SHARED:
        await send(conn, {"type": "error", "code": "too_big", "message": "The shared state is too big (8 KB at most)."})
        return
    if msg.get("version") != room.version:
        await send(conn, {"type": "error", "code": "state_conflict", "message": "Someone changed the shared state first.",
                          "version": room.version, "data": room.shared})
        return
    room.shared, room.version, room.last_move = msg["data"], room.version + 1, clock()
    await tell_all(room, {"type": "room_state", "version": room.version, "data": room.shared, "by": conn.seat})


async def play_move(room: Room, conn: Conn, move) -> None:
    if room.status != "playing":
        await send(conn, {"type": "error", "code": "not_playing", "message": "The game isn't running."})
        return
    state, error = room.rules.apply(room.state, conn.seat, move)
    if error:
        await send(conn, {"type": "error", "code": "bad_move", "message": error})
        return
    room.state, room.last_move = state, clock()
    result = room.rules.result(state)
    if result:
        await finish(room, result)
        return
    await tell_state(room)
    await bots_play(room)


async def host_setup(room: Room, conn: Conn, data) -> None:
    if not hosted(room.rules) or conn.seat != 0 or room.status != "waiting":
        return
    options, error = room.rules.validate_setup(data)
    if error:
        await send(conn, {"type": "error", "code": "bad_setup", "message": error})
        return
    room.options = options
    await send(conn, {"type": "setup_ok"})


async def host_start(room: Room, conn: Conn) -> None:
    if not startable(room.rules) or conn.seat != 0 or room.status != "waiting":
        return
    if len(room.seats) < room.rules.min_players:
        await send(conn, {"type": "error", "code": "need_players", "message": "Wait for someone to join first."})
        return
    try:
        room.state = room.rules.new_state(len(room.seats), room.options)
    except ValueError as err:
        await send(conn, {"type": "error", "code": "bad_setup", "message": str(err)})
        return
    room.status, room.last_move = "playing", clock()
    for seat in room.seats:
        if seat.conn is not None:
            await welcome(room, seat.conn)
    await bots_play(room)


async def rematch(room: Room, conn: Conn) -> None:
    """After a game ends, play again in the same room. Everyone hears who has asked; once every person still in a seat has asked
    (bots always agree), a new game starts with the seats moved round by one, so a different player goes first. In a game with
    a host (a quiz, bingo) only the host asks, and the room goes back to waiting for them to start it, with the same setup."""
    if room.status != "over" or relay(room.rules) or (hosted(room.rules) and conn.seat != 0):
        return
    room.rematch.add(conn.seat)
    people = {i for i, s in enumerate(room.seats) if not s.bot}
    if not hosted(room.rules) and not people <= room.rematch:
        await tell_all(room, {"type": "rematch", "seats": sorted(room.rematch)})
        return
    room.rematch = set()
    room.result, room.finished_at, room.last_move = None, None, clock()
    if hosted(room.rules):
        room.status, room.state = "waiting", None
    else:
        room.seats = room.seats[1:] + room.seats[:1]
        for i, seat in enumerate(room.seats):
            if seat.conn is not None:
                seat.conn.seat = i
        room.status = "playing"
        room.state = room.rules.new_state(len(room.seats), room.options) if startable(room.rules) else room.rules.new_state(len(room.seats))
    for seat in room.seats:
        if seat.conn is not None:
            await welcome(room, seat.conn)
    await bots_play(room)


async def tick() -> None:
    """Hosted games run on timers (a question's time, the next number to call): ask each one whether its time has come."""
    for room in list(rooms.values()):
        if room.status != "playing" or not hosted(room.rules) or not hasattr(room.rules, "tick"):
            continue
        state, changed = room.rules.tick(room.state, wall_ms())
        if not changed:
            continue
        room.state, room.last_move = state, clock()
        result = room.rules.result(state)
        if result:
            await finish(room, result)
        else:
            await tell_state(room)


async def add_bot(room: Room) -> None:
    room.seats.append(Seat(user_id=None, name="Bot", style=None, bot=True))
    await tell_all(room, {"type": "join", "player": players_of(room)[-1]})
    await start_if_ready(room)


# ---- connecting


def rate_ok(conn: Conn) -> bool:
    now = clock()
    while conn.window and now - conn.window[0] > 1:
        conn.window.popleft()
    if len(conn.window) >= MESSAGES_PER_SECOND:
        return False
    conn.window.append(now)
    return True


def too_many_bad_codes(user_id: int) -> bool:
    now = clock()
    q = bad_codes.setdefault(user_id, deque())
    while q and now - q[0] > 60:
        q.popleft()
    return len(q) >= BAD_CODES_PER_MINUTE


async def connect(ws: WebSocket, user, game: str, mode: str, seats: int | None = None) -> None:
    """Runs one player's connection from joining to leaving. `user` has id, name and style (what the Hangout loads). `seats` is
    how many players a new Relay room is for (a game that start.ng does not referee)."""
    seats = max(RELAY_SEATS[0], min(RELAY_SEATS[1], seats or 4))
    conn = Conn(user_id=user.id, name=user.name, style=user.style, ws=ws, game=game)
    try:
        existing = rooms.get(user_room.get((game, user.id), ""))
        if existing is not None and existing.status == "over":  # a finished game: starting again means a fresh room
            user_room.pop((game, user.id), None)
            existing = None
        if existing is not None and (mode in ("new", "match") or mode == existing.code):
            seat_index = next(i for i, s in enumerate(existing.seats) if s.user_id == user.id)
            await attach(existing, seat_index, conn)  # back to the room they're already in
        elif mode == "new":
            room = make_room(game, seats)
            seat_human(room, conn)
            await welcome(room, conn)
        elif mode == "match":
            if game not in GAMES or startable(GAMES[game]):
                await send(conn, {"type": "error", "code": "no_match", "message": "This game is played in a room with a host. Make a room, or join one with its code."})
                await ws.close(NOT_FOUND, "No matchmaking for this game")
                return
            await match(conn)
        else:
            if too_many_bad_codes(user.id):
                await ws.close(TOO_MANY, "Too many wrong room codes. Wait a minute.")
                return
            room = rooms.get(mode)
            if room is None or room.status in ("closed", "over"):
                bad_codes.setdefault(user.id, deque()).append(clock())
                await send(conn, {"type": "closed", "reason": "not_found"})
                await ws.close(NOT_FOUND, "That room has closed or never existed")
                return
            if len(room.seats) >= room.rules.max_players or (room.status == "playing" and not hosted(room.rules) and not relay(room.rules)):
                await ws.close(ROOM_FULL, "That room is full")
                return
            seat_human(room, conn)
            if hosted(room.rules) and room.status == "playing" and hasattr(room.rules, "add_player"):
                room.state = room.rules.add_player(room.state, conn.seat)  # someone who comes in late gets in from here on
            await tell_all(room, {"type": "join", "player": players_of(room)[conn.seat]}, skip=conn)
            if startable(room.rules) or relay(room.rules) or len(room.seats) < room.rules.max_players:  # otherwise the game starts, and starting welcomes everyone
                await welcome(room, conn)
            await start_if_ready(room)
        await pump(conn)
    finally:
        await dropped(conn)


async def match(conn: Conn) -> None:
    queue = queues.setdefault(conn.game, [])
    queue[:] = [c for c in queue if c.user_id != conn.user_id]  # one place in line each
    if queue:
        other = queue.pop(0)
        room = make_room(conn.game)
        seat_human(room, other)
        seat_human(room, conn)
        other.queued_at = None
        if len(room.seats) < room.rules.max_players:  # a game for more than two keeps waiting; otherwise starting welcomes both
            await welcome(room, other)
            await welcome(room, conn)
        await start_if_ready(room)
    else:
        conn.queued_at, conn.offered = clock(), False
        queue.append(conn)
        await send(conn, {"type": "waiting"})


async def pump(conn: Conn) -> None:
    while True:
        try:
            event = await conn.ws.receive()
        except RuntimeError:
            return
        if event["type"] == "websocket.disconnect":
            conn.left = event.get("code") == 1000
            return
        text = event.get("text")
        if not text:
            continue
        size = len(text.encode())
        if size > MAX_SETUP:
            await send(conn, {"type": "error", "code": "too_big", "message": "That message is too big."})
            continue
        if not rate_ok(conn):
            continue
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        if size > MAX_MESSAGE and not (isinstance(msg, dict) and msg.get("type") in ("setup", "set_state")):  # only a host's setup and a Relay room's shared state may be bigger than a normal message
            await send(conn, {"type": "error", "code": "too_big", "message": "That message is too big."})
            continue
        if not isinstance(msg, dict):
            continue
        kind = msg.get("type")
        room = conn.room
        if kind == "ping":
            await send(conn, {"type": "pong"})
        elif kind == "send" and room is not None and relay(room.rules):
            await relay_send(room, conn, msg)
        elif kind == "set_state" and room is not None and relay(room.rules):
            await relay_set_state(room, conn, msg)
        elif kind == "move" and room is not None and not relay(room.rules):
            await play_move(room, conn, msg.get("data"))
        elif kind == "setup" and room is not None:
            await host_setup(room, conn, msg.get("data"))
        elif kind == "start" and room is not None:
            await host_start(room, conn)
        elif kind == "rematch" and room is not None:
            await rematch(room, conn)
        elif kind == "resign" and room is not None and room.status == "playing" and not hosted(room.rules) and not relay(room.rules) and len(room.seats) == 2:
            others = [i for i in range(len(room.seats)) if i != conn.seat]
            await finish(room, {"winner": others[0] if len(others) == 1 else None, "reason": "resigned"})
        elif kind == "bot":
            if room is None and conn.queued_at is not None:  # in the matchmaking line: play a bot instead
                queues[conn.game] = [c for c in queues.get(conn.game, []) if c is not conn]
                conn.queued_at = None
                room = make_room(conn.game)
                seat_human(room, conn)
                await welcome(room, conn)
                await add_bot(room)
            elif room is not None and room.status == "waiting" and not hosted(room.rules) and not relay(room.rules) and (
                    (startable(room.rules) and conn.seat == 0 and len(room.seats) < room.rules.max_players)
                    or (not startable(room.rules) and len(room.seats) == 1)):
                await add_bot(room)


async def dropped(conn: Conn) -> None:
    queue = queues.get(conn.game, [])
    if conn in queue:
        queue.remove(conn)
    room = conn.room
    if room is None or room.status == "closed":
        return
    seat = room.seats[conn.seat]
    if seat.conn is not conn:  # already replaced by a newer connection
        return
    if conn.left and room.status == "waiting" and not relay(room.rules) and all(s.bot for i, s in enumerate(room.seats) if i != conn.seat):
        await close_room(room, "left")  # they cancelled before anyone else came: free them for a new room or a match
        return
    seat.conn, seat.away_since = None, clock()
    if all(s.bot or s.conn is None for s in room.seats):
        room.empty_since = clock()
    await tell_all(room, {"type": "away", "seat": conn.seat, "grace": SEAT_GRACE})


# ---- upkeep


async def sweep() -> None:
    """Closes rooms that ran out of time, gives the win to a player whose opponent stayed away too long, and tells people in the
    matchmaking line when to play a bot or give up."""
    now = clock()
    for room in list(rooms.values()):
        if room.status == "closed":
            continue
        if room.status == "playing" and getattr(room.rules, "forfeit", room.rules.max_players == 2):
            for i, seat in enumerate(room.seats):
                others = [j for j, s in enumerate(room.seats) if j != i and not s.bot]
                if seat.away_since is not None and now - seat.away_since > SEAT_GRACE and others and not seat.bot:
                    await finish(room, {"winner": others[0] if len(others) == 1 else None, "reason": "left"})
                    break
        if room.status == "playing" and startable(room.rules) and not hosted(room.rules):
            # a person who stayed away too long: a bot plays their seat, so the others can finish
            for seat in room.seats:
                if seat.away_since is not None and now - seat.away_since > SEAT_GRACE and not seat.bot:
                    seat.bot, seat.conn, seat.away_since = True, None, None
                    await tell_all(room, {"type": "join", "player": players_of(room)[room.seats.index(seat)]})
            await bots_play(room)
        if now - room.created > MAX_AGE:
            await close_room(room, "too_old")
        elif room.status == "waiting" and now - room.last_move > WAIT_FOR_FRIEND:  # last_move: made, or sent back to waiting by a rematch
            await close_room(room, "nobody_came")
        elif room.status == "playing" and now - room.last_move > IDLE:
            await close_room(room, "inactive")
        elif room.status == "over" and room.finished_at is not None and now - room.finished_at > OVER_KEEP:
            await close_room(room, "finished")
        elif room.empty_since is not None and now - room.empty_since > EMPTY:
            await close_room(room, "empty")
    for game, queue in list(queues.items()):
        for conn in list(queue):
            waited = now - (conn.queued_at or now)
            if waited > MATCH_WAIT:
                queue.remove(conn)
                await send(conn, {"type": "closed", "reason": "no_match"})
                try:
                    await conn.ws.close(1000, "no_match")
                except Exception:
                    pass
            elif waited > MATCH_OFFER and not conn.offered:
                conn.offered = True
                await send(conn, {"type": "no_match"})
