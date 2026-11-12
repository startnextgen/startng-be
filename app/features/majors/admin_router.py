"""Admins make and run majors (/admin/majors, permission majors.manage)."""
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from ... import admin as adm
from ...admin import AdminUser, require
from ...config import settings
from ...db import get_db
from ...errors import ApiError
from ...routers.games import _player_name
from . import service
from .models import Major
from .router import _color, detail, summary
from .schemas import AdminEntryOut, AdminMajorOut, AdminWinnerOut, CopyIn, MajorIn, MajorOut, RemoveIn

router = APIRouter(prefix="/admin/majors", tags=["majors"])
manage = require("majors.manage")


def _out(db: Session, m: Major) -> AdminMajorOut:
    res = service.results(db, m)
    return AdminMajorOut(
        **summary(db, m), about=m.about, cta_label=m.cta_label, cta_url=m.cta_url, published=bool(m.published),
        link=f"{settings.site_url.rstrip('/')}/majors/{m.slug}",
        winners=[AdminWinnerOut(place=p["place"], prize=p["prize"], player_id=st.user.id, name=_player_name(st.user),
                                color=_color(st.user), points=st.points, email=st.user.email)
                 for p, st in service.winners(m, res)],
        template=service.as_template(m),
    )


def _found(db: Session, slug: str) -> Major:
    m = service.find(db, slug)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There's no major at that address")
    return m


def _refuse(e: service.Refused) -> ApiError:
    return ApiError(status.HTTP_400_BAD_REQUEST, e.code, e.message)


def _fields(body: MajorIn) -> tuple[dict, list[str]]:
    fields = body.model_dump()
    return fields, fields.pop("games")


@router.get("", response_model=list[AdminMajorOut])
def list_all(admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    """Every major, drafts too."""
    return [_out(db, m) for m in service.listed(db, include_drafts=True)]


@router.get("/templates")
def templates(admin: AdminUser = Depends(manage)) -> list[dict]:
    """Ready-made majors to start from (app/features/majors/templates): a blank collaboration, and past ones."""
    return service.templates()


@router.post("", response_model=AdminMajorOut, status_code=status.HTTP_201_CREATED)
def create(body: MajorIn, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    """A new major, as a draft. Look at it (GET /admin/majors/<slug>), then publish it."""
    fields, games = _fields(body)
    try:
        m = service.create(db, admin.user, fields, games)
    except service.Refused as e:
        raise _refuse(e)
    adm.log(db, admin, "major.created", m.slug, title=m.title, games=games)
    db.commit()
    return _out(db, m)


@router.get("/{slug}", response_model=MajorOut)
def preview(slug: str, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    """The page as players will see it, also while it's a draft."""
    return detail(db, _found(db, slug), admin.user)


@router.put("/{slug}", response_model=AdminMajorOut)
def update(slug: str, body: MajorIn, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    m = _found(db, slug)
    fields, games = _fields(body)
    try:
        service.update(db, m, fields, games)
    except service.Refused as e:
        raise _refuse(e)
    adm.log(db, admin, "major.changed", m.slug, title=m.title)
    db.commit()
    return _out(db, m)


@router.post("/{slug}/publish", response_model=AdminMajorOut)
def publish(slug: str, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    m = _found(db, slug)
    m.published = True
    adm.log(db, admin, "major.published", m.slug, title=m.title)
    db.commit()
    return _out(db, m)


@router.post("/{slug}/unpublish", response_model=AdminMajorOut)
def unpublish(slug: str, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    """Back to a draft: players can't see it and runs stop counting. Entries and scores so far are kept."""
    m = _found(db, slug)
    m.published = False
    adm.log(db, admin, "major.unpublished", m.slug, title=m.title)
    db.commit()
    return _out(db, m)


@router.post("/{slug}/copy", response_model=AdminMajorOut, status_code=status.HTTP_201_CREATED)
def copy(slug: str, body: CopyIn, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    """Use this major as the template for a new draft: same partner, words, prizes and games, a new address and dates."""
    source = _found(db, slug)
    try:
        m = service.copy(db, admin.user, source, body.slug, body.title, body.starts_at, body.ends_at)
    except service.Refused as e:
        raise _refuse(e)
    adm.log(db, admin, "major.copied", m.slug, source=source.slug)
    db.commit()
    return _out(db, m)


@router.get("/{slug}/entries", response_model=list[AdminEntryOut])
def entries(slug: str, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    """Everyone who entered, best placed first, with the email to reach a winner."""
    m = _found(db, slug)
    by_user = {st.user.id: st for st in service.results(db, m).standings}
    rows = [AdminEntryOut(
        player_id=e.user_id, name=_player_name(e.user), email=e.user.email, joined_at=e.joined_at, removed=bool(e.removed),
        removed_note=e.removed_note, points=by_user[e.user_id].points if e.user_id in by_user else 0,
        place=by_user[e.user_id].place if e.user_id in by_user else None) for e in m.entries]
    return sorted(rows, key=lambda r: (r.place is None, r.place or 0, r.name.lower()))


@router.post("/{slug}/entries/{user_id}/remove", status_code=status.HTTP_204_NO_CONTENT)
def remove_player(slug: str, user_id: int, body: RemoveIn, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    """Take a player out of the running (cheating, a second account). Their runs stop counting and showing."""
    m = _found(db, slug)
    try:
        service.set_removed(db, m, user_id, True, body.note.strip())
    except service.Refused as e:
        raise _refuse(e)
    adm.log(db, admin, "major.player_removed", m.slug, user_id=user_id, note=body.note.strip())
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{slug}/entries/{user_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
def restore_player(slug: str, user_id: int, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    m = _found(db, slug)
    try:
        service.set_removed(db, m, user_id, False)
    except service.Refused as e:
        raise _refuse(e)
    adm.log(db, admin, "major.player_restored", m.slug, user_id=user_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def delete(slug: str, admin: AdminUser = Depends(manage), db: Session = Depends(get_db)):
    m = _found(db, slug)
    adm.log(db, admin, "major.deleted", m.slug, title=m.title)
    db.delete(m)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
