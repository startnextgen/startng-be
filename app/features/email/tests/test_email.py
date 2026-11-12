from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.features.email import service
from app.features.email.models import Email, Subscriber
from app.features.email.provider import LogProvider, SendError
from app.main import app
from app.models import User
from app.security import create_token

service.register_kind("test_result", "results", lambda d: service.Note(
    subject=f"You placed {d['place']}", html=f"<p>You placed {service.esc(d['place'])}</p>", text=f"You placed {d['place']}"))
service.register_kind("test_digest", "new_games", lambda d: service.Note(subject="New games", html="<p>New</p>", text="New"))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "super_admin_emails", "boss@example.com")
    monkeypatch.setattr(settings, "features", "email")
    monkeypatch.setattr(settings, "email_merge_minutes", 0)
    monkeypatch.setattr(settings, "brevo_webhook_key", "hook-key")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(service, "SessionLocal", Session)  # notify() and broadcast() open their own session
    with Session() as db:
        users = {k: User(email=f"{k}@example.com", name=k.title(), google_sub=f"g-{k}") for k in ("boss", "ada")}
        db.add_all(users.values())
        db.commit()
        ids = {k: u.id for k, u in users.items()}
        tokens = {k: {"Authorization": f"Bearer {create_token(u.id)}"} for k, u in users.items()}
    yield TestClient(app), tokens, ids, Session
    app.dependency_overrides.clear()


def send(Session, provider=None, later=timedelta()):
    provider = provider or LogProvider()
    with Session() as db:
        service.send_due(db, provider, at=service.now() + later)
    return provider


def statuses(Session):
    with Session() as db:
        return [r.status for r in db.scalars(select(Email).order_by(Email.id)).all()]


def test_players_choose_their_topics(env):
    client, tokens, ids, Session = env
    me = client.get("/email/me", headers=tokens["ada"]).json()
    assert me["email"] == "ada@example.com" and not any(t["on"] for t in me["topics"])
    assert service.notify(ids["ada"], "test_result", {"place": 3}) is False  # not opted in
    me = client.put("/email/me", json={"topics": ["results", "records"]}, headers=tokens["ada"]).json()
    assert sorted(t["key"] for t in me["topics"] if t["on"]) == ["records", "results"]
    assert client.put("/email/me", json={"topics": ["spam"]}, headers=tokens["ada"]).status_code == 422
    assert service.notify(ids["ada"], "test_result", {"place": 3}) is True
    sent = send(Session).sent
    assert [m.subject for m in sent] == ["You placed 3"]
    assert "List-Unsubscribe" in sent[0].headers and sent[0].headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_the_email_shell_keeps_the_look_and_unsubscribe_links(env):
    client, tokens, ids, Session = env
    client.put("/email/me", json={"topics": ["results"]}, headers=tokens["ada"])
    service.notify(ids["ada"], "test_result", {"place": 3})
    sent = send(Session).sent[0]
    assert sent.html.startswith("<!doctype html>")
    assert f"background:{service.CARD}" in sent.html  # the start.ng card colour
    assert "You placed 3" in sent.html
    assert "Choose your emails" in sent.html and "Unsubscribe from everything" in sent.html
    assert "Stop these emails" in sent.html  # one topic, so the footer can stop just that
    assert "You placed 3" in sent.text and "Choose your emails" in sent.text
    assert "<p>" not in sent.text  # the plain-text alternative stays plain


def test_visitors_double_opt_in(env):
    client, tokens, ids, Session = env
    assert client.post("/email/subscribe", json={"email": "Fan@Example.com"}).status_code == 202
    assert client.post("/email/subscribe", json={"email": "fan@example.com", "topics": ["results"]}).status_code == 422
    assert service.broadcast("new_games", "test_digest", {}) == 0  # not confirmed: nothing
    mail = send(Session).sent
    assert [m.to for m in mail] == ["fan@example.com"] and mail[0].subject == "Confirm your start.ng emails"
    # asking again straight away doesn't send another link, but remembers the extra topic
    client.post("/email/subscribe", json={"email": "fan@example.com", "topics": ["new_games"]})
    assert send(Session).sent == []
    with Session() as db:
        token = db.scalar(select(Subscriber)).confirm_token
    assert token in mail[0].text
    done = client.post("/email/confirm", json={"token": token}).json()
    assert done == {"email": "f**@example.com", "topics": ["new_games", "tournament"]}
    assert client.post("/email/confirm", json={"token": token}).status_code == 404  # used
    assert service.broadcast("new_games", "test_digest", {}) == 1
    assert [m.subject for m in send(Session).sent] == ["New games"]


def test_one_email_a_day(env, monkeypatch):
    client, tokens, ids, Session = env
    monkeypatch.setattr(settings, "email_merge_minutes", 10)
    client.put("/email/me", json={"topics": ["results", "new_games"]}, headers=tokens["ada"])
    service.notify(ids["ada"], "test_result", {"place": 20})
    service.broadcast("new_games", "test_digest", {})
    assert send(Session).sent == []  # waiting, so later notes can join
    sent = send(Session, later=timedelta(minutes=11)).sent
    assert [m.subject for m in sent] == ["You placed 20 (+1 more)"]
    assert "New" in sent[0].html and "You placed 20" in sent[0].text
    service.notify(ids["ada"], "test_result", {"place": 2})  # the day's email went out already
    assert send(Session, later=timedelta(minutes=30)).sent == []
    assert statuses(Session) == ["sent", "capped"]


