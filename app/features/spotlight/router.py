from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import admin as adm
from ... import leaderboard as boards
from ...admin import AdminUser, require
from ...db import get_db
from ...models import Game, User
from ...routers import pages
from ...routers.games import _cover_url
from ...routers.pages import _title
from ...security import optional_user
from . import service
from .models import DailyChallenge, GameOfTheWeek

# main.py mounts this with the feature's flag: every route answers 404 while the flag is off.
router = APIRouter(tags=["spotlight"])


class PickOut(BaseModel):
    game: str
    title: str
    cover_url: str | None
    note: str | None
    week_start: date


class ChallengeOut(BaseModel):
    day: date
    game: str
    game_title: str
    cover_url: str | None
    stat: str  # the stat's label, "Score"
    goal: int
    goal_shown: str  # as players read it: "600" or "1:05"
    goal_is_a_ceiling: bool  # true when lower is better (a time): "finish in 1:05 or less"
    ends_at: datetime
    done: bool | None  # null when signed out
    best: str | None  # the signed-in player's best today on this game


class SpotlightOut(BaseModel):
    game_of_the_week: PickOut | None
    challenge: ChallengeOut | None


def _pick(db: Session, row: GameOfTheWeek) -> PickOut:
    game = db.get(Game, row.game_name)
    return PickOut(game=game.name, title=_title(game.name), cover_url=_cover_url(game), note=row.note, week_start=row.week_start)


def _shown(stat: dict, value: int) -> str:
    return f"{value // 60}:{value % 60:02d}" if stat["format"] == "time" else f"{value:,}"


def _challenge(db: Session, c: DailyChallenge, user: User | None) -> ChallengeOut:
    game = db.get(Game, c.game_name)
    schema = game.leaderboard or boards.DEFAULT
    stat = next((s for s in schema if s["key"] == c.stat_key), schema[0])
    done = best = None
    if user is not None:
        service.sync(db, user.id, c)
        done = service.completed(db, c.day, user.id) is not None
        mine = service.best_today(db, c, user.id)
        best = _shown(stat, mine) if mine is not None else None
    return ChallengeOut(day=c.day, game=game.name, game_title=_title(game.name), cover_url=_cover_url(game), stat=stat["label"], goal=c.goal,
                        goal_shown=_shown(stat, c.goal), goal_is_a_ceiling=stat["order"] == "lowest", ends_at=service.day_start(c.day + timedelta(days=1)),
                        done=done, best=best)


@router.get("/spotlight", response_model=SpotlightOut)
def spotlight(user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    """This week's Game of the Week and today's daily challenge (made now if nobody has asked yet today)."""
    pick, c = service.game_of_the_week(db), service.today(db)
    return SpotlightOut(game_of_the_week=_pick(db, pick) if pick else None, challenge=_challenge(db, c, user) if c else None)


@router.get("/spotlight/challenge", response_model=ChallengeOut | None)
def daily_challenge(user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    c = service.today(db)
    return _challenge(db, c, user) if c else None


# ---- admin: the Spotlight tab

edit = require("spotlight.edit")


class PickIn(BaseModel):
    game: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=300)
    week: date | None = None  # any day of the week (default: this week)


class AdminPick(BaseModel):
    week_start: date
    game: str
    title: str
    note: str | None
    picked_by: str


class AdminSpotlight(BaseModel):
    picks: list[AdminPick]  # this week and the ones booked after it
    games: list[str]  # every live game, to choose from
    challenge: ChallengeOut | None


@router.get("/admin/spotlight", response_model=AdminSpotlight)
def admin_spotlight(admin: AdminUser = Depends(edit), db: Session = Depends(get_db)):
    this_week = service.week_start(service.local_day(service.now()))
    rows = db.scalars(select(GameOfTheWeek).where(GameOfTheWeek.week_start >= this_week).order_by(GameOfTheWeek.week_start).limit(8)).all()
    c = service.today(db)
    return AdminSpotlight(
        picks=[AdminPick(week_start=r.week_start, game=r.game_name, title=_title(r.game_name), note=r.note, picked_by=r.picked_by) for r in rows],
        games=[g.name for g in db.scalars(select(Game).where(Game.hidden.is_(None)).order_by(Game.name)).all()], challenge=_challenge(db, c, None) if c else None)


@router.put("/admin/spotlight/game-of-the-week", status_code=status.HTTP_204_NO_CONTENT)
def pick(body: PickIn, admin: AdminUser = Depends(edit), db: Session = Depends(get_db)):
    try:
        row = service.pick_game_of_the_week(db, body.game, admin.email, body.note, body.week)
    except service.SpotlightError as e:
        raise HTTPException(e.status, e.message)
    adm.log(db, admin, "spotlight.pick", body.game, week=str(row.week_start))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/admin/spotlight/game-of-the-week", status_code=status.HTTP_204_NO_CONTENT)
def unpick(week: date = Query(...), admin: AdminUser = Depends(edit), db: Session = Depends(get_db)):
    service.clear_game_of_the_week(db, week)
    adm.log(db, admin, "spotlight.clear", str(service.week_start(week)))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- /spotlight's text for search engines (nginx puts it inside #root, see startng-fe nginx.conf)

@router.get("/pages/spotlight", response_class=Response, include_in_schema=False)
def spotlight_page(db: Session = Depends(get_db)):
    """The same words and classes as src/features/spotlight/SpotlightPage.tsx, signed out. Times are Nigeria time."""
    s = spotlight(None, db)
    c, pick = s.challenge, s.game_of_the_week
    if c:
        goal = f"Finish {c.game_title} in {c.goal_shown} or less" if c.goal_is_a_ceiling else f"Reach {c.goal_shown} in {c.game_title}"
        daily = (
            f'<div class="spot-card daily"><p class="eyebrow">Daily challenge · until {pages._clock(c.ends_at)} Nigeria time</p>'
            f"<h2>{pages._esc(goal)}</h2><p class=\"lead-sm\">One goal a day, set from how people have been playing. "
            f"{pages._esc(c.stat)} counts, and only today's runs.</p>"
            f'<div class="spot-actions"><a class="btn" href="/games/{pages._esc(c.game)}">Play now</a></div></div>'
        )
    else:
        daily = '<p class="note">No daily challenge yet: it appears once a few games have enough plays.</p>'
    if pick:
        cover = f'<img src="{pages._esc(pick.cover_url)}" alt="" loading="lazy">' if pick.cover_url else ""
        note = f'<p class="lead-sm">{pages._esc(pick.note)}</p>' if pick.note else ""
        week = (
            f'<div class="spot-card week"><p class="eyebrow">Game of the Week</p><div class="spot-game">{cover}<div>'
            f'<h2>{pages._esc(pick.title)}</h2>{note}<a class="btn" href="/games/{pages._esc(pick.game)}">Play {pages._esc(pick.title)}</a></div></div></div>'
        )
    else:
        week = '<p class="note">No Game of the Week picked yet.</p>'
    body = f'{pages.NAV}<main><section class="container spotlight-page"><h1>Spotlight</h1>{daily}{week}</section></main>{pages.site_footer()}'
    return Response(body, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})
