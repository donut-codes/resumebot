"""Go's main check, the posting held compared with the page (`posting_match.py`, `go_checks.compared`), and its quick
signals: the one list of closed and missing phrases (`closed_words.py`) and a redirect to a page without the
posting's id (`go_checks.moved_away`). Two tabs opened on "The page you are looking for doesn't exist." after the
checks had called them open. Synthetic roles and pages only."""


import pytest

from app.jobsearch import adapters
from app.jobsearch.browser import go as apply_go
from app.jobsearch.browser import go_checks, posting_match
from app.jobsearch.browser.code import Page
from app.jobsearch.models import Opportunity
from app.jobsearch.postings import closed_words
from tests.jobsearch.test_go_checks import TODAY, _add, _packet, _roles, _trail, role
from tests.support import OWNER, fake_go_window

SOURCE = "https://careers.example.test/jobs/48213/product-lead"
POSTING = " ".join(
    f"Paragraph {n}: the product lead owns the roadmap for area {n}, works with design and engineering on release {n}, "
    f"and reports on outcome {n} to the leadership group every quarter." for n in range(12))
TITLE = "Senior Product Lead"


def held(**extra) -> Opportunity:
    return Opportunity(owner=OWNER, slug="held", company="Firm", title=TITLE, description=POSTING,
                       **{"source_url": SOURCE, "apply_url": SOURCE, **extra})


def page(body: str, url: str = SOURCE, status: int = 200) -> Page:
    return Page(url=url, status=status, html=f"<html><body>{body}</body></html>")


FILLER = "<p>" + " ".join(f"word{n}" for n in range(80)) + "</p>"  # a page with something on it


# --- the comparison ---------------------------------------------------------------------------------------------------


