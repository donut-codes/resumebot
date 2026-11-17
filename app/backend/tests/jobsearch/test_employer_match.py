"""One table decides what code may do with a pair of employer records that may be one employer (the employer merge
matching spec, rules 8 to 16): merge, or never (two employers), first match wins, and every decision
comes with the words that say why. The owner is never asked (the owner's decision, 2026-11-13: a pair that meets the
merge rules is merged, any other pair is two employers). A shared website merges only beside names that look alike, or
when the owner or the employer researcher confirmed it for both records; an agency and the client it hired for are never
merged by code; a brand and its parent are never merged by code; what the owner or the researcher decided stands.
Evidence, not a word that matches: the same job-system account beside one shared distinctive word merges; two confirmed
websites on different domains are never merged; a job board or an ATS host with no tenant is no signal; names alone
never merge, and the questions an older rule left open are closed. The night's pass and the researcher's word go
through the same table. Every name, address and number is made up, beside one well-known
public company."""

import pytest
from sqlalchemy import select

from app import db
from app.inbox import service as inbox
from app.jobsearch import ats
from app.jobsearch.employers import match as employer_match
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.employers.match import MERGE, NEVER, Ties
from app.jobsearch.employers.pairs import SAME, UNSURE, Evidence
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.research import employer as employer_research
from app.models.work import TASK_DONE, TASK_OPEN, TASK_SKIPPED, Task, TrailEvent
from tests.builders import pair_signals
from tests.employer_builders import BY_YOU, make_employer, make_role
from tests.jobsearch.example_world import example_world  # noqa: F401  (the fixture)
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("example_world")

R = employer_research
WHEN = "2026-10-13T00:00:00+00:00"


# --- who set a website ------------------------------------------------------------------------------------------------


def _source(by: str) -> dict:
    return {"source": "its About page", "at": WHEN, "by": by}


def by_analyst(url: str) -> dict:
    """The fields of an employer whose website the per-role analyst set while reading a posting: not confirmed."""
    return {"website": url, "sources": {"website": _source("analyst")}}


def by_researcher(url: str) -> dict:
    """A website the employer researcher set: confirmed."""
    return {"website": url, "sources": {"website": _source(R.BY)}}


def by_owner(url: str) -> dict:
    """A website the owner typed on the employer's page: confirmed."""
    return {"website": url, "sources": {"website": BY_YOU}, "owner_fields": ["website"]}


def unsourced(url: str) -> dict:
    """A website nothing says where it came from (the tracker import's, an old record's): not confirmed."""
    return {"website": url}


# --- small helpers ----------------------------------------------------------------------------------------------------


async def _all(model):
    async with db.session_factory()() as session:
        return list((await session.scalars(select(model))).all())


async def _names() -> list[str]:
    return sorted(e.name for e in await _all(Employer))


async def _questions() -> list[Task]:
    return [t for t in await _all(Task) if t.kind == employer_pairs.KIND_MERGE]


async def _settle() -> dict:
    async with db.session_factory()() as session:
        done = await employer_pairs.settle(session, OWNER)
        await session.commit()
    return done


async def _merge_lines() -> list[TrailEvent]:
    return [t for t in await _all(TrailEvent) if t.event == "employer_merged"]


async def _two(session, first: tuple[str, dict], second: tuple[str, dict], roles: tuple[int, int] = (2, 1)):
    """Two employers with roles, the first with more, so it is the one that stays. Returns their ids."""
    big = await make_employer(session, first[0], **first[1])
    small = await make_employer(session, second[0], **second[1])
    session.add_all([make_role(big, f"Lead {n}") for n in range(roles[0])]
                    + [make_role(small, f"Other lead {n}") for n in range(roles[1])])
    await session.commit()
    return big.id, small.id


# --- the table --------------------------------------------------------------------------------------------------------

SAYS = {
    "website": "they share the website acme.test",
    "careers": "they share the careers site acme.test",
    "board": "they post on the same Feedsys job board",
    "names": '"Acme International" begins with "Acme"',
    "word": 'both names hold "Acme"',
}
BRAND = ("Fabrikam Dental (Initech)", "Initech")
CONTRADICTION = "their websites are different, acme.test and globex.test, and you or the researcher confirmed both"


def _kinds(*names: str) -> dict[str, str]:
    return {name: SAYS[name] for name in names}


#: Each rule of the table: its id, the signals and ties that make it, and what it does.
ROWS = [
    pytest.param(employer_match.DECIDED, _kinds("website", "names"), Ties(decided=True), NEVER, id="N1 decided"),
    pytest.param(employer_match.AGENCY, _kinds("website"), Ties(agency=True), NEVER, id="N2 agency"),
    pytest.param(employer_match.CONTRADICTED, _kinds("board", "names"), Ties(contradiction=CONTRADICTION), NEVER,
                 id="N3 contradicted"),
    pytest.param(employer_match.BRAND, _kinds("website"), Ties(brand=BRAND), NEVER, id="N4 brand"),
    pytest.param(employer_match.TAGGED, _kinds("website", "names"), Ties(tagged=True), NEVER, id="N5 tagged"),
    pytest.param(employer_match.SAME_NAME, _kinds("names"), Ties(same_name=True), MERGE, id="M1 same name"),
    pytest.param(employer_match.WEBSITE_AND_NAMES, _kinds("website", "names"), Ties(), MERGE,
                 id="M2 website and names"),
    pytest.param(employer_match.CONFIRMED_WEBSITE, _kinds("website"), Ties(confirmed=True), MERGE,
                 id="M3 confirmed website"),
    pytest.param(employer_match.SAME_ACCOUNT, _kinds("board", "names"), Ties(), MERGE,
                 id="M4 same account"),
    pytest.param(employer_match.WEBSITE_UNCONFIRMED, _kinds("website"), Ties(), NEVER, id="A1 website"),
    pytest.param(employer_match.NAMES, _kinds("names"), Ties(), NEVER, id="A2 names"),
    pytest.param(employer_match.CAREERS, _kinds("careers"), Ties(), NEVER, id="A3 careers"),
    pytest.param(employer_match.NOTHING, {}, Ties(), NEVER, id="nothing the app can check"),
]


