"""The role tabs govern the funnel (docs/PRD.md 31.1 and 31.4): no posting is stopped or read first on the classifier's
word (every posting through the gates becomes a role, the very unlikely too); each stop records the step and the
version of What to look for that judged it; a save of What to look for judges nothing again; a deleted tab leaves no
role on a dead id, and a posting only it had said yes to goes on to become a role. Synthetic data only: every title and
word is made up."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import judged, stages
from app.jobsearch.doors import tools
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import classifier
from app.jobsearch.gating import regate, sift
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import JobPosting, Opportunity, RoleTagJob
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import prioritize
from app.jobsearch.work import tagging
from tests.support import EXAMPLE_SYSTEM, FIVE, OWNER, PROFILE, bare, load_profile, waiting_count

NOW = datetime(2026, 10, 9, 5, 30, tzinfo=UTC)
BUILDERS = PROFILE["criteria"]["target_roles"][0]
OPS = {"id": "ops", "name": "Operations", "titles": ["Operations Manager"],
       "description": "Keeps the organization running: facilities, vendors and the budget."}
ANALYSTS = {"id": "analysts", "name": "Analysts", "titles": ["Data Analyst"],
            "description": "Turns figures into a clear page."}
TWO = {**PROFILE["criteria"], "target_roles": [BUILDERS, OPS]}
THREE = {**PROFILE["criteria"], "target_roles": [BUILDERS, OPS, ANALYSTS]}

LIKED = ["Director of Governance and Risk", "Governance Risk Program Lead", "Risk and Compliance Program Director",
         "Head of Risk Governance", "Compliance Program Manager, Governance", "Governance Lead, Enterprise Risk",
         "Senior Program Director, Risk", "Risk Governance Manager"]
DISLIKED = ["Warehouse Packing Associate", "Payroll Specialist", "Forklift Operator", "Packing Line Associate",
            "Retail Sales Associate", "Warehouse Forklift Lead", "Payroll Clerk", "Store Sales Clerk"]


@pytest.fixture(autouse=True)
def fresh_classifier():
    classifier.clear_cache()
    yield
    classifier.clear_cache()


async def _posting(session, title, **fields):
    """A posting in the index; through the filter (waiting for the title check) unless `fields` say otherwise."""
    employer = await employers.find_or_make(session, OWNER, "Fabrikam")
    row = {"owner": OWNER, "employer_id": employer.id, "ats": EXAMPLE_SYSTEM, "ext_id": title, "company": employer.name,
           "title": title, "match_key": match_key(employer.name, title), "location": "Remote", "work_mode": "remote",
           "url": "https://jobs.example.test/" + title.replace(" ", "-"), "first_seen": NOW, "last_seen": NOW,
           "filtered_at": NOW, "links": [], "route": "titles", "text": f"The start of the posting for {title}.",
           **fields}
    posting = JobPosting(**row)
    session.add(posting)
    await session.flush()
    return posting


async def _stop(session, title, step, version, why=None, **fields):
    """A posting a step stopped, under a version of What to look for."""
    why = why or {"gate": "the place", "titles": "not your kind of job: a sales job"}[step]
    return await _posting(session, title, filtered_by=why, route=None, judged_by=step, judged_by_version=version,
                          **fields)


async def _role(session, n, **fields):
    role = Opportunity(owner=OWNER, slug=f"role-{n}", company=f"Company {n}", title=f"Role Title {n}",
                       status=fields.pop("status", stages.SECOND_IMPRESSION), fit=3,
                           match_key=f"company {n}|role title {n}",
                       origin="scout", created_at=NOW + timedelta(seconds=n), **fields)
    session.add(role)
    await session.flush()
    return role


def _history():
    """Roles for the owner: the liked ones applied to, the disliked ones found and scored low, all matched to the
    Operations tab (so a test that edits another tab has no role to send to the tagging)."""
    roles = []
    matched = {"target_role": "ops", "matched_by_version": 1}
    for r in range(3):
        for i, t in enumerate(LIKED):
            roles.append(Opportunity(owner=OWNER, slug=f"p{r}{i}", company=f"Co p{r}{i}", title=t, status="applied",
                                     fit=4, origin="scout", match_key=f"co p{r}{i}|{t.lower()}", **matched))
        for i, t in enumerate(DISLIKED):
            roles.append(Opportunity(owner=OWNER, slug=f"n{r}{i}", company=f"Co n{r}{i}", title=t,
                status="second_impression",
                                     fit=1, origin="scout", match_key=f"co n{r}{i}|{t.lower()}", **matched))
    return roles


async def _by_title():
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(JobPosting).execution_options(populate_existing=True))).all()
        return {p.title: p for p in rows}


async def _roles_by_title():
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(Opportunity).execution_options(populate_existing=True))).all()
        return {r.title: r for r in rows}


async def _put(client, criteria, why=""):
    """An edit of What to look for as the store keeps it. The page's save judges nothing again either (docs/PRD.md
    39.16): the tests of that are at the end of this file."""
    async with db.session_factory()() as session:
        version = await profile_store.save(session, OWNER, "criteria", criteria, by="owner", why=why)
        await session.commit()
    assert version is not None
    return version


def _with(role, **changes):
    return {**role, **changes}


# --- 31.1: the tabs govern the funnel ---------------------------------------------------------------------------


async def test_every_posting_through_the_gates_becomes_a_role_the_very_unlikely_too(js):
    """With a model, nothing is sent to a reader or stopped on the classifier's word: the likely ones and the very
    unlikely ones go on to become roles like the middling ones (the rank orders them later)."""
    titles_in = [*LIKED[:3], "Governance Director", "Risk Programs Lead", "Quantum Basket Weaver", "Payroll Clerk",
                 "Forklift Operator", "Store Sales Associate", "Packing Associate"]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "classifier_min_examples": 6})
        session.add_all(_history())
        for title in titles_in:
            await _posting(session, title, filtered_at=None, route=None, text=None)
        await session.commit()
        model = await classifier.train(session, OWNER)
        rating = {t: model.predict(classifier.text_of(t, None, None)) for t in titles_in}
        assert min(rating.values()) < 0.2 and max(rating[t] for t in LIKED[:3]) > 0.8, (
            "the made-up titles run from nearly sure to nearly never: before, a reader would have taken the one end "
            "and the classifier stopped the other")
        result = await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)
        rows = {p.title: p for p in (await session.scalars(select(JobPosting))).all()}
        waiting = await waiting_count(session, OWNER)
    assert result["routes"] == {"roles": len(titles_in)} and result["stopped"] == {}
    assert {p.route for p in rows.values()} == {"roles"}, "no posting goes to a reader, and none to nowhere"
    assert all(p.filtered_by is None and p.judged_by is None for p in rows.values()), "nothing is stopped or judged"
    assert all(rows[t].fit_prob == pytest.approx(rating[t]) for t in titles_in), "the rating is kept for the order"
    assert waiting == len(titles_in), "all of them wait to become a role"


async def test_a_posting_the_classifier_rates_unlikely_is_not_stopped_and_becomes_a_role(client, knocks):
    """docs/PRD.md 31.1, done when: a posting the classifier rates unlikely is not stopped: it becomes a role like the
    likely one, and the rank puts it where it belongs."""
    unlikely, likely = "Payroll Clerk", "Director of Governance and Risk"
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO, settings={**PROFILE["settings"], "classifier_min_examples": 6})
        session.add_all(_history())
        await _posting(session, unlikely, filtered_at=None, route=None, text=None)
        await _posting(session, likely, filtered_at=None, route=None, text=None)
        await session.commit()
    async with db.session_factory()() as session:
        sifted = await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)
        await session.commit()
    assert sifted["stopped"] == {} and sifted["routes"] == {"roles": 2}
    row = (await _by_title())[unlikely]
    assert (row.filtered_by, row.route, row.judged_by, row.fit_prob < 0.5) == (None, "roles", None, True)
    async with db.session_factory()() as session:
        assert await role_from_posting.make_roles(session, OWNER) == 2
        await session.commit()
    rows = await _by_title()
    assert rows[unlikely].opportunity_id is not None and rows[likely].opportunity_id is not None


# --- 31.4: each stop records who judged it ---------------------------------------------------------------------------


async def test_a_role_records_the_version_it_was_matched_under_by_the_tagging_and_by_a_report(client, knocks):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO)
        await _role(session, 1)
        await _role(session, 2)
        await session.commit()
    assert (await client.post("/api/v1/profile/roles/tag")).json() == {"open": True, "count": 2}
    async with db.session_factory()() as session:
        batch = await tools.titles_next_reply(session, OWNER)
        await session.commit()
    await _put(client, {**TWO, "target_roles": [BUILDERS, _with(OPS, description="Changed meanwhile.")]})
    async with db.session_factory()() as session:
        await tools.titles_report_reply(session, OWNER, [
            {"id": p["id"], "role": "ops" if p["title"] == "Role Title 1" else "none"} for p in batch["postings"]])
        await session.commit()
    roles = await _roles_by_title()
    assert (roles["Role Title 1"].target_role, roles["Role Title 1"].matched_by_version) == ("ops", 1)
    assert (roles["Role Title 2"].target_role, roles["Role Title 2"].matched_by_version) == (None, 1)

    # A role the scout reported is matched under the version in force when it was kept.
    async with db.session_factory()() as session:
        await _posting(session, "Reader Role", route="reader", judged_by="titles", judged_by_version=1,
                       target_role="builders")
        started = await run.start_round(session, OWNER, started_by="manual")
        await run.stage_report(session, started, [{
            "company": "Fabrikam", "title": "Reader Role", "url": "https://jobs.example.test/reader-role",
            "fit_breakdown": {"asks": [{"id": "promotion", "answer": "met", "evidence": "x"}]}}], False)
        await run.stage_report(session, started, [], done=True)
        await session.commit()
    kept = (await _roles_by_title())["Reader Role"]
    assert (kept.target_role, kept.matched_by_version) == ("builders", 2)


# --- 31.4: an edit of What to look for ------------------------------------------------------------------------------


async def _world(client):
    """The made-up owner after an edit. Version 1 had three tabs; version 2 changed the Builders description and added a
    title to Operations. The rows are read back by title. The edit is saved as the page saves it, and what the save
    answered is returned."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=THREE)
        await _stop(session, "Sales Associate", "titles", 1, nearest_role="builders")
        await _stop(session, "Cashier", "titles", None, nearest_role="ops")  # made before versions were recorded
        await _stop(session, "Shift Lead", "titles", 2, nearest_role="builders")  # judged under the version in force
        await _stop(session, "Night Guard", "gate", None)
        await _stop(session, "Cleaner", "gate", 1)
        await _posting(session, "Programs Lead")
        await _posting(session, "Builder Lead", route="reader", judged_by="titles", judged_by_version=1,
                       target_role="builders")
        await _posting(session, "Asked Anyway", route="reader", judged_by="you", judged_by_version=1,
                       owner_override=True)
        await _role(session, 1, target_role="builders", matched_by_version=1)
        await _role(session, 2, target_role="ops", matched_by_version=1)
        await _role(session, 3)
        await _role(session, 4, target_role="gone")  # a dead id, from before the save cleared them
        await _role(session, 5, status=stages.GATED_OUT)  # never reached Found: not for the tabs
        await _role(session, 6, target_role="analysts", matched_by_version=1)  # its tab did not change
        await session.commit()
    edited = {**THREE, "target_roles": [
        _with(BUILDERS, description="Builds programs and the teams that run them."),
        _with(OPS, titles=["Operations Manager", "Payroll Clerk"]), ANALYSTS]}
    saved = await client.put("/api/v1/profile/criteria", json={"data": edited, "why": "wider"})
    assert saved.status_code == 200 and saved.json()["version"] == 2, saved.text
    return saved.json()


