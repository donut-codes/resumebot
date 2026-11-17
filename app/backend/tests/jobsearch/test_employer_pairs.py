"""Pairs of employer records that may be one employer (docs/PRD.md 36.4 and 36.5): two records on one website with
names that look alike, or on one job-system board with names that look alike, are merged by code; any other pair
(found by its names alone, a website nobody confirmed, a careers site, a staffing firm's board) is two employers and
nobody is asked (the owner's decision of 2026-11-13); names that share only "Group" and "Inc." are no pair; what the
owner or the researcher said to keep separate is never merged by code; a question an older rule left open is closed, and
one the owner answers first is still carried out; the pass runs after an import and after the night's read. (The table
that decides is tested in test_employer_match.py.) Everything is made up."""

import argparse
import uuid

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.inbox import service as inbox
from app.jobsearch import actions, ats, cli, process, stages
from app.jobsearch.employers import match as employer_match
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.employers import records as employer_records
from app.jobsearch.employers.core import EmployerError, NoSuchEmployer
from app.jobsearch.employers.pairs import DIFFERENT, SAME, UNSURE, Evidence
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.missions import boards_night
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.models.work import TASK_DONE, Task, TrailEvent
from tests.builders import pair_signals
from tests.employer_builders import make_employer, make_posting, make_role
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import OWNER, load_profile, tracker_row, tracker_workbook

pytestmark = pytest.mark.usefixtures("example_world")

TENANTSYS = {"ats": "tenantsys", "board": {"tenant": "midco", "shard": "wd5", "site": "External"}}


def _fetch():
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


async def _all(model):
    async with db.read_session_factory()() as session:
        return list((await session.scalars(select(model))).all())


async def _decisions():
    return [t for t in await _all(Task) if t.kind in employer_pairs.KINDS]


async def _legacy_question(session, keep_id, away_id, detail="Raised before the owner's decision of 2026-11-13."):
    """An open merge question as an earlier release raised it (the app raises none now)."""
    return await inbox.raise_task(
        session, OWNER, key=employer_pairs.pair_key(keep_id, away_id), kind=employer_pairs.KIND_MERGE,
        title="These look the same", detail=detail, link=(employer_records.LINK, keep_id),
        options=[{"id": "merge", "label": "Merge", "plan": {"keep": str(keep_id), "away": str(away_id)}},
                 {"id": "keep_separate", "label": "Keep separate"}])


# --- the words of a name, a website, a board --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "words"), [
    ("Globex Inc.", {"globex"}),
    ("The Globex Group Holdings, LLC", {"globex"}),
    ("Alpha Group", {"alpha"}),
    ("Group Holdings Inc", set()),
    ("PartsDepot.com", {"partsdepot"}),
    ("ZENO (a Family of Builder Companies)", {"zeno", "family", "builder"}),
    ("ZENO a Family of Builder Companies", {"zeno", "family", "builder"}),
    ("Rivera Pharma and Company", {"rivera", "pharma"}),
    ("Northwind Insurance Companies", {"northwind", "insurance"}),
    ("Rivera Pharma (via Staffco)", {"rivera", "pharma"}),
    ("Via Christi Health", {"via", "christi", "health"}),
    (None, set()),
])
def test_the_words_of_a_name_leave_out_what_tells_nothing(name, words):
    assert set(employer_pairs.name_parts(name)) == words


def test_the_words_of_a_name_keep_their_order_and_their_repeats():
    assert employer_pairs.name_parts("Baker & Baker, Inc.") == ("baker", "baker")
    assert employer_pairs.name_parts("ZENO (a Family of Builder Companies)") == ("zeno", "family", "builder")
    assert employer_pairs.name_parts("Rivera Pharma (via Staffco)") == ("rivera", "pharma")


@pytest.mark.parametrize(("name", "parts"), [
    ("ITE (International Technology Exchange)", ("international", "technology", "exchange")),
    ("ITE - International Technology Exchange", ("international", "technology", "exchange")),
    ("International Technology Exchange (ITE)", ("international", "technology", "exchange")),
    ("GRID (Grand River Independent Distributor)", ("grand", "river", "independent", "distributor")),
    ("Grand River Independent Distributor (GRID)", ("grand", "river", "independent", "distributor")),
    ("Bank of Riverton (BOR)", ("bank", "riverton")),  # the initials count the "of"
    ("Pied Piper (PP)", ("pied", "piper")),
    # Not a name's own acronym: not the initials of the other words, or nothing beside it that it could stand for.
    ("Fabrikam Dental (Initech)", ("fabrikam", "dental", "initech")),
    ("ITE", ("ite",)),
    ("ITE Labs", ("ite", "labs")),
    ("Abc Foods Corp", ("abc", "foods")),
])
def test_a_name_that_holds_its_own_acronym_reads_as_its_long_form(name, parts):
    assert employer_pairs.name_parts(name) == parts


def test_a_via_tag_is_a_staffing_firm_and_not_a_name_that_begins_with_via():
    assert employer_pairs.has_via("Rivera Pharma (via Staffco)") and employer_pairs.has_via("Acme via Staffco")
    assert not employer_pairs.has_via("Via Christi Health") and not employer_pairs.has_via("Viacom")


@pytest.mark.parametrize(("text", "firms"), [
    ("Globex (via Staffco)", ["Staffco"]),
    ("Globex via Staffco", ["Staffco"]),
    ("Globex (via Staffco, Inc.)", ["Staffco, Inc."]),
    ("Globex via Staffco (Remote)", ["Staffco (Remote)"]),
    ("Via Christi Health", []),
    ("Viacom", []),
    ("Globex", []),
    (None, []),
])
def test_a_via_tag_names_the_staffing_firm(text, firms):
    assert employer_pairs.via_names(text) == firms


@pytest.mark.parametrize(("url", "expected"), [
    ("https://www.northwind.test/about", "northwind.test"),
    ("https://jobs.northwind.test/openings", "northwind.test"),
    ("northwind.test", "northwind.test"),
    ("https://www.northwind.co.uk", "northwind.co.uk"),
    # a job system's host, a job board, a social page: nobody's own website
    ("https://northwind.wd5.tenantsys.test/External", None),
    ("https://boards.feedsys.test/northwind", None),
    ("https://www.board.test/company/northwind", None),
    ("https://github.io", None),
    ("https://www.facebook.com/northwind", None),
    (None, None),
])
def test_a_website_is_the_registered_domain_of_the_employers_own_pages(url, expected):
    assert employer_pairs.domain_of(url) == expected


