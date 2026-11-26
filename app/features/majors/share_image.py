"""A major's link preview picture (1200x630, what X, WhatsApp and Facebook show when its link is pasted).

Drawn from the major's own details, so every collaboration gets one without design work: start.ng's mark x the
partner's logo and name, the title, the dates (Nigeria time), the prize on a ticket, and the homepage card's picture
fading in on the right under the partner's purple. The same look as the homepage card (src/features/majors/majors.css).

The partner's logo and the card picture are links an admin typed, so they're fetched here with limits: https only,
public addresses only, no redirects, a size cap and a short timeout. If one can't be fetched the picture is drawn
without it, never left broken."""
import hashlib
import io
import ipaddress
import json
import logging
import socket
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from ... import tournaments
from ...config import settings
from .models import Major

log = logging.getLogger(__name__)

W, H, M = 1200, 630, 64
FONT = Path(__file__).parents[1] / "share_cards" / "fonts" / "Archivo.ttf"  # Archivo, SIL Open Font License
NAVY, NAVY_DEEP = (28, 22, 70), (18, 13, 48)  # the card's background (--major-card in src/features/majors/majors.css)
PURPLE = (123, 47, 247)  # --major-tint
LIME, INK = (216, 253, 113), (30, 31, 26)  # start.ng's own colours
WHITE, SOFT, LAVENDER = (255, 255, 255), (226, 222, 246), (201, 168, 255)
JPEG_QUALITY = 88  # WhatsApp skips previews much over 300 KB

FETCH_BYTES = 6 * 1024 * 1024
FETCH_TIMEOUT = (3, 5)  # connect, read
FETCH_SECONDS = 3600  # a fetched picture is kept this long
PICTURES_KEPT = 32
DRAWN_KEPT = 64
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_pictures: OrderedDict[str, tuple[float, Image.Image | None]] = OrderedDict()
_drawn: OrderedDict[str, bytes] = OrderedDict()
_lock = threading.Lock()  # FreeType and the caches are shared between threads


@lru_cache(maxsize=32)
def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT), size)
    font.set_variation_by_name(weight)
    return font


# ---- the words


def _nigeria(when: datetime) -> datetime:
    return tournaments.utc(when).astimezone(timezone(timedelta(hours=settings.tournament_utc_offset)))


def dates(m: Major) -> str:
    """"Fri 27 – Sun 29 Nov", Nigeria time. An end at midnight belongs to the day before (it ends as Sunday does)."""
    start, end = _nigeria(m.starts_at), _nigeria(m.ends_at) - timedelta(minutes=1)
    day = lambda d, month=True: f"{d:%a} {d.day}" + (f" {MONTHS[d.month - 1]}" if month else "")
    if start.date() >= end.date():
        return day(start)
    return f"{day(start, start.month != end.month)} – {day(end)}"


def prize_line(m: Major) -> tuple[str, str]:
    """The ticket's small label and its big line: ("Top 3 each win", "A solo ticket to Tech Ember Fest")."""
    prizes = sorted(m.prizes, key=lambda p: p["place"])
    if len(prizes) == 1:
        return "The winner gets", prizes[0]["prize"]
    if all(p["prize"] == prizes[0]["prize"] for p in prizes):
        return f"Top {len(prizes)} each win", prizes[0]["prize"]
    return f"Top {len(prizes)} win prizes", f"1st: {prizes[0]['prize']}"


def version(m: Major) -> str:
    """Changes whenever anything on the picture does: its address's ?v= and the key of the drawn copy."""
    parts = [m.slug, m.title, m.partner_name, m.partner_logo_url, m.image_url, dates(m), prize_line(m), len(m.games)]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:12]


def url(m: Major) -> str:
    """The picture link previews use: the admin's own one if they gave one, else this drawn one."""
    if m.share_image_url:
        return m.share_image_url
    return f"{settings.api_url.rstrip('/')}/majors/{m.slug}/share.jpg?v={version(m)}"


# ---- fetching the partner's pictures


