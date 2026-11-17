"""The classifier reads the owner's roles narrowly: which roles it learns from is asked of
four columns of every role, and the title, description and first posting only of the ones it learns from; the model is
made with the loop given back between examples. What it learns is what it learned from every role read whole. Synthetic
roles only: every title and word here is made up."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import classifier
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.postings import coop
from tests.support import EXAMPLE_SYSTEM, OWNER

NOW = datetime(2026, 11, 5, 5, 30, tzinfo=UTC)
BODY = "You will lead the work with partners across the business and report to the leadership team."


@pytest.fixture(autouse=True)
def fresh_cache():
    classifier.clear_cache()
    yield
    classifier.clear_cache()


def _role(n, title, **more):
    return Opportunity(owner=OWNER, slug=f"co-{n}", company=f"Co {n}", title=title, match_key=f"co {n}|{title.lower()}",
                       origin="scout", **more)


def _posting(employer, ext_id, title, minutes=0, **more):
    return JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=ext_id, company=employer.name,
                      title=title, first_seen=NOW + timedelta(minutes=minutes), last_seen=NOW, links=[], **more)


async def _examples_as_they_were(session, owner):
    """`classifier.examples` as it was before the narrow reads (every role and every posting of a role read whole): the
    one to compare with. The first posting of a role was whichever the database gave first."""
    roles = (await session.scalars(select(Opportunity).where(Opportunity.owner == owner))).all()
    gone_far = await classifier.reached_set(session, owner)
    seen: dict = {}
    for p in await session.scalars(select(JobPosting).where(
            JobPosting.owner == owner, JobPosting.opportunity_id.is_not(None)).order_by(
            JobPosting.first_seen, JobPosting.id)):
        seen.setdefault(p.opportunity_id, p)
    out = []
    for role in roles:
        marked = classifier.label(role, gone_far)
        if marked is None:
            continue
        posting = seen.get(role.id)
        body = role.description or (posting.text if posting else None)
        out.append((classifier.text_of(role.title, posting.department if posting else None, body), *marked))
    for posting in await session.scalars(select(JobPosting).where(
            JobPosting.owner == owner, JobPosting.owner_override.is_(True))):
        out.append((classifier.posting_text(posting), True, classifier.MOVED_FORWARD_WEIGHT))
    return out


async def _world(session):
    """Roles of every kind the model learns from or does not, some with several postings, some with a description and
    some with only a posting's text, one with no posting, and a posting the owner asked to read anyway."""
    acme = await employers.find_or_make(session, OWNER, "Fabrikam")
    roles = [
        _role(1, "Governance Director", status="applied", description=BODY),
        _role(2, "Risk Program Lead", status="ready_to_launch"),  # no description: its first posting's text
        _role(3, "Payroll Clerk", status="withdrew", ended_reason="i_passed", description=BODY),
        _role(4, "Forklift Operator", status="rejected"),  # the employer said no: pursued, whatever it says
        _role(5, "Store Clerk", status="gone_quiet"),  # says nothing
        _role(6, "Warehouse Lead", status="gated_out", screened_out_by="kind"),  # not the owner's kind of job
        _role(7, "Compliance Manager", status="gated_out", screened_out_by="location"),  # says nothing of the kind
        _role(8, "Programs Lead", status="applied"),  # no posting and no description
    ]
    session.add_all(roles)
    session.add(Opportunity(owner="someone-else", slug="x", company="X", title="Not Mine", match_key="x|not mine",
                            status="applied"))
    await session.flush()
    for at, role in enumerate(roles[:7]):
        session.add(_posting(acme, f"first-{at}", role.title, minutes=2, department=f"Dept {at}",
                             text=f"First posting text of {role.title}. " + BODY, opportunity_id=role.id))
        session.add(_posting(acme, f"second-{at}", role.title, minutes=1 + 5, department="Other dept",
                             text="A later posting's text that is not the first.", opportunity_id=role.id))
    session.add(_posting(acme, "mine", "Night Nurse", owner_override=True, text=BODY))
    await session.flush()