def test_a_board_is_compared_whole_and_only_when_it_is_confirmed_as_the_employers():
    def shared(first: dict, second: dict) -> bool:
        """Whether two records with these job systems and addresses (and no other likeness) hold one board."""
        return "board" in pair_signals(Employer(**first), Employer(**second))

    here = {"ats": "tenantsys", "board": TENANTSYS["board"]}
    said = pair_signals(Employer(**here), Employer(**here))
    assert said["board"] == "they post on the same Tenantsys job board"
    assert shared(here, {"ats": "tenantsys", "board": {**TENANTSYS["board"], "tenant": "MIDCO"}}), "case is no matter"
    assert not shared(here, {"ats": "tenantsys", "board": {**TENANTSYS["board"], "site": "Other"}})
    part = {"ats": "tenantsys", "board": {"tenant": "midco"}}
    assert not shared(part, part), "an address in part"
    guessed = {"ats": "slugsys", "board": {"slug": "midco", "confirmed": False}}
    assert not shared(guessed, guessed), "found only by asking the system for the employer's name"
    assert not shared({"ats": None, "board": {}}, {"ats": None, "board": {}})
    # A system the reader never needed a whole address for, and whose customers each have a part of their own.
    own = next(s for s in ats.SYSTEMS.values() if s.own_parts)
    mine = {"ats": own.key, "board": {own.own_parts[0]: "acme"}}
    assert shared(mine, dict(mine))
    root = {"ats": "feedsys", "board": {}}
    assert not shared(root, dict(root)), "a system's root host is nobody's"


# --- merging by code --------------------------------------------------------------------------------------------------


async def test_two_records_on_one_website_whose_names_are_alike_are_merged_by_code_the_fewer_roles_into_the_other(js):
    async with db.session_factory()() as session:
        big = await make_employer(session, "Initech Labs", website="https://www.example-corp.test")
        small = await make_employer(session, "Initech", website="https://example-corp.test/about")
        other = await make_employer(session, "Hooli", website="https://www.hooli.test")
        session.add_all([make_role(big, "Lead A"), make_role(big, "Lead B"), make_role(small, "Lead C"),
                         make_role(other, "Lead D")])
        await session.commit()
        big_id = big.id
    async with db.session_factory()() as session:
        done = await employer_pairs.settle(session, OWNER)
        await session.commit()
    assert done == {"merged": 1}
    async with db.session_factory()() as session:
        left = {e.name: e for e in (await session.scalars(select(Employer))).all()}
        assert sorted(left) == ["Hooli", "Initech Labs"], "the other name is gone from the list"
        assert left["Initech Labs"].id == big_id and left["Initech Labs"].aliases == ["Initech"]
        assert {r.title for r in (await session.scalars(select(Opportunity).where(
            Opportunity.employer_id == big_id))).all()} == {"Lead A", "Lead B", "Lead C"}
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).one()
        assert line.sentence == (
            "Merged Initech into Initech Labs: they share the website example-corp.test and their names are alike")
        assert (line.actor, line.who) == ("app", "resumebot")
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}, "safe to run again"


async def test_two_records_on_one_website_whose_names_have_nothing_in_common_are_two_employers_nobody_is_asked(js):
    """A website the per-role analyst set says little of whose it is (the employer merge matching spec)."""
    async with db.session_factory()() as session:
        await make_employer(session, "Initech", website="https://www.example-corp.test")
        await make_employer(session, "Initrode", website="https://example-corp.test/about")
        await session.commit()
    async with db.session_factory()() as session:
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
        await session.commit()
    assert len(await _all(Employer)) == 2 and await _decisions() == []


async def test_a_careers_site_two_records_share_is_two_employers_and_nobody_is_asked(js):
    """A vendor hosts the careers pages of many employers; only a website says whose a page is."""
    async with db.session_factory()() as session:
        await make_employer(session, "Initech", careers_url="https://careers.example-corp.test/jobs")
        await make_employer(session, "Initrode", careers_url="https://jobs.example-corp.test/openings")
        await make_employer(session, "Hooli", website="https://www.hooli.test")
        await make_employer(session, "Pied Piper", careers_url="https://www.hooli.test/careers")
        await session.commit()
    async with db.session_factory()() as session:
        found = await employer_pairs.candidates(session, OWNER)
        assert sorted(c.reasons[0] for c in found) == ["they share the careers site example-corp.test",
                                                       "they share the careers site hooli.test"]
        assert [c.decision.rule for c in found] == [employer_match.CAREERS] * 2, "listed, never merged"
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
    assert len(await _all(Employer)) == 4 and await _decisions() == []


async def test_three_records_on_one_website_with_alike_names_become_one(js):
    async with db.session_factory()() as session:
        for name, roles in (("Initech Labs", 3), ("Initech", 2), ("Initech Labs International", 1)):
            e = await make_employer(session, name, website="https://www.example-corp.test")
            session.add_all([make_role(e, f"{name} role {i}") for i in range(roles)])
        await session.commit()
    async with db.session_factory()() as session:
        done = await employer_pairs.settle(session, OWNER)
        await session.commit()
    assert done == {"merged": 2}
    async with db.session_factory()() as session:
        (one,) = (await session.scalars(select(Employer))).all()
        assert one.name == "Initech Labs International" and sorted(one.aliases) == ["Initech", "Initech Labs"]
        assert len((await session.scalars(select(Opportunity).where(Opportunity.employer_id == one.id))).all()) == 6


async def test_two_records_on_one_board_whose_names_use_the_same_words_are_merged_by_code(js):
    async with db.session_factory()() as session:
        first = await make_employer(session, "GRID (Grand River Independent Distributor)", **TENANTSYS)
        second = await make_employer(session, "Grand River Independent Distributor (GRID)", **TENANTSYS)
        session.add_all([make_role(first, "Lead A"), make_role(second, "Lead B"),
                         make_posting(first, "r1", ats="tenantsys"), make_posting(second, "r1", ats="tenantsys")])
        await session.commit()
    async with db.session_factory()() as session:
        done = await employer_pairs.settle(session, OWNER)
        await session.commit()
    assert done == {"merged": 1}
    async with db.session_factory()() as session:
        (one,) = (await session.scalars(select(Employer))).all()
        assert one.name == "Grand River Independent Distributor (GRID)", "the real name, not the acronym"
        assert one.aliases == ["GRID (Grand River Independent Distributor)"]
        assert len((await session.scalars(select(Opportunity))).all()) == 2
        assert len((await session.scalars(select(JobPosting))).all()) == 1, "the posting both read is one"


