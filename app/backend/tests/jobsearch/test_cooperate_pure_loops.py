"""The quiet rules' unlinked-posting loop and the employer pairs' index and pair loops are pure work over thousands of
rows; each gives the event loop back (`postings.coop.Cooperate`) instead of holding it for seconds, and a search's end
warms the roles index before the process steps ask for it. Same results as before. Synthetic
data only."""


import pytest

from app import db
from app.jobsearch import process
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.missions import search_run as run
from app.jobsearch.pipeline import quiet
from app.jobsearch.postings import same_role
from tests.employer_builders import make_employer, make_posting, make_role
from tests.jobsearch.test_quiet_posting_went import _employer
from tests.support import OWNER, load_profile, reported


class Counting:
    """A `Cooperate` that counts how often a loop asked it to give the loop back."""
    calls = 0

    def __init__(self, *_a, **_k) -> None:
        pass

    async def __call__(self) -> None:
        type(self).calls += 1


@pytest.fixture
def counting():
    Counting.calls = 0
    return Counting


async def test_the_quiet_rules_give_the_loop_back_for_each_unlinked_posting_and_find_the_same_postings(js, monkeypatch,
                                                                                                         counting):
    monkeypatch.setattr(quiet, "Cooperate", counting)
    async with db.session_factory()() as session:
        acme = await _employer(session)
        role = make_role(acme, "Analyst", url="https://boards.example.test/acme/jobs/7")
        session.add(role)
        postings = [make_posting(acme, f"p{n}", url=f"https://boards.example.test/acme/jobs/{n}") for n in range(40)]
        session.add_all(postings)
        await session.flush()
        got = await quiet._speaking_postings(session, OWNER, [role])
        assert counting.calls == 40
        assert list(got[role.id]) == [postings[7].id], "only the posting carrying the role's link speaks"


async def test_the_pairs_give_the_loop_back_in_the_index_and_for_each_record_and_find_the_same_pairs(js, monkeypatch,
                                                                                                    counting):
    async with db.session_factory()() as session:
        await make_employer(session, "Initech", website="https://www.example-corp.test")
        await make_employer(session, "Initrode", website="https://example-corp.test/about")
        for n in range(20):
            await make_employer(session, f"Zeta{n}x Holdings")
        before = [(c.keep.name, c.away.name) for c in await employer_pairs.candidates(session, OWNER)]
        monkeypatch.setattr(employer_pairs, "Cooperate", counting)
        after = [(c.keep.name, c.away.name) for c in await employer_pairs.candidates(session, OWNER)]
    assert after == before and len(after) == 1
    assert counting.calls >= 22 + 22, "once for each record in the index build and for each record in the pair loop"


async def test_finishing_a_search_warms_the_roles_index_before_the_process_runs(js, monkeypatch):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        search = await run.start_round(session, OWNER, started_by="manual")
        await run.stage_report(session, search, [reported("Wingtip", "Programs Lead")], done=False)
        await session.commit()
    same_role.clear_cache()
    made: list[str] = []
    real = same_role._make

    async def make(session, owner, entry, grew):
        made.append("made")
        return await real(session, owner, entry, grew)

    monkeypatch.setattr(same_role, "_make", make)
    engine_cls = type(process.engine())
    real_complete = engine_cls.complete
    seen: list[int] = []

    async def complete(self, *a, **k):
        seen.append(len(made))
        return await real_complete(self, *a, **k)

    monkeypatch.setattr(engine_cls, "complete", complete)
    async with db.session_factory()() as session:
        search = await run.open_run(session, OWNER)
        await run.finish_round(session, search)
        await session.commit()
    assert seen and seen[0] == 1, "the index was made once, before the engine's call"
    assert len(made) == 1, "the steps in the call found it kept and made nothing"