# --- 31.4: deleting a tab ----------------------------------------------------------------------------------------


async def test_saving_without_a_tab_leaves_its_roles_unmatched_not_on_a_dead_id(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO)
        await _role(session, 1, target_role="ops", matched_by_version=1)
        await _role(session, 2, target_role="builders", matched_by_version=1)
        await _role(session, 3, target_role="ops", status=stages.GATED_OUT)
        await _posting(session, "Judged by ops", route="reader", judged_by="titles", judged_by_version=1,
                       target_role="ops")
        await _posting(session, "Judged by builders", route="reader", judged_by="titles", judged_by_version=1,
                       target_role="builders")
        await _stop(session, "Stopped near ops", "titles", 1, nearest_role="ops")
        await _stop(session, "Stopped near builders", "titles", 1, nearest_role="builders")
        await session.commit()
    version = await _put(client, {**TWO, "target_roles": [BUILDERS]}, "no more operations")
    assert version == 2
    roles = await _roles_by_title()
    assert (roles["Role Title 1"].target_role, roles["Role Title 1"].matched_by_version) == (None, None)
    assert roles["Role Title 3"].target_role is None, "no role is left on an id that is no tab, found or not"
    assert (roles["Role Title 2"].target_role, roles["Role Title 2"].matched_by_version) == ("builders", 1)
    postings = await _by_title()
    gone, kept = postings["Judged by ops"], postings["Judged by builders"]
    assert (gone.route, gone.target_role, gone.judged_by) == ("roles", None, None), (
        "the tab that said yes is gone: it goes on to become a role, matched by the analyst")
    assert gone.route_why is None
    assert (kept.route, kept.target_role, kept.judged_by) == ("reader", "builders", "titles")
    assert postings["Stopped near ops"].nearest_role is None and postings["Stopped near ops"].filtered_by, (
        "a stop near a deleted tab stays stopped, near none")
    assert postings["Stopped near builders"].nearest_role == "builders"