async def test_a_board_two_unrelated_names_share_is_a_board_given_to_the_wrong_one_not_a_pair(js):
    """A staffing firm's board is shared by every company it hires for; one found wrongly is cleared by the read."""
    async with db.session_factory()() as session:
        await make_employer(session, "Initech", **TENANTSYS)
        await make_employer(session, "Hooli", **TENANTSYS)
        await session.commit()
    async with db.session_factory()() as session:
        assert await employer_pairs.candidates(session, OWNER) == []
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
    assert await _decisions() == [] and len(await _all(Employer)) == 2


async def test_a_board_found_by_guessing_or_given_in_part_is_no_evidence(js):
    async with db.session_factory()() as session:
        await make_employer(session, "Initech", ats="slugsys", board={"slug": "shared", "confirmed": False})
        await make_employer(session, "Hooli", ats="slugsys", board={"slug": "shared", "confirmed": False})
        await make_employer(session, "Pied Piper", ats="tenantsys", board={"tenant": "x"})
        await make_employer(session, "Raviga", ats="tenantsys", board={"tenant": "x"})
        await session.commit()
    async with db.session_factory()() as session:
        assert await employer_pairs.candidates(session, OWNER) == []


async def test_a_job_system_a_job_board_or_a_social_page_is_not_a_website_two_employers_share(js):
    async with db.session_factory()() as session:
        await make_employer(session, "Initech", website="https://www.board.test/company/initech",
                            careers_url="https://initech.wd5.tenantsys.test/External")
        await make_employer(session, "Hooli", website="https://www.board.test/company/hooli",
                            careers_url="https://hooli.wd5.tenantsys.test/External")
        await session.commit()
    async with db.session_factory()() as session:
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
        assert len((await session.scalars(select(Employer))).all()) == 2


async def test_a_staffing_tag_keeps_a_pair_out_of_the_merge_by_code_even_on_one_website(js):
    async with db.session_factory()() as session:
        direct = await make_employer(session, "Rivera Pharma and Company", website="https://www.riverapharma.test")
        # Written without parentheses: a name's key drops "and" and a parenthesis (`employers.key_of`), so
        # "Rivera Pharma (via Staffco)" is the key "rivera pharma" too and could not be a record beside the first.
        hired = await make_employer(session, "Rivera Pharma via Staffco", website="https://www.riverapharma.test",
                                    gated=True, gated_reason="agency posts")
        session.add_all([make_role(direct, "Lead A"), make_role(hired, "Lead B", status=stages.GATED_OUT)])
        await session.commit()
    async with db.session_factory()() as session:
        done = await employer_pairs.settle(session, OWNER)
        await session.commit()
    assert done == {"merged": 0}
    assert await _decisions() == []
    assert len(await _all(Employer)) == 2


async def test_a_merge_that_fails_is_logged_not_asked_and_the_night_goes_on(js, monkeypatch, caplog):
    async with db.session_factory()() as session:
        await make_employer(session, "Initech", website="https://www.example-corp.test")
        await make_employer(session, "Initech Labs", website="https://www.example-corp.test")
        await session.commit()

    async def fails(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(employer_records, "merge", fails)
    async with db.session_factory()() as session:
        done = await employer_pairs.settle(session, OWNER)
        await session.commit()
    assert done == {"merged": 0}
    assert caplog.text.count("merging two employer records failed") == 1, "once, not once a round"
    assert len(await _all(Employer)) == 2 and await _decisions() == []


# --- the rest are two employers; a question an older rule left open ---------------------------------------------------


async def _brightwater(session):
    brightwater = await make_employer(session, "Brightwater")
    insurance = await make_employer(session, "Brightwater Insurance")
    session.add_all([make_role(brightwater, "Lead A"), make_role(insurance, "Lead B"), make_role(insurance, "Lead C"),
                     make_role(insurance, "Lead D")])
    await session.commit()
    return brightwater.id, insurance.id


async def test_a_name_inside_another_is_two_employers_nobody_is_asked_and_stays_listed_for_the_researcher(
        js, client):
    async with db.session_factory()() as session:
        await _brightwater(session)
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (employer_match.NEVER, employer_match.NAMES)
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
        await session.commit()
    assert await _decisions() == [] and len(await _all(Employer)) == 2
    assert (await client.get("/api/v1/inbox")).json()["items"] == []


async def test_an_open_question_an_older_rule_raised_is_closed_by_settle_with_a_note_and_nothing_is_merged(js, client):
    async with db.session_factory()() as session:
        brightwater_id, insurance_id = await _brightwater(session)
        await _legacy_question(session, insurance_id, brightwater_id)
        await session.commit()
    async with db.session_factory()() as session:
        assert not await employer_pairs.nothing_to_settle(session, OWNER), "a question to close is a turn to take"
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
        await session.commit()
        assert await employer_pairs.nothing_to_settle(session, OWNER)
    (closed,) = await _decisions()
    assert (closed.state, closed.answer, closed.assignee, closed.note) == (
        "skipped", employer_pairs.WITHDRAWN, "resumebot", employer_pairs.WITHDRAWN_NOTE)
    assert closed.note.endswith("Nothing was merged.") and len(await _all(Employer)) == 2
    assert (await client.get("/api/v1/inbox")).json()["items"] == []
    assert (await client.get("/api/v1/inbox/history")).json()["items"] == [], "closed by the app, not decided"


async def test_nothing_to_settle_is_true_only_with_no_pair_to_merge_and_no_question_open(js):
    async with db.session_factory()() as session:
        brightwater_id, insurance_id = await _brightwater(session)
        assert await employer_pairs.nothing_to_settle(session, OWNER), "a pair kept as two is nothing to settle"
        await make_employer(session, "Initech", website="https://www.example-corp.test")
        await make_employer(session, "Initech Labs", website="https://www.example-corp.test")
        await session.commit()
        assert not await employer_pairs.nothing_to_settle(session, OWNER), "a merge is something to settle"


async def test_keep_separate_answered_to_an_open_question_is_remembered_and_never_merged_by_code(js, client):
    async with db.session_factory()() as session:
        brightwater_id, insurance_id = await _brightwater(session)
        await _legacy_question(session, insurance_id, brightwater_id)
        await session.commit()
    (decision,) = await _decisions()
    answered = await client.post(f"/api/v1/tasks/{decision.id}/answer", json={"option": "keep_separate"})
    assert answered.status_code == 200 and answered.json()["answer"] == "keep_separate"
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(Employer))).all()) == 2
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
        # even when they come to share a website: the owner said so
        for e in (await session.scalars(select(Employer))).all():
            e.website = "https://www.brightwater.test"
        await session.commit()
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
        await session.commit()
    assert len(await _all(Employer)) == 2 and brightwater_id and insurance_id
    assert [t.state for t in await _decisions()] == [TASK_DONE]


