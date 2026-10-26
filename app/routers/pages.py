"""Pages for search engines and link previews.

The website is a client-side app, so crawlers (and chat apps making a link preview) get an empty page.
For a game's page, start.ng/games/<name>, nginx on the website asks this router for the page instead:
it is the website's own HTML with that game's title, description and preview image already in it,
plus a plain-text copy of the page inside #root. The browser then loads the app as usual and replaces
that text. If this router isn't reachable, nginx falls back to the plain page.

The homepage works differently: nginx serves the website's own homepage file and asks this router only for
the text inside #root (home_page below), so the scripts it loads always match the website that is live.

Also the sitemap, which lists every game (the site's fixed pages and the games from the database).
"""
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, defer, selectinload

from .. import flags
from .. import leaderboard as boards
from .. import tournaments
from ..config import settings
from ..db import get_db
from ..models import Game, IndieGame
from ..server_html import site_footer
from ..storage import game_url
from .games import _cover_url, _player_name

log = logging.getLogger(__name__)
router = APIRouter(prefix="/pages", tags=["pages"], include_in_schema=False)

SEO_BLOCK = re.compile(r"<!--seo-->[\s\S]*?<!--/seo-->")
ROOT = '<div id="root"></div>'
SHARE_IMAGE = "/hangout-cover.jpg"  # used when a game has no cover
SHELL_SECONDS = 300
FIXED_PAGES = ["/", "/games", "/creators", "/tournament", "/leaderboard", "/hangout", "/docs", "/about"]
# Pages of features (app/features) that anyone can read, listed while the feature is on: {feature: path}.
FEATURE_PAGES = {"spotlight": "/spotlight", "wardrobe": "/shop", "creator_hub": "/by", "private_tournaments": "/friends"}

_shell: tuple[float, str] | None = None

# Features add their own addresses to the sitemap: SITEMAP_PROVIDERS.append(fn), where fn(db) returns [(path, last changed date or None)].
SITEMAP_PROVIDERS: list = []
# Features add links to a game's server-written page: GAME_PAGE_PARTS.append(fn), where fn(db, game) returns HTML or "". Use the same
# words and class names as the website's own part for that feature (src/features/<feature>), so both versions of the page agree.
GAME_PAGE_PARTS: list = []
# The same for a game by an independent creator (/creators/<id>-<name>): INDIE_PAGE_PARTS.append(fn), where fn(db, game) returns HTML or "".
INDIE_PAGE_PARTS: list = []


def _site() -> str:
    return settings.site_url.rstrip("/")


def _get_shell() -> str | None:
    """The website's own index.html (so the page loads the same scripts), kept for a few minutes."""
    global _shell
    if _shell and time.monotonic() - _shell[0] < SHELL_SECONDS:
        return _shell[1]
    try:
        res = requests.get(f"{_site()}/index.html", timeout=3)
        res.raise_for_status()
        if "<!--seo-->" not in res.text or ROOT not in res.text:
            raise ValueError("the website's index.html has no seo block")
        _shell = (time.monotonic(), res.text)
        return res.text
    except (requests.RequestException, ValueError):
        log.warning("Couldn't read the website's index.html for game pages", exc_info=True)
        return _shell[1] if _shell else None  # an old copy beats none


def _title(name: str) -> str:
    return " ".join(w.capitalize() for w in name.split("-"))