async def test_a_save_that_keeps_every_tab_leaves_the_roles_matched_and_a_restore_that_drops_one_unmatches_them(client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO)
        await _role(session, 1, target_role="ops", matched_by_version=1)
        await session.commit()
    await _put(client, {**TWO, "target_roles": [_with(BUILDERS, description="Edited."), OPS]})
    assert (await _roles_by_title())["Role Title 1"].target_role == "ops"
    only_builders = await _put(client, {**TWO, "target_roles": [BUILDERS]})
    assert only_builders == 3 and (await _roles_by_title())["Role Title 1"].target_role is None
    # Whichever way a version is saved: restoring version 1 brings the tab back (nothing is removed), and restoring
    # version 3 drops it again, which unmatches the roles that were put on it in between.
    async with db.session_factory()() as session:
        (await session.scalars(select(Opportunity))).first().target_role = "ops"
        await session.commit()
    back = await client.post("/api/v1/profile/criteria/versions/1/restore")
    assert back.status_code == 200 and back.json()["version"] == 4
    assert (await _roles_by_title())["Role Title 1"].target_role == "ops", "the tab is back: nothing was removed"
    dropped = await client.post("/api/v1/profile/criteria/versions/3/restore")
    assert dropped.status_code == 200 and dropped.json()["version"] == 5
    assert (await _roles_by_title())["Role Title 1"].target_role is None


