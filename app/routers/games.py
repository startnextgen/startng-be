import hashlib
import logging
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from .. import auto_update, covers, events, flags, manifest, tournaments
from ..features import progress
from ..features.integrity import service as integrity
from ..activity import record
from ..features.moderation import service as moderation
from ..config import settings
from ..db import get_db
from ..errors import ApiError
from ..game_store import GameStore, get_game_store
from ..models import Game, GamePlay, PendingGame, Score, User
from ..checks import Problem, standards, uses_kit
from ..publish import GameBundle, PublishError, Repo, download_repo, game_name, parse_repo_url, read_game
from ..schemas import (
    CheckOut, GameOut, GameSessionOut, LeaderboardEntry, PendingInfo, PendingOut, PlayerOut, PublishIn, PublishOut,
    ScoreIn, ScoreOut,
)
from ..security import GAME_SESSION_MINUTES, create_game_session, current_user, game_player, lenient_user, optional_user
from .. import leaderboard as boards
from ..storage import game_url

log = logging.getLogger(__name__)
router = APIRouter(prefix="/games", tags=["games"])


def _plain(text: str) -> str:
    """Text from a request, for a log line: no line breaks, so it can't fake another log entry."""
    return text.replace("\r", "").replace("\n", "")


def _player_name(user: User, fallback: str = "Player") -> str:
    return (user.character or {}).get("name") or user.name or fallback


def _game_or_404(db: Session, name: str) -> Game:
    game = db.get(Game, name)
    if not game:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No game with that name")
    return game


def require_store(store: GameStore | None = Depends(get_game_store)) -> GameStore:
    if store is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "not_set_up", "Publishing isn't set up on this server yet")
    return store


def _linked_game(db: Session, repo_url: str) -> Game | None:
    """The live game a repo is linked to (GitHub links ignore case)."""
    return db.scalar(select(Game).where(func.lower(Game.repo_url) == repo_url.lower()))


def _check_name_free(db: Session, store: GameStore, name: str, owner_id: int | None) -> Game | None:
    """The game with this name if it's this owner's, None if the name is free; otherwise 409."""
    game = db.get(Game, name)
    if game and (owner_id is None or game.owner_id != owner_id):
        raise ApiError(status.HTTP_409_CONFLICT, "name_taken", f'"{name}" is taken, pick another name')
    # Folders already in the bucket without an owner here (uploaded by hand, like dave/) are taken too.
    if not game and store.paths(name):
        raise ApiError(status.HTTP_409_CONFLICT, "name_taken", f'"{name}" is already in use, pick another name')
    return game


def _download_and_check(repo: Repo) -> GameBundle:
    try:
        return read_game(download_repo(repo))
    except PublishError as err:
        raise ApiError.rejected(err)


def _word_problems(db: Session, name: str, created: bool, bundle: GameBundle) -> list[Problem]:
    """The name filter (app/features/moderation), while it's on: a new game's name, and the description in its
    startng.json. Live games keep their names (admins see any that fail in the Name filter tab). A description
    start.ng found by itself (in the page or README) is just left out when it fails."""
    if not moderation.enforcing():
        return []
    problems = []
    if created and moderation.check_text(name.replace("-", " "), db):
        problems.append(Problem("text_not_allowed", f"The name \"{name}\" has a word that isn't allowed on start.ng. Pick another name."))
    if moderation.check_text(bundle.description, db):
        if "description" in bundle.sections:
            problems.append(Problem("text_not_allowed", "The description in startng.json has a word that isn't allowed on start.ng. Please change it.", "startng.json"))
        else:
            bundle.description = None
    return problems


