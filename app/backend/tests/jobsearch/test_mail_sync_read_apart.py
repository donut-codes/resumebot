"""The mail read's first reads (the saved login, the handled message keys, the roles in play for the header filter) take
no writer turn: they are made in a reader's session, and the keys the mailbox read is given are what they always were.
Made-up data."""

import asyncio
from datetime import date

from sqlalchemy import update

from app import db
from app.jobsearch import stages
from app.jobsearch.mail import login as mail_login
from app.jobsearch.mail import match as mail_match
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.mail import sync as mail_sync
from app.jobsearch.models import MailMessage, Opportunity
from tests.employer_builders import make_employer, make_role
from tests.support import OWNER


async def _second_writer_gets_through() -> bool:
    async with db.session_factory()() as other:
        try:
            await asyncio.wait_for(other.execute(update(Opportunity).where(Opportunity.slug == "no-such").values(
                slug="no-such")), 1)
            return True
        except TimeoutError:
            return False
        finally:
            await other.rollback()


async def test_the_login_handled_keys_and_candidates_are_read_without_the_writers_turn(js, monkeypatch):
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Globex Corporation", website="https://www.globexcorp.example")
        session.add(make_role(employer, "Analyst", stages.APPLIED))
        session.add(MailMessage(owner=OWNER, message_key="seen-1", sender="Pat", sender_key="pat@x.example",
                                subject="Hello", day=date.today(), state="classed"))
        await session.commit()

    through: list[tuple[str, bool, bool]] = []
    real_candidates = mail_match.candidates_of

    async def find(session, owner):
        through.append(("login", db.is_reader(session), await _second_writer_gets_through()))
        return go_mail.Login("imap.example.test", 993, "me@mail.example", "pw")

    async def candidates_of(session, owner):
        through.append(("candidates", db.is_reader(session), await _second_writer_gets_through()))
        return await real_candidates(session, owner)

    got: list[tuple] = []

    def read_new(who, since, handled, keys):
        got.append((since, set(handled), list(keys)))
        return []

    monkeypatch.setattr(mail_login, "find", find)
    monkeypatch.setattr(mail_match, "candidates_of", candidates_of)
    monkeypatch.setattr(mail_sync, "read_new", read_new)

    async with db.session_factory()() as session:
        out = await mail_sync.action_read(session, None, {"owner": OWNER, "since": date.today().isoformat()})
        await session.commit()

    assert out == {"new": 0}
    assert through == [("login", True, True), ("candidates", True, True)]
    assert len(got) == 1
    _since, handled, keys = got[0]
    assert handled == {"seen-1"}
    assert "globex" in " ".join(keys).lower()


async def test_inside_a_transaction_that_holds_a_write_the_reads_stay_in_that_session(js, monkeypatch):
    seen: list[bool] = []

    async def find(session, owner):
        seen.append(db.is_reader(session))
        return go_mail.Login("imap.example.test", 993, "me@mail.example", "pw")

    monkeypatch.setattr(mail_login, "find", find)
    monkeypatch.setattr(mail_sync, "read_new", lambda *a: [])

    async with db.session_factory()() as session:
        await session.execute(update(Opportunity).where(Opportunity.slug == "no-such").values(slug="no-such"))
        await mail_sync.action_read(session, None, {"owner": OWNER, "since": date.today().isoformat()})
        await session.commit()

    assert seen == [False]