@pytest.mark.parametrize(("rule", "signals", "ties", "action"), ROWS)
def test_decide_gives_each_rule_of_the_table_for_the_signals_that_make_it(rule, signals, ties, action):
    decision = employer_match.decide(signals, ties)
    assert (decision.rule, decision.action) == (rule, action)


def test_no_rule_of_the_table_asks_the_owner():
    """The owner's decision (2026-11-13): a pair is merged by code or it is two employers; nothing is asked."""
    assert {row.values[3] for row in ROWS} == {MERGE, NEVER}
    assert not hasattr(employer_match, "ASK") and not hasattr(employer_match.Decision, "asked")


def test_the_table_has_a_case_for_every_rule():
    assert {row.values[0] for row in ROWS} == set(employer_match.RULES)


#: The rules read before the evidence, in order, with the tie that makes each.
ABOVE = [("decided", "decided"), ("agency", "agency"), ("contradicted", "contradiction"), ("brand", "brand"),
         ("tagged", "tagged"), ("same_name", "same_name")]


@pytest.mark.parametrize("rule", [rule for rule, _tie in ABOVE])
def test_the_first_rule_that_matches_decides_whatever_matches_below_it(rule):
    ties = {"decided": True, "agency": True, "contradiction": CONTRADICTION, "brand": BRAND, "tagged": True,
            "same_name": True, "confirmed": True}
    for _above, tie in ABOVE[:[r for r, _t in ABOVE].index(rule)]:  # the rules above do not hold; everything below does
        del ties[tie]
    everything = _kinds("website", "careers", "board", "names", "word")
    assert employer_match.decide(everything, Ties(**ties)).rule == rule


@pytest.mark.parametrize(("signals", "ties", "rule"), [
    pytest.param(_kinds("website", "careers", "board", "names"), Ties(confirmed=True),
                 employer_match.WEBSITE_AND_NAMES, id="a website beside alike names"),
    pytest.param(_kinds("website", "careers", "board"), Ties(confirmed=True),
                 employer_match.CONFIRMED_WEBSITE, id="a confirmed website beside no names"),
    pytest.param(_kinds("website", "careers", "board"), Ties(), employer_match.WEBSITE_UNCONFIRMED,
                 id="a website nobody confirmed is read before a careers site"),
    pytest.param(_kinds("careers", "board", "names"), Ties(), employer_match.SAME_ACCOUNT,
                 id="a board beside alike names merges before names alone are read"),
    pytest.param(_kinds("careers", "board", "word"), Ties(), employer_match.SAME_ACCOUNT,
                 id="one account beside a shared distinctive word merges"),
    pytest.param(_kinds("careers", "word"), Ties(), employer_match.CAREERS,
                 id="a shared word without an account is no evidence of its own"),
    pytest.param(_kinds("word"), Ties(), employer_match.NOTHING, id="a shared word alone is nothing to go on"),
    pytest.param(_kinds("careers", "names"), Ties(), employer_match.NAMES, id="names are read before a careers site"),
    pytest.param(_kinds("careers", "board"), Ties(), employer_match.CAREERS,
                 id="a careers site is the rule when nothing else is"),
])
def test_the_evidence_is_read_in_the_order_of_the_table(signals, ties, rule):
    assert employer_match.decide(signals, ties).rule == rule


def test_every_decision_has_words_and_a_reason_for_each_pair_kept_as_two():
    said = {}
    for row in ROWS:
        rule, signals, ties, _action = row.values
        words = employer_match.decide(signals, ties).words
        assert words and words == words.strip() and not words.endswith("."), f"{rule}: a clause, finished by its caller"
        assert len(words) <= employer_pairs.REASON_CHARS, f"{rule}: more than a line holds"
        said[rule] = words
    # The words a merge gives Activity ("Merged X into Y: …") and the reasons code did not merge.
    assert said[employer_match.WEBSITE_AND_NAMES] == "they share the website acme.test and their names are alike"
    assert said[employer_match.CONFIRMED_WEBSITE] == (
        "they share the website acme.test, which you or the researcher confirmed for both")
    assert said[employer_match.SAME_ACCOUNT] == (
        "they post on the same Feedsys job board and their names are alike")
    assert said[employer_match.WEBSITE_UNCONFIRMED] == "it has not confirmed the website is the employer's own"
    assert said[employer_match.NAMES] == "a name alone does not show they are one employer"
    assert said[employer_match.CAREERS] == "a vendor can host the careers site of many employers"
    assert said[employer_match.BRAND] == (
        '"Fabrikam Dental (Initech)" holds "Initech" in brackets, which looks like a brand and its parent')


# --- a shared website needs a second signal: alike names ------------------------------------------------------------


@pytest.mark.parametrize(("big", "small"), [
    pytest.param("Acme International, Inc.", "Acme", id="a name and its longer form"),
    pytest.param("Grand River Independent Distributor (GRID)", "GRID (Grand River Independent Distributor)",
                 id="the same words in another order"),
    pytest.param("ITE (International Technology Exchange)", "International Technology Exchange",
                 id="a name that holds its own acronym, in brackets"),
    pytest.param("ITE - International Technology Exchange", "International Technology Exchange (ITE)",
                 id="a name that holds its own acronym, after a dash"),
])
async def test_a_website_with_alike_names_is_merged_by_the_website_and_names_rule(js, big, small):
    async with db.session_factory()() as session:
        await _two(session, (big, by_analyst("https://www.example-corp.test")),
                   (small, by_analyst("https://example-corp.test/about")))
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (MERGE, employer_match.WEBSITE_AND_NAMES), (
            "a bracket that is the other name's long form beside its acronym is no brand")
        assert pair.decision.words == "they share the website example-corp.test and their names are alike"
    assert await _settle() == {"merged": 1}
    (line,) = await _merge_lines()
    assert "they share the website example-corp.test" in line.sentence, "Activity says the rule in words"
    assert len(await _all(Employer)) == 1


