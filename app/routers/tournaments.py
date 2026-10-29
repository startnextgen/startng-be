from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .. import leaderboard as boards
from .. import tournaments
from ..db import get_db
from ..models import Tournament, User
from ..schemas import OverallEntry, TournamentGameOut, TournamentOut, TournamentRun, TournamentStanding, TournamentSummary
from ..security import current_user, optional_user
from ..storage import game_url
from .games import _cover_url, _player_name

router = APIRouter(prefix="/tournaments", tags=["tournaments"])

TOP_RUNS = 3
TOP_STANDINGS = 20


def _color(user: User) -> str | None:
    return ((user.character or {}).get("style") or {}).get("primaryColor")


def _standing(st: tournaments.Standing) -> TournamentStanding:
    return TournamentStanding(place=st.place, player_id=st.user.id, name=_player_name(st.user), color=_color(st.user),
                              points=st.points, firsts=st.firsts)


def _out(db: Session, t: Tournament, user: User | None) -> TournamentOut:
    res = tournaments.results(db, t)
    games = []
    for tg in t.games:
        schema = tg.game.leaderboard or boards.DEFAULT
        runs = res.boards.get(tg.game_name, [])
        games.append(TournamentGameOut(
            name=tg.game_name,
            url=game_url(tg.game_name),
            cover_url=_cover_url(tg.game),
            description=tg.game.description,
            leaderboard=schema,
            players=len(runs),
            top=[
                TournamentRun(rank=i, player_id=s.user_id, name=_player_name(s.user), color=_color(s.user),
                              stats=boards.stats_of(schema, s.stats, s.best), points=tournaments.points_for(i))
                for i, s in enumerate(runs[:TOP_RUNS], start=1)
            ],
        ))
    mine = next((st for st in res.standings if user and st.user.id == user.id), None)
    finished = t.finished_at is not None
    return TournamentOut(
        id=t.id,
        starts_at=tournaments.utc(t.starts_at),
        ends_at=tournaments.utc(t.ends_at),
        finished=finished,
        games=games,
        entrants=tournaments.entrants(db, t),
        entered=bool(user) and tournaments.is_entered(db, t, user.id),
        standings=[_standing(st) for st in res.standings[:TOP_STANDINGS]],
        me=_standing(mine) if mine else None,
        winner=_standing(res.standings[0]) if finished and res.standings else None,
        next_starts_at=tournaments.window(tournaments.utc(t.ends_at))[0],
    )


@router.get("/current", response_model=TournamentOut | None)
def current_tournament(user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    """Today's tournament (made automatically the first time anyone asks), or null when there aren't enough games."""
    t = tournaments.current(db)
    return _out(db, t, user) if t else None


@router.post("/current/enter", response_model=TournamentOut)
def enter_tournament(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Enter today's tournament. start.ng calls this when the player shares the tournament on X;
    from then on their runs on its games count. Needs a start.ng sign-in, not a game session."""
    t = tournaments.current(db)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There's no tournament running today")
    tournaments.enter(db, t, user)
    return _out(db, t, user)


@router.get("/recent", response_model=list[TournamentSummary])
def recent_tournaments(limit: int = Query(7, ge=1, le=30), db: Session = Depends(get_db)):
    """Finished tournaments, newest first, with their winner and top 3."""
    out = []
    for t in tournaments.recent(db, limit):
        podium = [_standing(st) for st in tournaments.results(db, t).standings[:3]]
        out.append(TournamentSummary(
            id=t.id,
            starts_at=tournaments.utc(t.starts_at),
            ends_at=tournaments.utc(t.ends_at),
            games=[tg.game_name for tg in t.games],
            winner=podium[0] if podium else None,
            podium=podium,
        ))
    return out


@router.get("/leaderboard", response_model=list[OverallEntry])
def overall_leaderboard(
    period: Literal["all", "week"] = "all",
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """The overall leaderboard: tournament points from every finished tournament, or this week's (from Monday, Nigeria time)."""
    return [
        OverallEntry(rank=i, player_id=r.user.id, name=_player_name(r.user), color=_color(r.user),
                     points=r.points, wins=r.wins, firsts=r.firsts)
        for i, r in enumerate(tournaments.overall(db, period, limit), start=1)
    ]

@router.get("/{tournament_id}", response_model=TournamentOut)
def tournament_result(tournament_id: int, db: Session = Depends(get_db)):
    """The full, read-only results of a specific finished tournament."""
    t = db.get(Tournament, tournament_id)
    if not t or t.finished_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tournament not found or not finished yet")
    return _out(db, t, user=None)