def test_one_click_unsubscribe(env):
    client, tokens, ids, Session = env
    client.put("/email/me", json={"topics": ["results", "records"]}, headers=tokens["ada"])
    with Session() as db:
        token = db.scalar(select(Subscriber)).unsubscribe_token
    # one topic, from the page an email links to
    assert client.post("/email/unsubscribe", json={"token": token, "topic": "records"}).json()["topics"] == ["results"]
    service.notify(ids["ada"], "test_result", {"place": 1})
    # everything, straight from the mail app (RFC 8058): a form POST to the List-Unsubscribe address
    r = client.post(f"/email/unsubscribe?token={token}", data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and r.json()["topics"] == []
    assert send(Session).sent == []  # the waiting email is dropped
    assert statuses(Session) == ["skipped"]
    assert client.post("/email/unsubscribe", json={"token": "nope"}).status_code == 404


def test_bounces_stop_email(env):
    client, tokens, ids, Session = env
    client.put("/email/me", json={"topics": ["results"]}, headers=tokens["ada"])
    service.notify(ids["ada"], "test_result", {"place": 1})
    event = {"event": "hard_bounce", "email": "ada@example.com", "message-id": "<x>"}
    assert client.post("/email/webhooks/brevo?key=wrong", json=event).status_code == 404
    assert client.post("/email/webhooks/brevo?key=hook-key", json=event).status_code == 204
    assert send(Session).sent == []
    assert statuses(Session) == ["skipped"]
    assert service.notify(ids["ada"], "test_result", {"place": 1}) is False
    assert client.get("/email/me", headers=tokens["ada"]).json()["bad"] is True


def test_only_to_while_testing(env, monkeypatch):
    client, tokens, ids, Session = env
    monkeypatch.setattr(settings, "email_only_to", "boss@example.com")
    for who in ("ada", "boss"):
        client.put("/email/me", json={"topics": ["results"]}, headers=tokens[who])
        service.notify(ids[who], "test_result", {"place": 1})
    assert [m.to for m in send(Session).sent] == ["boss@example.com"]
    assert statuses(Session) == ["skipped", "sent"]


class Flaky:
    def __init__(self, retry):
        self.retry = retry

    def send(self, message):
        raise SendError("Brevo said 503", retry=self.retry)


def test_retries_then_gives_up(env):
    client, tokens, ids, Session = env
    client.put("/email/me", json={"topics": ["results"]}, headers=tokens["ada"])
    service.notify(ids["ada"], "test_result", {"place": 1})
    later = timedelta()
    for _ in range(service.MAX_ATTEMPTS):
        send(Session, Flaky(retry=True), later)
        later += timedelta(days=1)
    with Session() as db:
        row = db.scalar(select(Email))
        assert (row.status, row.attempts, row.last_error) == ("failed", service.MAX_ATTEMPTS, "Brevo said 503")
    service.notify(ids["boss"], "test_result", {"place": 1})  # boss isn't opted in: nothing queued
    client.put("/email/me", json={"topics": ["results"]}, headers=tokens["boss"])
    service.notify(ids["boss"], "test_result", {"place": 1})
    send(Session, Flaky(retry=False))
    assert statuses(Session) == ["failed", "failed"]


def test_admin_outbox_and_flag(env, monkeypatch):
    client, tokens, ids, Session = env
    assert client.get("/admin/email", headers=tokens["ada"]).status_code == 404  # not an admin
    r = client.post("/admin/email/test", headers=tokens["boss"]).json()
    assert (r["to"], r["status"], r["subject"]) == ("boss@example.com", "sent", "start.ng test email")
    box = client.get("/admin/email", headers=tokens["boss"]).json()
    assert box["provider"] == "log" and box["counts"] == {"sent": 1} and box["rows"][0]["kinds"] == ["test"]
    assert box["subscribers"] == 0  # the test email's address has no topics
    monkeypatch.setattr(settings, "features", "")
    assert client.get("/email/me", headers=tokens["ada"]).status_code == 404
    client.put("/email/me", json={"topics": ["results"]}, headers=tokens["ada"])
    assert service.notify(ids["ada"], "test_result", {"place": 1}) is False


def test_signed_in_players_can_ask_for_the_tournament_reminder(env):
    client, tokens, ids, Session = env
    me = client.put("/email/me", json={"topics": ["reminder"]}, headers=tokens["ada"]).json()
    assert [t["key"] for t in me["topics"] if t["on"]] == ["reminder"]
    me = client.put("/email/me", json={"topics": []}, headers=tokens["ada"]).json()  # and turn it off again
    assert not any(t["on"] for t in me["topics"])
    # visitors can't: a reminder is for players with an account
    assert client.post("/email/subscribe", json={"email": "fan@example.com", "topics": ["reminder"]}).status_code == 422