def _public_host(host: str) -> bool:
    """True when every address the host has is on the public internet (not this server, not a private network)."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def _download(link: str) -> Image.Image | None:
    parts = urlsplit(link)
    if parts.scheme != "https" or not parts.hostname or not _public_host(parts.hostname):
        return None
    with requests.get(link, stream=True, timeout=FETCH_TIMEOUT, allow_redirects=False,
                      headers={"User-Agent": "start.ng link preview"}) as res:
        if res.status_code != 200:
            return None
        data = bytearray()
        for chunk in res.iter_content(64 * 1024):
            data += chunk
            if len(data) > FETCH_BYTES:
                return None
    img = Image.open(io.BytesIO(bytes(data)))
    if img.format not in {"JPEG", "PNG", "WEBP", "GIF"} or img.width * img.height > 40_000_000:
        return None
    img.load()
    img = img.convert("RGBA")
    img.thumbnail((1600, 1600))
    return img


def _picture(link: str | None) -> Image.Image | None:
    """The picture at link, or None if it can't be fetched. Kept for an hour either way, so a dead link isn't asked
    for on every preview."""
    if not link:
        return None
    hit = _pictures.get(link)
    if hit and time.monotonic() - hit[0] < FETCH_SECONDS:
        return hit[1]
    try:
        img = _download(link)
    except (requests.RequestException, OSError, ValueError, Image.DecompressionBombError):
        log.warning("Couldn't fetch %s for a major's link preview", link, exc_info=True)
        img = None
    _pictures[link] = (time.monotonic(), img)
    _pictures.move_to_end(link)
    if len(_pictures) > PICTURES_KEPT:
        _pictures.popitem(last=False)
    return img


# ---- drawing


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """img scaled and cropped to fill w x h, like CSS object-fit: cover."""
    scale = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * scale)), max(h, round(img.height * scale))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def _glow(size: tuple[int, int], centre: tuple[int, int], radius: int, color, strength: int) -> Image.Image:
    """A soft round light (like a CSS radial-gradient), as an RGBA layer."""
    layer = Image.new("RGBA", size, color + (0,))
    mask = Image.new("L", size, 0)
    cx, cy = centre
    ImageDraw.Draw(mask).ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=strength)
    layer.putalpha(mask.filter(ImageFilter.GaussianBlur(radius * .45)))
    return layer


def _background(photo: Image.Image | None) -> Image.Image:
    img = Image.new("RGBA", (W, H), NAVY)
    shade = Image.linear_gradient("L").resize((W, H)).point(lambda v: v * 120 // 255)  # darker towards the bottom
    img.paste(Image.new("RGBA", (W, H), NAVY_DEEP + (255,)), (0, 0), shade)
    if photo is not None:
        left = 470  # the photo covers the right of the picture and fades out towards the words
        art = _cover(photo, W - left, H)
        fade = Image.linear_gradient("L").rotate(90, expand=True).resize((W - left, H))  # 0 on the left, 255 on the right
        fade = fade.point(lambda v: min(255, max(0, (v - 20) * 255 // 150)) * 92 // 100)
        img.paste(art, (left, 0), fade)
        tint = Image.new("RGBA", (W - left, H), PURPLE + (0,))
        tint.putalpha(fade.point(lambda v: v * 70 // 255))  # the partner's purple laid over it, like the card's ::before
        img.alpha_composite(tint, (left, 0))
        foot = Image.linear_gradient("L").resize((W, 220)).point(lambda v: v * 190 // 255)  # so the link reads over the photo
        img.paste(Image.new("RGBA", (W, 220), NAVY_DEEP + (255,)), (0, H - 220), foot)
    img.alpha_composite(_glow((W, H), (W - 120, -40), 420, PURPLE, 150))
    img.alpha_composite(_glow((W, H), (-60, H + 40), 360, PURPLE, 90))
    return img


def _mark(img: Image.Image, x: int, y: int, s: int) -> None:
    """start.ng's mark: the lime rounded square with the dark play arrow (public/favicon.svg)."""
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x, y, x + s, y + s), radius=round(s * .22), fill=(216, 253, 113))
    k = s / 64
    d.polygon([(x + 26 * k, y + 18.5 * k), (x + 26 * k, y + 45.5 * k), (x + 48 * k, y + 32 * k)], fill=(41, 42, 35))


def _logo(img: Image.Image, logo: Image.Image, x: int, y: int, s: int) -> None:
    """The partner's logo fitted in an s x s rounded square."""
    tile = _cover(logo, s, s)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=round(s * .22), fill=255)
    tile.putalpha(ImageChops.darker(tile.getchannel("A"), mask))  # the logo's own see-through parts stay see-through
    img.alpha_composite(tile, (x, y))


