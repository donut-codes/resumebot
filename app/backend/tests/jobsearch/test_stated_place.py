"""The place a posting states decides the work location gate, and a career page is read for its posting only (the
owner, 2026-10-17): the gates are confirmed again after analysis is complete.

Found on a role in use: its stored text was the whole page ("<!DOCTYPE html>", "Skip to content", the menu, a language
list, "Log in"), and though the posting said "Locations - <a city>" and onsite at the client, the role held only the
country, so the gate for in person work in the owner's own place came back undecided and the role reached the writer.
Every name, town, site and sentence here is made up, and no test reaches the network."""

from datetime import UTC, date, datetime

import pytest

from app import db
from app.agents import roster
from app.jobsearch import adapters, ats, stages
from app.jobsearch.actors import BY_APP
from app.jobsearch.fit import scoring
from app.jobsearch.gating import gate_facts, stated_place
from app.jobsearch.measures.pay_place import Gates, location_ok, match_key
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import whole
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.jobsearch.writing import packets
from tests.builders import findings, save_documents, submit_findings
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.jobsearch.test_gate_copy import list_posting
from tests.jobsearch.test_gate_paths_agree import give_copy
from tests.support import EXAMPLE_SYSTEM, FIVE, OWNER, POSTING_WORDS, answered, load_profile

pytestmark = pytest.mark.usefixtures("made_up_boards")

#: Only in person in one town is accepted, so a role worked on site in another town fails the work location gate.
IN_OAKFIELD = {"in_person": {"on": True, "locations": ["Oakfield"]}}
#: The posting's own location lines, as a career page writes them.
LABELLED = POSTING_WORDS + "\n\nGeneral Information\nLocations\n- Riverton, CA\nLocation: Riverton/Bay Area"

#: A career page as a site draws it: the posting in `main`, and around it everything that is not the posting.
PAGE = """<!DOCTYPE html>
<html lang="en"><head><title>Programs Lead - Contoso</title><style>.x{color:red}</style></head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
<header class="header"><nav aria-label="Main menu"><ul>
  <li><a href="/jobs">Open positions</a></li><li><a href="/talent">Join our Talent Community</a></li>
  <li><a href="/login">Log in</a></li>
  <li><ul role="menu"><li>English</li><li>French</li><li>German</li><li>Spanish</li></ul></li>
</ul></nav></header>
<main id="main">
  <div class="breadcrumbs"><ul><li><a href="/">Home</a></li><li>Job detail</li></ul></div>
  <div class="grid grid--2--sidebar"><div class="grid__item">
  <article class="article article--details">
    <header><h1>Programs Lead</h1></header>
    <h2>General Information</h2>
    <h3>Locations</h3><ul><li>Riverton, CA</li></ul>
    <h2>Description and Requirements</h2>
    <p>We need someone to lead the programs team &amp; keep the books in order.</p>
    <p>This role is not eligible for 100% remote work: you must live within a commutable distance of Riverton.</p>
    <h3>What you bring</h3><ul><li>Five years of programs experience</li><li>A head for numbers</li></ul>
    <h3>About us</h3><p>ABOUT</p>
    <form><button>Apply now</button></form>
  </article>
  </div></div>
  <aside class="aside"><article class="article--share"><a href="#">Share on social</a></article></aside>
  <section id="widgetSimilarJobs"><h2>Similar jobs</h2><a href="/jobs/2">Programs Analyst</a></section>
</main>
<footer class="footer">Copyright Contoso. All rights reserved. Privacy Policy</footer>
<portal-data id="cookieBannerData" data-html="&lt;p&gt;cookies&lt;/p&gt;"></portal-data>
</body></html>""".replace("ABOUT", "We are a small team that builds programs for the public good. " * 6)


# --- the page is read for its posting, never its chrome ---------------------------------------------------------------


def test_a_career_page_keeps_the_postings_body_and_drops_its_chrome_and_markup():
    text = adapters.page_text(PAGE)
    assert text is not None
    for kept in ("Programs Lead", "General Information", "Locations", "- Riverton, CA",
                 "We need someone to lead the programs team & keep the books in order.",
                 "commutable distance of Riverton", "What you bring", "- Five years of programs experience"):
        assert kept in text, kept
    for dropped in ("<", "DOCTYPE", "Skip to content", "Open positions", "Talent Community", "Log in", "English",
                    "French", "Job detail", "Apply now", "Share on social", "Similar jobs", "Programs Analyst",
                    "Copyright", "Privacy Policy", "cookies", "color:red"):
        assert dropped not in text, dropped
    assert not whole.page_dump(text)