async def test_an_acquired_brand_on_the_parents_website_with_unlike_names_is_two_employers_and_nobody_is_asked(js):
    """A brand and its parent merged on a website the per-role analyst had set."""
    async with db.session_factory()() as session:
        await _two(session, ("Initech", by_analyst("https://www.initech-corp.test")),
                   ("Fabrikam Dental", by_analyst("https://initech-corp.test/dental")))
    assert await _settle() == {"merged": 0}
    assert await _names() == ["Fabrikam Dental", "Initech"]
    assert await _questions() == []


async def test_a_local_chapter_of_an_association_on_a_companys_website_is_two_employers_and_nobody_is_asked(js):
    """A local chapter of a professional body merged into a company, the two names with
    nothing in common, on a website the per-role analyst had set."""
    async with db.session_factory()() as session:
        await _two(session, ("Globex", by_analyst("https://www.globex.test")),
                   ("Oakfield Chapter of the Example Engineers Society", by_analyst("https://globex.test/chapter")))
    assert await _settle() == {"merged": 0}
    assert await _names() == ["Globex", "Oakfield Chapter of the Example Engineers Society"]
    assert await _questions() == []


async def test_a_website_only_the_analyst_set_does_not_merge_unalike_names_and_nobody_is_asked(js):
    async with db.session_factory()() as session:
        await _two(session, ("Northwind", by_analyst("https://www.corp.test")),
                   ("Contoso Partners", by_analyst("https://corp.test/jobs")))
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (NEVER, employer_match.WEBSITE_UNCONFIRMED)
        assert pair.reasons == ["they share the website corp.test"], "still listed: the researcher's brief offers it"
    assert await _settle() == {"merged": 0}
    assert await _questions() == [] and len(await _all(Employer)) == 2
    assert await _settle() == {"merged": 0}, "nothing is remembered, nothing changes"


@pytest.mark.parametrize(("first", "second"), [
    pytest.param(by_owner, by_researcher, id="the owner's on one side, the researcher's on the other"),
    pytest.param(by_owner, by_owner, id="the owner's on both"),
    pytest.param(by_researcher, by_researcher, id="the researcher's on both"),
])
async def test_a_website_confirmed_for_both_merges_the_pair_whatever_the_names(js, first, second):
    async with db.session_factory()() as session:
        await _two(session, ("Northwind", first("https://www.corp.test")),
                   ("Contoso Partners", second("https://corp.test/jobs")))
    assert await _settle() == {"merged": 1}
    (line,) = await _merge_lines()
    assert line.sentence.endswith("they share the website corp.test, which you or the researcher confirmed for both")
    assert len(await _all(Employer)) == 1


@pytest.mark.parametrize(("confirmed", "other"), [
    pytest.param(by_owner, by_analyst, id="the owner's against the analyst's"),
    pytest.param(by_researcher, by_analyst, id="the researcher's against the analyst's"),
    pytest.param(by_owner, unsourced, id="the owner's against one nothing says the source of"),
    pytest.param(by_researcher, unsourced, id="the researcher's against one nothing says the source of"),
])
async def test_a_website_confirmed_for_one_side_only_is_two_employers(js, confirmed, other):
    async with db.session_factory()() as session:
        await _two(session, ("Northwind", confirmed("https://www.corp.test")),
                   ("Contoso Partners", other("https://corp.test/jobs")))
    assert await _settle() == {"merged": 0}
    assert await _names() == ["Contoso Partners", "Northwind"] and await _questions() == []


async def test_confirmed_reads_the_researchers_actor_name_from_the_constant(js):
    """What the researcher stores when it takes a report's facts is what `website_confirmed` reads: a rename of the
    researcher cannot quietly un-confirm every website it found."""
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Acme")
        employer.research = {"report": {"facts": {"website": "https://www.acme.test"},
                                        "sources": {"website": "its About page"}}}
        await R.take_facts(session, employer)
        assert employer.website == "https://www.acme.test"
        assert employer.sources["website"]["by"] == R.BY, "stored under the researcher's name"
        assert employer_pairs.website_confirmed(employer)
        employer.sources = {"website": {**employer.sources["website"], "by": "analyst"}}
        assert not employer_pairs.website_confirmed(employer), "the analyst's word is not confirmation"
        employer.sources = {"website": {**employer.sources["website"], "by": R.BY}}
        employer.website = None
        assert not employer_pairs.website_confirmed(employer), "a record with no website has none confirmed"
        employer.website, employer.sources, employer.owner_fields = "https://www.acme.test", {}, ["website"]
        assert employer_pairs.website_confirmed(employer), "the owner's own word"


# --- the same job-system board, whichever system ----------------------------------------------------------------------


def _board(system: str) -> dict:
    """An address that is one customer's own on this system."""
    named = ats.SYSTEMS[system]
    if named.needs != ("slug",):
        return {part: f"{part}-acme" for part in named.needs}
    if named.customer_host:
        return {"host": "careers-acme.example.test"}
    if named.own_parts:
        return {named.own_parts[0]: "acme"}
    return {"slug": "acme"}


@pytest.mark.parametrize("system", sorted(ats.SYSTEMS))
async def test_the_same_own_board_with_alike_names_is_merged_for_every_system_that_has_one(js, system):
    """A job system whose board is a customer's own and never `complete`: two records on one of
    those could not make a pair while the pair finder had a definition of a board of its own."""
    async with db.session_factory()() as session:
        await _two(session, ("Acme Labs", {"ats": system, "board": _board(system)}),
                   ("Acme", {"ats": system, "board": _board(system)}))
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (MERGE, employer_match.SAME_ACCOUNT)
        assert pair.decision.words == f"they post on the same {ats.label(system)} job board and their names are alike"
    assert await _settle() == {"merged": 1}