def _put_live(db: Session, store: GameStore, game: Game | None, name: str, owner_id: int, repo: Repo, bundle: GameBundle,
              auto: bool = False) -> PublishOut:
    """Uploads a checked game under its name and records it, owned by owner_id. auto: start.ng's own update check."""
    created = game is None
    problems = _word_problems(db, name, created, bundle)
    if problems:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, problems[0].code, problems[0].message, problems, bundle.warnings)
    if created:
        game = Game(name=name, owner_id=owner_id, repo_url=repo.url, branch=repo.branch)
        db.add(game)
        try:
            db.commit()  # claim the name before uploading, so two people can't both get it
        except IntegrityError:
            db.rollback()
            raise ApiError(status.HTTP_409_CONFLICT, "name_taken", f'"{name}" was just taken, pick another name')

    # Files a new version no longer has stay in the bucket; the Worker only serves what the
    # game links to, so they're harmless (same as the Worker's publish-game.js).
    # A repo file can't replace the cover the owner uploaded.
    files = {f.path: f.data for f in bundle.files if f.path != covers.COVER_FILE}
    try:
        store.put_game(name, files)
    except Exception:
        log.exception("Uploading game %s failed", _plain(name))
        if created:
            db.delete(game)
            db.commit()
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "upload_failed", "Couldn't upload the game, try again")

    game.repo_url, game.branch = repo.url, repo.branch
    game.leaderboard = bundle.leaderboard
    game.description = bundle.description
    game.brief = bundle.brief
    # Saved whether or not the standards are enforced: tournaments only pick games that meet them.
    game.meets_standards = not standards([(f.path, f.data) for f in bundle.files])
    game.uses_kit = uses_kit([(f.path, f.data) for f in bundle.files])
    manifest.apply_sections(db, game, bundle.sections)  # features' own startng.json sections (app/manifest.py)
    game.update_problem = None  # this version made it
    auto_update.record_commit(game)  # so the update check doesn't publish this same commit again
    if game.cover != covers.COVER_FILE:  # an uploaded cover wins over the game's own
        game.cover = covers.repo_cover(list(files))
        game.cover_at = datetime.now(timezone.utc) if game.cover else None
    if auto:
        record(db, None, "system", "game.auto_updated", name, commit=(game.last_commit or "")[:7])
    else:
        record(db, db.get(User, owner_id), "developer", "game.published" if created else "game.updated", name)
    db.commit()
    if created:
        events.publish("game_published", {"game": name, "owner_id": owner_id})
    else:
        events.publish("game_updated", {"game": name, "owner_id": owner_id, "auto": auto})
    db.refresh(game)  # a feature may have hidden it (game review: new games wait for an admin)
    return PublishOut(
        hidden=game.hidden if created else None,  # never another account's reason, on an anonymous update
        name=name,
        url=game_url(name),
        repo_url=repo.url,
        created=created,
        folder=bundle.folder,
        files=len(bundle.files),
        size_bytes=bundle.total_bytes,
        skipped=bundle.skipped,
        warnings=[p.as_dict() for p in bundle.warnings],
    )


# Publishing without signing in: at most this many new (waiting) games per address per hour.
ANONYMOUS_PER_HOUR = 20
PENDING_DAYS = 30
_recent: dict[str, deque] = defaultdict(deque)


