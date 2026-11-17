"""The owner's mail login (`mail/login.py`, `mail/seal.py`, `routes/mail.py`): sealed at rest, write-only over the API,
tested with a fake mailbox, and used by Go's checks. Made-up addresses and passwords only."""

import json
import logging

import pytest
from sqlalchemy import select, text

from app import db
from app.jobsearch.mail import login as mail_login
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.mail import seal as mail_seal
from app.jobsearch.mail import sync as mail_sync
from app.jobsearch.models import MailLogin
from app.mailmine import imap, providers
from tests.jobsearch.test_go_checks import _add, _prepare, pages, role  # noqa: F401  (fixtures and helpers)
from tests.support import OWNER

PASSWORD = "abcd efgh ijkl mnop"
SQUASHED = "abcdefghijklmnop"


@pytest.fixture(autouse=True)
def key_file(tmp_path, monkeypatch):
    from app.settings import settings

    path = tmp_path / "secrets" / "mail-key"
    monkeypatch.setattr(settings, "mail_key_file", str(path))
    return path


class FakeConn:
    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']


class FakeBox:
    """A mailbox that accepts one login and lists three folders (read-only)."""

    def __init__(self, accepts=True):
        self.accepts, self.opened = accepts, []
        self.conn = FakeConn()

    def __call__(self, login):
        self.opened.append(login)
        if not self.accepts:
            raise imap.MailReadError("The inbox didn't accept that login.")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


# --- sealing ----------------------------------------------------


def test_a_sealed_value_opens_only_for_its_owner_and_holds_no_plain_text(key_file):
    sealed = mail_seal.seal("hunter2-app-pass", owner="o1")
    assert sealed.startswith("v1.") and "hunter2" not in sealed
    assert mail_seal.unseal(sealed, owner="o1") == "hunter2-app-pass"
    assert key_file.exists()
    with pytest.raises(mail_seal.SealUnavailable):
        mail_seal.unseal(sealed, owner="o2")
    assert mail_seal.seal("hunter2-app-pass", owner="o1") != sealed  # a fresh nonce each time


def test_a_lost_or_changed_key_is_said_in_words_and_never_remade_on_a_read(key_file):
    sealed = mail_seal.seal("pw", owner="o1")
    key_file.write_text("x" * 40 + "\n", encoding="utf-8")
    with pytest.raises(mail_seal.SealUnavailable, match="can't open"):
        mail_seal.unseal(sealed, owner="o1")
    key_file.unlink()
    with pytest.raises(mail_seal.SealUnavailable, match="key is missing"):
        mail_seal.unseal(sealed, owner="o1")
    assert not key_file.exists()


def test_the_provider_is_found_by_the_address_and_gmail_spaces_are_dropped():
    assert providers.for_address("me@gmail.com").name == "Gmail"
    assert providers.for_address("ME@GMAIL.COM").host == "imap.gmail.com"
    assert providers.for_address("me@outlook.com").name == "Outlook"
    assert providers.for_address("me@own-domain.example") is None
    assert {"Gmail", "Outlook", "Yahoo Mail", "iCloud Mail", "Fastmail", "Another provider"} <= {
        p.name for p in providers.load()}


# --- the API: write-only ----------------------------------------------------


async def test_a_saved_login_is_sealed_in_the_database_and_never_returned_or_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    out = await client.put("/api/v1/mail-login", json={"username": "me@gmail.com", "password": PASSWORD})
    assert out.status_code == 200
    body = out.json()
    assert (body["connected"], body["username"], body["host"], body["port"], body["provider"]) == (
        True, "me@gmail.com", "imap.gmail.com", 993, "Gmail")
    shown = [out.text, (await client.get("/api/v1/mail-login")).text, caplog.text]
    async with db.session_factory()() as session:
        row = (await session.scalars(select(MailLogin))).one()
        stored = json.dumps([row.password_sealed, row.username, row.host])
        raw = (await session.execute(text("select * from js_mail_logins"))).all()
        who = await mail_login.saved(session, OWNER)
    for thing in [*shown, stored, str(raw)]:
        assert SQUASHED not in thing and "abcd" not in thing
    assert who is not None and who.password == SQUASHED  # spaces dropped, opened only for the read
    assert SQUASHED not in repr(who)


