"""What counts as a posting's text (`postings/whole.py`, docs/PRD.md 39.29, the owner, 2026-10-14): whole, verbatim,
from a source. The rule is pure and is asked in one place by every way a text comes in; these tests hold the rule
itself, the two thresholds it keeps on purpose, and that each way in refuses a fragment and a page dump and keeps
nothing. Every name, address and sentence is made up."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import String, literal, select

from app import db
from app.jobsearch import ats
from app.jobsearch.browser import add_link
from app.jobsearch.browser import handback as hb
from app.jobsearch.browser.pace import BrowserWorkError
from app.jobsearch.models import JobPosting, Opportunity, RawPosting
from app.jobsearch.postings import cleansed, whole
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.postings import text as posting_text
from app.sites import registry
from tests.builders import browser_card, browser_world, scan_hand_in
from tests.jobsearch.made_up_sites_b import made_up_boards  # noqa: F401 (used through pytestmark)
from tests.support import EXAMPLE_SYSTEM, OWNER, POSTING_WORDS

pytestmark = pytest.mark.usefixtures("made_up_boards")

PARAGRAPHS = [f"Paragraph {i} of the posting says one thing about the work and nothing else." for i in range(1, 21)]
WHOLE = "\n".join(PARAGRAPHS)
SEEN = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)
#: A second job system the app reads, besides the one the shared builders use.
OTHER_SYSTEM = next(r.key for r in registry.current().runbooks if r.kind == "job_system" and r.key != EXAMPLE_SYSTEM)
FRAGMENT = "Programs Lead. We are hiring. Sign in to see more."
CUT = WHOLE[:900] + " ..."
WALL = WHOLE[:600] + "\nSign in to view this job."
PAGE_DUMP = "<!DOCTYPE html>\nPrograms Lead - Contoso\nSkip to content\n-\nOpen positions\n" + POSTING_WORDS


# --- why_not: the rule ------------------------------------------------------------------------------------------------


def test_a_text_is_whole_only_when_nothing_says_it_is_not():
    assert whole.why_not(WHOLE) is None and whole.why_not(POSTING_WORDS) is None
    assert "snippet" in whole.why_not("A short listing.")
    assert whole.why_not(None) == "no text" and whole.why_not("   ") == "no text"
    for end in ("...", "…", "See more", "… more", "show more"):
        assert "cut off" in whole.why_not(WHOLE + " " + end), end
    assert "wall" in whole.why_not(WHOLE[:600] + "\nSign in to view this job.")
    long = WHOLE * 4  # a posting may say "sign in" in its middle: only a short text is taken for a wall
    assert whole.why_not(long + "\nSign in to your account to apply.\n" + WHOLE) is None


@pytest.mark.parametrize("text", [
    "<!DOCTYPE html>\n" + POSTING_WORDS, "<html><body>" + POSTING_WORDS, "Skip to content\n" + POSTING_WORDS,
    "Programs Lead\nLog in\n- English\n- French\n- German\n- Spanish\n" + POSTING_WORDS,
    POSTING_WORDS + "\n<div class=\"x\">", POSTING_WORDS + "\n<job-card data-id=\"1\">",
], ids=["doctype", "html tag", "skip link", "language menu beside log in", "a tag", "a custom element"])
def test_a_page_read_whole_is_a_dump_and_never_a_posting(text):
    assert whole.page_dump(text) and "page read whole" in (whole.why_not(text) or "")


@pytest.mark.parametrize("text", [
    POSTING_WORDS, POSTING_WORDS + "\nLanguages\n- English\n- French\n- German", "Log in to apply.\n" + POSTING_WORDS,
    None, ""], ids=["a posting", "languages without a log in line", "log in without languages", "none", "empty"])
def test_a_posting_that_only_names_a_menu_word_is_not_a_dump(text):
    assert not whole.page_dump(text)


def test_a_text_no_reply_could_hold_is_refused_with_its_size_and_never_cut():
    just = "x" * whole.CEILING_CHARS
    assert whole.too_long(just, "text") is None and whole.too_long(None, "text") is None
    said = whole.too_long(just + "y", "the description")
    assert said is not None and f"{whole.CEILING_CHARS + 1:,}" in said and "nothing was cut" in said
    assert whole.too_long(" " + just + " ", "text") is None, "a text is measured as it is kept, stripped"


#: The texts `thin_sql` must call as `thin` does, by name. The later ones are where its quick look at a text's first
#: characters cannot settle it (white space at either edge or inside, a character of four bytes) and the whole is read.
_THIN_SHAPES = {
    "none": None, "empty": "", "blank": "   \n", "299": "x" * 299, "300": "x" * 300,
    "padded under 300": "x" * 290 + " " * 30, "299 inside white space": " " + "x" * 299 + "\n",
    "300 with a blank line inside": "x" * 150 + "\n\n" + "x" * 150, "300 inside white space": "\t" + "x" * 300 + "\r\n",
    "300 after 400 spaces": " " * 400 + "x" * 300, "299 after 400 spaces": " " * 400 + "x" * 299,
    "300 before 400 spaces": "x" * 300 + " " * 400, "299 before 400 spaces": "x" * 299 + " " * 400,
    "300 round 400 spaces inside": "x" * 150 + " " * 400 + "x" * 150, "1,000": "x" * 1000,
    "299 four-byte": "𝔘" * 299, "300 four-byte": "𝔘" * 300, "299 four-byte in white space": " \f" + "𝔘" * 299 + "\v ",
    "300 four-byte in white space": " \f" + "𝔘" * 300 + "\v ", "long": "word " * 4000}


@pytest.mark.parametrize("text", list(_THIN_SHAPES.values()), ids=list(_THIN_SHAPES))
async def test_thin_in_sql_is_thin_in_python(js, text):
    """The rule is one: a query that picks postings by it (`thin_sql`) agrees with `thin` on every text, so a posting
    chosen to be read is one the rule calls thin, and one the rule calls whole is never read again."""
    async with db.session_factory()() as session:
        in_sql = await session.scalar(select(whole.thin_sql(literal(text, type_=String))))
    assert in_sql is whole.thin(text)


async def test_thin_in_sql_is_thin_in_python_for_a_text_that_is_stored(js):
    """The same on stored descriptions, which a database keeps packed: the rule a query asks of a column."""
    async with db.session_factory()() as session:
        for name, text in _THIN_SHAPES.items():
            session.add(Opportunity(owner=OWNER, slug=name, company="Fabrikam", title=name, origin="scout",
                                    match_key=name, found=SEEN.date(), description=text))
        await session.commit()
    async with db.session_factory()() as session:
        thin = set(await session.scalars(select(Opportunity.slug).where(whole.thin_sql(Opportunity.description))))
    assert thin == {name for name, text in _THIN_SHAPES.items() if whole.thin(text)}


async def test_a_text_is_thin_by_its_first_characters_when_they_settle_it_and_only_otherwise_by_the_whole(
        js, transactions):
    """Finding the thin postings once measured the text of
    every one, which unpacks each stored text. A text whose first
    characters already hold `MIN_TEXT` between their edges is not thin, whatever follows, so the query looks at only a
    prefix of such a text (`substr` unpacks only that much) and measures the whole where the prefix does not say."""
    async with db.session_factory()() as session:
        await session.scalar(select(whole.thin_sql(literal("x" * 400, type_=String))))
    asked = [s for s in transactions.timeline if "trim(" in s]
    assert asked and all("substr(" in s for s in asked), "the whole text is measured only where its start does not say"


def test_a_text_handed_in_whole_is_refused_for_the_first_reason_it_has_and_one_that_fits_is_not():
    """`refusal` is the question every way a whole text is handed in asks (`take(Capture)`, a board's own posting), as
    one call: a way in cannot ask `why_not` and forget `too_long`, and says the reason `take` acted on."""
    over = "Paragraph one says one thing about the work and nothing else. " * (whole.CEILING_CHARS // 60 + 10)
    assert whole.why_not(over) is None, "the text is whole but too long"
    assert whole.refusal(WHOLE, "the posting's text") is None
    assert whole.refusal(FRAGMENT, "the posting's text") == whole.why_not(FRAGMENT)
    assert whole.refusal(over, "the posting's text") == whole.too_long(over, "the posting's text")
    assert whole.refusal(None, "the posting's text") == "no text"


# --- of_posting: the text a posting holds, by where it came from ------------------------------------------------------


def _posting(text, system=EXAMPLE_SYSTEM, detail_at=SEEN) -> JobPosting:
    return JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats=system, ext_id="1", company="Fabrikam",
                      title="Programs Lead", match_key="fabrikam|programs lead", first_seen=SEEN, last_seen=SEEN,
                      links=[], text=text, detail_at=detail_at)


def test_a_posting_holds_a_text_only_from_a_source_whole_and_from_300_characters():
    snippet_300 = "x" * (cleansed.MIN_TEXT - 1)
    answer_300 = ("A job system's own answer, past a snippet, short of what a page must show. "
                  * 5)[:cleansed.MIN_TEXT + 20]
    assert whole.thin(snippet_300) and not whole.thin(answer_300)
    assert whole.of_posting(_posting("  " + WHOLE + "  ")) == WHOLE, "stripped, verbatim"
    assert whole.of_posting(_posting(snippet_300)) == "", "a snippet is no text"
    assert whole.of_posting(_posting(PAGE_DUMP)) == "", "a page read whole is no text"
    assert whole.of_posting(_posting(None)) == ""
    for system in (ats.OWN_SITE, OTHER_SYSTEM, EXAMPLE_SYSTEM):
        assert whole.of_posting(_posting(WHOLE, system)) == WHOLE, system
    for system in (ats.BROWSER, ats.SCOUT, ats.IMPORT, ats.OWNER, ats.JOB_SITE, "boardone"):
        assert whole.of_posting(_posting(WHOLE, system)) == "", f"{system} is no source's own system"
    assert whole.of_posting(_posting(WHOLE, ats.BROWSER, detail_at=None)) == "", "a browser snippet, never opened"


def test_the_two_thresholds_stay_two_a_systems_own_answer_is_kept_at_300_and_a_page_is_not():
    answer = ("A job system's own answer, past a snippet, short of what a page must show. " * 5)[:400]
    assert 300 <= len(answer) < 500
    assert whole.of_posting(_posting(answer)) == answer.strip(), "the system's reader gave it: kept from 300"
    assert whole.why_not(answer) is not None, "a page or a capture of that size is a snippet or a cookie notice"
    assert whole.why_not(answer + " " + answer) is None, "from 500 it may be a whole page"


# --- every way in refuses a fragment and a page dump, and keeps nothing -----------------------------------------------


async def _role_with_source(session, **more) -> Opportunity:
    role = Opportunity(owner=OWNER, slug="p", company="Fabrikam", title="Programs Lead",
                       match_key="fabrikam|programs lead", source_url="https://careers.example.test/jobs/1",
                       source_kind="employer", sourced_at=SEEN, **more)
    session.add(role)
    await session.flush()
    session.add(_posting(None, ats.OWNER))
    await session.flush()
    return role


async def _capture(text) -> object:
    async with db.session_factory()() as session:
        role = await _role_with_source(session)
        return await posting_text.take(session, role, posting_text.Capture(text)), role.description


async def _accept_board(text) -> object:
    async with db.session_factory()() as session:
        role = await _role_with_source(session)
        said = await sourcing.accept_source(session, role, "https://www.boardone.test/jobs/view/1001", "board", text,
                                            True, "owner")
        return said, role.description


async def _accept_employer(text) -> object:
    async with db.session_factory()() as session:
        role = await _role_with_source(session)
        said = await sourcing.accept_source(session, role, "https://careers.example.test/jobs/1", "employer", text,
                                            True, "owner", fetch=False)
        return said, role.description


async def _name_posting(text) -> object:
    posting = _posting(None, ats.OWNER)
    named = add_link.name_posting(posting, {"title": "Programs Lead", "company": "Fabrikam", "text": text}, SEEN)
    return named, posting.text


async def _hand_back(text) -> object:
    try:
        return hb._whole(text), None
    except BrowserWorkError as exc:
        return "refused", str(exc)


async def _hand_in(text) -> object:
    await browser_world()
    reply = await scan_hand_in([browser_card(1, description=text)], now=SEEN)
    async with db.session_factory()() as session:
        return reply["not_kept"], (await session.scalar(select(JobPosting))).text


#: Each way a text comes in, run with a text that is not a posting's: what it answers and what it kept.
INGRESS = {
    "take(Capture)": (_capture, lambda got: got == (None, None)),
    "accept_source, a board's own posting": (
        _accept_board, lambda got: got[0].startswith("rejected:") and got[1] is None),
    "accept_source, an employer's": (_accept_employer, lambda got: got == (sourcing.NO_TEXT, None)),
    "add_link.name_posting": (_name_posting, lambda got: got == (True, None)),
    "the browser's hand-back": (_hand_back, lambda got: got[0] == "refused" and "refused" in got[1]),
    "the reader's hand-in": (
        _hand_in, lambda got: len(got[0]) == 1 and got[1] == "Lead the programs team, number 1."),
}


@pytest.mark.parametrize("text", [FRAGMENT, CUT, WALL, PAGE_DUMP],
                         ids=["a fragment", "cut off", "a wall", "a page dump"])
@pytest.mark.parametrize("ingress", list(INGRESS))
async def test_every_way_a_text_comes_in_refuses_a_text_that_is_not_a_posting_and_keeps_nothing(js, ingress, text):
    """Add a row to `INGRESS` for any way in that gets a test harness later: one that forgets the rule fails here."""
    run, expected = INGRESS[ingress]
    got = await run(text)
    assert expected(got), got
    async with db.session_factory()() as session:
        raw = await session.scalar(select(RawPosting.id).where(RawPosting.kind == "detail"))
        assert raw is None, "no raw copy of a page was kept either"


def test_a_text_no_reply_could_hold_is_refused_at_the_hand_back_with_its_size():
    over = "Paragraph one says one thing about the work and nothing else. " * (whole.CEILING_CHARS // 60 + 10)
    assert whole.why_not(over) is None and whole.too_long(over, "text") is not None
    with pytest.raises(BrowserWorkError, match="nothing was cut"):
        hb._whole(over)