def _fit(text: str, font: ImageFont.FreeTypeFont, width: float) -> str:
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _tracked(d: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont, fill, track: float) -> float:
    """Spaced-out small capitals on the baseline xy. Returns the width."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill, anchor="ls")
        x += font.getlength(ch) + track
    return x - xy[0] - track


def _title_lines(title: str, width: float) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """The title in one or two lines, as big as fits (96 down to 56)."""
    words = title.split()
    for size in range(96, 54, -4):
        font = _font(size, "Black")
        if font.getlength(title) <= width:
            return font, [title]
        for i in range(len(words) - 1, 0, -1):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if font.getlength(a) <= width and font.getlength(b) <= width:
                return font, [a, b]
    font = _font(56, "Black")
    return font, [_fit(title, font, width)]


def _ticket(label: str, prize: str) -> Image.Image:
    """The prize as a lime ticket: the label, the prize, and a torn-off stub with notches, like the homepage card's."""
    tw, th, stub = 600, 128, 84
    img = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, tw - 1, th - 1), radius=16, fill=LIME + (255,))
    for cy in (0, th):  # the notches either side of the tear
        d.ellipse((tw - stub - 12, cy - 12, tw - stub + 12, cy + 12), fill=(0, 0, 0, 0))
    for y in range(20, th - 16, 14):  # the perforation
        d.line((tw - stub, y, tw - stub, y + 7), fill=INK + (110,), width=3)
    small = _font(17, "Bold")
    _tracked(d, (26, 50), label.upper(), small, (106, 31, 224), 2.6)
    room = tw - stub - 50
    size = 34
    while size > 24 and _font(size, "ExtraBold").getlength(prize) > room:
        size -= 2
    big = _font(size, "ExtraBold")
    d.text((26, 94), _fit(prize, big, tw - stub - 50), font=big, fill=INK, anchor="ls")
    s = _font(22, "Black")
    for i, ch in enumerate("FREE"):
        d.text((tw - stub / 2, 30 + i * 24), ch, font=s, fill=(106, 31, 224), anchor="mm")
    return img


def _draw(m: Major) -> bytes:
    photo, logo = _picture(m.image_url), _picture(m.partner_logo_url)
    img = _background(photo)
    d = ImageDraw.Draw(img)

    # start.ng x the partner, top left
    s, y = 48, M
    _mark(img, M, y, s)
    brand = _font(34, "ExtraBold")
    x = M + s + 14
    d.text((x, y + s * .74), "start.ng", font=brand, fill=WHITE, anchor="ls")
    x += brand.getlength("start.ng") + 20
    times = _font(34, "Bold")
    d.text((x, y + s * .74), "×", font=times, fill=LIME, anchor="ls")
    x += times.getlength("×") + 20
    if logo is not None:
        _logo(img, logo, round(x), y, s)
        x += s + 14
    name = _font(34, "ExtraBold")
    d.text((x, y + s * .74), _fit(m.partner_name, name, W - M - x), font=name, fill=WHITE, anchor="ls")

    # what it is
    games = len(m.games)
    eyebrow = f"Partner tournament · {games} game{'' if games == 1 else 's'}"
    _tracked(d, (M, 186), eyebrow.upper(), _font(18, "Bold"), LAVENDER, 3)
    font, lines = _title_lines(m.title, 680)
    y = 186 + 22 + round(font.size * .82)
    for line in lines:
        d.text((M - 3, y), line, font=font, fill=WHITE, anchor="ls")
        y += round(font.size * .94)

    # the prize ticket, tilted a little like the flyer's
    ticket = _ticket(*prize_line(m)).rotate(2.5, resample=Image.BICUBIC, expand=True)
    shadow = Image.new("RGBA", ticket.size, PURPLE + (255,))
    shadow.putalpha(ticket.getchannel("A"))
    tx, ty = M - 6, H - M - ticket.height + 14
    img.alpha_composite(shadow, (tx + 9, ty + 9))
    img.alpha_composite(ticket, (tx, ty))

    # when and where to play, bottom right: the dates in a lime pill, the link under it
    link = f"start.ng/majors/{m.slug}"
    lf = _font(24, "Bold")
    d.text((W - M, H - M + 4), _fit(link, lf, 520), font=lf, fill=WHITE, anchor="rs")
    when = dates(m).upper()
    pill = _font(26, "ExtraBold")
    pw = round(pill.getlength(when) + 2.4 * (len(when) - 1) + 40)
    py = H - M - 24 - 22 - 52
    d.rounded_rectangle((W - M - pw, py, W - M, py + 52), radius=10, fill=LIME)
    _tracked(d, (W - M - pw + 20, py + 36), when, pill, INK, 2.4)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return buf.getvalue()


def draw(m: Major) -> bytes:
    """The picture as a JPEG. A major that hasn't changed is drawn once."""
    key = version(m)
    with _lock:
        if key in _drawn:
            _drawn.move_to_end(key)
            return _drawn[key]
        out = _draw(m)
        _drawn[key] = out
        if len(_drawn) > DRAWN_KEPT:
            _drawn.popitem(last=False)
    return out