async def test_a_board_that_was_only_guessed_is_no_evidence_and_one_given_to_unalike_names_is_no_pair(js):
    async with db.session_factory()() as session:
        await make_employer(session, "Acme Labs", ats="slugsys", board={"slug": "acme", "confirmed": False})
        await make_employer(session, "Acme", ats="slugsys", board={"slug": "acme", "confirmed": False})
        await make_employer(session, "Northwind", ats="feedsys", board={"slug": "shared"})
        await make_employer(session, "Contoso Partners", ats="feedsys", board={"slug": "shared"})
        await session.commit()
    async with db.session_factory()() as session:
        pairs = await employer_pairs.candidates(session, OWNER)
        assert [sorted([p.keep.name, p.away.name]) for p in pairs] == [["Acme", "Acme Labs"]], (
            "the guessed board is no evidence, but the names are alike: listed, not merged")
        assert [p.decision.rule for p in pairs] == [employer_match.NAMES]
    assert await _settle() == {"merged": 0} and await _questions() == []


# --- five website merges, on invented records --------------------------------------------------------------------


async def test_five_pairs_on_invented_records_are_three_merges_and_two_pairs_kept_as_two(js):
    """Merges by code, all on a website the per-role analyst had set. Some pairs had
    names that were forms of one name; one was a brand merged into its parent (the parent's name in brackets), one a
    local chapter merged into a company, the two names with nothing in common."""
    pairs = [
        (("Acme International, Inc.", "https://www.acme-one.test"), ("Acme", "https://acme-one.test/about")),
        (("Harbor Light Mutual", "https://www.harbor.test"), ("Mutual Harbor Light", "https://harbor.test")),
        (("Grand River Independent Distributor (GRID)", "https://www.grid.test"),
         ("GRID (Grand River Independent Distributor)", "https://grid.test/jobs")),
        (("Initech", "https://www.initech-corp.test"), ("Fabrikam Dental (Initech)", "https://initech-corp.test")),
        (("Globex", "https://www.globex.test"),
         ("Oakfield Chapter of the Example Engineers Society", "https://globex.test/chapter")),
    ]
    async with db.session_factory()() as session:
        for (big, big_site), (small, small_site) in pairs:
            await _two(session, (big, by_analyst(big_site)), (small, by_analyst(small_site)))
    assert await _settle() == {"merged": 3}
    assert await _questions() == []
    assert await _names() == [
        "Acme International, Inc.", "Fabrikam Dental (Initech)", "Globex", "Grand River Independent Distributor (GRID)",
        "Harbor Light Mutual", "Initech", "Oakfield Chapter of the Example Engineers Society"]
    lines = await _merge_lines()
    assert len(lines) == 3 and all("they share the website" in line.sentence for line in lines)


# --- two names that read the same -----------------------------------------------------------------------------------


async def test_two_records_whose_names_read_the_same_are_merged_by_the_same_name_rule(js):
    """The pair a changed key rule can leave: one key for two records (`employers.key_of` reads their names the same)
    while the keys they were stored under still differ."""
    async with db.session_factory()() as session:
        big = await make_employer(session, "Acme Labs")
        small = await make_employer(session, "Acme Co")
        session.add_all([make_role(big, "Lead A"), make_role(big, "Lead B"), make_role(small, "Lead C")])
        big.name = "Acme Corporation"  # the stored key stays "acme labs"; both names are now "acme" to key_of
        await session.commit()
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (MERGE, employer_match.SAME_NAME)
    assert await _settle() == {"merged": 1}
    (line,) = await _merge_lines()
    assert line.sentence.endswith("their names read the same once punctuation, brackets and endings like Inc. are set "
                                  "aside")


async def test_a_group_and_its_companies_under_two_old_keys_are_one_employer_to_the_same_name_rule(js):
    """Found when an application to "X Insurance Group, Inc." was invisible to a role at "X Insurance Companies": the
    plural ending "Companies" was part of the key, so the stored keys were "x insurance" and "x insurance companies"
    and the pair was no pair. Both names read "northwind insurance" now, so code merges them."""
    async with db.session_factory()() as session:
        group = await make_employer(session, "Northwind Insurance Group, Inc.")
        companies = await make_employer(session, "Northwind Insurance Companies Old")  # stored under its own key
        companies.name = "Northwind Insurance Companies"  # the name a role is found under; the stored key stays old
        session.add_all([make_role(group, "Lead A"), make_role(companies, "Lead B"), make_role(companies, "Lead C")])
        await session.commit()
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (MERGE, employer_match.SAME_NAME)
    assert await _settle() == {"merged": 1}
    async with db.session_factory()() as session:
        (one,) = (await session.scalars(select(Employer))).all()
        assert len((await session.scalars(select(Opportunity))).all()) == 3, "every role is kept"
        assert {one.name, *one.aliases} == {"Northwind Insurance Group, Inc.", "Northwind Insurance Companies"}


# --- an agency and the client it hired for ------------------------------------------------------------------------


async def _agency_and_client(session, how: str, shares: str, site=by_analyst):
    """A client hired through a staffing firm, and the firm, sharing a website or a job-system board. `how` says where
    the "via" tag that ties them sits: in a role's company text (`role`, where the app keeps it), in the client's
    name (`name`), in one of its aliases (`alias`), naming the firm by another name it goes by (`alias_key`), or on
    the firm's role naming the client (`reverse`). Returns the ids of the client and the firm."""
    tag = "Globex Staffing" if shares == "board" else "Staffco"  # a board needs names that look alike to be a pair
    if shares == "website":
        client_fields, firm_fields = site("https://www.corp.test"), site("https://corp.test/jobs")
    else:
        client_fields = {"ats": "feedsys", "board": {"slug": "globex"}}
        firm_fields = {"ats": "feedsys", "board": {"slug": "globex"}}
    client_name, firm_name = "Globex", tag
    client_company = firm_company = None
    if how == "role":
        client_company = f"Globex (via {tag})"
    elif how == "name":
        client_name = f"Globex via {tag}"
    elif how == "alias":
        client_fields["aliases"] = [f"Globex (via {tag})"]
    elif how == "alias_key":
        firm_name = f"{tag} International"
        firm_fields["aliases"] = [tag]
        client_company = f"Globex (via {tag})"
    else:  # reverse
        firm_company = f"{tag} (via Globex)"
    client = await make_employer(session, client_name, **client_fields)
    firm = await make_employer(session, firm_name, **firm_fields)
    session.add_all([make_role(client, "Lead A", company=client_company),
                     make_role(client, "Lead B", company=client_company),
                     make_role(firm, "Lead C", company=firm_company)])
    await session.commit()
    return client.id, firm.id


