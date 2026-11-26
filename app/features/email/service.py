"""Email and notifications (H1). The contract other features use (PRD C7):

    from app.features.email import service as email

    email.register_kind("record_beaten", topic="records", render=my_render)   # once, at import
    email.notify(user_id, "record_beaten", {"game": "lane-dash", "score": 1200})

notify() never sends straight away: it adds a note to the player's email for the day, and the background sender
(send_due) renders and sends it a few minutes later. One email per player per day (the cap): notes that arrive while
the day's email is still waiting join it; notes after it went out are dropped and logged as "capped". Players only
get topics they opted into, and every email has a one-click unsubscribe. A kind registered with capped=False (the
tournament reminder) goes out at once in its own email, outside the cap: it only matters before a set time.
"""
import html
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import flags
from ...config import settings
from ...db import SessionLocal
from ...models import User
from .models import Email, Subscriber, SubscriberTopic
from .provider import EmailProvider, Message, SendError, get_provider

log = logging.getLogger(__name__)

NAME = "email"

# What players can opt into (PRD H1). Labels are what the settings page shows.
TOPICS: dict[str, str] = {
    "tournament": "The daily tournament: when it starts and ends",
    "reminder": "A reminder 10 minutes before the daily tournament starts",
    "results": "How you did in a tournament",
    "records": "When someone beats your record",
    "new_games": "New games, once a week",
    "follows": "When a creator you follow publishes a game",
    "newsletter": "The start.ng newsletter (at most every 2 weeks)",
}
VISITOR_TOPICS = ("tournament", "new_games", "newsletter")  # what someone who isn't signed in can ask for

CONFIRM_FOR = timedelta(days=7)  # a confirmation link works this long
CONFIRM_EVERY = timedelta(minutes=10)  # at most one confirmation email to an address this often
RETRY_AFTER = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
MAX_ATTEMPTS = len(RETRY_AFTER) + 1


def now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when  # SQLite drops the zone


def today(at: datetime | None = None) -> str:
    """The day in start.ng's time zone (the tournament's), which the 1-a-day cap counts in."""
    return (at or now()).astimezone(timezone(timedelta(hours=settings.tournament_utc_offset))).date().isoformat()


# ---- kinds: what an email note can be. The feature that emits a kind declares it.

@dataclass
class Note:
    subject: str
    html: str  # a fragment: escape anything from players with esc()
    text: str


Render = Callable[[dict], Note]


@dataclass
class Kind:
    topic: str | None  # None: sent whatever the player opted into (confirmations, test emails), outside the cap
    render: Render
    capped: bool = True  # False: an opted-in topic that is sent at once, outside the 1-a-day cap (the reminder)


KINDS: dict[str, Kind] = {}


def register_kind(kind: str, topic: str | None, render: Render, capped: bool = True) -> None:
    if topic is not None and topic not in TOPICS:
        raise KeyError(f'Unknown email topic "{topic}". Topics: {", ".join(TOPICS)}')
    KINDS[kind] = Kind(topic, render, capped)


def esc(value) -> str:
    return html.escape(str(value))


# The start.ng look, taken from the website's palette (src/styles.css). Emails carry their styles inline, so the
# values live here: cream page, cream-white card, dark ink, lime highlights and an 8px lime offset shadow.
BG = "#f6f5ee"
CARD = "#fffefa"
INK = "#292a23"
MUTED = "#6b6c63"
LINE = "#e4e2d6"
LIME = "#d8fd71"
LIME_TINT = "#f1fbd2"
FONT = "'Helvetica Neue',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"


def button(link: str, label: str, arrow: bool = False) -> str:
    """A start.ng button: dark ink, white label, rounded ends (a lime offset in clients that support it)."""
    tail = ' <span style="font-weight:400">&rarr;</span>' if arrow else ""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:26px 0 4px">'
        f'<tr><td align="center" bgcolor="{INK}" style="background:{INK};border-radius:10px;'
        f'box-shadow:4px 4px 0 {LIME}">'
        f'<a href="{esc(link)}" style="display:inline-block;padding:14px 22px;font:700 16px/1 {FONT};'
        f'color:#ffffff;text-decoration:none;border-radius:10px">{esc(label)}{tail}</a>'
        '</td></tr></table>'
    )