async def test_set_aside_is_remembered_the_same_way(js, client):
    async with db.session_factory()() as session:
        brightwater_id, insurance_id = await _brightwater(session)
        await _legacy_question(session, insurance_id, brightwater_id)
        await session.commit()
    (decision,) = await _decisions()
    assert (await client.post(f"/api/v1/tasks/{decision.id}/skip", json={})).status_code == 200
    async with db.session_factory()() as session:
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}


async def test_merge_answered_to_an_open_question_merges_the_pair_keeping_every_role_and_both_names(js, client):
    async with db.session_factory()() as session:
        brightwater_id, insurance_id = await _brightwater(session)
        await _legacy_question(session, insurance_id, brightwater_id)
        await session.commit()
    (decision,) = await _decisions()
    answered = await client.post(f"/api/v1/tasks/{decision.id}/answer", json={"option": "merge"})
    assert answered.status_code == 200 and answered.json()["state"] == TASK_DONE
    async with db.session_factory()() as session:
        (one,) = (await session.scalars(select(Employer))).all()
        assert (one.id, one.name, one.aliases) == (insurance_id, "Brightwater Insurance", ["Brightwater"])
        assert len((await session.scalars(select(Opportunity).where(Opportunity.employer_id == one.id))).all()) == 4
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).one()
        assert line.sentence == "Merged Brightwater into Brightwater Insurance: you chose Merge in the Inbox"
        assert line.actor == "person" and brightwater_id
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
    (kept,) = await _decisions()
    assert (kept.id, kept.state, kept.answer) == (decision.id, TASK_DONE, "merge"), "what was decided stays on record"
    past = (await client.get("/api/v1/inbox/history")).json()["items"]
    assert [(d["decision"], d["decided_by"]) for d in past] == [("Merge", "You")]


async def test_the_northwind_pair_written_with_and_without_a_parenthesis_is_two_employers_and_nobody_is_asked(js):
    async with db.session_factory()() as session:
        with_paren = await make_employer(session, "Northwind Group (a Family of Companies)")
        without = await make_employer(session, "Northwind Group a Family of Companies")
        session.add_all([make_role(with_paren, "Lead A"), make_role(without, "Lead B")])
        await session.commit()
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert pair.decision.rule == employer_match.NAMES and "use the same words" in pair.reasons[0]
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
        await session.commit()
    assert await _decisions() == [] and len(await _all(Employer)) == 2


async def test_fabrikam_and_fabrikam_dental_are_a_candidate_and_are_not_merged_by_code_or_asked(js):
    async with db.session_factory()() as session:
        await make_employer(session, "Fabrikam")
        await make_employer(session, "Fabrikam Dental")
        await session.commit()
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert pair.decision.action == employer_match.NEVER
        assert pair.reasons == ['"Fabrikam Dental" begins with "Fabrikam"']
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
    assert await _decisions() == []


async def test_names_that_share_only_words_like_group_and_inc_are_no_pair(js):
    async with db.session_factory()() as session:
        for name in ("Alpha Group", "Beta Group", "Gamma Holdings Inc", "Delta Company", "Epsilon Corporation",
                     "Zeta Group Holdings", "Eta Group of Companies", "Theta Group of Companies"):
            await make_employer(session, name)
        await session.commit()
    async with db.session_factory()() as session:
        assert await employer_pairs.candidates(session, OWNER) == []
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
    assert await _decisions() == [] and len(await _all(Employer)) == 8


async def test_a_name_that_only_holds_the_words_of_another_is_no_pair_nor_one_that_adds_another_name(js):
    async with db.session_factory()() as session:
        for name in ("Ashford", "Ashford Risk", "Ashford Insurance", "The Mutual Group", "Harbor Mutual",
                     "Baker & Baker, Inc.", "Meyer Baker", "Elm Health", "Birch Street Health (Elm Health)",
                     "The Bancorp", "Traders Bancorp"):
            await make_employer(session, name)
        await session.commit()
    async with db.session_factory()() as session:
        found = await employer_pairs.candidates(session, OWNER)
        assert [sorted([c.keep.name, c.away.name]) for c in found] == [["Ashford", "Ashford Insurance"]], (
            "Ashford Risk adds a word that is not a descriptor; Baker & Baker is not Meyer Baker, nor is Birch Street "
            "Health the parent named in its parenthesis")


async def test_a_word_many_employers_names_hold_tells_none_of_them_from_another(js):
    async with db.session_factory()() as session:
        for name in ("Summit Group", "Summit One", "Ridge Summit", "Pine Summit", "Lark Summit",
                     "Oak Summit", "Fox Summit Group Ltd.", "Brightwater", "Brightwater Insurance"):
            await make_employer(session, name)
        await session.commit()
    async with db.session_factory()() as session:
        found = await employer_pairs.candidates(session, OWNER)
        assert [sorted([c.keep.name, c.away.name]) for c in found] == [["Brightwater", "Brightwater Insurance"]]


async def test_another_name_a_record_goes_by_counts_when_looking_for_a_pair(js):
    async with db.session_factory()() as session:
        await make_employer(session, "Initech", aliases=["Initrode Systems"])
        await make_employer(session, "Initrode Systems Corporation")
        await session.commit()
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert pair.reasons == ['"Initrode Systems" and "Initrode Systems Corporation" use the same words']


async def test_a_stale_decision_whose_records_are_gone_is_nothing_to_carry_out(js, client):
    async with db.session_factory()() as session:
        task = await inbox.raise_task(
            session, OWNER, key=employer_pairs.pair_key(uuid.uuid4(), uuid.uuid4()), kind=employer_pairs.KIND_MERGE,
            title="same?", options=[{"id": "merge", "label": "Merge", "plan": {
                "keep": str(uuid.uuid4()), "away": str(uuid.uuid4())}}, {"id": "keep_separate", "label": "No"}])
        await session.commit()
        task_id = task.id
    assert (await client.post(f"/api/v1/tasks/{task_id}/answer", json={"option": "merge"})).status_code == 200


# --- the researcher's word --------------------------------------------------------------------------------------------