async def test_a_deleted_tab_is_cleared_by_the_store_whoever_saves_it(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO)
        await _role(session, 1, target_role="ops")
        await session.flush()
        assert await profile_store.save(session, OWNER, "criteria", {**TWO, "target_roles": [BUILDERS]}, by="import")
        await session.commit()
    assert (await _roles_by_title())["Role Title 1"].target_role is None


# --- the words a stop is shown with live in one place ----------------------------------------------------------------


def test_the_words_of_the_title_checks_stops_are_held_in_one_place_and_the_classifier_has_none():
    """`judged.py` owns them, and what the app does with a stop rests on who judged it (`judged_by`) or which gate gave
    it (`screened_out_by`), never on how the sentence begins (docs/PRD.md 31.8). The classifier stops nothing, so it has
    no stop to word."""
    assert judged.NOT_YOUR_KIND == "not your kind of job"
    assert (judged.GATE, judged.TITLE_CHECK, judged.YOU) == ("gate", "titles", "you")
    assert not {"UNLIKELY", "CLASSIFIER", "AUDIT_WHY", "TRIM", "AUDIT"} & set(dir(judged))
    assert classifier.kind_of_job_stop("kind")
    assert not classifier.kind_of_job_stop("location") and not classifier.kind_of_job_stop(None)
    assert not classifier.kind_of_job_stop("fit"), "there is no stop for a role's fit"