def slugify(name: str) -> str:
    """Words for an address: "Moon Garden!" -> "moon-garden". Same as the website's (src/GamePage.tsx)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")


def indie_path(game: IndieGame) -> str:
    """start.ng/creators/<id>-<name>: the id finds the game, the name is for people and search."""
    slug = slugify(game.name)
    return f"/creators/{game.id}-{slug}" if slug else f"/creators/{game.id}"


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _page(shell: str, *, title: str, description: str, path: str, image: str, body: str, noindex: bool = False, extra_head: str = "") -> str:
    url = f"{_site()}{path}"
    robots = "noindex, nofollow" if noindex else "index, follow, max-image-preview:large"
    head = "\n    ".join([
        "<!--seo-->",
        f"<title>{_esc(title)}</title>",
        f'<meta name="description" content="{_esc(description)}" />',
        f'<meta name="robots" content="{robots}" />',
        f'<link rel="canonical" href="{_esc(url)}" />',
        '<meta property="og:type" content="website" />',
        '<meta property="og:site_name" content="start.ng" />',
        f'<meta property="og:title" content="{_esc(title)}" />',
        f'<meta property="og:description" content="{_esc(description)}" />',
        f'<meta property="og:url" content="{_esc(url)}" />',
        f'<meta property="og:image" content="{_esc(image)}" />',
        '<meta name="twitter:card" content="summary_large_image" />',
        f'<meta name="twitter:title" content="{_esc(title)}" />',
        f'<meta name="twitter:description" content="{_esc(description)}" />',
        f'<meta name="twitter:image" content="{_esc(image)}" />',
        extra_head,
        "<!--/seo-->",
    ])
    out = SEO_BLOCK.sub(lambda _m: head, shell, count=1)
    return out.replace(ROOT, f'<div id="root">{body}</div>', 1)


@router.get("/games/{name}", response_class=Response)
def game_page(name: str = Path(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$"), db: Session = Depends(get_db)):
    shell = _get_shell()
    if shell is None:
        return Response("The page isn't available right now", status_code=503, media_type="text/plain")

    game = db.get(Game, name)
    if not game or game.hidden:  # hidden while admins look at it (app/admin.py)
        body = "<main><h1>Game not found</h1><p>There's no game with that name on start.ng.</p><p><a href=\"/games\">Browse all games</a></p></main>"
        page = _page(shell, title="Game not found · start.ng", description="There's no game with that name on start.ng. Browse the games instead.",
                     path=f"/games/{name}", image=_site() + SHARE_IMAGE, body=body, noindex=True)
        return Response(page, status_code=404, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})

    title = _title(game.name)
    by = _player_name(game.owner, "a builder")
    description = game.description or f"Play {title} free in your browser on start.ng, a game by {by}."
    image = _cover_url(game) or _site() + SHARE_IMAGE
    path = f"/games/{game.name}"
    ld = {
        "@context": "https://schema.org",
        "@type": "VideoGame",
        "name": title,
        "description": description,
        "url": f"{_site()}{path}",
        "image": image,
        "author": {"@type": "Person", "name": by},
        "gamePlatform": "Web browser",
        "applicationCategory": "Game",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
    }
    extra = f'<script type="application/ld+json">{json.dumps(ld).replace("<", chr(92) + "u003c")}</script>'
    parts = "".join(part(db, game) for part in GAME_PAGE_PARTS)
    body = (
        f"<main><h1>{_esc(title)}</h1><p>{_esc(description)}</p><p>By {_esc(by)}</p>"
        f'<p><a href="{_esc(game_url(game.name))}">Play {_esc(title)}</a> · <a href="/games">All games</a></p>{parts}</main>'
    )
    page = _page(shell, title=f"{title} · Play free on start.ng", description=description, path=path, image=image, body=body, extra_head=extra)
    return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


@router.get("/creators/{game_id}", response_class=Response)
def indie_page(game_id: int = Path(ge=1, le=2_000_000_000), db: Session = Depends(get_db)):
    """The page of a game by an independent creator (hosted outside start.ng), like game_page above."""
    shell = _get_shell()
    if shell is None:
        return Response("The page isn't available right now", status_code=503, media_type="text/plain")

    game = db.scalar(select(IndieGame).options(defer(IndieGame.cover), selectinload(IndieGame.owner)).where(IndieGame.id == game_id))
    if not game:
        body = "<main><h1>Game not found</h1><p>There's no game with that address on start.ng.</p><p><a href=\"/creators\">Browse games by independent creators</a></p></main>"
        page = _page(shell, title="Game not found · start.ng", description="There's no game with that address on start.ng. Browse the games instead.",
                     path=f"/creators/{game_id}", image=_site() + SHARE_IMAGE, body=body, noindex=True)
        return Response(page, status_code=404, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})

    by = _player_name(game.owner, "an independent creator")
    description = game.description
    image = f"{settings.api_url.rstrip('/')}/indie/{game.id}/cover.webp"
    path = indie_path(game)
    ld = {
        "@context": "https://schema.org",
        "@type": "VideoGame",
        "name": game.name,
        "description": description,
        "url": f"{_site()}{path}",
        "image": image,
        "author": {"@type": "Person", "name": by},
        "gamePlatform": "Web browser",
        "applicationCategory": "Game",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
    }
    extra = f'<script type="application/ld+json">{json.dumps(ld).replace("<", chr(92) + "u003c")}</script>'
    parts = "".join(part(db, game) for part in INDIE_PAGE_PARTS)
    body = (
        f"<main><h1>{_esc(game.name)}</h1><p>{_esc(description)}</p><p>By {_esc(by)}, an independent creator</p>"
        f'<p><a href="/creators">All games by independent creators</a></p>{parts}</main>'
    )
    page = _page(shell, title=f"{game.name} · Play free on start.ng", description=description, path=path, image=image, body=body, extra_head=extra)
    return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


# The homepage's text. Everything here is also on the real page, in the same words: keep it in step with the
# home view in startng-fe (src/App.tsx, and the About and questions in src/home-copy.ts).
HANGOUT = "hangout"  # start.ng's own game has its own card, so it isn't in the community list
HOME_GAMES = 8  # cards the homepage shows before "See all" (HOME_GAMES in src/App.tsx)
HOME_GAMES_LISTED = 200  # games listed by name, A to Z (AZ_GAMES in src/App.tsx)
HOME_ABOUT = [
    "start.ng is Nigeria's home for browser games, built by the HNG community. Every game is free to play in your "
    "browser with no download, and many are made in Naija: danfo rush hour, jollof catching, Lagos markets and more.",
    "We're also building a home for web games from across Africa. If you make games anywhere on the continent, "
    "you can put yours on start.ng for free.",
]
HOME_QUESTIONS = [
    ("What is start.ng?",
     "A home for free browser games made by the HNG community. Each game has its own address, like "
     "yourgame.start.ng, and you play it right in your browser."),
    ("Is it free?", "Yes. Every game on start.ng is free to play in your browser, with nothing to download."),
    ("Are there Nigerian or African web games?",
     "Yes. Many games here are Naija-themed, from danfo rush hour to jollof and Lagos markets, and we welcome "
     "games from across Africa."),
    ("Do I need an account to play?",
     "No. Sign in to get your start.ng character, keep your scores on the leaderboards and join the daily tournament."),
    ("What is the daily tournament?",
     "Every day a few start.ng games are picked for 24 hours, and the best player wins. Points count towards the "
     "overall leaderboard."),
    ("How do I put my game on start.ng?",
     "Push your browser game to a public GitHub repo, publish it with one API call and approve it once. It goes "
     "live at yourgame.start.ng. The docs have every step."),
]
# (title, text) for "Got a game? Put it on start.ng"; the text is ours, not a player's, so it may hold <code>.
HOW_STEPS = [
    ("Build it", "Any static browser game: HTML, JavaScript, three.js, Phaser. Players bring their start.ng character, scores and friends."),
    ("Push it to GitHub", "A public repo with a ready-to-play <code>index.html</code> at the top or in <code>dist/</code>, plus a <code>cover.png</code> for its card."),
    ("Publish it", "One API call with your repo link, no key needed. Or hand your AI <code>start.ng/docs</code> and let it do the lot."),
    ("Approve it once", "Open the link you get back and it's live at <code>yourgame.start.ng</code>, with its own leaderboard if you want one. After that, every push updates it."),
]
# The site's header and footer, as the app draws them (src/App.tsx).
NAV = (
    '<header class="container nav"><a class="logo" href="/"><b>start<span>.ng</span></b></a>'
    '<nav aria-label="Pages"><a href="/games">Games</a><a href="/tournament">Tournament</a>'
    '<a href="/creators">Creators</a><a href="/docs">Docs</a></nav></header>'
)


def _visible_games(db: Session) -> list[Game]:
    """The community's games, newest first, as GET /games lists them: not hidden, and not the Hangout."""
    return list(db.scalars(
        select(Game).options(selectinload(Game.owner))
        .where(Game.hidden.is_(None), Game.name != HANGOUT).order_by(Game.updated_at.desc())
    ))