def test_a_page_with_no_main_region_is_read_without_its_menus_header_and_footer():
    page = ("<!doctype html><html><body><header><nav><a>Log in</a></nav></header><div><h1>Programs Lead</h1>"
            "<p>Lead the programs team.</p></div><footer>Site footer</footer></body></html>")
    assert adapters.main_text(page) == "Programs Lead\nLead the programs team."


def test_a_text_that_is_a_page_read_whole_is_not_a_postings_text():
    dumped = "<!DOCTYPE html>\nPrograms Lead - Contoso\nSkip to content\n-\nOpen positions\n" + POSTING_WORDS
    menu = "Programs Lead\nLog in\n- English\n- French\n- German\n- Spanish\n" + POSTING_WORDS
    assert whole.page_dump(dumped) and whole.page_dump(menu)
    assert "page read whole" in (whole.why_not(dumped) or "")
    assert not whole.page_dump(POSTING_WORDS + "\nLanguages\n- English\n- French\n- German")
    assert whole.why_not(LABELLED) is None


# --- the place the posting states ------------------------------------------------------------------------------------


def test_the_place_a_posting_states_is_read_from_its_own_location_lines():
    assert stated_place.stated(LABELLED) == "Riverton, CA; Riverton/Bay Area"
    assert stated_place.stated("Job Location: Oakfield, OH\n" + POSTING_WORDS) == "Oakfield, OH"
    assert stated_place.stated(POSTING_WORDS + "\nOur Locations\n- Riverton, CA") is None, "a site's footer list"
    assert stated_place.stated("Location: United States\n" + POSTING_WORDS) is None, "a country names no town"
    assert stated_place.stated(POSTING_WORDS) is None


@pytest.mark.parametrize(("held", "mode", "place", "want"), [
    ("United States", "onsite", "Riverton, CA", True),
    (None, None, "Riverton, CA", True),
    ("Not stated", "hybrid", "Riverton, CA", True),
    ("Oakfield", "onsite", "Riverton, CA", False),  # a town the role holds is never overwritten
    ("United States", "remote", "Riverton, CA", False),  # a remote role's country stays
    ("Remote, US", None, "Riverton, CA", False),
    ("United States", "onsite", "United States", False),
    # a job list gives the first city of a multi-city posting: the posting's list that includes it and names more wins
    ("Oakfield, Oakfield, OH, United States", "onsite", "Riverton, CA; Oakfield, OH; Lakeside, WA", True),
    ("Oakfield", "hybrid", "Lakeside, WA; Oakfield, OH", True),
    ("Oakfield, Oakfield, OH, United States", "onsite", "Oakfield, OH", False),  # nothing more named
    ("Oakfield, OH", "onsite", "Riverton, CA; Lakeside, WA", False),  # the held town is not in the list
    ("Oakfield, OH", "onsite", "Oakfield, OH; Riverton, CA", True),
    ("Oakfield, OH; Riverton, CA", "onsite", "Oakfield, OH; Riverton, CA", False),  # already the list: no rewrite
    ("Oakfield, OH", "remote", "Oakfield, OH; Riverton, CA", False),  # a remote role is left as it is
])
def test_a_stated_place_fills_only_a_place_that_names_no_town(held, mode, place, want):
    assert stated_place.fills(held, mode, place) is want


def test_a_role_holding_the_first_city_takes_the_postings_list_and_the_gate_judges_every_city():
    text = POSTING_WORDS + "\n\nLocations\n- Riverton, CA\n- Oakfield, OH\n- Lakeside, WA"
    role = Opportunity(location="Riverton, Riverton, CA, United States", move_type="onsite")
    assert stated_place.fill(role, text) == "Riverton, CA; Oakfield, OH; Lakeside, WA"
    assert role.location == "Riverton, CA; Oakfield, OH; Lakeside, WA"
    assert stated_place.fill(role, text) is None, "the list is held now: nothing more to put"
    only_oakfield = Gates(places_gated=True, onsite_places=["oakfield"])
    assert location_ok("Riverton, Riverton, CA, United States", "onsite", only_oakfield) is False
    assert location_ok(role.location, "onsite", only_oakfield) is True


# --- research: the stated place fills the role and the gate decides on it --------------------------------------------


