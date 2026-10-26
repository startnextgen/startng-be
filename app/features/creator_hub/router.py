from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import flags
from ...db import get_db
from ...models import Game, User
from ...routers import pages
from ...routers.games import _cover_url, _player_name
from ...routers.pages import _title
from ...security import current_user, optional_user
from ..share_cards import service as share
from . import card as creator_card, service

# main.py mounts this with the feature's flag: every route answers 404 while the flag is off.
router = APIRouter(tags=["creator hub"])

CREATOR = Path(ge=1, le=2_000_000_000)


class CreatorGame(BaseModel):
    name: str
    title: str
    cover_url: str | None
    plays_30: int


class CreatorOut(BaseModel):
    """A creator's public page. Names and numbers only: never an email, never anything about a player."""
    id: int
    slug: str
    name: str
    character: dict | None
    bio: str | None
    games: list[CreatorGame]
    total_plays: int
    players_30: int
    followers: int | None  # null while the follows feature is off
    me: bool


class LeaderOut(BaseModel):
    id: int
    slug: str
    name: str
    character: dict | None
    games: int
    players_30: int
    plays_30: int


class BioIn(BaseModel):
    bio: str | None = Field(default=None, max_length=400)


class TrendDay(BaseModel):
    day: date
    plays: int


class GameStatsOut(BaseModel):
    game: str
    title: str
    hidden: str | None
    plays_7: int
    plays_30: int
    plays_all: int
    visitors_30: int
    players_30: int
    repeat_players_30: int
    runs_30: int
    avg_session_seconds: int | None  # null unless the game uses the Kit
    change_7: int
    trend: list[TrendDay]


class DashboardOut(BaseModel):
    games: list[GameStatsOut]
    best_game: str | None
    plays_7: int
    plays_30: int
    visitors_30: int


def _followers(db: Session, user_id: int) -> int | None:
    if not flags.is_on("follows"):
        return None
    from ...features.follows import service as follows

    return follows.counts(db, user_id)["followers"]


def _creator(db: Session, user_id: int) -> tuple[User, list]:
    """The creator and their visible games; 404 for someone with no game on start.ng."""
    user = db.get(User, user_id)
    games = service.visible_games(db, user_id) if user else []
    if user is None or not games:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No creator here")
    return user, games


def _page_data(db: Session, user: User, games: list, viewer: User | None) -> CreatorOut:
    names = [g.name for g in games]
    series = service.daily(db, names, 30)
    totals = service.total_plays(db, names)
    return CreatorOut(
        id=user.id, slug=service.slug_for(user), name=_player_name(user), character=user.character, bio=service.bio_of(db, user.id),
        games=[CreatorGame(name=g.name, title=_title(g.name), cover_url=_cover_url(g), plays_30=sum(d["plays"] for d in series[g.name])) for g in games],
        total_plays=sum(totals.values()), players_30=service.unique_visitors(db, names), followers=_followers(db, user.id),
        me=viewer is not None and viewer.id == user.id)


@router.get("/by", response_model=list[LeaderOut])
def leaderboard(db: Session = Depends(get_db)):
    """The top creators by different people who played their games in the last 30 days."""
    return [LeaderOut(id=l.user.id, slug=service.slug_for(l.user), name=_player_name(l.user), character=l.user.character,
                      games=l.games, players_30=l.players, plays_30=l.plays) for l in service.leaderboard(db)]


@router.get("/by/{user_id}", response_model=CreatorOut)
def creator(user_id: int = CREATOR, viewer: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    user, games = _creator(db, user_id)
    return _page_data(db, user, games, viewer)


@router.get("/by/{user_id}/page", response_class=Response)
def creator_page(user_id: int = CREATOR, db: Session = Depends(get_db)):
    """The website's HTML with this creator's name and card in its tags, for search engines and shared links (nginx asks)."""
    shell = pages._get_shell()
    if shell is None:
        return Response("The page isn't available right now", status_code=503, media_type="text/plain")
    user = db.get(User, user_id)
    games = service.visible_games(db, user_id) if user else []
    if user is None or not games:
        body = '<main><h1>No creator here</h1><p>There is no creator at this address.</p><p><a href="/by">See top creators</a></p></main>'
        page = pages._page(shell, title="No creator here · start.ng", description="There's no creator at this address.", path=f"/by/{user_id}",
                           image=pages._site() + pages.SHARE_IMAGE, body=body, noindex=True)
        return Response(page, status_code=404, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})
    name, bio = _player_name(user), service.bio_of(db, user_id)
    description = bio or f"{name} makes games on start.ng: " + ", ".join(_title(g.name) for g in games[:5]) + ". Play them free in your browser."
    listing = "".join(f'<li><a href="/games/{pages._esc(g.name)}">{pages._esc(_title(g.name))}</a></li>' for g in games[:20])
    page = pages._page(shell, title=f"{name}'s games · start.ng", description=description, path=f"/by/{service.slug_for(user)}",
                       image=share.links("creator", str(user.id), creator_card.describe(db, str(user.id))).image_url, body=f"<main><h1>{pages._esc(name)}</h1><p>{pages._esc(description)}</p><ul>{listing}</ul><p><a href=\"/by\">See top creators</a></p></main>")
    return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


@router.put("/me/creator-bio", response_model=dict)
def my_bio(body: BioIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"bio": service.set_bio(db, user.id, body.bio)}


@router.get("/me/creator-dashboard", response_model=DashboardOut)
def my_dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Your games' numbers: plays, different people, repeat players, scores sent, average session (Kit games), trend."""
    d = service.dashboard(db, user.id)
    return DashboardOut(games=[GameStatsOut(**g.__dict__) for g in d.games], best_game=d.best_game, plays_7=d.plays_7,
                        plays_30=d.plays_30, visitors_30=d.visitors_30)


def sitemap_urls(db: Session) -> list[tuple[str, str | None]]:
    """Every creator's page (anyone with a game that isn't hidden), while the feature is on."""
    if not flags.is_on("creator_hub"):
        return []  # the routes answer 404 while the flag is off, so the sitemap must not list them
    owners = db.scalars(select(User).where(User.id.in_(select(Game.owner_id).where(Game.hidden.is_(None)))).order_by(User.id))
    return [(f"/by/{service.slug_for(u)}", None) for u in owners]


pages.SITEMAP_PROVIDERS.append(sitemap_urls)


@router.get("/pages/by", response_class=Response, include_in_schema=False)
def leaderboard_page(db: Session = Depends(get_db)):
    """/by's text for search engines, for inside #root (nginx includes it): the same words and classes as
    src/features/creator_hub/CreatorsBoard.tsx."""
    leaders = service.leaderboard(db)
    rows = "".join(
        f'<li><a href="/by/{pages._esc(service.slug_for(l.user))}"><span class="rk">{i}</span>'
        f'<span class="nm"><b>{pages._esc(_player_name(l.user))}</b><em>{pages._plural(l.games, "game", "games")}</em></span>'
        f'<span class="num">{l.players:,} <em>{"player" if l.players == 1 else "players"}</em></span></a></li>'
        for i, l in enumerate(leaders, start=1)
    )
    board = (f'<ol class="creator-board">{rows}</ol>' if leaders
             else '<p class="empty">No plays yet this month. <a href="/games">Play something</a> and the board fills up.</p>')
    body = (
        f'{pages.NAV}<main><section class="container creator-page"><h1>Top creators</h1>'
        '<p class="lead-sm">The people whose games the most different players opened in the last 30 days.</p>'
        f"{board}</section></main>{pages.site_footer()}"
    )
    return Response(body, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})