async def test_the_researcher_saying_same_merges_what_code_finds_to_share_a_website_beside_alike_names(js):
    async with db.session_factory()() as session:
        one = await make_employer(session, "Initech", website="https://www.example-corp.test")
        two = await make_employer(session, "Initech Labs", website="https://example-corp.test/about")
        session.add_all([make_role(one, "Lead A"), make_role(two, "Lead B")])
        await session.commit()
        one_id, two_id = one.id, two.id
    async with db.session_factory()() as session:
        said = await employer_pairs.propose_merge(
            session, OWNER, two_id, one_id, Evidence(SAME, ("Both name the same headquarters on their own site.",)),
            by="employer research")
        await session.commit()
    assert said.outcome == employer_pairs.MERGED and said.employer is not None
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(Employer))).all()) == 1
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).one()
        assert line.actor == "agent" and line.who == "employer research"
        assert line.sentence.endswith(
            "they share the website example-corp.test and their names are alike, and research found them the same")


async def test_the_researchers_word_alone_never_merges_and_asks_nobody(js):
    async with db.session_factory()() as session:
        one = await make_employer(session, "Initech")
        two = await make_employer(session, "Hooli")
        await session.commit()
        one_id, two_id = one.id, two.id
    evidence = Evidence(SAME, ("Both are headquartered in Boston, says each company's own site.", "  "))
    async with db.session_factory()() as session:
        said = await employer_pairs.propose_merge(session, OWNER, one_id, two_id, evidence, by="employer research")
        again = await employer_pairs.propose_merge(session, OWNER, two_id, one_id, evidence, by="employer research")
        unsure = await employer_pairs.propose_merge(
            session, OWNER, one_id, two_id, Evidence(UNSURE, ("A subsidiary, perhaps.",)), by="employer research")
        await session.commit()
    assert said.outcome == again.outcome == unsure.outcome == employer_pairs.KEPT_SEPARATE
    assert said.task is None and again.task is None and unsure.task is None
    assert len(await _all(Employer)) == 2 and await _decisions() == [], "nothing asked, nothing remembered"


async def test_the_researcher_saying_unsure_on_a_pair_the_table_merges_merges_it_with_the_tables_words(js):
    async with db.session_factory()() as session:
        one = await make_employer(session, "Initech", website="https://www.example-corp.test")
        two = await make_employer(session, "Initech Labs", website="https://example-corp.test/about")
        await session.commit()
        one_id, two_id = one.id, two.id
    async with db.session_factory()() as session:
        said = await employer_pairs.propose_merge(
            session, OWNER, one_id, two_id, Evidence(UNSURE, ("Could be a subsidiary.",)), by="employer research")
        await session.commit()
    assert said.outcome == employer_pairs.MERGED and await _decisions() == []
    async with db.session_factory()() as session:
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_merged"))).one()
        assert line.sentence.endswith("they share the website example-corp.test and their names are alike")


async def test_the_researcher_saying_different_is_remembered_and_the_pair_is_never_asked_or_merged_again(js):
    async with db.session_factory()() as session:
        one = await make_employer(session, "Brightwater", website="https://www.brightwater.test")
        two = await make_employer(session, "Brightwater Insurance", website="https://www.brightwater.test")
        await session.commit()
        one_id, two_id = one.id, two.id
        await _legacy_question(session, two_id, one_id)
        await session.commit()
        assert len(await _decisions()) == 1, "an open question about the pair"
        said = await employer_pairs.propose_merge(
            session, OWNER, one_id, two_id, Evidence(DIFFERENT, ("One sells boat engines; the other, insurance.",)),
            by="employer research")
        await session.commit()
    assert said.outcome == employer_pairs.KEPT_SEPARATE
    (kept,) = await _decisions()
    assert (kept.state, kept.answer, kept.assignee) == (TASK_DONE, "keep_separate", "employer research")
    assert kept.note == "One sells boat engines; the other, insurance."
    async with db.session_factory()() as session:
        assert await employer_pairs.candidates(session, OWNER) == []
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}, "not even on one website"
        again = await employer_pairs.propose_merge(
            session, OWNER, one_id, two_id, Evidence(SAME), by="employer research")
        assert again.outcome == employer_pairs.KEPT_SEPARATE, "what was decided stands"
    assert len(await _all(Employer)) == 2


async def test_the_owners_keep_separate_stands_against_the_researcher(js):
    async with db.session_factory()() as session:
        one = await make_employer(session, "Initech", website="https://www.example-corp.test")
        two = await make_employer(session, "Initrode", website="https://www.example-corp.test")
        await session.commit()
        task = await inbox.raise_task(session, OWNER, key=employer_pairs.pair_key(one.id, two.id),
                                      kind=employer_pairs.KIND_MERGE, title="same?")
        task.state, task.answer = TASK_DONE, "keep_separate"
        await session.commit()
        said = await employer_pairs.propose_merge(
            session, OWNER, one.id, two.id, Evidence(SAME), by="employer research")
    assert said.outcome == employer_pairs.KEPT_SEPARATE and len(await _all(Employer)) == 2


async def test_a_proposal_that_cannot_be_made_is_refused(js):
    async with db.session_factory()() as session:
        one = await make_employer(session, "Initech")
        await session.commit()
        with pytest.raises(EmployerError, match="a verdict is one of"):
            await employer_pairs.propose_merge(session, OWNER, one.id, uuid.uuid4(), Evidence("maybe"), by="r")
        with pytest.raises(EmployerError, match="same employer"):
            await employer_pairs.propose_merge(session, OWNER, one.id, one.id, Evidence(SAME), by="r")
        with pytest.raises(NoSuchEmployer):
            await employer_pairs.propose_merge(session, OWNER, one.id, uuid.uuid4(), Evidence(SAME), by="r")


SITE = "https://careers.fabrikam.test/jobs/"
EXBOARD = "https://www.board.test/jobs/view/"


async def _fabrikam(session, *, urls=(SITE + "a", SITE + "b", None)):
    """A record "Northwind Labs" that goes by "Fabrikam" too; its roles A and B were found under that name and carry
    these apply addresses, C does not."""
    north = await make_employer(session, "Northwind Labs", aliases=["Fabrikam"],
                                website="https://www.northwind.test")
    roles = [make_role(north, "Lead A", company="Fabrikam", apply_url=urls[0]),
             make_role(north, "Lead B", company="Fabrikam", apply_url=urls[1]),
             make_role(north, "Lead C", apply_url=urls[2])]
    session.add_all(roles)
    await session.flush()
    session.add_all([make_posting(north, "1", opportunity_id=roles[0].id)])
    await session.commit()
    return north.id, [roles[0].id, roles[1].id]


async def _propose(north_id, roles, new_name="Fabrikam"):
    async with db.session_factory()() as session:
        done = await employer_pairs.propose_split(
            session, OWNER, north_id, role_ids=roles, new_name=new_name,
            evidence=Evidence(SAME, ("The researcher's hunch.",)), by="employer research")
        await session.commit()
    return done