async def _roles(*roles: tuple[str, dict]) -> dict[str, object]:
    """The made-up profile with in person work in Oakfield only, and these roles `(company, fields)`: their ids."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring=FIVE, gates={"other": [], "location": IN_OAKFIELD})
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        profile = await profile_store.current(session, OWNER)
        made = {}
        for company, fields in roles:
            breakdown = scoring.compute(answered(), profile)
            role = Opportunity(**{
                "owner": OWNER, "slug": company.lower(), "company": company, "title": "Programs Lead",
                "fit": breakdown["total"], "origin": "scout", "match_key": match_key(company, "Programs Lead"),
                "status": stages.SECOND_IMPRESSION, "found": date.today(), "url": f"https://jobs.example.test/{company.lower()}",
                "fit_breakdown": breakdown, "location": "United States", "move_type": "onsite",
                "description": POSTING_WORDS, **fields})
            session.add(role)
            made[company] = role
        await session.flush()
        for role in made.values():
            if role.status == stages.SECOND_IMPRESSION:
                await analysis.start(session, OWNER, role)
        await session.commit()
        return {company: role.id for company, role in made.items()}


async def _role(role_id) -> Opportunity:
    async with db.session_factory()() as session:
        return await session.get(Opportunity, role_id, populate_existing=True)


async def test_a_place_the_posting_labels_gates_the_role_out_before_the_analyst_is_sent_it(js):
    """Gates first (the token efficiency plan spec step 1): the posting's own label line is read in code as soon as
    the role has its text, so the out-of-area onsite role is Gated out with no analysis at all."""
    ids = await _roles(("Contoso", {"description": LABELLED}))
    role = await _role(ids["Contoso"])
    assert role.status == stages.GATED_OUT and role.analysis_state is None
    assert role.location == "Riverton, CA; Riverton/Bay Area"
    assert role.screened_out_by == "location" and "Oakfield" in role.screened_out_reason
    assert role.gate_facts["facts"]["location"]["quote"] == "Locations - Riverton, CA"


async def test_a_place_the_posting_states_fills_the_role_and_an_out_of_area_onsite_role_fails_the_gate(
        js, monkeypatch):
    monkeypatch.setattr(gate_facts, "ENABLED", False)  # the step off: the analyst's own fill and the gate after it
    ids = await _roles(("Contoso", {"description": LABELLED}))
    # The analyst sent no place: the posting's own line gives it.
    reply = await submit_findings(findings(ids["Contoso"], place_not_stated=False))
    role = await _role(ids["Contoso"])
    assert reply["taken"] is True and reply["stage"] == stages.GATED_OUT
    assert role.location == "Riverton, CA; Riverton/Bay Area"
    assert role.screened_out_by == "location" and "Oakfield" in role.screened_out_reason
    assert role.analysis[stated_place.PLACE_KEY] == stated_place.STATED


async def test_the_analyst_is_asked_for_the_place_once_and_its_answer_decides_the_gate(js):
    ids = await _roles(("Contoso", {}), ("Fabrikam", {}))
    refused = await submit_findings(findings(ids["Contoso"], place_not_stated=False))
    assert refused["taken"] is False and "place_not_stated" in refused["reason"]
    assert "work location gate" in refused["reason"]
    sent = await submit_findings(findings(ids["Contoso"], location="Riverton, CA", work_mode="onsite",
                                          place_not_stated=False))
    assert sent["taken"] is True and sent["stage"] == stages.GATED_OUT
    assert (await _role(ids["Contoso"])).location == "Riverton, CA", "a country gave way to the town the posting states"
    silent = await submit_findings(findings(ids["Fabrikam"], place_not_stated=True))
    role = await _role(ids["Fabrikam"])
    assert silent["taken"] is True and role.status == stages.READY_TO_LAUNCH, "a missing place never fails a role"
    assert role.analysis[stated_place.PLACE_KEY] == stated_place.NOT_STATED


async def test_the_place_is_kept_not_stated_when_the_analyst_says_the_posting_gives_none_whatever_the_card_names(js):
    """A role whose card named a town was kept "stated" though the analyst said its posting names no
    place; the town on the card is the listing's, not the posting's."""
    ids = await _roles(("Contoso", {"location": "Oakfield", "move_type": "onsite"}),
                       ("Fabrikam", {"location": "Oakfield", "move_type": "onsite"}))
    reply = await submit_findings(findings(ids["Contoso"], place_not_stated=True))
    assert reply["taken"] is True
    assert (await _role(ids["Contoso"])).analysis[stated_place.PLACE_KEY] == stated_place.NOT_STATED
    sent = await submit_findings(findings(ids["Fabrikam"], location="Oakfield", place_not_stated=True))
    assert sent["taken"] is True
    assert (await _role(ids["Fabrikam"])).analysis[stated_place.PLACE_KEY] == stated_place.STATED, \
        "a town the analyst sends is a place it stated"


async def test_the_place_is_kept_stated_when_the_posting_labels_one_though_the_analyst_says_it_gives_none(
        js, monkeypatch):
    monkeypatch.setattr(gate_facts, "ENABLED", False)
    ids = await _roles(("Contoso", {"description": LABELLED, "location": "Oakfield", "move_type": "onsite"}))
    reply = await submit_findings(findings(ids["Contoso"], place_not_stated=True))
    assert reply["taken"] is True
    assert (await _role(ids["Contoso"])).analysis[stated_place.PLACE_KEY] == stated_place.STATED


async def _task_of(role_id) -> dict:
    async with db.session_factory()() as session:
        return await analysis.brief_part(session, OWNER, await session.get(Opportunity, role_id), "task")


async def test_the_task_asks_for_the_place_in_the_first_submission_only_when_the_gate_is_undecided(js):
    """187 of 486 first submissions were refused for the work mode or the place and sent again whole,
    about a tenth of the night. The hand-out says so up front, so the first submission carries them."""
    ids = await _roles(("Contoso", {}), ("Fabrikam", {"location": "Oakfield", "move_type": "onsite"}),
                       ("Initech", {"description": LABELLED}),
                       ("Hooli", {"location": "Riverton, CA", "move_type": None}))
    asked = (await _task_of(ids["Contoso"]))["send_the_place"]
    assert "Your first submission must carry" in asked and "`work_mode`" in asked and "place_not_stated" in asked
    assert (await _task_of(ids["Fabrikam"]))["send_the_place"] is None, "a town and a way of working: decided"
    assert (await _task_of(ids["Initech"]))["send_the_place"] is None, "the posting's own label states the place"
    hooli = (await _task_of(ids["Hooli"]))["send_the_place"]
    assert hooli is None or "work_mode" in hooli


async def _copy_states_oakfield(role_id) -> None:
    """Give the role a cleansed copy that says where the work is (its card holds only a country)."""
    async with db.session_factory()() as session:
        await give_copy(session, await session.get(Opportunity, role_id), locations=["Oakfield"], work_mode="onsite")
        await session.commit()


async def test_the_task_does_not_ask_for_a_place_the_roles_copy_states(js):
    """Found by review of the place fix (2026-11-04): the hand-out read the role's card alone, so an analyst was told to
    send a place the posting's copy already gave."""
    ids = await _roles(("Contoso", {}), ("Fabrikam", {}))
    await _copy_states_oakfield(ids["Contoso"])
    assert (await _task_of(ids["Contoso"]))["send_the_place"] is None
    assert (await _task_of(ids["Fabrikam"]))["send_the_place"] is not None, "no copy: the place is asked for"