def test_the_posting_on_its_page_matches_part_of_it_is_changed_and_none_of_it_is_missing():
    whole = posting_match.compare(TITLE, POSTING, f"{TITLE} {POSTING}")
    assert (whole.verdict, whole.title) == (posting_match.MATCH, True) and whole.share == 1.0
    half = POSTING[: len(POSTING) // 3] + " ".join(f"new sentence {n} about other work" for n in range(60))
    assert posting_match.compare(TITLE, POSTING, f"{TITLE} {half}").verdict == posting_match.CHANGED
    gone = posting_match.compare(TITLE, POSTING, "Search for jobs. " + " ".join(f"job{n}" for n in range(80)))
    assert (gone.verdict, gone.title) == (posting_match.MISSING, False)


def test_too_little_held_or_shown_gives_no_verdict():
    assert posting_match.compare(TITLE, "Lead the product.", POSTING).verdict is None
    assert posting_match.compare(TITLE, POSTING, "Loading").verdict is None, "a page not drawn yet is not missing"


def test_the_title_is_found_through_short_forms():
    assert posting_match.compare("Sr. Product Lead", POSTING, f"Senior Product Lead {POSTING}").title


# --- in the app's Chrome ----------------------------------------------------------------------------------------------


def test_the_page_its_text_came_from_is_open_changed_or_closed_by_the_posting_held():
    role = held()
    assert go_checks.page_verdict(page(f"<h1>{TITLE}</h1><p>{POSTING}</p>"), SOURCE, TODAY, role) == (
        go_checks.OPEN, "")
    part = POSTING[: len(POSTING) // 3] + " ".join(f"new sentence {n} about other work" for n in range(60))
    state, said = go_checks.page_verdict(page(f"<h1>{TITLE}</h1><p>{part}</p>"), SOURCE, TODAY, role)
    assert state == go_checks.CHANGED and "of the posting's text is on the page" in said
    state, said = go_checks.page_verdict(page(f"<h1>Careers</h1>{FILLER}"), SOURCE, TODAY, role)
    assert (state, said) == (go_checks.UNKNOWN, "")  # was CLOSED: lacking the posting is not proof


def test_a_missing_text_closes_nothing_when_the_page_shows_the_title_its_data_or_a_frame():
    role = held()
    with_title = page(f"<h1>{TITLE}</h1><form>Apply: name, email</form>{FILLER}")
    assert go_checks.page_verdict(with_title, SOURCE, TODAY, role)[0] == go_checks.OPEN, "an application form"
    framed = page(f'<h1>Careers</h1><iframe src="/jobs/48213/product-lead?in_iframe=1"></iframe>{FILLER}')
    assert go_checks.page_verdict(framed, SOURCE, TODAY, role)[0] == go_checks.OPEN, "the posting is in the frame"
    data = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Product Lead"}</script>'
            f"<h1>Careers</h1>{FILLER}")
    assert go_checks.page_verdict(page(data), SOURCE, TODAY, role)[0] == go_checks.OPEN


#: What a consent manager draws: about sixty words, among them a bot check's names (reCAPTCHA, bot management).
BANNER = ('<div id="onetrust-consent-sdk"><h2>We value your privacy</h2><p>We use cookies. Strictly necessary cookies '
          "keep the site secure: Example reCAPTCHA, bot management and unusual activity checks. "
          + "Your choices apply to this site only and can be changed at any time in the settings. " * 4
          + "</p><button>Reject all</button><button>Accept all</button></div>")


def test_a_page_that_is_only_a_cookie_banner_closes_nothing():
    """Found in review, 2026-11-04: with the banner's words left out of the check reading (`page_state`), a page that
    was only a banner stopped being a bot check, and its sixty words counted as a page drawn without the posting."""
    role = held()
    assert go_checks.page_verdict(page(BANNER), SOURCE, TODAY, role) == (go_checks.UNKNOWN, "")
    assert go_checks.page_verdict(page(BANNER + "<h1>Loading</h1>"), SOURCE, TODAY, role)[0] != go_checks.CLOSED


def test_a_cookie_banner_over_the_posting_or_over_a_page_without_it_is_judged_by_the_rest_of_the_page():
    role = held()
    assert go_checks.page_verdict(page(BANNER + f"<h1>{TITLE}</h1><p>{POSTING}</p>"), SOURCE, TODAY, role) == (
        go_checks.OPEN, "")
    state, said = go_checks.page_verdict(page(BANNER + f"<h1>Careers</h1>{FILLER}"), SOURCE, TODAY, role)
    assert (state, said) == (go_checks.UNKNOWN, "")  # was CLOSED: lacking the posting is not proof


def test_a_page_other_than_the_one_its_text_came_from_is_not_judged_by_it():
    role = held()
    form = "https://apply.example.test/form/48213"
    assert go_checks.page_verdict(page(f"<h1>Careers</h1>{FILLER}", form), form, TODAY, role) == (go_checks.OPEN, "")


async def test_the_browser_check_reads_the_source_page_when_the_apply_link_is_another(monkeypatch):
    role = held(apply_url="https://apply.example.test/form/48213")
    shown = {role.apply_url: page(f"<h1>{TITLE}</h1><form>Apply</form>", role.apply_url),
             SOURCE: page(f"<h1>Careers</h1>{FILLER}")}

    read = []

    async def browser_page(link):
        read.append(link)
        return shown[link]

    monkeypatch.setattr(go_checks, "browser_page", browser_page)
    [(state, said)] = await go_checks._browse([(role, role.apply_url, go_checks.UNKNOWN)], TODAY)
    assert SOURCE in read, "the source page is read when the apply link is another"
    assert state == go_checks.UNKNOWN, "the source page lacks the posting: unreadable, not closed (was CLOSED)"


# --- the phrases ------------------------------------------------------------------------------------------------------

PHRASES = [
    "The page you are looking for doesn't exist.", "The page you are looking for doesn’t exist.",
    "Page not found", "Job not found", "This job is no longer available", "No longer accepting applications",
    "The position has been filled", "This job has expired", "This posting is closed", "The job posting has expired",
    "We couldn't find the job you were looking for", "Sorry, the job you are looking for could not be found",
    "Applications are now closed", "This requisition is no longer available",
]


@pytest.mark.parametrize("phrase", PHRASES)
def test_each_closed_or_missing_phrase_is_on_the_one_list(phrase):
    assert closed_words.found(phrase) is not None
    assert go_checks.closed_words(phrase) is not None


@pytest.mark.parametrize("phrase", PHRASES)
def test_the_browser_closes_a_page_that_says_each_phrase(phrase):
    state, said = go_checks.page_verdict(page(f"<p>{phrase}</p>", "https://x.example.test/j/1"),
                                         "https://x.example.test/j/1", TODAY)
    assert state == go_checks.CLOSED and "as the browser shows it" in said


def test_an_open_posting_says_none_of_the_phrases():
    assert closed_words.found(POSTING + " Apply now. Accepting applications until 1 November.") is None


# --- a phrase inside a condition --------------------------------------------------------------------------------------

# Made-up sentences of the shapes open postings carry: the list's words as a condition, not as what the page says.
CONDITIONS = [
    "Applications are accepted on an ongoing basis until the position is filled.",
    "Apply through 3 March, 2031, or until the role is filled.",
    "Apply until 3 March, 2031, or the position is filled.",
    "The posting stays up for 30 days or until the job is no longer posted.",
    "The posting may be removed earlier if the position is filled before the closing date.",
    "Pay depends on the level at which the position is filled.",
    "Interviews stop once the role has been filled.",
    "We will tell every applicant when applications are closed.",
    "Open till the job is filled.",
    "We read every application unless the vacancy has been filled.",
    "The listing comes down as soon as the position has been filled.",
    "Apply early in case the role is filled ahead of the date.",
    "Late applications are read only before the posting has expired.",
    "Recruiters own each search, from the first call to ensuring the role is filled.",
    "Applications are accepted\nuntil the position is filled.",  # a sentence the markup wrapped
    # A time's colon, a bracket, an abbreviation's full stop or a comma between the condition word and the phrase.
    "Applications are accepted until 5:00 PM on 3 March or the position is filled.",
    "Applications are accepted until Jan. 5 or the position is filled.",
    "Applications are accepted until 3 March (11:59 PM) or the position is filled.",
    "Open until the closing date, Friday 3 March, or the position is filled.",
    "This posting stays open until 3 March 2031, whichever comes first, or the position is filled.",
    "Priority deadline: 3 March. Open until 5 p.m. E.T. or the role is filled.",
    "Open until 5 p.m. ET or the role is filled.",
    "The posting stays up until 5:00 PM on the day the position is filled.",
]
STATEMENTS = [
    "This position has been filled.", "The position is filled.", "Sorry, this job has been filled",
    "The job you are trying to apply for has been filled", "This job is no longer posted",
    "No longer accepting applications", "Applications are closed.",
    "Apply until Friday. This position has been filled.",  # the condition word is in the sentence before
    "If you came here to apply, this job has been filled.",  # ... or in the clause before
    "While this position has been filled, we have other openings.",
    "Applications were open until Friday\nThis job has expired",  # a line end before a capital ends the sentence
    f"{TITLE}\nRemote Full-time\nNo longer accepting applications\nAbout the job",  # a job board's banner
    "Applications were open until Friday. Apply elsewhere, or the position is filled.",  # an "or" stays in its sentence
    "We took applications until 5 p.m. This position has been filled.",  # a full stop before a capital ends a sentence
    "Apply until Friday. THIS POSITION HAS BEEN FILLED.",
    "If you came here to apply: this job has been filled.",  # a colon that is no time's starts a clause
]


@pytest.mark.parametrize("sentence", CONDITIONS)
def test_a_closed_phrase_inside_a_condition_is_not_the_page_saying_it_is_closed(sentence):
    assert closed_words.found(sentence) is None
    assert go_checks.closed_words(sentence) is None


@pytest.mark.parametrize("sentence", CONDITIONS)
def test_an_open_posting_with_a_closed_phrase_inside_a_condition_is_open_in_the_browser(sentence):
    shown = page(f"<h1>{TITLE}</h1><p>{POSTING}</p><p>{sentence}</p>")
    assert go_checks.page_verdict(shown, SOURCE, TODAY, held()) == (go_checks.OPEN, "")


@pytest.mark.parametrize("sentence", STATEMENTS)
def test_a_closed_phrase_said_plainly_is_still_found_beside_a_condition(sentence):
    assert closed_words.found(sentence) is not None
    state, said = go_checks.page_verdict(page(f"<h1>{TITLE}</h1><p>{POSTING}</p><pre>{sentence}</pre>"), SOURCE, TODAY,
                                         held())
    assert state == go_checks.CLOSED and "as the browser shows it" in said


def test_the_statement_is_the_evidence_not_the_condition_before_it():
    text = "Applications are accepted until the position is filled. This position has been filled."
    assert closed_words.found(text) == "This position has been filled"


# --- "This opportunity is currently not available." -------------------------------------------------------------------

#: The whole page a careers site draws at a closed posting's own address (status 200, the address unchanged): about
#: twenty words, so the comparison with the posting held gives no verdict (`posting_match.MIN_PAGE_WORDS`). Found
#: 2026-11-11: eight checks of such a page said "unknown" and the launched tab said "open". The employer is made up.
NOT_AVAILABLE_LINK = "https://recruiting.example.test/EXA1000/JobBoard/0f0f0f0f/OpportunityDetail?opportunityId=48213"
NOT_AVAILABLE_SAYS = "This opportunity is currently not available"
NOT_AVAILABLE_PAGE = (
    "<html><body><div id='app'><header><a>Example Employer</a><nav><a>Find Opportunities</a>"
    "<select><option>English (United States)</option></select><a>Sign In</a></nav></header>"
    f"<main><h3>{NOT_AVAILABLE_SAYS}.</h3><p>Still curious? Take a look at other opportunities.</p></main>"
    "</div></body></html>")


def test_a_page_that_says_the_opportunity_is_currently_not_available_is_closed_by_its_words():
    shown = Page(url=NOT_AVAILABLE_LINK, status=200, html=NOT_AVAILABLE_PAGE)
    text = adapters.html_to_text(shown.html)
    assert len(posting_match.words(text)) < posting_match.MIN_PAGE_WORDS, "too short for the comparison to judge"
    assert closed_words.found(text) == NOT_AVAILABLE_SAYS
    role = held(source_url=NOT_AVAILABLE_LINK, apply_url=NOT_AVAILABLE_LINK)
    for of_role in (role, None):
        state, said = go_checks.page_verdict(shown, NOT_AVAILABLE_LINK, TODAY, of_role)
        assert state == go_checks.CLOSED
        assert said == f'{NOT_AVAILABLE_LINK} says "{NOT_AVAILABLE_SAYS}" (as the browser shows it).'


@pytest.mark.parametrize("text", [
    "This opportunity is currently not available.", "This opportunity is not available.",
    "THIS OPPORTUNITY IS CURRENTLY NOT AVAILABLE", "The position is unavailable.", "This job is not available!",
    "Sorry, this posting is not currently available.", "We're sorry. This role is currently unavailable.",
    "Careers\nThis vacancy is not available\nSearch all jobs",  # a heading with no full stop: the line ends there
    "This opportunity has been filled.", "The opportunity you are looking for does not exist.",
])
def test_the_posting_itself_said_to_be_not_available_is_a_closed_phrase(text):
    assert closed_words.found(text) is not None


@pytest.mark.parametrize("text", [
    "This position is not available to candidates outside the country.",
    "Relocation is not available for this role.", "Remote work is currently not available.",
    "This opportunity is available to current employees.", "Sponsorship for this position is not available.",
    "Relocation assistance for this role is currently not available.", "This benefit is not available.",
    "Saving a search is not available. Sign in to use this feature.", "The position is not available for remote work",
    "Is this opportunity not available to you? Tell us why.", "A hybrid schedule in this role is unavailable.",
])
def test_something_else_said_to_be_not_available_is_not_a_closed_phrase(text):
    assert closed_words.found(text) is None
    assert go_checks.page_verdict(page(f"<p>{text}</p>{FILLER}", "https://x.example.test/j/1"),
                                  "https://x.example.test/j/1", TODAY)[0] == go_checks.OPEN


@pytest.mark.parametrize("html", [
    # The sentence goes on, on the next line of the markup or after a <br>: `html_to_text` keeps both as line breaks.
    "<p>This position is not available\n   to candidates outside the country.</p>",
    "<p>This position is not available<br>to candidates outside the country.</p>",
    "<p>Please note: relocation is offered.\nThis role is not available\nfor remote work in some states.</p>",
    "<p>The position is unavailable<br/>for remote work.</p>",
    # The sentence began on the line before: the posting is not its subject.
    "<p>Sponsorship for\n   this position is not available.</p>",
    "<p>Relocation assistance for<br>this role is currently not available.</p>",
])
def test_a_sentence_broken_across_lines_is_still_one_sentence_and_not_a_closed_phrase(html):
    assert closed_words.found(adapters.html_to_text(html)) is None
    assert go_checks.page_verdict(page(f"{html}{FILLER}", "https://x.example.test/j/1"),
                                  "https://x.example.test/j/1", TODAY)[0] == go_checks.OPEN


@pytest.mark.parametrize("html", [
    "<h1>Careers</h1><h3>This vacancy is not available</h3><a>Search all jobs</a>",
    "<p>Our company<br>This opportunity is currently not available<br>Still curious? Take a look.</p>",
    "<p>This job is not available.\n   see our other openings.</p>",
])
def test_a_line_that_is_the_whole_sentence_is_a_closed_phrase_beside_other_lines(html):
    assert closed_words.found(adapters.html_to_text(html)) is not None


# --- a redirect to a page without the posting -------------------------------------------------------------------------


@pytest.mark.parametrize(("link", "final", "closed"), [
    ("https://jobs.example.test/job/Remote/Product-Lead_R13956", "https://jobs.example.test/search?q=", True),
    ("https://careers.example.test/jobs/48213", "https://careers.example.test/", True),
    ("https://careers.example.test/jobs/48213", "https://careers.example.test/jobs/48213/", False),
    ("https://careers.example.test/jobs/48213", "https://careers.example.test/apply?job=48213", False),
    ("https://track.example.test/c/48213", "https://employer.example.org/careers/77", False),
    ("https://careers.example.test/careers", "https://careers.example.test/search", False),
])
def test_a_redirect_to_a_page_of_the_site_without_the_postings_id_is_closed(link, final, closed):
    assert go_checks.moved_away(link, final) is closed
    state, _ = go_checks.page_verdict(page(FILLER, final), link, TODAY)
    assert (state == go_checks.CLOSED) is closed


# --- a changed posting, and Go's window ---------------------------------------------------------------------------


async def test_a_changed_posting_is_flagged_on_its_history_and_opens(js, monkeypatch):
    opened: list[str] = []
    link = "https://careers.example.test/open/a"
    part = POSTING[: len(POSTING) // 3] + " ".join(f"new sentence {n} about other work" for n in range(60))
    pages = {link: Page(url=link, status=200, html=f"<h1>Product Lead a</h1><p>{part}</p>")}
    monkeypatch.setattr(go_checks, "start_browser", _chrome_up)
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened, pages))
    await _add(role("a", description=POSTING, source_url=link, go_check=go_checks.OPEN,
                    go_checked_at=_now()))
    await _packet("a")
    body = await apply_go.go(OWNER, poll=0, wait=0)
    assert [o["title"] for o in body["opened"]] == ["Product Lead a"]
    [line] = await _trail(go_checks.CHANGED_EVENT)
    assert "Posting changed" in line.sentence and "of the posting's text is on the page" in (line.detail or "")
    roles = await _roles()
    assert roles["a"].status == "ready_to_launch" and roles["a"].go_opened_at is not None, (
        "a changed posting is flagged, and the role opens all the same")


async def test_go_opens_the_batch_in_the_apps_chrome_and_never_in_the_owners_own(js, monkeypatch):
    opened: list[str] = []
    launched: list[str] = []

    async def open_links(links, pause=None):
        launched.extend(links)

    monkeypatch.setattr(go_checks, "start_browser", _chrome_up)
    monkeypatch.setattr(apply_go, "go_window", fake_go_window(opened))
    monkeypatch.setattr(apply_go, "open_links", open_links)
    await _add(role("a", go_check=go_checks.OPEN, go_checked_at=_now()))
    await _packet("a")
    body = await apply_go.go(OWNER, poll=0, wait=0)
    assert opened == ["https://careers.example.test/open/a"] and launched == []
    assert not any("your own Chrome" in n for n in body["notes"])


async def _chrome_up():
    return ""


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