def _client_address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _rate_limit(request: Request, recent: dict[str, deque] | None = None, per_hour: int | None = None,
                message: str = "Too many new games from here, try again in an hour") -> None:
    """At most per_hour hits an hour per address (default: new games published without signing in)."""
    recent = _recent if recent is None else recent
    per_hour = ANONYMOUS_PER_HOUR if per_hour is None else per_hour
    now = time.monotonic()
    hits = recent[_client_address(request)]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= per_hour:
        raise ApiError(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", message)
    hits.append(now)


def _expired(when: datetime) -> bool:
    if when.tzinfo is None:  # SQLite drops the zone
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


def _remove_expired(db: Session) -> None:
    db.execute(delete(PendingGame).where(PendingGame.expires_at <= datetime.now(timezone.utc)))
    db.commit()


@router.post("/publish", response_model=PublishOut | PendingOut)
def publish(
    body: PublishIn,
    request: Request,
    response: Response,
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
    store: GameStore = Depends(require_store),
):
    """Publish (or update) a client-side game from a public GitHub repo at <name>.start.ng.

    Signed in: it goes live on your account. Not signed in: a repo that's already live updates
    straight away; a new one is checked and waits (202) until someone opens approve_url, signs in
    with start.ng and approves it, which puts it live on their account.
    """
    try:
        repo = parse_repo_url(body.repo_url, body.branch)
        name = game_name(body.name or repo.name)
    except PublishError as err:
        raise ApiError.rejected(err, status.HTTP_400_BAD_REQUEST)

    linked = _linked_game(db, repo.url)
    if user is not None:
        if linked is None:
            game = _check_name_free(db, store, name, user.id)
            return _put_live(db, store, game, name, user.id, repo, _download_and_check(repo))
        # A repo is one game: uploading it again updates that game, never makes a second one.
        if linked.owner_id != user.id:
            raise ApiError(status.HTTP_409_CONFLICT, "repo_linked", "This repo is already live on another start.ng account")
        if body.name and name != linked.name:
            raise ApiError(status.HTTP_409_CONFLICT, "repo_linked", f'This repo is live as "{linked.name}"; it can only update that game')
        return _put_live(db, store, linked, linked.name, user.id, repo, _download_and_check(repo))

    if linked is not None:
        # Already approved: this repo is that game, on its owner's account.
        if body.name and name != linked.name:
            raise ApiError(status.HTTP_409_CONFLICT, "repo_linked", f'This repo is live as "{linked.name}"; it can only update that game')
        return _put_live(db, store, linked, linked.name, linked.owner_id, repo, _download_and_check(repo))

    _rate_limit(request)
    _check_name_free(db, store, name, None)
    bundle = _download_and_check(repo)  # report problems now, before anyone approves
    _remove_expired(db)
    pending = PendingGame(
        code=secrets.token_urlsafe(12),
        name=name,
        repo_url=repo.url,
        branch=repo.branch,
        expires_at=datetime.now(timezone.utc) + timedelta(days=PENDING_DAYS),
    )
    db.add(pending)
    db.commit()
    response.status_code = status.HTTP_202_ACCEPTED
    approve_url = f"{settings.site_url.rstrip('/')}/approve/{pending.code}"
    return PendingOut(
        name=name,
        repo_url=repo.url,
        approve_url=approve_url,
        expires_at=pending.expires_at,
        folder=bundle.folder,
        files=len(bundle.files),
        size_bytes=bundle.total_bytes,
        skipped=bundle.skipped,
        warnings=[p.as_dict() for p in bundle.warnings],
        message=(
            f"Checked and ready. Open {approve_url} to sign in with start.ng and put it live on your account. "
            "After that, publishing this repo again updates the game straight away."
        ),
    )


# Checking a repo downloads it, so it has its own hourly budget per address.
CHECKS_PER_HOUR = 30
_recent_checks: dict[str, deque] = defaultdict(deque)


@router.post("/check", response_model=CheckOut)
def check_game(
    body: PublishIn,
    request: Request,
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
    store: GameStore | None = Depends(get_game_store),
):
    """Everything POST /games/publish checks, without publishing: 200 when the game would go live
    (with any warnings), 422 with every problem when it wouldn't."""
    try:
        repo = parse_repo_url(body.repo_url, body.branch)
        name = game_name(body.name or repo.name)
    except PublishError as err:
        raise ApiError.rejected(err, status.HTTP_400_BAD_REQUEST)
    _rate_limit(request, _recent_checks, CHECKS_PER_HOUR, "Too many checks from here, try again in an hour")

    errors: list[Problem] = []
    linked = _linked_game(db, repo.url)
    if linked is not None:
        if user is not None and linked.owner_id != user.id:
            errors.append(Problem("repo_linked", "This repo is already live on another start.ng account"))
        elif body.name and name != linked.name:
            errors.append(Problem("repo_linked", f'This repo is live as "{linked.name}"; it can only update that game'))
        name = linked.name
    else:
        taken = db.get(Game, name)
        if (taken and (user is None or taken.owner_id != user.id)) or (not taken and store and store.paths(name)):
            errors.append(Problem("name_taken", f'"{name}" is taken, pick another name'))
    try:
        bundle = read_game(download_repo(repo))
    except PublishError as err:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, err.code, str(err), errors + err.errors, err.warnings)
    errors += _word_problems(db, name, linked is None and db.get(Game, name) is None, bundle)
    if errors:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, errors[0].code, errors[0].message, errors, bundle.warnings)
    return CheckOut(
        name=name,
        url=game_url(name),
        repo_url=repo.url,
        update=linked is not None,
        folder=bundle.folder,
        files=len(bundle.files),
        size_bytes=bundle.total_bytes,
        skipped=bundle.skipped,
        warnings=[p.as_dict() for p in bundle.warnings],
    )


