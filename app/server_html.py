"""Pieces shared by the pages the server writes (a game's own pages, the category pages): the top bar and footer as plain
links, and a game card that looks like the website's own (src/GameCard.tsx). React does not start on these pages, so
nothing here needs a script."""
import html

CARD_COLORS = [("#ffd400", "#e10600"), ("#1dd1a1", "#0c6e5a"), ("#a55eea", "#4b7bec"), ("#ff6b6b", "#f7b731"), ("#45aaf2", "#2b2f4a"), ("#fd9644", "#eb3b5a")]


def colors_for(name: str) -> tuple[str, str]:
    """The colour pair for a name: the same as src/games.ts colorsFor, so a card looks the same here and in the app."""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return CARD_COLORS[h % len(CARD_COLORS)]


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def site_footer() -> str:
    """The site's footer, word for word what src/SiteFooter.tsx draws (a page that search engines read must match what people see).
    Categories is linked only while the tags feature is on, like the app."""
    from datetime import datetime, timezone

    from . import flags

    year = datetime.now(timezone.utc).year
    categories = '<li><a href="/categories">Categories</a></li>' if flags.is_on("tags") else ""
    return (
        '<footer class="container site-footer"><div class="site-footer-grid">'
        '<div class="site-footer-brand"><a class="logo" href="/"><b>start<span>.ng</span></b></a>'
        "<p>Nigeria's home for browser games, made by the community and by creators across Africa.</p></div>"
        '<nav aria-label="Play"><h2>Play</h2><ul><li><a href="/games">All games</a></li><li><a href="/tournament">Daily tournament</a></li>'
        f'<li><a href="/leaderboard">Leaderboard</a></li>{categories}<li><a href="/hangout">The Hangout</a></li></ul></nav>'
        '<nav aria-label="Make games"><h2>Make games</h2><ul><li><a href="/creators">Independent creators</a></li>'
        '<li><a href="/docs">Build a game</a></li><li><a href="/docs.md">Docs for your AI (.md)</a></li></ul></nav>'
        '<nav aria-label="About"><h2>About</h2><ul><li><a href="/about">About start.ng</a></li></ul></nav></div>'
        f'<p class="site-footer-base"><span>© {year} start.ng</span><span>Games from Nigeria and across Africa.</span></p></footer>'
    )


def chrome(inner: str) -> str:
    return (
        '<header class="container sub-top"><a class="logo" href="/"><b>start<span>.ng</span></b></a>'
        '<nav aria-label="Pages"><a href="/games">Games</a><a href="/tournament">Tournament</a><a href="/creators">Creators</a><a href="/docs">Docs</a></nav></header>'
        f"{inner}"
        f"{site_footer()}"
    )


def game_card(name: str, title: str, by: str, host: str, cover_url: str | None) -> str:
    a, b = colors_for(name)
    art = f'<img src="{esc(cover_url)}" alt="" loading="lazy" decoding="async">' if cover_url else f"<span>{esc(title[:1])}</span>"
    return (
        f'<article class="game"><a class="game-link" href="/games/{esc(name)}">'
        f'<div class="art" style="background: linear-gradient(135deg, {a}, {b})">{art}</div>'
        f'<div class="info"><h3>{esc(title)}</h3><p>{esc(host)}</p><span class="by">by {esc(by)}</span></div></a>'
        f'<div class="meta"><a class="play" href="/games/{esc(name)}">View</a></div></article>'
    )


def tag_tile(slug: str, label: str, games: int) -> str:
    a, b = colors_for(slug)
    return (
        f'<a class="tag-tile" href="/categories/{esc(slug)}" style="background: url(/category-art/{esc(slug)}.webp) center / cover, linear-gradient(135deg, {a}, {b})">'
        f'<b>{esc(label)}</b><span>{games} {"game" if games == 1 else "games"}</span></a>'
    )
