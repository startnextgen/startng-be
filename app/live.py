"""Live rooms for multiplayer games, like the start.ng Hangout.

A game never holds the player's sign-in. It asks start.ng's /connect page for a one-minute
ticket (POST /games/{name}/ticket), then opens the WebSocket /games/{name}/live?ticket=...
The server reads the ticket and attaches the player's real name and look from the database,
so nobody can pretend to be someone else. Rooms live in this process's memory.

Only people who are connected are in the room: nobody who has left, or never came in, is shown.
Each player gets a numbered bench seat while they're here (up to MAX_SEATS; after that they start
mid-park) and starts standing in front of it. When they leave, the seat is free for the next
person. Only a player moves their own character.

Messages (JSON text):
  server -> you     {type: "welcome", you, players: [player], seats: [resident], chat: [chat]}
  server -> others  {type: "join", player} | {type: "move", id, x, z, ry, moving}
                    {type: "wave", id} | {type: "leave", id, seat: null}
  server -> all     {type: "chat", id, name, text, at}   (everyone in the room, the sender too)
  you -> server     {type: "move", x, z, ry, moving} | {type: "wave"} | {type: "chat", text}
  player            {id, name, style, seat, x, z, ry, moving}
                    (x and z are null until they first move: they're standing at their seat)
  resident          {id, name, style, seat}   a player in the room and their seat
  chat              {id, name, text, at}   (at: milliseconds since 1970, set by the server)
  style null means: use start.ng's starting look for that id.

Chat: the room keeps its last 50 messages and sends them in the welcome, so a player who comes in
sees what was said. Text is trimmed, cut to 200 characters and has control characters removed;
an empty message, or one sent too soon after the last (0.7 s), is dropped.
"""
import asyncio
import json
import math
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import anyio
import jwt
from fastapi import WebSocket, WebSocketDisconnect

from .config import settings

TICKET_SECONDS = 60
MAX_PLAYERS = 50
MAX_SEATS = 60
WORLD = 60.0  # players stay within this many metres of the middle, on both axes
MOVE_EVERY = 0.04  # seconds; faster moves from one player are dropped
WAVE_EVERY = 1.0
CHAT_EVERY = 0.7  # seconds; faster messages from one player are dropped
MAX_CHAT = 200  # characters in one chat message
CHAT_HISTORY = 50  # messages a room remembers for players who come in later
MAX_MESSAGE = 512  # characters

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# WebSocket close codes the game understands.
BAD_TICKET = 4401
ROOM_FULL = 4403
REPLACED = 4409  # the same player joined again, e.g. from another tab


def create_ticket(user_id: int, game: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(seconds=TICKET_SECONDS)
    return jwt.encode({"sub": str(user_id), "game": game, "typ": "live", "exp": exp}, settings.jwt_secret, algorithm="HS256")


def read_ticket(ticket: str, game: str) -> int | None:
    """The player id from a valid, unexpired ticket for this game, else None."""
    try:
        payload = jwt.decode(ticket, settings.jwt_secret, algorithms=["HS256"])
        if payload.get("typ") != "live" or payload.get("game") != game:
            return None
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def clean_chat(value) -> str:
    """A chat message ready to send: no control characters, trimmed, at most MAX_CHAT characters. Empty if nothing is left."""
    if not isinstance(value, str):
        return ""
    return CONTROL_CHARS.sub(" ", value).strip()[:MAX_CHAT].strip()


def number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


@dataclass
class Resident:
    """A start.ng user, as shown in the room."""

    id: int
    name: str
    style: dict | None


@dataclass
class Player:
    id: int
    name: str
    style: dict | None
    ws: WebSocket
    seat: int | None = None
    x: float | None = None
    z: float | None = None
    ry: float = 0.0
    moving: bool = False
    last_move: float = 0.0
    last_wave: float = 0.0
    last_chat: float = 0.0

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "style": self.style, "seat": self.seat,
            "x": self.x, "z": self.z, "ry": self.ry, "moving": self.moving,
        }