def _pending_or_404(db: Session, code: str) -> PendingGame:
    pending = db.get(PendingGame, code)
    if pending is None or _expired(pending.expires_at):
        raise ApiError(status.HTTP_404_NOT_FOUND, "link_expired", "This link has expired or was already used")
    return pending


@router.get("/publish/{code}", response_model=PendingInfo)
def pending_game(code: str, db: Session = Depends(get_db)):
    """What the approve page shows about a game waiting to be approved."""
    pending = _pending_or_404(db, code)
    taken = db.get(Game, pending.name) is not None and _linked_game(db, pending.repo_url) is None
    return PendingInfo(name=pending.name, repo_url=pending.repo_url, expires_at=pending.expires_at, available=not taken)


@router.post("/publish/{code}/approve", response_model=PublishOut)
def approve_game(
    code: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    store: GameStore = Depends(require_store),
):
    """Put a waiting game live on your account. Its repo is then linked to you for good."""
    pending = _pending_or_404(db, code)
    repo = parse_repo_url(pending.repo_url, pending.branch)
    linked = _linked_game(db, repo.url)
    if linked is not None and linked.owner_id != user.id:
        raise ApiError(status.HTTP_409_CONFLICT, "repo_linked", "This repo is already live on another start.ng account")
    name = linked.name if linked is not None else pending.name
    game = _check_name_free(db, store, name, user.id)
    out = _put_live(db, store, game, name, user.id, repo, _download_and_check(repo))
    # Every link for this repo is spent now: the repo belongs to this account.
    db.execute(delete(PendingGame).where(func.lower(PendingGame.repo_url) == repo.url.lower()))
    db.commit()
    return out


def _game_out(g: Game, owner_view: bool = False, plays: int = 0) -> GameOut:
    return GameOut(
        plays=plays,
        update_problem=g.update_problem if owner_view else None,
        checked_at=g.checked_at if owner_view else None,
        live_commit=(g.last_commit or None) and g.last_commit[:7] if owner_view else None,
        updates_paused_until=auto_update.paused_until() if owner_view else None,
        hidden=g.hidden if owner_view else None,
        name=g.name,
        url=game_url(g.name),
        repo_url=g.repo_url,
        by=_player_name(g.owner, "A builder"),
        updated_at=g.updated_at,
        cover_url=_cover_url(g),
        description=g.description,
        leaderboard=_board_columns(g),
    )


def _schema(g: Game) -> list[dict]:
    return g.leaderboard or boards.DEFAULT


def _board_columns(g: Game) -> list[dict] | None:
    """Columns for the game's leaderboard; None when it has none (opt-in: a startng.json, or saved scores)."""
    if g.leaderboard:
        return g.leaderboard
    has_scores = object_session(g).scalar(select(Score.id).where(Score.game_name == g.name).limit(1))
    return boards.DEFAULT if has_scores else None


def _cover_url(g: Game) -> str | None:
    """Served by the Worker from the game's folder. ?v= changes with the cover, so browsers
    and Cloudflare never show an old one."""
    if not g.cover:
        return None
    version = int(g.cover_at.timestamp()) if g.cover_at else 0
    return f"{game_url(g.name)}/{quote(g.cover)}?v={version}"


PLAYS_DAYS = 7  # "most played" means the last week
PLAYS_KEPT_DAYS = 30
PLAY_AGAIN_MINUTES = 30  # the same person opening the same game again within this counts once
PLAYS_PER_HOUR = 300  # a separate budget per address, so the repeat check above can't be dodged by changing headers
_plays: dict[str, deque] = defaultdict(deque)