async def test_saving_again_replaces_it_and_remove_forgets_it(client):
    await client.put("/api/v1/mail-login", json={"username": "me@gmail.com", "password": PASSWORD})
    await client.put("/api/v1/mail-login", json={"username": "other@yahoo.com", "password": "zzzz yyyy xxxx wwww"})
    body = (await client.get("/api/v1/mail-login")).json()
    assert (body["username"], body["host"], body["provider"]) == (
        "other@yahoo.com", "imap.mail.yahoo.com", "Yahoo Mail")
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(MailLogin))).all()) == 1
    gone = (await client.delete("/api/v1/mail-login")).json()
    assert gone["connected"] is False and gone["username"] is None
    assert (await client.post("/api/v1/mail-login/test")).status_code == 404


async def test_another_provider_needs_its_server_and_a_bad_address_is_refused_in_words(client):
    bad = await client.put("/api/v1/mail-login", json={"username": "not-an-address", "password": "pw"})
    assert bad.status_code == 422 and "whole mail address" in bad.json()["detail"]
    no_host = await client.put("/api/v1/mail-login", json={"username": "me@own-domain.example", "password": "pw"})
    assert no_host.status_code == 422 and "mail server" in no_host.json()["detail"]
    empty = await client.put("/api/v1/mail-login", json={"username": "me@gmail.com", "password": "  "})
    assert empty.status_code == 422
    ok = await client.put("/api/v1/mail-login", json={
        "username": "me@own-domain.example", "password": "pw", "host": "mail.own-domain.example", "port": 1993})
    assert ok.status_code == 200 and (ok.json()["host"], ok.json()["port"]) == ("mail.own-domain.example", 1993)


async def test_the_screen_gets_the_providers_help(client):
    help_ = (await client.get("/api/v1/mail-login")).json()["providers"]
    gmail = next(p for p in help_ if p["name"] == "Gmail")
    assert gmail["steps"] and gmail["match"] == ["gmail.com", "googlemail.com"]
    assert next(p for p in help_ if p["name"] == "Another provider")["match"] == []


# --- test connection ----------------------------------------------------


async def test_test_connection_logs_in_read_only_and_lists_the_folders(client, monkeypatch):
    box = FakeBox()
    monkeypatch.setattr(go_mail, "open_inbox", box)
    await client.put("/api/v1/mail-login", json={"username": "me@gmail.com", "password": PASSWORD})
    out = (await client.post("/api/v1/mail-login/test")).json()
    assert out["ok"] is True and out["folders"] == ["INBOX", "[Gmail]/All Mail"]
    assert "me@gmail.com" in out["message"] and SQUASHED not in json.dumps(out)
    assert [(w.host, w.port, w.username, w.password) for w in box.opened] == [
        ("imap.gmail.com", 993, "me@gmail.com", SQUASHED)]


async def test_a_refused_login_is_an_answer_in_words(client, monkeypatch):
    monkeypatch.setattr(go_mail, "open_inbox", FakeBox(accepts=False))
    await client.put("/api/v1/mail-login", json={"username": "me@gmail.com", "password": PASSWORD})
    out = await client.post("/api/v1/mail-login/test")
    assert out.status_code == 200
    assert out.json() == {"ok": False, "message": "The inbox didn't accept that login.", "folders": []}


# --- Go uses it ----------------------------------------------------


async def test_go_uses_the_saved_login_and_finds_none_when_none_is_saved(client, js):
    async with db.session_factory()() as session:
        assert await mail_login.find(session, OWNER) is None
    await client.put("/api/v1/mail-login", json={"username": "me@gmail.com", "password": PASSWORD})
    async with db.session_factory()() as session:
        who = await mail_login.find(session, OWNER)
    assert (who.host, who.username, who.password) == ("imap.gmail.com", "me@gmail.com", SQUASHED)
    await client.delete("/api/v1/mail-login")
    async with db.session_factory()() as session:
        assert await mail_login.find(session, OWNER) is None


async def test_go_reads_the_mail_with_the_saved_login(client, monkeypatch, pages):  # noqa: F811
    box = FakeBox()
    monkeypatch.setattr(go_mail, "open_inbox", box)
    box.read_headers = lambda *a, **k: 0
    await client.put("/api/v1/mail-login", json={"username": "me@gmail.com", "password": PASSWORD})
    await _add(role("a"))
    out = await _prepare()
    assert [r.slug for r in out.ready] == ["a"]
    assert mail_sync.NO_MAILBOX not in out.notes
    assert [(w.host, w.username, w.password) for w in box.opened] == [("imap.gmail.com", "me@gmail.com", SQUASHED)]