def _home_body(games: list[Game]) -> str:
    listed = sorted(games, key=lambda g: _title(g.name).lower())[:HOME_GAMES_LISTED]
    total = len(games)
    links = "".join(
        f'<li><a href="/games/{_esc(g.name)}">{_esc(_title(g.name))}</a> <span>by {_esc(_player_name(g.owner, "A builder"))}</span></li>'
        for g in listed
    )
    az = f'<details class="az-games"><summary>Every game on start.ng, A to Z ({total})</summary><ul>{links}</ul></details>' if games else ""
    see_all = f'<div class="see-all"><a class="btn" href="/games">See all {total} games →</a></div>' if total > HOME_GAMES else ""
    steps = "".join(f"<li><b>{_esc(t)}</b><span>{text}</span></li>" for t, text in HOW_STEPS)
    about = "".join(f"<p>{_esc(p)}</p>" for p in HOME_ABOUT) + '<p><a href="/about">More about start.ng →</a></p>'
    questions = "".join(f"<details><summary>{_esc(q)}</summary><p>{_esc(a)}</p></details>" for q, a in HOME_QUESTIONS)
    return (
        f"{NAV}<main>"
        '<section class="container hero"><p class="eyebrow">Big imagination. Played everywhere.</p>'
        "<h1>Good vibes.<br><mark>Good games.</mark></h1>"
        '<p class="lead">Games made by builders in the community. Each one lives at its own start.ng address. '
        "Pick one and play right in your browser.</p>"
        '<div class="actions"><a class="btn" href="#games">Play now</a></div></section>'
        f'<section id="games" class="container"><div class="section-head"><div><h2>Games from the community</h2></div></div>{see_all}{az}</section>'
        f'<section id="how" class="container how"><h2>Got a game? Put it on start.ng</h2><ol>{steps}</ol>'
        '<div class="how-actions"><a class="btn" href="/docs">Read the docs</a>'
        '<a class="btn ghost" href="/docs.md">Docs for your AI (.md)</a></div></section>'
        f'<section id="about" class="container about"><h2>About start.ng</h2>{about}'
        f'<h3>Questions</h3><div class="faq">{questions}</div></section>'
        f"</main>{site_footer()}"
    )