def visitor_of(request: Request) -> str:
    """A one-way fingerprint of the address (never the address itself), to count each person once.

    Only the server-seen address goes in: a header the caller controls (like User-Agent) would let
    one person look like many.
    """
    raw = f"{settings.jwt_secret}|{_client_address(request)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _recent_plays(db: Session) -> dict[str, int]:
    since = datetime.now(timezone.utc) - timedelta(days=PLAYS_DAYS)
    rows = db.execute(select(GamePlay.game_name, func.count()).where(GamePlay.at >= since).group_by(GamePlay.game_name))
    return {name: count for name, count in rows}


@router.get("", response_model=list[GameOut])
def list_games(db: Session = Depends(get_db)):
    """Every visible game, newest first by when it was first published, so new games are the first thing people see. (An update
    does not move a game up.)"""
    plays = _recent_plays(db)
    games = db.scalars(select(Game).where(Game.hidden.is_(None)).order_by(Game.created_at.desc(), Game.name)).all()
    return [_game_out(g, plays=plays.get(g.name, 0)) for g in games]


@router.post("/{name}/plays", status_code=status.HTTP_204_NO_CONTENT)
def count_play(name: str, request: Request, user: User | None = Depends(lenient_user), db: Session = Depends(get_db)):
    """start.ng calls this when someone opens a game, to rank the most played. No sign-in needed; a signed-in player's
    open is also remembered (the Progress Engine)."""
    _game_or_404(db, name)
    _rate_limit(request, _plays, PLAYS_PER_HOUR, "Too many plays from here, try again later")
    visitor = visitor_of(request)
    now = datetime.now(timezone.utc)
    again = db.scalar(select(GamePlay.id).where(
        GamePlay.game_name == name, GamePlay.visitor == visitor, GamePlay.at >= now - timedelta(minutes=PLAY_AGAIN_MINUTES),
    ).limit(1))
    if again is None:
        db.add(GamePlay(game_name=name, visitor=visitor, at=now))
    db.execute(delete(GamePlay).where(GamePlay.at < now - timedelta(days=PLAYS_KEPT_DAYS)))
    db.commit()
    if user is not None and flags.is_on(progress.NAME):
        progress.service.record_open(db, user.id, name)


@router.get("/mine", response_model=list[GameOut])
def my_games(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The signed-in builder's own games, newest first (for their profile)."""
    query = select(Game).where(Game.owner_id == user.id).order_by(Game.updated_at.desc())
    plays = _recent_plays(db)
    return [_game_out(g, owner_view=True, plays=plays.get(g.name, 0)) for g in db.scalars(query).all()]


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def unpublish(
    name: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    store: GameStore = Depends(require_store),
):
    """Take your game offline and free its name (its scores go with it)."""
    game = _game_or_404(db, name)
    if game.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the game's owner can remove it")
    store.delete_game(name)
    db.delete(game)
    record(db, user, "developer", "game.removed", name)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _own_game(db: Session, name: str, user: User) -> Game:
    game = _game_or_404(db, name)
    if game.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the game's owner can change its cover")
    return game


async def cover_upload(request: Request) -> bytes:
    """The picture, sent as the raw request body. Read with a size cap, whatever Content-Length says."""
    too_big = HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "The picture must be 2 MB or less")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > covers.MAX_UPLOAD_BYTES:
        raise too_big
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > covers.MAX_UPLOAD_BYTES:
            raise too_big
        chunks.append(chunk)
    if not size:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Send a PNG, JPEG or WebP picture")
    return b"".join(chunks)


