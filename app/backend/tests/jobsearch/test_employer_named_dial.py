"""The Employer named dial (docs/PRD.md 46.4; the owner, 2026-11-03): a Must on Hard pass that screens out an anonymous
posting, one whose employer is hidden. It restores the owner's "No anonymous postings. A posting must name a company"
that migration 0124 dropped with every reader-judged line. The app tells from what it holds: a name that is a
placeholder ("Confidential", "A leading healthcare company"), a staffing firm whose text speaks only of "our client",
and a blank. No list of companies lives in code. Every name, place and sentence here is made up."""

import copy
from datetime import date

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import posting_facts as pf
from app.jobsearch.measures import dials, employer_name
from app.jobsearch.profile import schema as p
from tests.jobsearch.test_regate import GATES, by_ext, by_slug, judge, posting, role
from tests.support import OWNER, PROFILE, load_profile

TODAY = date(2026, 11, 3)


# --- the name -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "Confidential", "Confidential Company", "CONFIDENTIAL EMPLOYER", "Company Confidential",
    "Confidential - Healthcare",
    "Anonymous", "Anonymous Employer", "Undisclosed", "undisclosed client", "Unknown", "N/A", "TBD", "None", "Company",
    "Hiring Company", "Our Client", "Stealth Startup", "Stealth Mode", "Private Company", "Not disclosed",
    "A Leading Healthcare Company", "A Global Financial Services Firm", "An Established Manufacturer",
    "Fortune 500 Company", "A Fortune 100 Client", "Top 10 Bank",
    "Staffco (Undisclosed Client)", "Recruitco (recruiter, client undisclosed)", "Searchco (client confidential)",
    "", "   ", None,
])
def test_a_name_that_is_a_placeholder_or_blank_names_no_employer(name):
    assert employer_name.hidden(name)


@pytest.mark.parametrize("name", [
    "Fabrikam", "Fabrikam Labs, Inc.", "Wingtip Toys (via Staffco)", "A Place for Neighbours", "Stealth Health",
    "Unknown Pleasures Records", "Anonymous Labs", "The Leading Edge Cafe", "Private Eye Press", "fabrikam.example",
    "jobs.example.test", "Top Ten Tools", "Global Holdings Group",
])
def test_a_real_looking_name_is_never_judged_hidden(name):
    assert employer_name.hidden(name) is None


def test_the_reason_says_what_the_name_is():
    assert employer_name.hidden("Confidential Company") == 'the company is listed as "Confidential Company"'
    assert employer_name.hidden(None) == "the posting gives no company"
    assert employer_name.hidden("  ") == "the posting gives no company"


# --- a staffing firm that names no client ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentence", [
    "Our client is seeking a Senior Network Engineer to support the data center.",
    "Our client, a leading healthcare company, is looking for an analyst.",
    "Our client is putting analytics at the center of its strategy and is seeking an architect.",
    "Our client's data team is hiring a pipeline engineer.",
    "Our client is searching for a controller to run the plant accounts.",
    "Fabrikam Staffing has a client in Oakfield, ST that is seeking a Data Manager.",
])
def test_a_sentence_where_an_unnamed_client_is_the_one_hiring_is_an_unnamed_employer(sentence):
    found = pf.unnamed_client_from_text(f"Data Manager\nPosition overview\n{sentence}\nResponsibilities\n- Lead.")
    assert found is not None and found.quote == sentence and found.by == pf.CODE


@pytest.mark.parametrize("sentence", [
    "Our client, Wingtip Toys, is seeking a Data Manager.",
    "Our client Wingtip Toys is hiring a controller.",
    "Our clients are seeking measurable value from analytics.",
    "You will work with our client teams to ship the product.",
    "The client is looking to understand what is possible.",
    "We help clients grow, and we are hiring a Data Manager.",
    "Our client service team is seeking a coordinator.",
])
def test_a_sentence_that_names_the_client_or_speaks_of_clients_in_general_is_no_unnamed_employer(sentence):
    assert pf.unnamed_client_from_text(f"Data Manager\nPosition overview\n{sentence}\nMore words.") is None


def test_the_employer_named_fact_is_stored_only_when_the_text_says_something_the_name_does_not():
    said = "Our client is seeking a Data Manager."
    found = pf.extract(f"Data Manager\n{said}\n", company="Staffco")
    assert found.facts()["employer_named"] == {"value": "no", "quote": said, "by": "code"}
    # A hidden name over a text that names the employer: the text names it.
    named = pf.extract("Data Manager\nCompany: Wingtip Toys\nLead the team.\n", company="Confidential Company")
    assert named.facts()["employer_named"]["value"] == "yes"
    assert "Wingtip Toys" in named.facts()["employer_named"]["quote"]
    # The text's own label is a placeholder.
    label = pf.extract("Data Manager\nCompany: Confidential\nLead the team.\n", company="Staffco")
    assert label.facts()["employer_named"]["value"] == "no"
    assert "Confidential" in label.facts()["employer_named"]["quote"]
    # Nothing to add: a real name over a quiet text, and a hidden name over a quiet text (the name alone speaks).
    assert "employer_named" not in pf.extract("Data Manager\nLead the team.\n", company="Fabrikam").facts()
    assert "employer_named" not in pf.extract("Data Manager\nLead the team.\n", company="Confidential").facts()
    assert "employer_named" not in pf.extract("Data Manager\nLead the team.\n").facts()