def _link(path: str) -> str:
    return settings.site_url.rstrip("/") + path


register_kind("confirm", None, lambda d: Note(
    subject="Confirm your start.ng emails",
    html=f'<p style="margin:0 0 10px;font-size:16px;line-height:1.6">Someone (we hope you) asked for emails from '
         f'start.ng about: {esc(d["topics"])}.</p>'
         f'{button(d["link"], "Yes, send them")}'
         f'<p style="margin:18px 0 0;color:{MUTED};font-size:13px;line-height:1.55">'
         f'If it wasn\'t you, ignore this email and we won\'t write again.</p>',
    text=f'Someone (we hope you) asked for emails from start.ng about: {d["topics"]}.\n\n'
         f'Yes, send them: {d["link"]}\n\nIf it wasn\'t you, ignore this email and we won\'t write again.',
))
register_kind("test", None, lambda d: Note(
    subject="start.ng test email",
    html=f'<p>This is a test email from start.ng, sent by {esc(d["by"])}. If you can read it, sending works.</p>',
    text=f'This is a test email from start.ng, sent by {d["by"]}. If you can read it, sending works.',
))


# ---- subscribers

def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _subscriber(db: Session, email: str) -> Subscriber | None:
    return db.scalar(select(Subscriber).where(Subscriber.email == email.strip().lower()))


def subscriber_for(db: Session, user: User, create: bool = False) -> Subscriber | None:
    """The player's subscriber row. A visitor row with the same address becomes theirs."""
    sub = db.scalar(select(Subscriber).where(Subscriber.user_id == user.id)) or _subscriber(db, user.email)
    if sub is None and create:
        sub = Subscriber(email=user.email.lower(), unsubscribe_token=_new_token())
        db.add(sub)
    if sub is not None and sub.user_id is None:
        sub.user_id = user.id
    return sub


def set_topics(db: Session, sub: Subscriber, topics: list[str]) -> None:
    wanted = set(topics)
    unknown = wanted - set(TOPICS)
    if unknown:
        raise KeyError(f'Unknown email topic "{sorted(unknown)[0]}"')
    sub.topics = [t for t in sub.topics if t.topic in wanted] + [
        SubscriberTopic(topic=t) for t in sorted(wanted - set(sub.topic_names))]


def set_my_topics(db: Session, user: User, topics: list[str]) -> Subscriber:
    """A signed-in player's choices. Google already checked their address, so no confirmation email."""
    sub = subscriber_for(db, user, create=True)
    if sub.confirmed_at is None:
        sub.confirmed_at = now()
    set_topics(db, sub, topics)
    db.commit()
    return sub


def ask_to_subscribe(db: Session, email: str, topics: list[str]) -> None:
    """A visitor left their address: send a link that adds the topics when they click it (double opt-in). Says
    nothing about whether the address is known, and sends at most one link every CONFIRM_EVERY."""
    bad = set(topics) - set(VISITOR_TOPICS)
    if bad:
        raise KeyError(f'Sign in to get "{sorted(bad)[0]}" emails')
    sub = _subscriber(db, email)
    if sub is None:
        sub = Subscriber(email=email.strip().lower(), unsubscribe_token=_new_token())
        db.add(sub)
        db.flush()
    if sub.bad_at is not None:
        return
    if sub.confirm_sent_at is not None and now() - _utc(sub.confirm_sent_at) < CONFIRM_EVERY:
        sub.pending_topics = sorted(set(sub.pending_topics or []) | set(topics))
        db.commit()
        return
    sub.pending_topics = sorted(set(sub.pending_topics or []) | set(topics))
    sub.confirm_token = _new_token()
    sub.confirm_sent_at = now()
    labels = ", ".join(TOPICS[t].split(":")[0].lower() for t in sub.pending_topics)
    _queue(db, sub, "confirm", {"topics": labels, "link": _link(f"/emails?confirm={sub.confirm_token}")})
    db.commit()


def confirm(db: Session, token: str) -> Subscriber | None:
    """The visitor clicked the link: they're in. None when the link is unknown or too old."""
    sub = db.scalar(select(Subscriber).where(Subscriber.confirm_token == token))
    if sub is None or sub.confirm_sent_at is None or now() - _utc(sub.confirm_sent_at) > CONFIRM_FOR:
        return None
    sub.confirmed_at = sub.confirmed_at or now()
    set_topics(db, sub, sub.topic_names + list(sub.pending_topics or []))
    sub.pending_topics = None
    sub.confirm_token = None
    db.commit()
    return sub