@pytest.mark.parametrize("shares", ["website", "board"])
@pytest.mark.parametrize("how", ["role", "name", "alias", "alias_key", "reverse"])
async def test_an_agency_and_its_client_are_never_merged_by_code_and_never_asked_about(js, how, shares):
    async with db.session_factory()() as session:
        client_id, firm_id = await _agency_and_client(session, how, shares)
    async with db.session_factory()() as session:
        assert await employer_pairs.candidates(session, OWNER) == [], "the app knows these are two employers"
    assert await _settle() == {"merged": 0}
    assert len(await _all(Employer)) == 2 and await _questions() == []
    async with db.session_factory()() as session:  # the researcher's word does not change that
        said = await employer_pairs.propose_merge(
            session, OWNER, client_id, firm_id, Evidence(SAME, ("Both are called Globex.",)), by="employer research")
        await session.commit()
    assert said.outcome == employer_pairs.KEPT_SEPARATE and len(await _all(Employer)) == 2
    assert [(t.state, t.answer) for t in await _questions()] == [(TASK_DONE, employer_pairs.SEPARATE)], (
        "remembered as separate, with no question")


async def test_an_agency_and_its_client_are_not_merged_even_on_a_website_the_owner_confirmed(js):
    async with db.session_factory()() as session:
        await _agency_and_client(session, "role", "website", site=by_owner)
    assert await _settle() == {"merged": 0}
    assert len(await _all(Employer)) == 2


@pytest.mark.parametrize("site", [by_analyst, by_owner])
async def test_a_record_with_a_via_tag_that_names_no_other_record_is_never_merged_by_code_and_nobody_is_asked(
        js, site):
    async with db.session_factory()() as session:
        await _two(session, ("Rivera Pharma and Company", site("https://www.riverapharma.test")),
                   ("Rivera Pharma via Staffco", site("https://riverapharma.test/jobs")))
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (NEVER, employer_match.TAGGED)
        assert "a staffing firm" in pair.decision.words
    assert await _settle() == {"merged": 0}
    assert await _questions() == [] and len(await _all(Employer)) == 2


# --- a brand and its parent -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("site", [by_analyst, by_owner, by_researcher])
async def test_a_brand_with_its_parent_in_brackets_is_never_merged_even_on_a_confirmed_shared_website(
        js, site):
    async with db.session_factory()() as session:
        await _two(session, ("Initech", site("https://www.initech-corp.test")),
                   ("Fabrikam Dental (Initech)", site("https://initech-corp.test/dental")))
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (NEVER, employer_match.BRAND)
        assert '"Fabrikam Dental (Initech)" holds "Initech" in brackets, which looks like a brand and its parent' == (
            pair.decision.words)
    assert await _settle() == {"merged": 0}
    assert await _questions() == []
    assert await _names() == ["Fabrikam Dental (Initech)", "Initech"]


async def test_a_brand_named_in_an_alias_is_a_brand_too(js):
    async with db.session_factory()() as session:
        await _two(session, ("Initech", by_owner("https://www.initech-corp.test")),
                   ("Fabrikam Dental", {**by_owner("https://initech-corp.test/dental"),
                                        "aliases": ["Fabrikam Dental Group (Initech)"]}))
    assert await _settle() == {"merged": 0}
    assert len(await _all(Employer)) == 2 and await _questions() == []


# --- what was decided stands ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("first", "second"), [
    pytest.param(("Acme Labs", by_analyst("https://www.corp.test")), ("Acme", by_analyst("https://corp.test")),
                 id="alike names on a website"),
    pytest.param(("Northwind", by_owner("https://www.corp.test")),
                 ("Contoso Partners", by_researcher("https://corp.test")), id="a website confirmed for both"),
    pytest.param(("Acme Labs", {"ats": "feedsys", "board": {"slug": "acme"}}),
                 ("Acme", {"ats": "feedsys", "board": {"slug": "acme"}}), id="alike names on one board"),
])
async def test_a_pair_the_owner_or_the_researcher_decided_stands_whatever_a_rule_says(js, first, second):
    async with db.session_factory()() as session:
        first_id, second_id = await _two(session, first, second)
        task = await inbox.raise_task(session, OWNER, key=employer_pairs.pair_key(first_id, second_id),
                                      kind=employer_pairs.KIND_MERGE, title="same?")
        task.state, task.answer = TASK_DONE, employer_pairs.SEPARATE
        await session.commit()
    async with db.session_factory()() as session:
        assert await employer_pairs.candidates(session, OWNER) == []
        said = await employer_pairs.propose_merge(
            session, OWNER, first_id, second_id, Evidence(SAME, ("One company.",)), by="employer research")
    assert said.outcome == employer_pairs.KEPT_SEPARATE
    assert await _settle() == {"merged": 0} and len(await _all(Employer)) == 2


# --- the night and the researcher use the same table ------------------------------------------------------------------


async def _alike_on_a_website(session):
    return await _two(session, ("Acme Labs", by_analyst("https://www.corp.test")),
                      ("Acme", by_analyst("https://corp.test/jobs")))