# --- the dial -------------------------------------------------------------------------------------------------------


def test_the_dial_is_a_job_dial_with_a_must_and_no_value_to_set():
    dial = dials.DIALS["employer-named"]
    assert (dial.side, dial.fact, dial.kind, dial.choices) == ("job", "employer_named", "flag", ("must",))
    assert dials.says("employer_named", "=", "yes") == "The posting names its employer."
    assert dials.is_dial("employer_named", "=") and not dials.is_dial("employer_named", "in")


def rules_with() -> gating.Rules:
    data = copy.deepcopy(PROFILE)
    gate = {"id": "dial-employer-named-must", "kind": "job_fact", "fact": "employer_named", "op": "=", "value": "yes"}
    data["gates"] = {"location": {}, "other": [gate]}
    data["scoring"] = {"asks": []}
    return gating.Rules.of(p.Profile.model_validate(data), today=TODAY, hard_pass=1, criteria=1)


def subject(company: str | None, **stored) -> gating.Subject:
    return gating.Subject(company=company, location=None, work_mode=None, comp=None, comp_min=None, comp_max=None,
                          posted=None, found=None, stored=stored or None)


def line_for(s: gating.Subject) -> gating.Line:
    return next(line for line in gating.results(s, rules_with()) if line.id == "dial-employer-named-must")


def test_a_posting_that_gives_a_real_name_passes_and_one_that_gives_a_placeholder_or_none_fails():
    named = line_for(subject("Fabrikam"))
    assert named.result == gating.PASS and named.fact == "the posting names its employer"
    hidden = line_for(subject("Confidential Company"))
    assert hidden.result == gating.FAIL
    assert hidden.fact == 'the posting names no employer: the company is listed as "Confidential Company"'
    assert line_for(subject(None)).result == gating.FAIL
    assert line_for(subject("")).result == gating.FAIL


def test_the_dial_never_waits_for_its_fact():
    assert all(line_for(subject(c)).result in (gating.PASS, gating.FAIL) for c in ("Fabrikam", "Confidential", None))


def test_what_the_text_says_stands_over_the_name():
    said = {"employer_named": {"value": "no", "quote": "Our client is seeking a Data Manager.", "by": "code"}}
    line = line_for(subject("Staffco", **said))
    assert line.result == gating.FAIL and line.fact == (
        "the posting names no employer: Our client is seeking a Data Manager.")
    named = {"employer_named": {"value": "yes", "quote": "Company: Wingtip Toys", "by": "code"}}
    assert line_for(subject("Confidential Company", **named)).result == gating.PASS


def test_a_fact_the_text_stored_that_is_neither_yes_nor_no_is_not_read():
    junk = {"employer_named": {"value": "maybe", "quote": "q", "by": "code"}}
    assert line_for(subject("Fabrikam", **junk)).result == gating.PASS
    assert line_for(subject("Confidential", **junk)).result == gating.FAIL


def test_a_posting_and_a_role_are_read_by_their_companies():
    class Row:  # the columns the subjects read
        company = "Confidential"
        location = move_type = comp = posted = found = target_role = None
        gate_answers = analysis = gate_facts = employer_id = None
        title = "Data Manager"
        work_mode = comp_min = comp_max = url = apply_url = first_seen = None

    assert line_for(gating.role_subject(Row(), None)).result == gating.FAIL
    assert line_for(gating.posting_subject(Row(), None)).result == gating.FAIL
    assert line_for(gating.report_subject({"company": "Confidential"}, None, None)).result == gating.FAIL
    assert line_for(gating.report_subject({"company": "Fabrikam"}, None, None)).result == gating.PASS


# --- a save of Hard pass --------------------------------------------------------------------------------------------


async def test_saving_the_dial_stops_the_postings_and_roles_with_a_hidden_employer_and_leaves_the_rest(js, client):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates={"location": {}})
        fab = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([posting(fab, "named", 100_000),
                         posting(fab, "hidden", 100_000, company="Confidential Company")])
        said = {"facts": {"employer_named": {"value": "no", "quote": "Our client is seeking an analyst.",
                                             "by": "code"}}}
        session.add_all([role("a", stages.SECOND_IMPRESSION, company="A Leading Healthcare Company"),
                         role("b", stages.SECOND_IMPRESSION),
                         role("c", stages.SECOND_IMPRESSION, gate_facts=said)])
        await session.flush()
        await judge(session)
        await session.commit()

    gate = {"id": "dial-employer-named-must", "kind": "job_fact", "section": "posting", "fact": "employer_named",
            "op": "=", "value": "yes"}
    saved = await client.put(GATES, json={"data": {"location": {}, "other": [gate]}, "why": "made-up test"})
    assert saved.status_code == 200
    async with db.session_factory()() as session:
        roles, posts = await by_slug(session), await by_ext(session)
        assert roles["a"].status == roles["c"].status == stages.GATED_OUT
        assert roles["a"].screened_out_gate_id == "dial-employer-named-must"
        assert "Leading Healthcare" in roles["a"].screened_out_reason
        assert roles["b"].status == stages.SECOND_IMPRESSION
        assert posts["hidden"].filtered_by is not None and posts["hidden"].stop_gate_id == "dial-employer-named-must"
        assert posts["named"].filtered_by is None