# --- 39.16: saving What to look for judges nothing again ------------------------------------------------------------


async def test_saving_what_to_look_for_judges_nothing_again_and_says_nothing_of_it(client, knocks):
    """The owner (2026-10-14): changing what to look for is not changing how a posting is graded. The save stores the
    new version, which the next search looks for; the title check's stops and the roles' tabs are as they were, no
    tagging job opens and the runner is not woken."""
    saved = await _world(client)
    assert bare(saved) == {"saved": True, "version": 2}, "no line about judging again"
    after = await _by_title()
    roles_after = await _roles_by_title()
    assert (after["Sales Associate"].filtered_by, after["Sales Associate"].judged_by,
            after["Sales Associate"].judged_by_version, after["Sales Associate"].route) == (
        "not your kind of job: a sales job", "titles", 1, None), "a stop made under an older version stays"
    assert (after["Cashier"].filtered_by, after["Cashier"].judged_by_version) == (
        "not your kind of job: a sales job", None), "so does one made before versions were recorded"
    assert after["Shift Lead"].filtered_by and after["Night Guard"].filtered_by == "the place"
    assert after["Sales Associate"].route_why is None and after["Cashier"].route_why is None, "no stop was returned"
    assert {t: (r.target_role, r.matched_by_version) for t, r in roles_after.items()} == {
        "Role Title 1": ("builders", 1), "Role Title 2": ("ops", 1), "Role Title 3": (None, None),
        "Role Title 4": ("gone", None), "Role Title 5": (None, None), "Role Title 6": ("analysts", 1)}, (
        "no role was put back to unmatched, and none was matched")
    async with db.session_factory()() as session:
        assert (await session.scalars(select(RoleTagJob))).all() == [], "no titles job was started"
        assert await tagging.open_job(session, OWNER) is None
    assert knocks == [], "the runner is not woken"


async def test_a_save_of_only_what_the_reader_reads_says_nothing_and_wakes_nothing(client, knocks):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO)
        await _stop(session, "Sales Associate", "titles", 1)
        await _role(session, 1, target_role="builders", matched_by_version=1)
        await session.commit()
    saved = await client.put("/api/v1/profile/criteria", json={"data": {
        **TWO, "target_roles": [_with(BUILDERS, responsibilities="Plans differently now."), OPS]}})
    assert bare(saved.json()) == {"saved": True, "version": 2} and knocks == []
    assert (await _by_title())["Sales Associate"].filtered_by, "the same title judged the same way stays stopped"


async def test_a_save_with_no_target_role_left_leaves_the_stops_and_roles_alone(client, knocks):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria={"company": "A public agency."})
        await _stop(session, "Sales Associate", "titles", None)
        await _role(session, 1, target_role="gone")
        await session.commit()
    saved = await client.put("/api/v1/profile/criteria", json={"data": {"company": "A county agency."}})
    assert bare(saved.json()) == {"saved": True, "version": 2}, saved.json()
    assert (await _by_title())["Sales Associate"].filtered_by, "the stop stands: nothing is judged again"
    assert (await _roles_by_title())["Role Title 1"].target_role == "gone"
    assert knocks == []