async def _unalike_on_an_analysts_website(session):
    return await _two(session, ("Northwind", by_analyst("https://www.corp.test")),
                      ("Contoso Partners", by_analyst("https://corp.test/jobs")))


async def _unalike_on_a_confirmed_website(session):
    return await _two(session, ("Northwind", by_owner("https://www.corp.test")),
                      ("Contoso Partners", by_researcher("https://corp.test/jobs")))


async def _names_alone(session):
    return await _two(session, ("Brightwater Insurance", {}), ("Brightwater", {}))


async def _a_careers_site_alone(session):
    return await _two(session, ("Northwind", {"careers_url": "https://careers.corp.test/jobs"}),
                      ("Contoso Partners", {"careers_url": "https://jobs.corp.test/openings"}))


async def _a_brand_on_a_confirmed_website(session):
    return await _two(session, ("Initech", by_owner("https://www.corp.test")),
                      ("Fabrikam Dental (Initech)", by_owner("https://corp.test/dental")))


async def _an_agency_on_a_website(session):
    return await _agency_and_client(session, "role", "website")


async def _a_tag_that_names_no_record(session):
    return await _two(session, ("Rivera Pharma and Company", by_analyst("https://www.corp.test")),
                      ("Rivera Pharma via Staffco", by_analyst("https://corp.test/jobs")))


async def _alike_on_a_board(session):
    return await _two(session, ("Acme Labs", {"ats": "slugsys", "board": {"slug": "acme"}}),
                      ("Acme", {"ats": "slugsys", "board": {"slug": "acme"}}))


SCENARIOS = [
    pytest.param(_alike_on_a_website, MERGE, id="alike names on a website"),
    pytest.param(_unalike_on_an_analysts_website, NEVER, id="unalike names on a website only the analyst set"),
    pytest.param(_unalike_on_a_confirmed_website, MERGE, id="unalike names on a website confirmed for both"),
    pytest.param(_names_alone, NEVER, id="alike names alone"),
    pytest.param(_a_careers_site_alone, NEVER, id="a careers site alone"),
    pytest.param(_a_brand_on_a_confirmed_website, NEVER, id="a brand and its parent on a confirmed website"),
    pytest.param(_an_agency_on_a_website, NEVER, id="an agency and its client"),
    pytest.param(_a_tag_that_names_no_record, NEVER, id="a via tag that names no record"),
    pytest.param(_alike_on_a_board, MERGE, id="alike names on one board"),
]


@pytest.mark.parametrize("flow", ["the night", "the researcher"])
@pytest.mark.parametrize(("build", "action"), SCENARIOS)
async def test_the_night_and_the_researcher_use_the_same_table(js, build, action, flow):
    """`settle` (the night, the import, the researcher's last pass) and `propose_merge` (the researcher's word that two
    records are the same) end the same way for the same pair: merged only when the table says merge, otherwise two
    employers and nobody asked."""
    async with db.session_factory()() as session:
        first, second = await build(session)
    if flow == "the night":
        done = await _settle()
    else:
        async with db.session_factory()() as session:
            said = await employer_pairs.propose_merge(
                session, OWNER, first, second, Evidence(SAME, ("They share a headquarters.",)), by="employer research")
            await session.commit()
        assert said.outcome in (employer_pairs.MERGED, employer_pairs.KEPT_SEPARATE)
        done = {"merged": int(said.outcome == employer_pairs.MERGED)}
    assert done == {"merged": int(action == MERGE)}
    assert len(await _all(Employer)) == (1 if action == MERGE else 2)
    assert [t for t in await _questions() if t.state == TASK_OPEN] == []


async def test_the_researcher_saying_same_merges_only_when_the_table_says_merge(js):
    """The researcher's word alone merges nothing: a pair the table keeps as two stays two, with no question and
    nothing remembered; an unsure researcher on a pair the table would merge merges it (the criteria hold)."""
    async with db.session_factory()() as session:
        unlike = await _unalike_on_an_analysts_website(session)
        alike = await _two(session, ("Acme Labs", by_analyst("https://www.acme-corp.test")),
                           ("Acme", by_analyst("https://acme-corp.test/jobs")))
    async with db.session_factory()() as session:
        same = await employer_pairs.propose_merge(
            session, OWNER, *unlike, Evidence(SAME, ("Both name one headquarters.",)), by="employer research")
        unsure = await employer_pairs.propose_merge(
            session, OWNER, *alike, Evidence(UNSURE, ("Could be a subsidiary.",)), by="employer research")
        await session.commit()
    assert (same.outcome, unsure.outcome) == (employer_pairs.KEPT_SEPARATE, employer_pairs.MERGED)
    assert same.task is None and await _questions() == []
    assert len(await _all(Employer)) == 3
    (line,) = await _merge_lines()
    assert "research found them the same" not in line.sentence, "unsure is not a finding that they are the same"


async def test_a_same_from_the_researcher_on_alike_names_alone_keeps_two_and_remembers_nothing(js):
    async with db.session_factory()() as session:
        first, second = await _names_alone(session)
    async with db.session_factory()() as session:
        said = await employer_pairs.propose_merge(
            session, OWNER, first, second, Evidence(SAME, ("One company.",)), by="employer research")
        await session.commit()
    assert said.outcome == employer_pairs.KEPT_SEPARATE and said.task is None
    assert await _questions() == [] and len(await _all(Employer)) == 2


async def test_a_pair_kept_as_two_is_merged_later_when_both_websites_are_confirmed_and_nobody_is_asked(js):
    async with db.session_factory()() as session:
        first, second = await _unalike_on_an_analysts_website(session)
    assert await _settle() == {"merged": 0} and await _questions() == []
    async with db.session_factory()() as session:
        for employer_id in (first, second):  # the researcher confirms both websites
            employer = await session.get(Employer, employer_id)
            employer.sources = {"website": _source(R.BY)}
        await session.commit()
    assert await _settle() == {"merged": 1}
    assert len(await _all(Employer)) == 1 and await _questions() == []