@router.put("/{name}/cover", response_model=GameOut)
def upload_cover(
    name: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    store: GameStore = Depends(require_store),
    data: bytes = Depends(cover_upload),
):
    """Set your game's card picture. It's cropped to 640 x 400 and saved as WebP."""
    game = _own_game(db, name, user)
    try:
        cover = covers.make_cover(data)
    except covers.CoverError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err))
    try:
        store.put_file(name, covers.COVER_FILE, cover, "image/webp")
    except Exception:
        log.exception("Uploading the cover of %s failed", _plain(name))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't save the picture, try again")
    game.cover, game.cover_at = covers.COVER_FILE, datetime.now(timezone.utc)
    record(db, user, "developer", "game.cover_changed", name)
    db.commit()
    return _game_out(game)


@router.delete("/{name}/cover", response_model=GameOut)
def remove_cover(
    name: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    store: GameStore = Depends(require_store),
):
    """Remove the picture you uploaded. The game's own cover.png (if any) or the letter tile shows again."""
    game = _own_game(db, name, user)
    if game.cover == covers.COVER_FILE:
        store.delete_file(name, covers.COVER_FILE)
        game.cover = covers.repo_cover(store.paths(name))
        game.cover_at = datetime.now(timezone.utc) if game.cover else None
        record(db, user, "developer", "game.cover_removed", name)
        db.commit()
    return _game_out(game)


def _player_out(user: User) -> PlayerOut:
    return PlayerOut(id=user.id, name=_player_name(user), style=(user.character or {}).get("style"))


@router.post("/{name}/session", response_model=GameSessionOut)
def game_session(name: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """A game session for the signed-in player: the key a game uses to call its player API.
    It only works for this game, expires after GAME_SESSION_MINUTES, and is never a sign-in.
    start.ng hands it to the game (sign-in redirect or /connect); games never see the real token."""
    _game_or_404(db, name)
    return GameSessionOut(session=create_game_session(user.id, name), expires_in=GAME_SESSION_MINUTES * 60, player=_player_out(user))


@router.get("/{name}/player", response_model=PlayerOut)
def game_player_info(name: str, user: User = Depends(game_player), db: Session = Depends(get_db)):
    """Who the game session belongs to: id, character name and look (never the email)."""
    _game_or_404(db, name)
    return _player_out(user)


@router.post("/{name}/scores", response_model=ScoreOut, response_model_exclude_unset=True)  # `unlocked` shows only when set
def submit_score(
    name: str,
    body: ScoreIn,
    user: User = Depends(game_player),
    db: Session = Depends(get_db),
):
    """Save the player's run: their game's stats (startng.json) or a plain score. Only their best run is kept."""
    game = _game_or_404(db, name)
    schema = _schema(game)
    on = flags.is_on("integrity")  # score integrity (PRD D2): a rate limit, the startng.json `max`, a fast leaderboard
    if on:
        integrity.check_rate(user.id, name)
    if body.stats is not None:
        try:
            stats = boards.check_stats(schema, body.stats)
        except ValueError as err:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err))
    elif body.score is not None and [s["key"] for s in schema] == ["score"]:
        stats = {"score": body.score}
    else:
        keys = ", ".join(s["key"] for s in schema)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"send stats: {keys}")
    if on:
        integrity.check_limits(schema, stats)
    first = stats[schema[0]["key"]]

    # Locked until the commit, so two runs sent at once are compared one after the other and a lower one can't overwrite a higher best.
    mine = select(Score).where(Score.user_id == user.id, Score.game_name == name).with_for_update()
    row = db.scalar(mine)
    if on:  # a run that looks too good to be true is held for review, not scored
        previous = boards.stats_of(schema, row.stats, row.best)[schema[0]["key"]] if row is not None else None
        why = integrity.outlier_reason(db, name, schema, stats, previous)
        if why:
            return _held_back(db, user, name, schema, stats, previous, why, row)
        top_before = integrity.previous_top(db, name)
    if row is None:
        row = Score(user_id=user.id, game_name=name, best=first, stats=stats)
        db.add(row)
        improved = True
        try:
            db.commit()
        except IntegrityError:  # two first runs at once: compare with the one that won
            db.rollback()
            row = db.scalar(mine)
            improved = None
    else:
        improved = None
    if improved is None:
        improved = boards.is_better(schema, stats, boards.stats_of(schema, row.stats, row.best))
        if improved:
            row.best, row.stats = first, stats
            db.commit()
    counted = tournaments.record(db, game, user, stats, schema)  # also counts in today's tournament, if the player entered
    events.publish("score_submitted", {"user_id": user.id, "game": name, "stats": stats})  # private tournaments, and more later
    best = boards.stats_of(schema, row.stats, row.best)
    if on:
        integrity.invalidate(name)
        rank = integrity.rank_of(db, user.id, name)
        if improved and rank == 1 and top_before not in (None, user.id):  # the record changed hands
            events.publish("record_beaten", {"game": name, "user_id": user.id, "previous_user_id": top_before})
    else:
        rows = _ranked(db, name, schema)
        rank = next(i for i, r in enumerate(rows) if r.user_id == user.id) + 1
    extra = {}
    if flags.is_on(progress.NAME):  # the Progress Engine (PRD D1): log the run, award what its rules say
        running = tournaments.running(db) if counted else None
        extra["unlocked"] = progress.service.after_run(
            db, user.id, name, stats, running.id if running else None, rank_after=rank if on else None).unlocked
    return ScoreOut(best=row.best, improved=improved, rank=rank, stats=best, **extra)