async def test_findings_with_no_place_are_taken_when_the_roles_copy_states_it(js):
    """The same for the refusal: a submission that sends no place is refused only when neither the card, the posting's
    labels nor the copy says where the work is."""
    ids = await _roles(("Contoso", {}), ("Fabrikam", {}))
    await _copy_states_oakfield(ids["Contoso"])
    taken = await submit_findings(findings(ids["Contoso"], place_not_stated=False))
    assert taken["taken"] is True
    refused = await submit_findings(findings(ids["Fabrikam"], place_not_stated=False))
    assert refused["taken"] is False and "work location gate" in refused["reason"], "no copy: refused"


async def _employers_posting_states_oakfield(role_id) -> None:
    """Two postings: the employer's own (older, the shorter) says where the work is, and a job board's (newer, the
    longer, the copy the gates read before 2026-11-04) says nothing."""
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        await list_posting(session, role, "own", system=EXAMPLE_SYSTEM, seen=30, size=300, places=("Oakfield",),
                           mode="onsite")
        await list_posting(session, role, "board", system=ats.JOB_SITE, seen=1, size=9000)
        await session.commit()


async def test_the_task_asks_about_the_place_the_employers_posting_states_not_the_boards_longer_copy(js):
    ids = await _roles(("Contoso", {}), ("Fabrikam", {}))
    await _employers_posting_states_oakfield(ids["Contoso"])
    assert (await _task_of(ids["Contoso"]))["send_the_place"] is None
    assert (await _task_of(ids["Fabrikam"]))["send_the_place"] is not None, "no posting states it: the place is asked"