async def test_deleting_a_tab_through_the_page_says_only_what_its_release_sent_on_and_matches_nothing_again(
        client, knocks):
    """A deleted tab's release is the store's, for whoever saves (`profile_store.release_tabs`): the roles are never
    left on an id that is no tab, and a posting only that tab said yes to goes on to become a role, since the tab that
    judged it is gone. The save does not match the roles again: they wait, unmatched, for the owner."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO)
        await _role(session, 1, target_role="ops", matched_by_version=1)
        await _posting(session, "Judged by ops", route="reader", judged_by="titles", judged_by_version=1,
                       target_role="ops")
        await session.commit()
    saved = await client.put("/api/v1/profile/criteria", json={"data": {**TWO, "target_roles": [BUILDERS]}})
    assert bare(saved.json()) == {
        "saved": True, "version": 2,
        "said": "1 posting the deleted role had said yes to goes on to become a role."}, saved.json()
    assert (await _roles_by_title())["Role Title 1"].target_role is None
    assert (await _by_title())["Judged by ops"].route == "roles"
    async with db.session_factory()() as session:
        assert (await session.scalars(select(RoleTagJob))).all() == [], "no matching was started"
    assert knocks == []


async def test_bringing_back_an_earlier_version_judges_nothing_again_either(client, knocks):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO)
        await _role(session, 1, target_role="ops", matched_by_version=1)
        await session.commit()
    await client.put("/api/v1/profile/criteria", json={"data": {**TWO, "target_roles": [BUILDERS]}})
    async with db.session_factory()() as session:
        (await session.scalars(select(Opportunity))).first().target_role = "ops"
        await session.commit()
    back = await client.post("/api/v1/profile/criteria/versions/1/restore")
    assert back.status_code == 200 and bare(back.json()) == {"restored": 1, "version": 3}, "no line: the tab is back"
    dropped = await client.post("/api/v1/profile/criteria/versions/2/restore")
    assert bare(dropped.json()) == {"restored": 2, "version": 4}, dropped.json()
    assert (await _roles_by_title())["Role Title 1"].target_role is None, "the dropped tab's role is not left on it"
    async with db.session_factory()() as session:
        assert (await session.scalars(select(RoleTagJob))).all() == []
    assert knocks == []


async def test_hard_pass_and_prioritization_saves_still_re_run_by_themselves_and_what_to_look_for_does_not(
        client, knocks, monkeypatch):
    """Only What to look for stays quiet: a new Hard pass checks the log and the roles again, and a new Prioritization
    works every score out again, each in its own save (docs/PRD.md 39.14, 39.21). The title check's stops are not a
    gate's to undo, so the Hard pass re-check leaves them stopped."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=TWO, scoring=FIVE)
        await _stop(session, "Sales Associate", "titles", 1)
        await _posting(session, "Programs Lead")
        await session.commit()
    ran: list[str] = []

    def spy(module, name):
        original = getattr(module, name)

        async def watch(*args, **kwargs):
            ran.append(f"{module.__name__.rsplit('.', 1)[-1]}.{name}")
            return await original(*args, **kwargs)

        monkeypatch.setattr(module, name, watch)

    spy(regate, "run")
    spy(prioritize, "rescore")

    await client.put("/api/v1/profile/criteria", json={"data": {**TWO, "company": "A different kind of employer."}})
    assert ran == [], "What to look for runs nothing"
    gates = await client.put("/api/v1/profile/gates", json={"data": {
        "location": {"remote": {"on": True}}, "compensation": {"amount": 50_000, "against": "maximum"}}})
    assert gates.json()["saved"] is True and gates.json()["said"].startswith("Re-checked 2 postings"), gates.json()
    assert ran == ["regate.run"]
    bigger = {"asks": [{**FIVE["asks"][0], "points": 4}, *FIVE["asks"][1:]]}
    assert (await client.put("/api/v1/profile/scoring", json={"data": bigger})).json()["saved"] is True
    assert ran == ["regate.run", "prioritize.rescore"]
    await client.put("/api/v1/profile/criteria", json={"data": {**TWO, "company": "A third kind of employer."}})
    assert knocks == []
    assert (await _by_title())["Sales Associate"].filtered_by, "the Hard pass re-check left the title check's stop"
