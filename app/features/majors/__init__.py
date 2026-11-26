"""Majors: big public tournaments run with a partner (a festival, a company, a community), with real prizes.

A major is data, not code: a title, the partner, the prizes, 1 to 50 games (the admin picks how many) and a window. So the next collaboration
is a copy of the last one with the words changed (see `templates/` and "Use as a template" on the admin page).
Players enter on the major's own page, start.ng/majors/<slug>; from then on their runs in its games count, with the
daily tournament's 10/7/5/3/1 points per game. Nothing here gives global points, XP or coins."""
from fastapi import APIRouter

from . import admin_router, service  # noqa: F401  (service subscribes to score_submitted)
from .router import router as _players

NAME = "majors"  # the feature flag
PERMISSIONS: dict[str, str] = {
    "majors.manage": "Make and run majors: partner tournaments with prizes (and see who won, to send prizes)",
}

router = APIRouter()
router.include_router(_players)
router.include_router(admin_router.router)