def unsubscribe(db: Session, token: str, topic: str | None = None) -> Subscriber | None:
    """One click from an email: stop one topic, or everything. None when the link is unknown."""
    sub = db.scalar(select(Subscriber).where(Subscriber.unsubscribe_token == token))
    if sub is None:
        return None
    set_topics(db, sub, [t for t in sub.topic_names if topic is not None and t != topic])
    sub.pending_topics = None
    db.commit()
    return sub


def mark_bad(db: Session, email: str, reason: str) -> bool:
    """A hard bounce, spam complaint or blocked address: never write to it again. Its waiting emails are skipped."""
    sub = _subscriber(db, email)
    if sub is None:
        return False
    if sub.bad_at is None:
        sub.bad_at, sub.bad_reason = now(), reason[:40]
    db.commit()
    return True


# ---- notify (C7): the one function other features call

def _queue(db: Session, sub: Subscriber, kind: str, data: dict) -> Email:
    """Add a note to the subscriber's email: today's waiting one (the cap), or a new one."""
    capped = KINDS[kind].topic is not None and KINDS[kind].capped
    day = today()
    note = {"kind": kind, "data": data}
    if capped:
        waiting = db.scalar(select(Email).where(Email.subscriber_id == sub.id, Email.day == day, Email.capped.is_(True),
                                                Email.status == "queued"))
        if waiting is not None:
            waiting.notes = [*waiting.notes, note]  # a new list, so the JSON column sees the change
            return waiting
        if db.scalar(select(Email.id).where(Email.subscriber_id == sub.id, Email.day == day, Email.capped.is_(True),
                                            Email.status == "sent")):
            row = Email(subscriber_id=sub.id, to_email=sub.email, day=day, capped=True, notes=[note], status="capped")
            db.add(row)
            return row
    row = Email(subscriber_id=sub.id, to_email=sub.email, day=day, capped=capped, notes=[note],
                send_after=now() + (timedelta(minutes=settings.email_merge_minutes) if capped else timedelta()))
    db.add(row)
    db.flush()
    return row


def _wants(sub: Subscriber | None, kind: str) -> bool:
    if sub is None or sub.confirmed_at is None or sub.bad_at is not None:
        return False
    topic = KINDS[kind].topic
    return topic is None or topic in sub.topic_names


def notify(user_id: int, kind: str, data: dict, db: Session | None = None) -> bool:
    """Email a player about something, if they opted into its topic. Returns whether it was queued.

    With db, the note is added to that session and the caller commits; without, notify uses and commits its own.
    Does nothing while the email feature is off."""
    if kind not in KINDS:
        raise KeyError(f'Unknown email kind "{kind}". Declare it with register_kind first.')
    if not flags.is_on(NAME):
        return False
    own = db is None
    db = db or SessionLocal()
    try:
        sub = db.scalar(select(Subscriber).where(Subscriber.user_id == user_id))
        if not _wants(sub, kind):
            return False
        _queue(db, sub, kind, data)
        if own:
            db.commit()
        return True
    finally:
        if own:
            db.close()


def broadcast(topic: str, kind: str, data: dict, db: Session | None = None) -> int:
    """Email everyone who opted into a topic, signed in or not (the daily tournament, the newsletter). Each still
    gets at most one email a day. Returns how many were queued."""
    if KINDS[kind].topic != topic:
        raise KeyError(f'Kind "{kind}" isn\'t a "{topic}" email')
    if not flags.is_on(NAME):
        return 0
    own = db is None
    db = db or SessionLocal()
    try:
        subs = db.scalars(select(Subscriber).join(SubscriberTopic).where(
            SubscriberTopic.topic == topic, Subscriber.confirmed_at.is_not(None), Subscriber.bad_at.is_(None))).all()
        for sub in subs:
            _queue(db, sub, kind, data)
        if own:
            db.commit()
        return len(subs)
    finally:
        if own:
            db.close()