async def test_a_pair_closed_as_withdrawn_is_merged_later_on_confirmed_websites_without_a_question(js):
    async with db.session_factory()() as session:
        first, second = await _unalike_on_an_analysts_website(session)
        await inbox.raise_task(session, OWNER, key=employer_pairs.pair_key(first, second),
                               kind=employer_pairs.KIND_MERGE, title="same?")
        await session.commit()
    assert await _settle() == {"merged": 0}
    (closed,) = await _questions()
    assert (closed.state, closed.answer) == (TASK_SKIPPED, employer_pairs.WITHDRAWN)
    async with db.session_factory()() as session:
        for employer_id in (first, second):
            employer = await session.get(Employer, employer_id)
            employer.sources = {"website": _source(R.BY)}
        await session.commit()
    assert await _settle() == {"merged": 1}
    assert len(await _all(Employer)) == 1


async def test_a_question_the_owner_already_answered_is_left_as_it_was(js):
    async with db.session_factory()() as session:
        first, second = await _unalike_on_an_analysts_website(session)
        await inbox.raise_task(session, OWNER, key=employer_pairs.pair_key(first, second),
                               kind=employer_pairs.KIND_MERGE, title="same?", detail="Before this rule.")
        await session.commit()
    async with db.session_factory()() as session:
        (question,) = (await session.scalars(select(Task).where(Task.kind == employer_pairs.KIND_MERGE))).all()
        question.state, question.answer = TASK_DONE, employer_pairs.SEPARATE
        await session.commit()
    async with db.session_factory()() as session:
        said = await employer_pairs.propose_merge(
            session, OWNER, first, second, Evidence(SAME, ("One company.",)), by="employer research")
        await session.commit()
    assert said.outcome == employer_pairs.KEPT_SEPARATE
    (kept,) = await _questions()
    assert (kept.state, kept.answer, kept.detail) == (TASK_DONE, employer_pairs.SEPARATE, "Before this rule.")


# --- evidence, not a word that matches --------------------------------------------------------------------------------


def _alike(first: str, second: str, held: dict[str, int] | None = None) -> str | None:
    return employer_pairs.names_alike(employer_pairs.name_parts(first), employer_pairs.name_parts(second),
                                      employer_pairs.Rarity(held or {}))


async def test_american_express_and_its_travel_business_are_no_pair_neither_merged_nor_asked(js):
    """The owner's example: one name beginning with the other is not enough when the extra words are another company's
    name ("Global Business Travel"), not words that say what kind of employer it is."""
    assert _alike("American Express", "American Express Global Business Travel") is None
    async with db.session_factory()() as session:
        await _two(session, ("American Express", {}), ("American Express Global Business Travel", {}))
    async with db.session_factory()() as session:
        assert await employer_pairs.candidates(session, OWNER) == []
    assert await _settle() == {"merged": 0}
    assert len(await _all(Employer)) == 2 and await _questions() == []


@pytest.mark.parametrize(("first", "second", "how"), [
    pytest.param("Brightwater", "Brightwater Insurance", "begins", id="one name and a descriptor"),
    pytest.param("Riverton Health System", "Riverton", "begins", id="one name and two descriptors"),
    pytest.param("Grand River Distributor", "Distributor Grand River", "the same words", id="the same words"),
    pytest.param("Contoso Bank Riverton", "Contoso Riverton", "nearly", id="a descriptor inside the name"),
])
def test_near_identical_names_are_alike(first, second, how):
    assert _alike(first, second) == how


@pytest.mark.parametrize(("first", "second", "held"), [
    pytest.param("Summit Health Partners", "Summit Dental", {"summit": 6}, id="only a common word and descriptors"),
    pytest.param("Pine Summit", "Summit Group", {"summit": 6}, id="a common word in another place"),
    pytest.param("Ashford", "Ashford Risk", None, id="a word that is not a descriptor added"),
    pytest.param("Health Partners", "Health Partners Group", None, id="nothing but descriptors"),
])
def test_names_that_share_only_common_words_or_add_another_name_are_not_alike(first, second, held):
    assert _alike(first, second, held) is None


async def test_a_near_identical_name_is_never_merged_by_code_and_nobody_is_asked(js):
    async with db.session_factory()() as session:
        await _two(session, ("Contoso Bank Riverton", {}), ("Contoso Riverton", {}))
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (NEVER, employer_match.NAMES)
        assert pair.reasons == ['"Contoso Riverton" and "Contoso Bank Riverton" are nearly the same name']
    assert await _settle() == {"merged": 0}
    assert len(await _all(Employer)) == 2 and await _questions() == []


async def test_a_word_many_employers_hold_is_silent_and_never_a_pair(js):
    async with db.session_factory()() as session:
        for name in ("Summit Health Partners", "Summit Dental", "Summit One", "Ridge Summit", "Pine Summit"):
            await make_employer(session, name)
        await session.commit()
    async with db.session_factory()() as session:
        assert await employer_pairs.candidates(session, OWNER) == []
    assert await _settle() == {"merged": 0}


async def test_the_same_tenantsys_tenant_with_a_shared_distinctive_word_merges(js):
    tenant = {"ats": "tenantsys", "board": {"tenant": "northwind", "shard": "wd5", "site": "External"}}
    async with db.session_factory()() as session:
        await _two(session, ("Northwind Traders", dict(tenant)), ("Northwind Global Logistics", dict(tenant)))
    async with db.session_factory()() as session:
        (pair,) = await employer_pairs.candidates(session, OWNER)
        assert (pair.decision.action, pair.decision.rule) == (MERGE, employer_match.SAME_ACCOUNT)
        assert pair.decision.words == 'they post on the same Tenantsys job board and both names hold "Northwind"'
    assert await _settle() == {"merged": 1}
    (line,) = await _merge_lines()
    assert 'both names hold "Northwind"' in line.sentence