async def _at(employer_id):
    async with db.session_factory()() as session:
        return sorted(r.title for r in (await session.scalars(
            select(Opportunity).where(Opportunity.employer_id == employer_id))).all())


async def _named(name):
    async with db.session_factory()() as session:
        return (await session.scalars(select(Employer).where(Employer.name == name))).one()


async def test_roles_whose_postings_sit_on_a_site_named_for_the_new_company_move_to_a_new_record_with_no_task(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
    first = await _propose(north_id, folded)
    again = await _propose(north_id, folded)
    assert first.outcome == employer_pairs.SPLIT_DONE and first.employer is not None and first.task is None
    assert again.outcome == employer_pairs.KEPT_TOGETHER and again.task is None, "nothing more to move"
    assert await _decisions() == [], "nobody is asked and nothing is remembered"
    fabrikam, north = await _named("Fabrikam"), await _named("Northwind Labs")
    assert await _at(fabrikam.id) == ["Lead A", "Lead B"] and await _at(north.id) == ["Lead C"]
    assert north.aliases == [], "the name that went with the roles goes too, or the two would be merged again"
    async with db.session_factory()() as session:
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_split"))).one()
        assert line.sentence == ("Split 2 roles out of Northwind Labs into Fabrikam: their own postings are on a "
                                 "site named for Fabrikam")
        assert line.who == "employer research", "Activity names who proposed it"
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}, "and the night does not undo it"
        await session.commit()
    assert len(await _all(Employer)) == 2


async def test_keep_together_is_remembered_and_a_proposal_that_does_not_fit_is_refused(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
        key = employer_pairs.split_key(north_id, folded, "Fabrikam")
        task = await inbox.raise_task(session, OWNER, key=key, kind=employer_pairs.KIND_SPLIT, title="Move 2 roles?")
        task.state, task.answer = TASK_DONE, employer_pairs.TOGETHER
        await session.commit()
        with pytest.raises(EmployerError, match="no such role"):
            await employer_pairs.propose_split(
                session, OWNER, north_id, role_ids=[uuid.uuid4()], new_name="Fabrikam", evidence=Evidence(SAME),
                by="employer research")
    again = await _propose(north_id, folded)
    assert again.outcome == employer_pairs.KEPT_TOGETHER and again.task is not None and len(await _all(Employer)) == 1


async def test_a_withdrawn_question_is_no_decision_so_the_same_word_moves_the_roles_once_the_facts_are_there(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session, urls=(EXBOARD + "1", EXBOARD + "2", None))
        await inbox.raise_task(
            session, OWNER, key=employer_pairs.split_key(north_id, folded, "Fabrikam"),
            kind=employer_pairs.KIND_SPLIT, title="Move 2 roles?",
            options=[{"id": "split", "label": "Split them out", "plan": {
                "from": str(north_id), "roles": [str(i) for i in folded], "names": [], "new_name": "Fabrikam"}}])
        await session.commit()
        assert await employer_pairs.settle_splits(session, OWNER) == 1
        await session.commit()
    (closed,) = await _decisions()
    assert (closed.state, closed.answer) == ("skipped", employer_pairs.WITHDRAWN)
    assert (await _propose(north_id, folded)).outcome == employer_pairs.KEPT_TOGETHER, "no evidence yet"
    async with db.session_factory()() as session:
        for role in (await session.scalars(select(Opportunity).where(Opportunity.title.in_(["Lead A", "Lead B"])))):
            role.apply_url = SITE + role.title[-1].lower()
        await session.commit()
    assert (await _propose(north_id, folded)).outcome == employer_pairs.SPLIT_DONE


# --- what shows another employer: S1, S2, S3 and what stays ----------------------------------------------------------


async def test_roles_on_the_job_system_account_of_an_employer_that_exists_move_to_it(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session, urls=("https://boards.feedsys.test/fabrikam/jobs/1",
                                                          "https://boards.feedsys.test/fabrikam/jobs/2", None))
        fab = await make_employer(session, "Fabrikam", ats="feedsys", board={"slug": "fabrikam"})
        await session.commit()
    done = await _propose(north_id, folded)
    assert done.outcome == employer_pairs.SPLIT_DONE and done.employer is not None and done.employer.id == fab.id
    assert await _at(fab.id) == ["Lead A", "Lead B"]
    async with db.session_factory()() as session:
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.event == "employer_split"))).one()
    assert line.sentence.endswith("their own postings are on Fabrikam's job-system account")


async def test_roles_on_an_account_named_for_an_employer_that_has_no_board_move_to_it(js):
    """S1b: most records exist only because roles named them, so they hold no board to compare."""
    async with db.session_factory()() as session:
        urls = ("https://fabrikam.wd5.tenantsys.test/External/job/Remote/Lead-A_R1",
                "https://fabrikam.wd5.tenantsys.test/External/job/Remote/Lead-B_R2", None)
        north_id, folded = await _fabrikam(session, urls=urls)
        fab = await make_employer(session, "Fabrikam Inc.", aliases=["Fabrikam"])
        await session.commit()
    done = await _propose(north_id, folded, "Fabrikam Inc.")
    assert done.outcome == employer_pairs.SPLIT_DONE and await _at(fab.id) == ["Lead A", "Lead B"]


@pytest.mark.parametrize(("confirmed", "moves"), [(True, True), (False, False)])
async def test_roles_on_the_confirmed_website_of_an_employer_that_exists_move_to_it_and_on_an_unconfirmed_one_stay(
        js, confirmed, moves):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session, urls=("https://careers.contoso.test/a",
                                                          "https://www.contoso.test/b", None))
        fields = {"owner_fields": ["website"]} if confirmed else {}
        other = await make_employer(session, "Contoso", website="https://contoso.test", **fields)
        await session.commit()
    done = await _propose(north_id, folded, "Contoso")
    assert (done.outcome == employer_pairs.SPLIT_DONE) is moves
    assert await _at(other.id) == (["Lead A", "Lead B"] if moves else [])


@pytest.mark.parametrize(("url", "moves"), [
    ("https://careers.fabrikam.test/x", True),
    ("https://boards.feedsys.test/fabrikam/jobs/1", True),
    ("https://boards.feedsys.test/fabrikam-health/jobs/1", False),
    ("https://careers.fabrikamhealth.test/x", False),
    ("https://boards.feedsys.test/fab/jobs/1", False),
])
async def test_a_new_record_is_made_only_when_a_name_on_the_address_equals_the_new_name(js, url, moves):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session, urls=(url, url + "2", None))
    done = await _propose(north_id, folded)
    assert (done.outcome == employer_pairs.SPLIT_DONE) is moves
    assert len(await _all(Employer)) == (2 if moves else 1)