@router.get("/home", response_class=Response)
def home_page(db: Session = Depends(get_db)):
    """The homepage's text as HTML for inside #root, not a whole page: nginx includes it in the homepage file it
    serves itself (an SSI include, see nginx.conf in startng-fe), so search engines and AI tools read the
    homepage, the games and the questions without running the app. The app replaces it when it loads."""
    return Response(_home_body(_visible_games(db)), media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


# /games: every game, PER_PAGE to a page (PER_PAGE in src/App.tsx), the Hangout first, then newest first.
PER_PAGE = 20
HANGOUT_CARD = ("The Hangout", "/hangout", "start.ng/hangout", "start.ng")  # HANGOUT in src/games.ts


def _pagination(page: int, pages: int, base: str = "/games") -> str:
    """The same page links as the app's (src/Pagination.tsx): first, last, and two either side of this page (base: /games or /creators)."""
    if pages <= 1:
        return ""
    # Everything from the page number is escaped too: FastAPI makes it a whole number, but the HTML shouldn't rely on that.
    def href(n: int) -> str:
        return _esc(base if n == 1 else f"{base}?page={n}")

    shown = sorted(n for n in {1, pages, page - 2, page - 1, page, page + 1, page + 2} if 1 <= n <= pages)
    current = ' aria-current="page"'
    numbers = "".join(
        ('<span class="gap">…</span>' if i and n - shown[i - 1] > 1 else "")
        + f'<a href="{href(n)}" aria-label="Page {_esc(str(n))}"{current if n == page else ""}>{_esc(str(n))}</a>'
        for i, n in enumerate(shown)
    )
    prev = f'<a href="{href(page - 1)}" rel="prev" aria-label="Previous page">← Previous</a>' if page > 1 else '<span class="off">← Previous</span>'
    nxt = f'<a href="{href(page + 1)}" rel="next" aria-label="Next page">Next →</a>' if page < pages else '<span class="off">Next →</span>'
    return f'<nav class="pagination" aria-label="Pages">{prev}<span class="numbers">{numbers}</span>{nxt}</nav>'


def _games_body(games: list[Game], page: int) -> str | None:
    """None for a page past the last one."""
    cards = [HANGOUT_CARD] + [
        (_title(g.name), f"/games/{g.name}", game_url(g.name).removeprefix("https://"), _player_name(g.owner, "A builder"))
        for g in games
    ]
    pages = max(1, -(-len(cards) // PER_PAGE))
    if page > pages:
        return None
    shown = cards[(page - 1) * PER_PAGE:page * PER_PAGE]
    start = (page - 1) * PER_PAGE + 1
    count = f"{start}–{start + len(shown) - 1} of {len(cards)}" if pages > 1 else f"{len(cards)} {'game' if len(cards) == 1 else 'games'}"
    empty = (
        '<p class="empty">No community games yet. The first ones are on their way. <a href="/#how">Put yours here.</a></p>'
        if not games else ""
    )
    items = "".join(
        f'<article class="card"><h3><a href="{_esc(link)}">{_esc(title)}</a></h3><p>{_esc(host)}</p><span class="by">by {_esc(by)}</span></article>'
        for title, link, host, by in shown
    )
    return (
        f"{NAV}<main>"
        '<section id="games" class="container all-games"><div class="section-head"><h1>All games from the community</h1></div>'
        f'<div class="list-tools"><span class="count">{_esc(count)}</span></div>{empty}<div class="grid">{items}</div>'
        f"{_pagination(page, pages)}</section>"
        f"</main>{site_footer()}"
    )


@router.get("/games", response_class=Response)
def games_page(page: int = Query(1, ge=1, le=100_000), db: Session = Depends(get_db)):
    """/games' text as HTML for inside #root, one page of games at a time (?page=2), like home_page: nginx includes
    it in the games page it serves itself, so search engines can follow every page to every game. Searches and
    filters (?q=, ?category= ...) aren't rendered here: nginx keeps those addresses out of search results.
    A page past the last one is a 404, which nginx turns into the site's 404 page (the app shows "Page not found")."""
    body = _games_body(_visible_games(db), page)
    if body is None:
        return Response("No such page", status_code=404, media_type="text/plain", headers={"Cache-Control": "public, max-age=60"})
    return Response(body, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


# /creators: the newest games by independent creators, PER_PAGE to a page, like /games (CreatorsPage in src/Creators.tsx,
# which lists the newest INDIE_LISTED). Searches (?q=) aren't rendered here: nginx keeps them out of search results.
INDIE_LISTED = 200


def _host(url: str) -> str:
    """The game's site as cards show it: "itch.io", without "www." (hostOf in src/indie.ts)."""
    return (urlparse(url).netloc or url).removeprefix("www.")


def _creators_body(games: list[IndieGame], page: int) -> str | None:
    """None for a page past the last one."""
    pages = max(1, -(-len(games) // PER_PAGE))
    if page > pages:
        return None
    shown = games[(page - 1) * PER_PAGE:page * PER_PAGE]
    start = (page - 1) * PER_PAGE + 1
    count = f"{start}–{start + len(shown) - 1} of {len(games)}" if pages > 1 else _plural(len(games), "game", "games")
    empty = '<p class="empty">No games from independent creators yet.</p>' if not games else ""
    api = settings.api_url.rstrip("/")
    items = "".join(
        f'<article class="game"><a class="game-link indie-open" href="{_esc(indie_path(g))}">'
        f'<div class="art"><img src="{api}/indie/{g.id}/cover.webp" alt="" loading="lazy" decoding="async"></div>'
        f'<div class="info"><h3>{_esc(g.name)}</h3><p>{_esc(_host(g.url))}</p><span class="by">by {_esc(_player_name(g.owner, "A creator"))}</span></div>'
        "</a></article>"
        for g in shown
    )
    return (
        f"{NAV}<main>"
        '<section class="container all-games creators-page"><div class="section-head"><h1>Games by independent creators</h1></div>'
        "<div class=\"creators-intro\"><p>Made and hosted by creators outside start.ng. Tap a card to read about a game, or play it on the creator's own site.</p></div>"
        f'<div class="list-tools"><span class="count">{_esc(count)}</span></div>{empty}<div class="grid">{items}</div>'
        f'{_pagination(page, pages, "/creators")}</section>'
        f"</main>{site_footer()}"
    )


@router.get("/creators", response_class=Response)
def creators_page(page: int = Query(1, ge=1, le=100_000), db: Session = Depends(get_db)):
    """/creators' text as HTML for inside #root, one page at a time (?page=2), like games_page. A page past the last one is a 404."""
    games = list(db.scalars(
        select(IndieGame).options(defer(IndieGame.cover), selectinload(IndieGame.owner))
        .order_by(IndieGame.created_at.desc(), IndieGame.id.desc()).limit(INDIE_LISTED)
    ))
    body = _creators_body(games, page)
    if body is None:
        return Response("No such page", status_code=404, media_type="text/plain", headers={"Cache-Control": "public, max-age=60"})
    return Response(body, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


# /tournament and /leaderboard (src/Tournament.tsx). The server doesn't know the reader's time zone, so times are
# Nigeria time here; the app shows the reader's own once it loads.
POINTS_NOTE = "1st on a game gets 10 points, 2nd 7, 3rd 5, 4th 3, and everyone else who plays it 1."
PAST_WINNERS = 7  # as many as the app asks for (recentTournaments(7))
TOP_STANDINGS = 20  # as many as GET /tournaments/current sends
LEADERBOARD_ROWS = 50  # as many as the app asks for (overallLeaderboard(period, 50))


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _nigeria(when: datetime) -> datetime:
    return tournaments.utc(when).astimezone(timezone(timedelta(hours=settings.tournament_utc_offset)))


def _clock(when: datetime) -> str:
    """"6:00 PM", Nigeria time."""
    local = _nigeria(when)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _day(when: datetime) -> str:
    """"Mon 19 Oct", Nigeria time."""
    local = _nigeria(when)
    return f"{local:%a} {local.day} {local:%b}"


def _stat(stat: dict, value: int | None) -> str:
    """A stat as players read it: times as m:ss, numbers with separators, "–" when missing (formatStat in src/BoardTable.tsx)."""
    if value is None:
        return "–"
    if stat.get("format") == "time":
        return f"{value // 60}:{value % 60:02d}"
    return f"{value:,}"


def _table(label: str, heads: list[str], rows: list[list[str]]) -> str:
    """The app's board tables (.board-table): the place, the player, then numbers."""
    cols = "36px minmax(0, 1fr) " + " ".join("auto" for _ in heads[2:])
    head = "".join(f'<span role="columnheader" class="num">{h}</span>' if i > 1 else f'<span role="columnheader">{h}</span>'
                   for i, h in enumerate(heads))
    kinds = ["rk", "nm"] + ["num"] * (len(heads) - 2)
    body = "".join(
        '<div role="row" class="board-row">' + "".join(f'<span class="{k}">{cell}</span>' for k, cell in zip(kinds, row)) + "</div>"
        for row in rows
    )
    return (f'<div class="board-table" role="table" aria-label="{label}" style="grid-template-columns: {cols}">'
            f'<div class="board-row board-head" role="row">{head}</div>{body}</div>')


def _tournament_body(db: Session) -> str:
    t = tournaments.current(db)
    head = (
        "<div class=\"section-head\"><div><h1>Today's tournament</h1>"
        '<p class="tournament-sub">A few games, 24 hours, one winner. <a href="/leaderboard">Overall leaderboard →</a></p></div></div>'
    )
    if t is None:
        empty = "<p class=\"empty\">No tournament today: there aren't enough games with leaderboards yet. Check back tomorrow.</p>"
        return f'{NAV}<main><section class="container tournament-page">{head}{empty}</section></main>{site_footer()}'

    res = tournaments.results(db, t)
    status = (
        '<div class="tournament-status"><div>'
        f"<p>Ends at <b>{_clock(t.ends_at)}</b> Nigeria time. {_plural(tournaments.entrants(db, t), 'player has', 'players have')} entered.</p>"
        f'<p class="note">{POINTS_NOTE} Most points wins the day.</p></div>'
        "<div class=\"tournament-enter\"><p class=\"note\">Share the tournament on X to enter. Runs before you enter don't count.</p></div></div>"
    )
    cards = []
    for tg in t.games:
        schema = tg.game.leaderboard or boards.DEFAULT
        runs = res.boards.get(tg.game_name, [])
        top = "".join(
            f'<li><span class="nm">{_esc(_player_name(s.user))}</span>'
            f'<span class="sc">{_stat(schema[0], boards.stats_of(schema, s.stats, s.best).get(schema[0]["key"]))}</span></li>'
            for s in runs[:3]
        ) or '<li class="none">No runs yet. Be the first.</li>'
        cards.append(
            f'<article class="game"><a class="game-link" href="/games/{_esc(tg.game_name)}"><div class="info">'
            f"<h3>{_esc(_title(tg.game_name))}</h3><p>{_plural(len(runs), 'player', 'players')}</p></div></a>"
            f'<ol class="tournament-top">{top}</ol>'
            f'<div class="meta"><a class="play" href="{_esc(game_url(tg.game_name))}">Play ↗</a></div></article>'
        )
    standings = (
        _table("Today's standings", ["#", "Player", "Firsts", "Points"],
               [[str(st.place), _esc(_player_name(st.user)), str(st.firsts), str(st.points)] for st in res.standings[:TOP_STANDINGS]])
        if res.standings else "<p class=\"board-empty\">No points yet. Enter, play, and you'll show up here.</p>"
    )
    winners = []
    for past in tournaments.recent(db, PAST_WINNERS):
        first = tournaments.results(db, past).standings[:1]
        who = (f'<span class="nm"><b>{_esc(_player_name(first[0].user))}</b> · {first[0].points} pts</span>' if first
               else '<span class="nm none">No one played</span>')
        winners.append(f'<li><span class="when">{_day(past.ends_at)}</span>{who}</li>')
    past_html = (f'<ul class="winners">{"".join(winners)}</ul>' if winners
                 else "<p class=\"board-empty\">The first winner is named when today's tournament ends.</p>")
    return (
        f'{NAV}<main><section class="container tournament-page">{head}{status}'
        f'<div class="grid tournament-games">{"".join(cards)}</div><div class="tournament-columns">'
        f'<section aria-labelledby="standings-heading"><h2 id="standings-heading">Standings</h2>{standings}</section>'
        f'<section aria-labelledby="winners-heading"><h2 id="winners-heading">Past winners</h2>{past_html}</section>'
        f"</div></section></main>{site_footer()}"
    )


@router.get("/tournament", response_class=Response)
def tournament_page(db: Session = Depends(get_db)):
    """/tournament's text as HTML for inside #root, like home_page: today's games, standings and past winners.
    It asks for today's tournament the way the app does (GET /tournaments/current), so it makes the day's one if no one has yet."""
    return Response(_tournament_body(db), media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


@router.get("/leaderboard", response_class=Response)
def leaderboard_page(period: Literal["all", "week"] = "all", db: Session = Depends(get_db)):
    """/leaderboard's text as HTML for inside #root: tournament points, all time or this week (?period=week, its own address)."""
    rows = tournaments.overall(db, period, LEADERBOARD_ROWS)
    on = ' aria-current="page"'
    tabs = (
        '<div class="segmented" role="group" aria-label="Period">'
        f'<a href="/leaderboard"{on if period == "all" else ""}>All time</a>'
        f'<a href="/leaderboard?period=week"{on if period == "week" else ""}>This week</a></div>'
    )
    board = (
        _table("Overall leaderboard", ["#", "Player", "Days won", "Firsts", "Points"],
               [[str(i), _esc(_player_name(r.user)), str(r.wins), str(r.firsts), str(r.points)] for i, r in enumerate(rows, start=1)])
        if rows else
        f'<p class="board-empty">No points {"this week" if period == "week" else "yet"}. '
        "They come from the daily tournament: <a href=\"/tournament\">enter today's</a>.</p>"
    )
    body = (
        f'{NAV}<main><section class="container tournament-page"><div class="section-head"><div><h1>Leaderboard</h1>'
        "<p class=\"tournament-sub\">Points from the daily tournaments. <a href=\"/tournament\">Today's tournament →</a></p></div></div>"
        f"{tabs}{board}</section></main>{site_footer()}"
    )
    return Response(body, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


# /friends (a feature, private_tournaments): the page that starts a tournament with friends. Its words are the app's
# (src/features/private_tournaments/MyTournaments.tsx); the rest of that page is a form.
FRIENDS_GAMES = 5  # MAX_GAMES there
FRIENDS_LENGTHS = "1 day, 3 days, 1 week, 2 weeks or 1 month"  # DURATIONS there


@router.get("/friends", response_class=Response)
def friends_page():
    """/friends' text as HTML for inside #root. A 404 while the feature is off, like its other routes."""
    if not flags.is_on("private_tournaments"):
        return Response("Not found", status_code=404, media_type="text/plain")
    body = (
        f'{NAV}<main><section class="container tournament-page"><div class="section-head"><div><h1>Tournament with friends</h1>'
        f'<p class="tournament-sub">Pick up to {FRIENDS_GAMES} games and how long it runs, send the link, and see how everyone scores. '
        "<a href=\"/tournament\">Today's tournament →</a></p></div></div>"
        f"<p>Name it, pick the games, and choose how long it runs: {FRIENDS_LENGTHS}. Only the friends you invite are in it.</p>"
        '<p class="note">It starts as soon as you tap the button. Scores count from the moment each player joins.</p>'
        f"</section></main>{site_footer()}"
    )
    return Response(body, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})


@router.get("/sitemap.xml", response_class=Response)
def sitemap(db: Session = Depends(get_db)):
    site = _site()
    urls = [f"  <url><loc>{_esc(site + p)}</loc></url>" for p in FIXED_PAGES + [p for f, p in FEATURE_PAGES.items() if flags.is_on(f)]]
    for name, updated in db.execute(select(Game.name, Game.updated_at).where(Game.hidden.is_(None)).order_by(Game.name)):
        when = (updated.replace(tzinfo=updated.tzinfo or timezone.utc)).date().isoformat() if updated else None
        lastmod = f"<lastmod>{when}</lastmod>" if when else ""
        urls.append(f"  <url><loc>{_esc(f'{site}/games/{name}')}</loc>{lastmod}</url>")
    for game in db.scalars(select(IndieGame).options(defer(IndieGame.cover), selectinload(IndieGame.owner)).order_by(IndieGame.id)):
        urls.append(f"  <url><loc>{_esc(site + indie_path(game))}</loc></url>")
    for provider in SITEMAP_PROVIDERS:
        for path, when in provider(db):
            urls.append(f"  <url><loc>{_esc(site + path)}</loc>{f'<lastmod>{when}</lastmod>' if when else ''}</url>")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(urls) + "\n</urlset>\n"
    return Response(xml, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})