async def test_the_same_tenant_under_names_with_no_word_in_common_is_still_no_pair(js):
    tenant = {"ats": "tenantsys", "board": {"tenant": "shared", "shard": "wd5", "site": "External"}}
    async with db.session_factory()() as session:
        await _two(session, ("Northwind Traders", dict(tenant)), ("Contoso Partners", dict(tenant)))
    assert await _settle() == {"merged": 0}


@pytest.mark.parametrize("link", [
    pytest.param("https://www.board.test/company/acme/jobs", id="the same Exboard page"),
    pytest.param("https://www.listings.test/cmp/acme/jobs", id="the same Exlistings page"),
    pytest.param("https://www.hostsys.test/", id="the root of an ATS with no tenant"),
    pytest.param("https://www.tenantsys.test/", id="the root of Tenantsys with no tenant"),
    pytest.param("https://careers.feedsys.test/", id="a vendor's careers platform"),
])
async def test_a_job_board_or_an_ats_host_with_no_tenant_gives_no_signal(js, link):
    first = Employer(name="Northwind Traders", website=link, careers_url=link)
    second = Employer(name="Contoso Partners", website=link, careers_url=link)
    assert pair_signals(first, second) == {}
    async with db.session_factory()() as session:
        await _two(session, ("Northwind Traders", {"website": link, "careers_url": link}),
                   ("Contoso Partners", {"website": link, "careers_url": link}))
    assert await _settle() == {"merged": 0}


async def test_different_confirmed_websites_are_never_merged_and_never_asked_about(js):
    async with db.session_factory()() as session:
        first, second = await _two(session, ("Acme Labs", by_owner("https://www.acme-labs.test")),
                                   ("Acme", by_researcher("https://acme.test")))
    async with db.session_factory()() as session:
        assert await employer_pairs.candidates(session, OWNER) == [], "alike names, but the websites say otherwise"
    assert await _settle() == {"merged": 0}
    async with db.session_factory()() as session:
        said = await employer_pairs.propose_merge(
            session, OWNER, first, second, Evidence(SAME, ("Both are called Acme.",)), by="employer research")
        await session.commit()
    assert said.outcome == employer_pairs.KEPT_SEPARATE
    assert len(await _all(Employer)) == 2 and await _questions() == []


async def test_every_open_question_is_closed_with_a_note_and_is_no_decision(js):
    """Questions raised before the owner's decision of 2026-11-13, with the researcher's words on them or not, are all
    closed, and a pair that is two employers is not merged. A closed one is not the owner's answer: new evidence
    still merges the pair."""
    async with db.session_factory()() as session:
        stale = await _two(session, ("Ashford", {}), ("Ashford Risk", {}))
        kept = await _two(session, ("Brightwater", {}), ("Brightwater Insurance", {}))
        researched = await _two(session, ("Northwind", {}), ("Contoso Partners", {}))
        for (a, b), detail in ((stale, "Old rule."), (kept, "Still asked."),
                               (researched, "The researcher found: one headquarters.")):
            await inbox.raise_task(session, OWNER, key=employer_pairs.pair_key(a, b), kind=employer_pairs.KIND_MERGE,
                                   title="same?", detail=detail)
        await session.commit()
    assert await _settle() == {"merged": 0}
    for pair in (stale, kept, researched):
        closed = {t.key: t for t in await _questions()}[employer_pairs.pair_key(*pair)]
        assert (closed.state, closed.answer, closed.note, closed.assignee) == (
            TASK_SKIPPED, employer_pairs.WITHDRAWN, employer_pairs.WITHDRAWN_NOTE, "resumebot")
    assert not [t for t in await _questions() if t.state == TASK_OPEN]
    async with db.session_factory()() as session:
        assert frozenset(stale) not in await employer_pairs.decided(session, OWNER), "closed by the app, not answered"
        for employer_id in stale:  # new evidence: one Slugsys account
            employer = await session.get(Employer, employer_id)
            employer.ats, employer.board = "slugsys", {"slug": "ashford"}
        await session.commit()
    assert await _settle() == {"merged": 1}
    assert len(await _all(Employer)) == 5


async def test_candidates_still_lists_the_pairs_kept_as_two_for_the_researcher_and_settle_writes_nothing_for_them(js):
    async with db.session_factory()() as session:
        await _unalike_on_an_analysts_website(session)  # A1
        await _two(session, ("Brightwater Insurance", {}), ("Brightwater", {}))  # A2
    async with db.session_factory()() as session:
        rules = sorted(c.decision.rule for c in await employer_pairs.candidates(session, OWNER))
        assert rules == [employer_match.NAMES, employer_match.WEBSITE_UNCONFIRMED]
        assert await employer_pairs.nothing_to_settle(session, OWNER), "no merge to make, no question to close"
    assert await _settle() == {"merged": 0}
    assert await _questions() == [] and len(await _all(Employer)) == 4


async def test_a_merge_that_fails_is_logged_not_asked_and_the_other_merges_go_on(js, monkeypatch, caplog):
    async with db.session_factory()() as session:
        await _two(session, ("Acme Labs", by_analyst("https://www.acme-corp.test")),
                   ("Acme", by_analyst("https://acme-corp.test/jobs")))
        await _two(session, ("Globex Labs", by_analyst("https://www.globex-corp.test")),
                   ("Globex", by_analyst("https://globex-corp.test/jobs")))
    real = employer_pairs.employer_records.merge

    async def flaky(session, owner, keep_id, away_id, **kw):
        if "Acme" in (await session.get(Employer, keep_id)).name:
            raise RuntimeError("cannot merge")
        return await real(session, owner, keep_id, away_id, **kw)

    monkeypatch.setattr(employer_pairs.employer_records, "merge", flaky)
    assert await _settle() == {"merged": 1}
    names = await _names()
    assert "Acme" in names and "Acme Labs" in names and len(names) == 3, "the Acme pair stays two, the other merged"
    assert await _questions() == []
    assert "merging two employer records failed" in caplog.text