async def test_the_narrow_examples_are_the_ones_read_whole_on_roles_with_several_postings(js):
    async with db.session_factory()() as session:
        await _world(session)
        old = await _examples_as_they_were(session, OWNER)
        new = await classifier.examples(session, OWNER)
    assert len(new) >= 5 and "Warehouse Lead" in " ".join(t for t, *_x in new), "the world has something to learn from"
    assert sorted(new) == sorted(old)
    assert not any("A later posting" in text for text, *_x in new), "a role's first posting, never a later one"


async def test_a_role_reads_its_first_posting_whatever_the_order_the_database_gives_and_ties_go_to_the_least_id(js):
    """The window that picks each role's first posting (Postgres's `distinct on` was the first version) gives what a
    reading in Python of the definition gives: the posting first seen, and among postings first seen together the least
    id, for every role, and the examples come out in the roles' id order."""
    async with db.session_factory()() as session:
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        roles = [_role(1, "Governance Director", status="applied"), _role(2, "Risk Program Lead", status="applied"),
                 _role(3, "Policy Lead", status="applied")]
        session.add_all(roles)
        await session.flush()
        # Role 1: five postings first seen together. Role 2: the earliest has the greatest id of three. Role 3: one.
        made = [_posting(acme, f"a{n}", "Governance Director", minutes=0, department=f"Dept {n}", text=f"Text a{n}",
                         opportunity_id=roles[0].id) for n in range(5)]
        made += [_posting(acme, f"b{n}", "Risk Program Lead", minutes=(0 if n == 2 else 9), department=f"Dept b{n}",
                          text=f"Text b{n}", opportunity_id=roles[1].id) for n in range(3)]
        made.append(_posting(acme, "c0", "Policy Lead", minutes=3, department="Dept c", text="Text c",
                             opportunity_id=roles[2].id))
        session.add_all(made)
        await session.flush()
        gone_far = await classifier.reached_set(session, OWNER)
        expected = []
        for role in sorted(roles, key=lambda r: r.id):
            first = min((p for p in made if p.opportunity_id == role.id), key=lambda p: (p.first_seen, p.id))
            marked = classifier.label(role, gone_far)
            assert marked is not None
            expected.append((classifier.text_of(role.title, first.department, first.text), *marked))
        got = await classifier.examples(session, OWNER)
    assert got == expected


async def test_the_sample_of_the_background_is_the_postings_whose_id_has_the_least_md5(js, monkeypatch):
    """No md5 in SQLite: the ids are ordered here by the md5 of their text, as the database ordered them, the same ones
    each time, and only the chosen postings' texts are read."""
    monkeypatch.setattr(classifier, "BACKGROUND_MAX", 5)
    monkeypatch.setattr(classifier, "ROLES_AT_ONCE", 2)  # more than one statement for the texts
    async with db.session_factory()() as session:
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        made = [_posting(acme, f"n{n}", f"Role {n}", text=f"Never a role {n}") for n in range(12)]
        session.add_all(made)
        await session.flush()
        ordered = sorted(made, key=lambda p: hashlib.md5(str(p.id).encode()).hexdigest())
        expected = [classifier.text_of(p.title, p.department, p.text) for p in ordered[:5]]
        got = await classifier.background(session, OWNER, 10.0, 0.0)
        again = await classifier.background(session, OWNER, 10.0, 0.0)
    assert [text for text, *_x in got] == expected and got == again
    assert {positive for _text, positive, _w in got} == {False}