def send_test(db: Session, to: str, by: str) -> Email:
    """An admin's test email to themselves: checks the provider works. Outside the cap and the topics."""
    sub = _subscriber(db, to)
    if sub is None:
        sub = Subscriber(email=to.lower(), unsubscribe_token=_new_token(), confirmed_at=now())
        db.add(sub)
        db.flush()
    row = _queue(db, sub, "test", {"by": by})
    db.commit()
    return row


# ---- account emails: about the person's own games (a game hidden, a review's result), not a topic they chose

def _account_kind(kind: str) -> None:
    if kind not in KINDS:
        raise KeyError(f'Unknown email kind "{kind}". Declare it with register_kind first.')
    if KINDS[kind].topic is not None:
        raise KeyError(f'Kind "{kind}" has a topic; send it with notify() or broadcast()')


def _confirmed_sub(db: Session, address: str, user: User | None = None) -> Subscriber:
    sub = (subscriber_for(db, user) if user is not None else None) or _subscriber(db, address)
    if sub is None:
        sub = Subscriber(email=address.strip().lower(), unsubscribe_token=_new_token(), confirmed_at=now())
        db.add(sub)
    if user is not None and sub.user_id is None:
        sub.user_id = user.id
    if sub.confirmed_at is None:
        sub.confirmed_at = now()
    db.flush()
    return sub


def send_to_user(db: Session, user: User, kind: str, data: dict) -> bool:
    """Email a player about their own game, whatever topics they chose (a topic-less kind, outside the daily cap).
    Only to an address Google has checked, never to a bounced one. The caller commits. Does nothing while the email
    feature is off. Returns whether it was queued."""
    _account_kind(kind)
    if not flags.is_on(NAME) or not user.google_sub or user.blocked_at is not None:
        return False
    sub = _confirmed_sub(db, user.email, user)
    if sub.bad_at is not None:
        return False
    _queue(db, sub, kind, data)
    return True


def send_to_admins(db: Session, kind: str, data: dict) -> int:
    """Email every super admin (SUPER_ADMIN_EMAILS). The caller commits. Returns how many were queued."""
    from ...admin import super_admins

    _account_kind(kind)
    if not flags.is_on(NAME):
        return 0
    sent = 0
    for address in sorted(super_admins()):
        sub = _confirmed_sub(db, address)
        if sub.bad_at is None:
            _queue(db, sub, kind, data)
            sent += 1
    return sent


# ---- the sender

def _page(body_html: str, body_text: str, sub: Subscriber, topics: list[str]) -> tuple[str, str]:
    """Wrap the notes in start.ng's email layout, with the unsubscribe links."""
    manage = _link("/emails")
    stop = _link(f"/emails?unsubscribe={sub.unsubscribe_token}")
    footer_link = f"color:{MUTED};text-decoration:underline;text-underline-offset:2px"
    stop_one = (f' &middot; <a href="{esc(stop)}&amp;topic={esc(topics[0])}" style="{footer_link}">Stop these emails</a>'
                if len(topics) == 1 else "")
    html_out = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="color-scheme" content="light">'
        '<meta name="supported-color-schemes" content="light">'
        '</head><body style="margin:0;padding:0;width:100%;background:' + BG + ';">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{BG}" '
        f'style="width:100%;background:{BG};border-collapse:collapse">'
        '<tr><td align="center" style="padding:36px 12px 30px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:560px;border-collapse:separate">'
        '<tr><td style="padding:0 4px 15px">'
        f'<span style="font:800 21px/1.2 {FONT};letter-spacing:-.04em;color:{INK}">start'
        f'<span style="color:{MUTED}">.ng</span></span>'
        '</td></tr>'
        f'<tr><td bgcolor="{CARD}" style="background:{CARD};border:1px solid {LINE};border-radius:24px;'
        f'padding:34px 32px;box-shadow:8px 8px 0 {LIME}">'
        f'{body_html}'
        '</td></tr>'
        '<tr><td style="padding:26px 12px 0;text-align:center">'
        f'<p style="margin:0;font:400 12px/1.7 {FONT};color:{MUTED}">'
        f'<a href="{esc(manage)}" style="{footer_link}">Choose your emails</a>{stop_one}'
        f' &middot; <a href="{esc(stop)}" style="{footer_link}">Unsubscribe from everything</a>'
        '</p>'
        f'<p style="margin:10px 0 0;font:700 10px/1.4 {MONO};letter-spacing:.16em;text-transform:uppercase;'
        f'color:{MUTED}">start.ng</p>'
        '</td></tr></table></td></tr></table></body></html>'
    )
    text_out = f"{body_text}\n\n--\nChoose your emails: {manage}\nUnsubscribe from everything: {stop}\n"
    return html_out, text_out


