from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from ... import leaderboard as boards
from ... import tournaments
from ...db import get_db
from ...errors import ApiError
from ...models import User
from ...routers.games import _cover_url, _player_name
from ...security import current_user, optional_user
from ...storage import game_url
from . import service
from .models import Major
from .schemas import GameOut, MajorOut, PartnerOut, PrizeOut, RunOut, StandingOut, SummaryOut, WinnerOut

# main.py mounts this with the feature's flag: every route answers 404 while the flag is off.
router = APIRouter(prefix="/majors", tags=["majors"])

TOP_RUNS = 10
TOP_STANDINGS = 100


def _color(user: User) -> str | None:
    return ((user.character or {}).get("style") or {}).get("primaryColor")


def _standing(st: service.Standing) -> StandingOut:
    return StandingOut(place=st.place, player_id=st.user.id, name=_player_name(st.user), color=_color(st.user),
                       points=st.points, firsts=st.firsts, games_played=st.games_played)


def summary(db: Session, m: Major) -> dict:
    return dict(
        slug=m.slug, title=m.title, tagline=m.tagline,
        partner=PartnerOut(name=m.partner_name, url=m.partner_url, logo_url=m.partner_logo_url),
        prizes=[PrizeOut(**p) for p in m.prizes], starts_at=tournaments.utc(m.starts_at), ends_at=tournaments.utc(m.ends_at),
        state=service.state(m), entrants=service.entrant_count(db, m), games=[g.game_name for g in m.games],
    )


def detail(db: Session, m: Major, user: User | None) -> MajorOut:
    """The whole page: the games with their boards, the standings, and the winners once it has ended."""
    res = service.results(db, m)
    game_boards = []
    for mg in m.games:
        schema = mg.game.leaderboard or boards.DEFAULT
        runs = res.boards.get(mg.game_name, [])
        run = lambda i, s: RunOut(rank=i, player_id=s.user_id, name=_player_name(s.user), color=_color(s.user),
                                  stats=boards.stats_of(schema, s.stats, s.best), points=tournaments.points_for(i))
        own = next(((i, s) for i, s in enumerate(runs, start=1) if user and s.user_id == user.id), None)
        game_boards.append(GameOut(
            name=mg.game_name, url=game_url(mg.game_name), cover_url=_cover_url(mg.game), description=mg.game.description,
            leaderboard=schema, players=len(runs),
            top=[run(i, s) for i, s in enumerate(runs[:TOP_RUNS], start=1)], mine=run(*own) if own else None,
        ))
    mine = service.entry(db, m, user.id) if user else None
    me = next((st for st in res.standings if user and st.user.id == user.id), None)
    return MajorOut(
        **summary(db, m), about=m.about, cta_label=m.cta_label, cta_url=m.cta_url, published=bool(m.published),
        entered=mine is not None, removed=bool(mine and mine.removed), played=sum(1 for g in game_boards if g.mine),
        game_boards=game_boards, standings=[_standing(st) for st in res.standings[:TOP_STANDINGS]],
        me=_standing(me) if me else None,
        winners=[WinnerOut(place=p["place"], prize=p["prize"], player_id=st.user.id, name=_player_name(st.user),
                           color=_color(st.user), points=st.points) for p, st in service.winners(m, res)],
    )


def _published(db: Session, slug: str) -> Major:
    m = service.find(db, slug)
    if m is None or not m.published:  # a draft doesn't exist for players
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There's no major at that address")
    return m


def _refuse(e: service.Refused) -> ApiError:
    return ApiError(status.HTTP_400_BAD_REQUEST, e.code, e.message)


@router.get("", response_model=list[SummaryOut])
def list_majors(db: Session = Depends(get_db)):
    """Published majors: running, then upcoming, then the ones that ended."""
    return [SummaryOut(**summary(db, m)) for m in service.listed(db)]


@router.get("/{slug}", response_model=MajorOut)
def get_one(slug: str, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    return detail(db, _published(db, slug), user)


@router.post("/{slug}/enter", response_model=MajorOut)
def enter(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _published(db, slug)
    try:
        service.enter(db, m, user)
    except service.Refused as e:
        raise _refuse(e)
    return detail(db, m, user)


@router.delete("/{slug}/entry", status_code=status.HTTP_204_NO_CONTENT)
def leave(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Leave before it starts. Once it runs, entrants stay on the table (so nobody dodges a result)."""
    m = _published(db, slug)
    if service.state(m) != "upcoming":
        raise ApiError(status.HTTP_400_BAD_REQUEST, "started", "It has started, so you can't leave now.")
    mine = service.entry(db, m, user.id)
    if mine is not None and mine.removed:  # leaving and entering again would wipe an admin's decision
        raise ApiError(status.HTTP_400_BAD_REQUEST, "removed", "An admin has taken you out of this major, so you can't leave or enter it again.")
    if mine is not None:
        db.delete(mine)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