@pytest.mark.parametrize("url", [
    "https://www.northwind.test/jobs/1",
    "https://careers.northwind.test/jobs/1",
    "https://northwind.wd5.tenantsys.test/External/job/x/y_R1",
    EXBOARD + "1",
    "https://www.listings.test/viewjob?jk=abc",
    "https://tenantsys.test/x",
    "https://boards.feedsys.test/",
])
async def test_a_role_whose_address_is_the_old_employers_or_a_job_board_or_a_vendors_stays(js, url):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session, urls=(url, url, None))
        north = await session.get(Employer, north_id)
        assert north is not None
        north.ats, north.board = "tenantsys", {"tenant": "northwind", "shard": "wd5", "site": "External"}
        await session.commit()
    done = await _propose(north_id, folded)
    assert done.outcome == employer_pairs.KEPT_TOGETHER and len(await _all(Employer)) == 1
    assert await _decisions() == []


async def test_where_applying_happens_decides_not_where_the_role_is_listed(js):
    """A board posing as an employer lists every role on its own pages; only the apply address names the real one."""
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session, urls=("https://www.northwind.test/a", "https://www.northwind.test/b",
                                                          None))
        for role in (await session.scalars(select(Opportunity).where(Opportunity.title == "Lead A"))):
            role.url, role.apply_url = "https://www.northwind.test/jobs/a", SITE + "a"  # listed here, applied there
        for role in (await session.scalars(select(Opportunity).where(Opportunity.title == "Lead B"))):
            role.company, role.url = "Northwind Labs", SITE + "b"
            role.apply_url = "https://www.northwind.test/apply/b"  # the reverse
        await session.commit()
    assert (await _propose(north_id, folded)).outcome == employer_pairs.SPLIT_DONE
    fabrikam = await _named("Fabrikam")
    assert await _at(fabrikam.id) == ["Lead A"] and await _at(north_id) == ["Lead B", "Lead C"]