async def test_findings_with_no_place_are_taken_when_the_employers_posting_states_it(js):
    ids = await _roles(("Contoso", {}), ("Fabrikam", {}))
    await _employers_posting_states_oakfield(ids["Contoso"])
    taken = await submit_findings(findings(ids["Contoso"], place_not_stated=False))
    assert taken["taken"] is True
    refused = await submit_findings(findings(ids["Fabrikam"], place_not_stated=False))
    assert refused["taken"] is False and "work location gate" in refused["reason"]


# --- before the writer drafts ----------------------------------------------------------------------------------------


RESEARCHED = {"status": stages.READY_TO_LAUNCH, "analysis_state": "done",
              "analysis": {"required": [{"item": "Five years", "class": "P", "by": "analyst"}]},
              "analysed_at": datetime(2026, 10, 16, tzinfo=UTC)}


async def test_the_writer_never_drafts_a_role_that_now_fails_a_gate_and_one_with_an_unanswered_place_goes_back(js):
    ids = await _roles(("Contoso", {**RESEARCHED, "description": LABELLED}), ("Fabrikam", RESEARCHED),
                       ("Initech", {**RESEARCHED, "analysis": {**RESEARCHED["analysis"], "place": "not_stated"}}))
    async with db.session_factory()() as session:
        for company in ("Contoso", "Fabrikam"):
            with pytest.raises(packets.PacketError):
                await packets.start(session, OWNER, ids[company], by=BY_APP)
        drafted = await packets.start(session, OWNER, ids["Initech"], by=BY_APP)
        await session.commit()
    contoso, fabrikam = await _role(ids["Contoso"]), await _role(ids["Fabrikam"])
    assert contoso.status == stages.GATED_OUT and contoso.screened_out_by == "location"
    assert (fabrikam.status, fabrikam.analysis_state) == (stages.READY_TO_LAUNCH, analysis.WAITING)
    assert drafted.status == "writing", "a posting that truly states no place goes on, as before"


# --- a town the posting names ----------------------------------------------------------------------------------------


async def test_a_role_whose_town_is_known_fails_the_place_rule_at_once_even_while_waiting_for_the_analyst(js):
    waiting = {**RESEARCHED, "analysis_state": analysis.WAITING}
    ids = await _roles(
        ("Contoso", {**waiting, "location": "Riverton, CA"}),
        ("Globex", {**waiting, "location": "Riverton, CA", "status": stages.READY_TO_LAUNCH}),
        ("Hooli", {**waiting, "location": "Oakfield"}),
        ("Initech", {**waiting, "location": "Riverton, CA", "move_type": "remote"}),
    )
    async with db.session_factory()() as session:
        for company in ("Contoso", "Globex", "Hooli", "Initech"):
            await stated_place.decide(session, OWNER, await session.get(Opportunity, ids[company]),
                                      cause="The place the role names was judged")
        await session.commit()
    for company in ("Contoso", "Globex"):
        role = await _role(ids[company])
        assert role.status == stages.GATED_OUT and role.screened_out_by == "location", company
        assert "Oakfield" in role.screened_out_reason
    assert (await _role(ids["Hooli"])).status == stages.READY_TO_LAUNCH, "the owner's own town passes"
    assert (await _role(ids["Initech"])).status == stages.READY_TO_LAUNCH, "a remote role is the rule's, as it stands"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Hooli"])
        role.location = "Riverton, CA"
        assert await stated_place.decide(session, OWNER, role, cause="The role's place changed") is True


async def test_no_turn_of_the_open_roles_settle_holds_more_than_one_batch_of_roles(js, monkeypatch):
    """`settle_open_places` once loaded and settled `PLACES_BATCH * 20` roles in one call, one long hold after a
    restart. Each call to `settle_places` now holds at most a batch."""
    from tests.jobsearch.work_queue.support import make_employer, make_posting, make_role

    monkeypatch.setattr(stated_place, "PLACES_BATCH", 2)
    sizes: list[int] = []
    real = stated_place.settle_places

    async def spy(session, owner, roles):
        roles = list(roles)
        sizes.append(len(roles))
        return await real(session, owner, roles)

    monkeypatch.setattr(stated_place, "settle_places", spy)
    async with db.session_factory()() as session:
        employer = make_employer()
        session.add(employer)
        made = [make_role(status=stages.CLEANSED_DATA, location="2 Locations") for _ in range(5)]
        session.add_all(made)
        await session.flush()
        session.add_all(make_posting(employer_id=employer.id, opportunity_id=r.id, location="Town A; Town B")
                        for r in made)
        await session.flush()
        placed = await stated_place.settle_open_places(session, OWNER)
        assert {r.id for r in placed} == {r.id for r in made}
    assert sizes and max(sizes) <= 2 and sum(sizes) == 5, sizes