class Room:
    def __init__(self):
        self.players: dict[int, Player] = {}
        self.chat: deque[dict] = deque(maxlen=CHAT_HISTORY)
        self.seats: dict[int, int] = {}  # user id -> seat number
        self.residents: dict[int, Resident] = {}

    def take_seat(self, user_id: int) -> int | None:
        if user_id not in self.seats:
            free = sorted(set(range(MAX_SEATS)) - set(self.seats.values()))
            if free:
                self.seats[user_id] = free[0]
        return self.seats.get(user_id)

    def seat_list(self) -> list[dict]:
        """The seats of the people in the room (nobody else has one)."""
        return [
            {"id": uid, "name": self.residents[uid].name, "style": self.residents[uid].style, "seat": seat}
            for uid, seat in sorted(self.seats.items(), key=lambda kv: kv[1])
            if uid in self.residents and uid in self.players
        ]

    def free_seat(self, user_id: int) -> None:
        self.seats.pop(user_id, None)
        self.residents.pop(user_id, None)

    async def broadcast(self, message: dict, skip: int | None = None) -> None:
        data = json.dumps(message)
        await asyncio.gather(*(self._send(p, data) for pid, p in list(self.players.items()) if pid != skip))

    @staticmethod
    async def _send(player: Player, data: str) -> None:
        try:
            await player.ws.send_text(data)
        except Exception:
            pass  # they're leaving; their own connection cleans up

    @staticmethod
    def spawn(player: Player) -> None:
        """For players without a seat: a spot near the middle, facing it."""
        angle = random.uniform(0, 2 * math.pi)
        dist = random.uniform(2.0, 6.0)
        player.x, player.z = math.cos(angle) * dist, math.sin(angle) * dist
        player.ry = math.atan2(-player.x, -player.z)


rooms: dict[str, Room] = {}


def room_for(game: str) -> Room:
    return rooms.setdefault(game, Room())


async def run_player(room: Room, player: Player) -> None:
    """Joins the room, relays this player's moves and waves until they leave."""
    ws = player.ws
    room.residents[player.id] = Resident(player.id, player.name, player.style)  # their latest name and look
    player.seat = room.take_seat(player.id)
    if player.seat is None:
        room.spawn(player)
    old = room.players.get(player.id)
    room.players[player.id] = player
    if old:
        await room.broadcast({"type": "leave", "id": player.id, "seat": player.seat}, skip=player.id)
        try:
            await old.ws.close(REPLACED, "Joined again from somewhere else")
        except Exception:
            pass
    await ws.send_text(json.dumps({
        "type": "welcome",
        "you": player.id,
        "players": [p.public() for p in room.players.values()],
        "seats": room.seat_list(),
        "chat": list(room.chat),
    }))
    await room.broadcast({"type": "join", "player": player.public()}, skip=player.id)
    try:
        while True:
            event = await ws.receive()
            if event["type"] == "websocket.disconnect":
                break
            text = event.get("text")
            if not text or len(text) > MAX_MESSAGE:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            now = time.monotonic()
            if msg.get("type") == "move":
                x, z, ry = number(msg.get("x")), number(msg.get("z")), number(msg.get("ry"))
                if x is None or z is None or ry is None or now - player.last_move < MOVE_EVERY:
                    continue
                player.last_move = now
                player.x = max(-WORLD, min(WORLD, x))
                player.z = max(-WORLD, min(WORLD, z))
                player.ry = ry % (2 * math.pi)
                player.moving = msg.get("moving") is True
                await room.broadcast(
                    {"type": "move", "id": player.id, "x": player.x, "z": player.z, "ry": player.ry, "moving": player.moving},
                    skip=player.id,
                )
            elif msg.get("type") == "wave" and now - player.last_wave >= WAVE_EVERY:
                player.last_wave = now
                await room.broadcast({"type": "wave", "id": player.id}, skip=player.id)
            elif msg.get("type") == "chat":
                text = clean_chat(msg.get("text"))
                if not text or now - player.last_chat < CHAT_EVERY:
                    continue
                player.last_chat = now
                said = {"id": player.id, "name": player.name, "text": text, "at": int(time.time() * 1000)}
                room.chat.append(said)
                await room.broadcast({"type": "chat", **said})  # to everyone, so the sender's own screen shows the same thing
    except (WebSocketDisconnect, RuntimeError):
        pass  # gone, or closed by us when they joined again elsewhere
    finally:
        if room.players.get(player.id) is player:  # not already replaced by a newer connection
            del room.players[player.id]
            room.free_seat(player.id)  # gone from the park, seat and all
            # Everyone must hear that they left, even when this handler is being cancelled right after the disconnect
            # (Starlette's test client does that, and so does a server shutting down). Unshielded, the cancel could land
            # first and the "leave" never went out, so other players kept a ghost in the park.
            with anyio.CancelScope(shield=True):
                await room.broadcast({"type": "leave", "id": player.id, "seat": None})