async def test_a_role_with_addresses_at_the_old_employer_and_at_another_has_no_evidence_and_stays(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
        for role in (await session.scalars(select(Opportunity).where(Opportunity.title == "Lead A"))):
            role.company = "Northwind Labs"
            session.add(make_posting(await session.get(Employer, north_id), "2", opportunity_id=role.id,
                                     apply_url="https://www.northwind.test/apply"))
        await session.commit()
    assert (await _propose(north_id, folded)).outcome == employer_pairs.SPLIT_DONE
    assert await _at((await _named("Fabrikam")).id) == ["Lead B"] and "Lead A" in await _at(north_id)


async def test_a_group_moves_in_part_and_a_role_at_a_placeholder_named_record_with_only_a_job_board_link_stays(js):
    async with db.session_factory()() as session:
        holder = await make_employer(session, "Not shown on listing")
        roles = [make_role(holder, "Lead A", company="Not shown", apply_url=EXBOARD + "1"),
                 make_role(holder, "Lead B", company="Fabrikam", apply_url="https://boards.feedsys.test/fabrikam/jobs/9"),
                 make_role(holder, "Lead C", company="Unrelated Co", apply_url=SITE + "c")]
        session.add_all(roles)
        await session.flush()
        ids = [r.id for r in roles]
        holder_id = holder.id
        await session.commit()
    done = await _propose(holder_id, ids)
    assert done.outcome == employer_pairs.SPLIT_DONE
    assert await _at((await _named("Fabrikam")).id) == ["Lead B", "Lead C"], "the company text counts for nothing"
    assert await _at(holder_id) == ["Lead A"]


@pytest.mark.parametrize("which", ["role", "source", "new"])
async def test_a_via_tag_is_an_agency_and_is_never_split_by_code(js, which):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
        if which == "role":
            for role in (await session.scalars(select(Opportunity))):
                role.company = "Fabrikam (via Staffco)"
        elif which == "source":
            (await session.get(Employer, north_id)).name = "Northwind Labs (via Staffco)"  # type: ignore[union-attr]
        await session.commit()
    done = await _propose(north_id, folded, "Fabrikam (via Staffco)" if which == "new" else "Fabrikam")
    assert done.outcome == employer_pairs.KEPT_TOGETHER and len(await _all(Employer)) == 1


async def test_nothing_moves_when_a_role_left_behind_carries_the_new_name_or_the_name_is_the_old_ones(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
        for role in (await session.scalars(select(Opportunity).where(Opportunity.title == "Lead C"))):
            role.company = "Fabrikam"
        await session.commit()
    assert (await _propose(north_id, folded)).outcome == employer_pairs.KEPT_TOGETHER, "it would be merged back"
    assert (await _propose(north_id, folded)).outcome == employer_pairs.KEPT_TOGETHER, "and again, with no change"
    assert len(await _all(Employer)) == 1
    with pytest.raises(EmployerError, match="choose another name"):
        await _propose(north_id, folded, "Northwind Labs")


async def test_a_name_that_is_the_placeholder_or_no_name_moves_nothing(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
    assert (await _propose(north_id, folded, "Added link")).outcome == employer_pairs.KEPT_TOGETHER
    with pytest.raises(EmployerError):
        await _propose(north_id, folded, "  ")
    assert len(await _all(Employer)) == 1


async def test_a_name_that_belongs_to_another_employer_through_an_alias_moves_roles_only_on_its_evidence(js):
    """Never a new record under another's alias (the night would merge it into that one): the roles go to the one that
    has the alias, and only on a board or confirmed site, never on a site that merely carries the name."""
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
        other = await make_employer(session, "Contoso", aliases=["Fabrikam Group"])
        await session.commit()
    assert (await _propose(north_id, folded, "Fabrikam Group")).outcome == employer_pairs.KEPT_TOGETHER
    assert len(await _all(Employer)) == 2 and await _at(other.id) == []


# --- the questions an earlier release left open ----------------------------------------------------------------------


async def _legacy_split(session, north_id, roles, new_name="Fabrikam"):
    return await inbox.raise_task(
        session, OWNER, key=employer_pairs.split_key(north_id, roles, new_name), kind=employer_pairs.KIND_SPLIT,
        title="Northwind Labs may hold roles of another company", link=(employer_records.LINK, north_id),
        options=[{"id": "split", "label": "Split them out", "plan": {
            "from": str(north_id), "roles": [str(i) for i in roles], "names": [], "new_name": new_name}},
            {"id": "keep_together", "label": "Keep together"}])


async def test_settle_closes_an_open_split_question_and_moves_the_roles_whose_postings_show_it(js, client):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
        await _legacy_split(session, north_id, folded)
        await session.commit()
    async with db.session_factory()() as session:
        assert not await employer_pairs.nothing_to_settle(session, OWNER), "a question to close is a turn to take"
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}
        await session.commit()
        assert await employer_pairs.nothing_to_settle(session, OWNER)
        assert await employer_pairs.settle(session, OWNER) == {"merged": 0}, "a second run is a no-op"
    (closed,) = await _decisions()
    assert (closed.state, closed.answer, closed.assignee, closed.note) == (
        "skipped", employer_pairs.WITHDRAWN, "resumebot",
        "Closed by the app: it moves roles to another employer only when their own postings show it. "
        "2 moved, the rest stayed.")
    assert await _at((await _named("Fabrikam")).id) == ["Lead A", "Lead B"]
    assert (await client.get("/api/v1/inbox")).json()["items"] == []
    assert (await client.get("/api/v1/inbox/history")).json()["items"] == [], "closed by the app, not decided"


async def test_settle_closes_an_open_split_question_whose_roles_do_not_show_it_and_moves_nothing(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session, urls=(EXBOARD + "1", EXBOARD + "2", None))
        await _legacy_split(session, north_id, folded)
        await session.commit()
    async with db.session_factory()() as session:
        await employer_pairs.settle(session, OWNER)
        await session.commit()
    (closed,) = await _decisions()
    assert closed.note == employer_pairs.SPLIT_KEPT_NOTE and closed.note.endswith("Nothing was moved.")
    assert len(await _all(Employer)) == 1 and await _at(north_id) == ["Lead A", "Lead B", "Lead C"]


async def test_settle_closes_a_split_question_whose_record_or_roles_are_gone_and_goes_on_to_the_next(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
        gone = uuid.uuid4()
        await _legacy_split(session, gone, folded, "Contoso")
        await _legacy_split(session, north_id, [uuid.uuid4()], "Fabrikam")
        await _legacy_split(session, north_id, folded)
        await session.commit()
    async with db.session_factory()() as session:
        assert (await employer_pairs.settle(session, OWNER)) == {"merged": 0}
        await session.commit()
    states = sorted(t.state for t in await _decisions())
    assert states == ["skipped"] * 3, "all three are closed, and one failure stopped none"
    assert await _at((await _named("Fabrikam")).id) == ["Lead A", "Lead B"]


async def test_an_open_split_question_is_closed_when_the_researcher_sends_the_same_split(js):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session)
        await _legacy_split(session, north_id, folded)
        await session.commit()
    assert (await _propose(north_id, folded)).outcome == employer_pairs.SPLIT_DONE
    (closed,) = await _decisions()
    assert (closed.state, closed.answer) == ("skipped", employer_pairs.WITHDRAWN)


async def test_an_answer_the_owner_gives_to_a_split_question_before_it_is_closed_is_still_carried_out(js, client):
    async with db.session_factory()() as session:
        north_id, folded = await _fabrikam(session, urls=(EXBOARD + "1", EXBOARD + "2", None))
        task = await _legacy_split(session, north_id, folded)
        await session.commit()
    answered = await client.post(f"/api/v1/tasks/{task.id}/answer", json={"option": "split"})
    assert answered.status_code == 200
    assert await _at((await _named("Fabrikam")).id) == ["Lead A", "Lead B"]


# --- after an import, after the night's read --------------------------------------------------------------------------


async def test_the_pass_runs_as_a_step_of_the_nights_read_after_the_boards_are_read_and_the_roles_followed():
    library = process.build_library().get(process.READ_BOARDS)
    flows = {(f.source, f.target) for f in library.flows.values()}
    assert ("Follow_postings", "Merge_employers") in flows and ("Merge_employers", "Layoff_notices") in flows
    assert library.elements["Merge_employers"].action == "jobs.merge_employers"


async def test_the_nights_read_merges_the_records_that_share_a_website_beside_alike_names(js, monkeypatch):
    monkeypatch.setattr(polite, "make", _fetch)  # no network
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        one = await make_employer(session, "Initech", website="https://www.example-corp.test")
        two = await make_employer(session, "Initech Labs", website="https://www.example-corp.test")
        brightwater = await make_employer(session, "Brightwater")
        await make_employer(session, "Brightwater Insurance")
        session.add_all([make_role(one, "Lead A"), make_role(two, "Lead B"), make_role(brightwater, "Lead C")])
        await session.commit()
    outcome, _summary, _counts = await boards_night.read_boards(OWNER, details_per_night=5)
    assert outcome == "done"
    async with db.session_factory()() as session:
        names = sorted(e.name for e in (await session.scalars(select(Employer))).all())
        assert [n for n in names if n.startswith("Brightwater")] == ["Brightwater", "Brightwater Insurance"]
        assert len(names) == 3 and {"Initech", "Initech Labs"} & set(names), "the two on one website became one"
        assert await _decisions() == [], "and no question about the Brightwater pair"
        assert (await actions.merge_employers(session, None, {"owner": OWNER})) == {"merged": 0}


async def test_a_pass_that_cannot_finish_is_logged_and_never_stops_the_nights_read(js, monkeypatch, caplog):
    async with db.session_factory()() as session:
        await make_employer(session, "Initech", website="https://www.example-corp.test")
        await make_employer(session, "Initech Labs", website="https://www.example-corp.test")
        await session.commit()

    async def fails(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(employer_pairs, "settle", fails)
    async with db.session_factory()() as session:
        assert await actions.merge_employers(session, None, {"owner": OWNER}) == {"merged": 0}
    assert "looking for duplicate employer records failed" in caplog.text
    assert len(await _all(Employer)) == 2


async def test_an_import_of_the_tracker_merges_and_asks_nobody_after_it_linked_the_roles(js, tmp_path, capsys):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    path = tmp_path / "tracker.xlsx"
    path.write_bytes(tracker_workbook([
        tracker_row("Fabrikam", "Director of Programs"), tracker_row("Fabrikam Dental", "Director of Programs"),
        tracker_row("Northwind", "Lead")]).getvalue())
    from app.settings import settings

    original, settings.owner = settings.owner, OWNER
    try:
        assert await cli._import_tracker(argparse.Namespace(xlsx=str(path))) == 0
    finally:
        settings.owner = original
    out = capsys.readouterr().out
    assert "employers merged 0" in out and "employers asked" not in out
    assert sorted(e.name for e in await _all(Employer)) == ["Fabrikam", "Fabrikam Dental", "Northwind"]
    assert await _decisions() == []