def _held_back(db: Session, user: User, name: str, schema: list[dict], stats: dict, previous: int | None, why: str, row: Score | None) -> ScoreOut:
    """A run that looks too good to be true: logged and flagged for an admin to review (score integrity, PRD D2). It isn't
    saved as the player's best, doesn't reach the board or a tournament, earns nothing and publishes no event. The answer
    looks like any other, so nobody can tell it was held back."""
    run_id = progress.service.after_run(db, user.id, name, stats, flagged=True).run_id if flags.is_on(progress.NAME) else None
    integrity.add_flag(db, user.id, name, stats, previous, why, run_id)
    extra = {"unlocked": []} if flags.is_on(progress.NAME) else {}
    if row is not None:
        return ScoreOut(best=row.best, improved=False, rank=integrity.rank_of(db, user.id, name), stats=boards.stats_of(schema, row.stats, row.best), **extra)
    first = stats[schema[0]["key"]]
    return ScoreOut(best=first, improved=False, rank=integrity.hypothetical_rank(db, name, schema, first), stats=stats, **extra)


def _ranked(db: Session, name: str, schema: list[dict]) -> list[Score]:
    """A game's best runs, best first; ties go to whoever got there first."""
    rows = db.scalars(select(Score).where(Score.game_name == name)).all()
    return sorted(rows, key=lambda r: (boards.rank_key(schema, boards.stats_of(schema, r.stats, r.best)), r.updated_at))


@router.get("/{name}/leaderboard", response_model=list[LeaderboardEntry])
def leaderboard(name: str, limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db)):
    """A game's top players, ranked by its own stats (startng.json) or a plain score."""
    game = _game_or_404(db, name)
    schema = _schema(game)

    def build() -> list[LeaderboardEntry]:
        top = integrity.ranked(db, name, limit) if flags.is_on("integrity") else _ranked(db, name, schema)[:limit]
        return [
            LeaderboardEntry(
                rank=i + 1,
                player_id=s.user_id,
                name=_player_name(s.user),
                color=((s.user.character or {}).get("style") or {}).get("primaryColor"),
                score=s.best,
                stats=boards.stats_of(schema, s.stats, s.best),
            )
            for i, s in enumerate(top)
        ]

    return integrity.cached(name, limit, build) if flags.is_on("integrity") else build()


@router.delete("/{name}/scores/{player_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_score(name: str, player_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The game's owner removes a player's score (for example, an impossible one)."""
    if _game_or_404(db, name).owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the game's owner can remove scores")
    row = db.scalar(select(Score).where(Score.user_id == player_id, Score.game_name == name))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That player has no score in this game")
    db.delete(row)
    record(db, user, "developer", "score.removed", name, player_id=player_id)
    db.commit()
    integrity.invalidate(name)
    tournaments.remove_scores(db, name, player_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