def render(row: Email, sub: Subscriber) -> tuple[Message, list[str]]:
    """The email for a row, and the topics it covers. Notes for topics the player has since left are dropped."""
    notes, topics = [], []
    for n in row.notes:
        kind = KINDS.get(n["kind"])
        if kind is None or (kind.topic is not None and kind.topic not in sub.topic_names):
            continue
        notes.append(kind.render(n["data"]))
        if kind.topic and kind.topic not in topics:
            topics.append(kind.topic)
    if not notes:
        return None, []  # type: ignore[return-value]
    subject = notes[0].subject if len(notes) == 1 else f"{notes[0].subject} (+{len(notes) - 1} more)"
    sep_html = f'<hr style="border:0;border-top:1px solid {LINE};margin:20px 0">'
    html_out, text_out = _page(sep_html.join(n.html for n in notes), "\n\n---\n\n".join(n.text for n in notes), sub, topics)
    one_click = f"{settings.api_url.rstrip('/')}/email/unsubscribe?token={sub.unsubscribe_token}"
    headers = {"List-Unsubscribe": f"<{one_click}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return Message(to=sub.email, subject=subject, html=html_out, text=text_out, headers=headers,
                   tags=[n["kind"] for n in row.notes][:5]), topics


def _only_to() -> set[str]:
    return {a.strip().lower() for a in settings.email_only_to.split(",") if a.strip()}


def send_due(db: Session, provider: EmailProvider | None = None, at: datetime | None = None, limit: int = 50) -> int:
    """Send the emails whose time has come. Returns how many went out."""
    at = at or now()
    rows = db.scalars(select(Email).where(Email.status == "queued", Email.send_after <= at)
                      .order_by(Email.id).limit(limit)).all()
    if not rows:
        return 0
    try:
        provider = provider or get_provider()
    except SendError as e:
        log.error("Email isn't set up, so nothing was sent: %s", e)
        return 0
    only_to, sent = _only_to(), 0
    for row in rows:
        # Claim the row until its commit below. Another server sending at the same time skips it instead of sending
        # it again (Postgres; SQLite has no row locks and runs one sender anyway).
        row = db.scalar(select(Email).where(Email.id == row.id, Email.status == "queued")
                        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if row is None:
            continue
        sub = row.subscriber
        message, _ = render(row, sub)
        if sub.bad_at is not None:
            row.status, row.last_error = "skipped", f"bad address ({sub.bad_reason})"
        elif message is None:
            row.status, row.last_error = "skipped", "unsubscribed"
        elif only_to and sub.email not in only_to:
            row.status, row.last_error = "skipped", "not in EMAIL_ONLY_TO"
        else:
            row.subject = message.subject
            row.attempts += 1
            try:
                row.provider_id = provider.send(message)
                row.status, row.sent_at, row.last_error = "sent", now(), None
                sent += 1
            except SendError as e:
                row.last_error = str(e)
                if not e.retry or row.attempts >= MAX_ATTEMPTS:
                    row.status = "failed"
                else:
                    row.send_after = at + RETRY_AFTER[row.attempts - 1]
                log.warning("Email %s to %s didn't send: %s", row.id, sub.email, e)
        db.commit()
    return sent


TICK_SECONDS = 30


async def run_forever() -> None:
    """The background sender, started with the server while the email feature is on (see main.py)."""
    import asyncio

    from starlette.concurrency import run_in_threadpool

    from . import tournament_mail

    def tick() -> int:
        if not flags.is_on(NAME):
            return 0
        with SessionLocal() as db:
            try:
                tournament_mail.tick(db)  # the day's tournament email, once, when it starts
            except Exception:
                db.rollback()
                log.exception("Tournament emails failed")
            try:
                tournament_mail.remind(db)  # the reminder, once, 10 minutes before the next one starts
            except Exception:
                db.rollback()
                log.exception("Tournament reminders failed")
            return send_due(db)

    while True:
        try:
            await run_in_threadpool(tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Sending emails failed")
        await asyncio.sleep(TICK_SECONDS)
