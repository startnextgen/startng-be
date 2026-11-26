"""start.ng/majors/<slug> as the server writes it (nginx asks for /pages/majors/<slug>): the major's own title,
description and link preview picture in the head, and its text in the page, so search engines and link previews
(X, WhatsApp) see the major, not the general start.ng page. The website takes over when its scripts load.

Also the drawn preview picture itself, /majors/<slug>/share.jpg (share_image.py)."""
import json
import re

from fastapi import APIRouter, Depends, Path, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import flags, tournaments
from ...db import get_db
from ...routers import pages
from . import service, share_image
from .models import Major

router = APIRouter(include_in_schema=False)
SLUG = Path(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=40)
_esc = pages._esc


def _published(db: Session, slug: str) -> Major | None:
    m = service.find(db, slug)
    return m if m is not None and m.published else None


def title(m: Major) -> str:
    """"Tech Ember Fest × start.ng", or "Summer Cup with Acme × start.ng" when the partner's name isn't the title."""
    name = m.title if m.title == m.partner_name else f"{m.title} with {m.partner_name}"
    return f"{name} × start.ng"


def description(m: Major) -> str:
    """The prize, the games and the dates, then the tagline: "Top 3 each win a solo ticket to Tech Ember Fest. Play 5
    games on start.ng, Fri 27 – Sun 29 Nov (Nigeria time). No ticket? No wahala..." """
    label, prize = share_image.prize_line(m)
    prize = re.sub(r"^(A|An|The) ", lambda w: w.group(0).lower(), prize)
    lead = f"{label} ({prize})" if label.endswith("win prizes") else f"{label} {prize}"
    games = len(m.games)
    return f"{lead}. Play {games} game{'' if games == 1 else 's'} on start.ng, {share_image.dates(m)} (Nigeria time). {m.tagline}"


def _event_data(m: Major, link: str, image: str) -> str:
    """schema.org Event, so search engines know what and when it is."""
    data = {
        "@context": "https://schema.org", "@type": "Event", "name": m.title, "description": description(m),
        "startDate": tournaments.utc(m.starts_at).isoformat(), "endDate": tournaments.utc(m.ends_at).isoformat(),
        "eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode", "eventStatus": "https://schema.org/EventScheduled",
        "location": {"@type": "VirtualLocation", "url": link}, "image": [image], "isAccessibleForFree": True,
        "organizer": {"@type": "Organization", "name": "start.ng", "url": pages._site()},
    }
    if m.partner_url:
        data["sponsor"] = {"@type": "Organization", "name": m.partner_name, "url": m.partner_url}
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _body(m: Major) -> str:
    """The same words as the major's page (src/features/majors/MajorPage.tsx), as plain HTML."""
    prizes = "".join(f"<li>{ordinal(p['place'])}: {_esc(p['prize'])}</li>" for p in sorted(m.prizes, key=lambda p: p["place"]))
    games = "".join(f'<li><a href="/games/{_esc(g.game_name)}">{_esc(pages._title(g.game_name))}</a></li>' for g in m.games)
    partner = (f'<a href="{_esc(m.partner_url)}" rel="noopener">{_esc(m.partner_name)}</a>' if m.partner_url else _esc(m.partner_name))
    about = f"<p>{_esc(m.about)}</p>" if m.about else ""
    return (f"<main><h1>{_esc(m.title)}</h1><p>With {partner}. {_esc(share_image.dates(m))}, Nigeria time.</p>"
            f"<p>{_esc(m.tagline)}</p>{about}<h2>Prizes</h2><ol>{prizes}</ol><h2>The games</h2><ul>{games}</ul>"
            f'<p><a href="/majors">All majors</a> · <a href="/tournament">Today\'s tournament</a></p></main>')


@router.get("/pages/majors/{slug}", response_class=Response)
def major_page(slug: str = SLUG, db: Session = Depends(get_db)):
    shell = pages._get_shell()
    if shell is None:
        return Response("The page isn't available right now", status_code=503, media_type="text/plain")
    path = f"/majors/{slug}"
    m = _published(db, slug)
    if m is None:  # a draft doesn't exist for players
        body = '<main><h1>Major not found</h1><p>There\'s no major at that address.</p><p><a href="/majors">See all majors</a></p></main>'
        page = pages._page(shell, title="Major not found · start.ng", description="There's no major at that address on start.ng.",
                           path=path, image=pages._site() + pages.SHARE_IMAGE, body=body, noindex=True)
        return Response(page, status_code=404, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})
    image, name = share_image.url(m), title(m)
    extra = "\n    ".join([
        '<meta property="og:image:width" content="1200" />',
        '<meta property="og:image:height" content="630" />',
        f'<meta property="og:image:alt" content="{_esc(name)}: {_esc(share_image.dates(m))}" />',
        f'<script type="application/ld+json">{_event_data(m, pages._site() + path, image)}</script>',
    ])
    page = pages._page(shell, title=name, description=description(m), path=path, image=image, body=_body(m), extra_head=extra)
    return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


@router.get("/majors/{slug}/share.jpg", response_class=Response)
def share_picture(slug: str = SLUG, db: Session = Depends(get_db)):
    """The drawn link preview picture. Its address carries ?v=<version>, so it can be cached for a long time."""
    m = _published(db, slug)
    if m is None:
        return Response("There's no major at that address", status_code=404, media_type="text/plain")
    jpg = share_image.draw(m)  # a plain def: FastAPI runs it off the event loop
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400", "Access-Control-Allow-Origin": "*"})


def sitemap_urls(db: Session) -> list[tuple[str, str | None]]:
    """Published majors for /sitemap.xml. Not listed while the feature is off: the pages answer 404 then."""
    if not flags.is_on("majors"):  # NAME in __init__.py
        return []
    return [(f"/majors/{m.slug}", None) for m in db.scalars(select(Major).where(Major.published.is_(True)).order_by(Major.id))]


pages.SITEMAP_PROVIDERS.append(sitemap_urls)