async def test_a_role_that_teaches_nothing_is_read_for_nothing_but_its_four_columns(js):
    """The posting text is the one large thing the model reads, and most roles teach it
    nothing: no statement asks for the title, the description or a posting of a role the model does not learn from."""
    async with db.session_factory()() as session:
        await _world(session)
        statements: list[str] = []

        def noted(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        engine = db.engine().sync_engine
        event.listen(engine, "before_cursor_execute", noted)
        try:
            await classifier.examples(session, OWNER)
        finally:
            event.remove(engine, "before_cursor_execute", noted)
        roles = [s for s in statements if "FROM js_opportunities" in s]
    everything = [s for s in roles if "js_opportunities.status" in s]
    assert len(everything) == 1 and "js_opportunities.description" not in everything[0], everything
    by_id = [s for s in roles if "js_opportunities.description" in s]
    assert by_id and all("js_opportunities.id IN" in s for s in by_id), "the description of the roles asked for only"


async def test_the_model_built_with_the_loop_given_back_is_the_model_built_at_once():
    examples = [(f"Director of Governance {i} program risk", i % 3 != 0, 2.0 if i % 3 != 0 else 1.0) for i in range(60)]
    once = classifier.build(examples)
    cooperating = await classifier.build_giving_the_loop_back(examples)
    assert (cooperating.positive, cooperating.negative, cooperating.positive_total, cooperating.negative_total,
            cooperating.positive_weight, cooperating.negative_weight, cooperating.positives, cooperating.negatives,
            cooperating.vocabulary) == (once.positive, once.negative, once.positive_total, once.negative_total,
                                        once.positive_weight, once.negative_weight, once.positives, once.negatives,
                                        once.vocabulary)


async def test_reading_the_examples_gives_the_loop_back_between_them(monkeypatch):
    """Tokenising many texts takes long with no `await`: another task runs between examples."""
    monkeypatch.setattr(classifier, "Cooperate", lambda: coop.Cooperate(every=0.0))
    ran = 0

    async def other():
        nonlocal ran
        while True:
            await asyncio.sleep(0)
            ran += 1

    task = asyncio.get_running_loop().create_task(other())
    try:
        await classifier.build_giving_the_loop_back([(f"Governance Lead {i}", True, 1.0) for i in range(50)])
    finally:
        task.cancel()
    assert ran >= 40


class _Turns:
    """Counts the turns another task gets (one for each time the code under test gives the loop back), and notes the
    count each time `classifier.text_of` is asked: a text made with no yield before it has the count of the one made
    before it."""

    def __init__(self, monkeypatch) -> None:
        self.ran = 0
        self.at: list[int] = []
        monkeypatch.setattr(classifier, "Cooperate", lambda: coop.Cooperate(every=0.0))
        real = classifier.text_of

        def noted(*args):
            self.at.append(self.ran)
            return real(*args)

        monkeypatch.setattr(classifier, "text_of", noted)

    async def _other(self) -> None:
        while True:
            await asyncio.sleep(0)
            self.ran += 1

    def start(self) -> asyncio.Task:
        return asyncio.get_running_loop().create_task(self._other())


async def test_the_texts_of_the_examples_are_made_with_the_loop_given_back_between_them(js, monkeypatch):
    """Building the text of each of 5,500 labelled roles splits its whole body: a quarter of a second with no `await`
    (measured with the garbage collector off). Each text is made after a yield, so no stretch of the loop is longer
    than one text."""
    turns = _Turns(monkeypatch)
    async with db.session_factory()() as session:
        await _world(session)
        task = turns.start()
        try:
            made = await classifier.examples(session, OWNER)
        finally:
            task.cancel()
    assert len(made) >= 5 and len(turns.at) == len(made)
    assert all(later > earlier for earlier, later in zip(turns.at, turns.at[1:], strict=False)), (
        f"another task ran before each text was made: {turns.at}")


async def test_the_texts_of_the_background_are_made_with_the_loop_given_back_between_them(js, monkeypatch):
    turns = _Turns(monkeypatch)
    async with db.session_factory()() as session:
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([_posting(acme, f"loose-{n}", f"Loose Posting {n}", text=BODY) for n in range(6)])
        await session.flush()
        task = turns.start()
        try:
            made = await classifier.background(session, OWNER, 100.0, 0.0)
        finally:
            task.cancel()
    assert len(made) == 6 and len(turns.at) == 6
    assert all(later > earlier for earlier, later in zip(turns.at, turns.at[1:], strict=False)), (
        f"another task ran before each text was made: {turns.at}")
