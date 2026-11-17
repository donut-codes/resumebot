"""A cookie or consent banner is never a block (the owner, 2026-11-04): the banner's own words are left
out of every reading for a bot check, a sign-in wall or a refusal, the control that declines is told from the one that
accepts, and a session that hands a banner back as `blocked` is refused so no host is stopped for one. A real CAPTCHA,
sign-in wall or refusal still stops. The clicking is tested beside the other recipe steps (`test_recipe_interact.py`,
`test_page_recipes.py`), and the finder's script under Node in the scout's tests (`app/scout/consent-find.test.mjs`).
Every page and name is made up; no network, no browser."""

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.harness import instructions as hi
from app.jobsearch import consent
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import mcp as browser_mcp
from app.jobsearch.browser import reader as br
from app.jobsearch.browser.recipes import record as recipe_record
from app.jobsearch.fetch import host_stops
from app.jobsearch.models import BrowserDomain
from app.sites import read_probe
from app.sites.models import BY_APP, BY_BROWSER
from tests.builders import ONE, WORKER, browser_world, hand_back, lease, next_item
from tests.jobsearch.test_browser_work import NOW, _role, _sourced

pytestmark = pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")

# What a consent manager shows: it lists reCAPTCHA and bot security among the cookies it explains, and offers a choice.
BANNER = ('<div id="onetrust-consent-sdk"><div class="banner"><h2>We value your privacy</h2>'
          "<p>We use cookies. Strictly necessary cookies keep the site secure (Example reCAPTCHA, bot management and "
          "unusual activity checks). Sign in to view your cookie preferences.</p>"
          "<button>Manage preferences</button><button>Reject all</button><button>Accept all</button></div></div>")
POSTING = ("<h1>Programs Director</h1><p>Plan and run programs across the region. " + "Lead the team. " * 12 + "</p>"
           '<a href="https://careers.example.test/apply/1">Apply</a>')
CHECK_WORDS = "<h1>Security verification</h1><p>Verify you are human to continue.</p>"
SIGN_IN_WORDS = "<h1>Programs Director</h1><p>Join now to see who you know. Sign in to view the full posting.</p>"


def page(*parts: str) -> str:
    return "<html><head><title>Careers</title></head><body>" + "".join(parts) + "</body></html>"


# --- the banner taken out of a page -----------------------------------------------------------------------------------


def test_a_banner_is_taken_out_and_the_rest_of_the_page_is_kept():
    kept = consent.without_banners(page(BANNER, POSTING))
    assert "reCAPTCHA" not in kept and "Reject all" not in kept and "cookies" not in kept
    assert "Programs Director" in kept and "Plan and run programs" in kept and "<title>Careers</title>" in kept


def test_a_page_with_no_banner_is_returned_as_it_is():
    plain = page(POSTING)
    assert consent.without_banners(plain) == plain
    assert consent.without_banners(None) == "" and consent.without_banners("") == ""


@pytest.mark.parametrize("opening", [
    '<div id="onetrust-consent-sdk">', '<div id="CybotCookiebotDialog" role="dialog">',
    '<section class="cookie-banner">',
    '<div id="cookieConsentBar">', '<div class="didomi-popup-container">', '<aside aria-label="Cookie notice">',
    '<div id="usercentrics-root">', '<div class="gdpr-notice">', '<div class="truste-consent-track">',
    '<div id="cmplz-cookiebanner-container">'])
def test_the_banners_of_the_usual_consent_managers_are_found_by_what_they_call_themselves(opening):
    tag = opening[1:].split()[0].rstrip(">")
    closing = f"</{tag}>"
    kept = consent.without_banners(page(opening, "<p>reCAPTCHA and unusual activity</p>", closing, POSTING))
    assert "reCAPTCHA" not in kept and "Plan and run programs" in kept


def test_a_banner_nested_in_another_and_two_side_by_side_are_all_taken_out():
    nested = '<div class="cookie-wall"><div id="consent-box"><p>captcha one</p></div><p>captcha two</p></div>'
    side = '<div class="cookie-a">captcha three</div><div class="cookie-b">captcha four</div>'
    kept = consent.without_banners(page(nested, side, POSTING))
    assert "captcha" not in kept and "Plan and run programs" in kept


def test_the_page_itself_is_never_taken_for_a_banner_whatever_class_it_carries():
    """A `body` that says a banner is open, or an app wrapper around the page's own content, is not the banner."""
    body = ('<html><body class="cookie-banner-open"><div id="app" class="has-cookie-consent">'
            "<main><h1>Programs Director</h1><p>Verify you are human to continue.</p></main></div></body></html>")
    assert "Verify you are human" in consent.without_banners(body)
    wrapped = page('<div class="cookie-consent-page"><h1>Programs Director</h1><p>Verify you are human</p></div>')
    assert "Verify you are human" in consent.without_banners(wrapped), "a wrapper that holds the page's heading stays"


def test_a_banner_that_is_never_closed_is_left_in_so_a_real_check_is_never_missed_for_want_of_a_tag():
    open_ended = '<div class="cookie-banner"><p>We use cookies.</p>' + CHECK_WORDS
    assert "Verify you are human" in consent.without_banners(open_ended)


def test_a_word_in_a_script_or_a_page_that_only_mentions_cookies_does_not_hide_the_page():
    script = "<script>window.consentManager = 'onetrust';</script>"
    kept = consent.without_banners(page(script, POSTING, "<p>Our cookie policy is on the site.</p>"))
    assert "Plan and run programs" in kept and "cookie policy" in kept


def test_a_banners_own_h1_title_is_taken_out_with_it_and_a_wrapper_holding_the_pages_own_h1_stays():
    """A banner that titled itself with an `h1` was read as the page's own content, so its
    reCAPTCHA wording read as a bot check again. Only an `h1` that is about cookies or privacy is the banner's title."""
    titled = ('<div id="cmp-consent"><h1>We value your privacy</h1><p>Example reCAPTCHA and bot management cookies</p>'
              "<button>Reject all</button></div>")
    kept = consent.without_banners(page(titled, POSTING))
    assert "reCAPTCHA" not in kept and "Plan and run programs" in kept
    cookie_title = '<div class="cookie-banner"><h1>Cookie settings</h1><p>captcha cookies</p></div>'
    assert "captcha" not in consent.without_banners(page(cookie_title, POSTING))
    wrapped = page('<div class="cookie-consent-page"><h1>Security verification</h1><p>Verify you are human</p></div>')
    assert "Verify you are human" in consent.without_banners(wrapped)


@pytest.mark.parametrize("opening", [
    '<div class="cc-window cc-banner">', '<div id="qc-cmp2-container">', '<div id="sp_message_container_123456">',
    '<div id="termly-code-snippet-support">', '<div class="truste_overlay">', '<div id="trustarc-banner">'])
def test_the_banners_of_consent_managers_that_name_no_cookie_are_found_by_the_managers_own_names(opening):
    tag = opening[1:].split()[0].rstrip(">")
    kept = consent.without_banners(page(opening, "<p>reCAPTCHA and cookies</p>", f"</{tag}>", POSTING))
    assert "reCAPTCHA" not in kept and "Plan and run programs" in kept


def test_a_generic_privacy_notice_container_is_left_in_because_it_may_be_the_pages_own():
    """Fails toward stopping: a page's own wrapper may carry that name, and what a real check says must be read."""
    kept = consent.without_banners(page('<div class="privacy-notice"><p>Verify you are human</p></div>', POSTING))
    assert "Verify you are human" in kept


def test_only_what_is_over_the_size_a_caller_reads_is_cut_off():
    assert consent.without_banners("<p>" + "x" * 50 + "</p>", limit=10) == "<p>" + "x" * 7


# --- which control declines -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", [
    "Reject all", "Reject All Cookies", "REJECT ALL", "Decline", "Decline all", "Decline cookies", "Deny", "Deny all",
    "Refuse all", "Disagree", "Reject non-essential cookies", "Reject optional cookies", "Necessary only",
    "Necessary cookies only", "Only essential", "Only essential cookies", "Use necessary cookies only",
    "Accept necessary cookies only", "Allow essential cookies", "Strictly necessary only", "No, thanks", "No thanks",
    "Continue without accepting", "Continue without accepting →", "I decline", "I don't accept",
    "I do not accept", "Reject all and close", "Opt out"])
def test_the_controls_that_refuse_cookies_are_told_by_their_words(label):
    assert consent.is_decline(label)


@pytest.mark.parametrize("label", [
    "Accept", "Accept all", "Accept all cookies", "Allow all", "Allow all cookies", "I accept", "I agree", "Agree",
    "OK", "Got it", "Accept and close", "Save preferences", "Save and continue", "Confirm my choices",
    "Manage preferences", "Cookie settings", "Customize", "Learn more", "Close", "Apply", "Sign in", "", None,
    "Necessary", "Essential", "Required", "Strictly Necessary Cookies", "Necessary cookies", "Essential cookies",
    "Reject all the cookies you like and then accept every other one that the site wants to set in your browser"])
def test_a_control_that_accepts_or_does_anything_else_is_never_taken_for_the_decline(label):
    assert not consent.is_decline(label)


# --- a page's state, with and without a banner on it ------------------------------------------------------------------


def test_a_banner_that_names_recaptcha_or_a_sign_in_is_no_bot_check_and_no_sign_in_wall():
    """A short page whose banner lists reCAPTCHA read as a security check."""
    assert browser_code.page_state(page(BANNER, POSTING), "https://careers.example.test/job/1") \
        == browser_code.STATE_OPEN
    assert browser_code.page_state(page(BANNER), "https://careers.example.test/job/1") == browser_code.STATE_OPEN


def test_a_real_bot_check_or_sign_in_wall_under_a_banner_still_says_so():
    assert browser_code.page_state(page(BANNER, CHECK_WORDS), "https://x.example.test/job/1") \
        == browser_code.STATE_CHECK
    assert browser_code.page_state(page(BANNER, SIGN_IN_WORDS), "https://x.example.test/job/1") \
        == browser_code.STATE_SIGN_IN
    assert browser_code.page_state(page(BANNER, POSTING), "https://x.example.test/checkpoint/challenge/1") \
        == browser_code.STATE_CHECK, "an address that is a challenge is one whatever the banner says"


def test_the_digest_of_a_page_with_a_banner_is_open_and_its_text_is_the_postings():
    got = browser_code.digest(page(BANNER, POSTING), "https://careers.example.test/job/1")
    assert got["state"] == "open" and "reCAPTCHA" not in got["text"] and "Plan and run programs" in got["text"]


def test_the_apply_link_of_a_page_with_a_banner_is_read_and_the_state_is_open():
    got = browser_code.apply_link(page(BANNER, POSTING), "https://careers.example.test/job/1")
    assert got["state"] == "open" and got["candidates"] == ["https://careers.example.test/apply/1"]




def test_a_short_page_that_is_only_a_banner_naming_captcha_is_no_bot_check_for_a_recording():
    assert recipe_record.looks_walled(page(BANNER)) is False
    assert recipe_record.looks_walled(page(BANNER, "<h1>Just a moment...</h1>")) is True
    assert recipe_record.looks_walled(page("<h1>Attention Required!</h1><p>px-captcha</p>")) is True


def _reply(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


def test_a_site_whose_short_page_is_only_a_banner_naming_captcha_is_still_open_to_the_app():
    """The test of a site the owner adds: a banner is no "turned away" page, so the app, not the browser, reads it."""
    assert read_probe.judge(_reply(page(BANNER, "<p>Open roles: Programs Director</p>"))).read == BY_APP
    assert read_probe.judge(_reply(page(BANNER))).read == BY_APP


def test_a_site_that_turns_the_visit_away_is_still_read_in_the_browser_whatever_banner_is_on_it():
    assert read_probe.judge(_reply(page(BANNER, "<h1>Access denied</h1><p>CAPTCHA</p>"))).read == BY_BROWSER
    assert read_probe.judge(_reply(page("<h1>Just a moment...</h1>"))).read == BY_BROWSER


# --- a hand-back that names only a cookie banner ----------------------------------------------------------------------


@pytest.mark.parametrize("note", [
    "a cookie banner covers the page", "cookie consent wall", "Cookie notice blocked the page, only Accept offered",
    "GDPR consent popup", "the site asks for consent to cookies before it shows anything",
    "privacy preferences dialog on top of the posting", "the page was blocked by a cookie banner with only Accept",
    "a cookie consent popup hides the posting; no reject button", "GDPR banner covers the whole page",
    "Cookie notice is blocking the page", "the cookie wall has completely covered the listing"])
def test_a_note_that_names_only_a_cookie_notice_is_one(note):
    assert host_stops.is_cookie_notice(note)


@pytest.mark.parametrize("note", [
    "a Cloudflare bot check", "CAPTCHA after the cookie banner", "HTTP 403 even after declining cookies",
    "cookie banner, then unusual activity notice", "rate-limit page, too many requests (cookie banner too)",
    "verify you are human", "the page showed access denied", "security check behind the consent banner", "", None,
    # what a real refusal says when it also mentions cookies: none of these is a banner
    "Cloudflare: Sorry, you have been blocked. Please enable cookies.",
    "robots.txt disallows this path; also a cookie consent notice",
    "cookie notice, then Incapsula incident ID 123", "Imperva blocked request, cookies",
    "Please confirm you are not a robot, cookie settings",
    "Site detected automated access after the cookie consent; page shows Press & Hold",
    "Your IP has been temporarily banned. cookies",
    "cookie banner appeared and then the page asked me to log in",
    "cookie banner declined, then the page asked me to sign in to continue",
    "the site refuses automated visitors (cookie consent shown first)",
    "Akamai reference number on the page after the cookie banner", "DataDome says the request was flagged; cookies",
    "cookie notice, then HTTP 503 from the host", "cookie banner and then Pardon Our Interruption",
    "the cookie banner was declined but the page says the session is restricted",
    "cookie banner covers the page, and then I was blocked", "the cookie banner then blocked me",
    "a cookie banner and the site said access is forbidden"])
def test_a_note_with_a_real_stops_word_in_it_is_never_taken_for_a_cookie_notice(note):
    assert not host_stops.is_cookie_notice(note)


async def _pace(domain: str) -> BrowserDomain | None:
    async with db.session_factory()() as session:
        return await session.scalar(select(BrowserDomain).where(BrowserDomain.domain == domain))


async def test_a_blocked_hand_back_that_names_only_a_cookie_banner_is_refused_and_stops_no_host(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    item_id = got["item"]["item_id"]
    with pytest.raises(br.BrowserError, match="cookie or consent banner is not a block"):
        await hand_back(WORKER, item_id, "blocked", now=NOW, note="a cookie consent banner covered the page")
    pace = await _pace("example.test")
    assert pace is None or (pace.stopped_at is None and pace.cool_until is None and not pace.blocks)
    # The item is still the session's: it declines the banner, reads the page and hands back what it found.
    reply = await hand_back(WORKER, item_id, "not_found", now=NOW,
                            note="no posting with this title on the careers site")
    assert reply["ok"] and reply["item"] == "waiting"


async def test_a_blocked_hand_back_that_names_a_real_check_behind_the_banner_still_stops_the_host(js):
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    reply = await hand_back(WORKER, got["item"]["item_id"], "blocked", now=NOW,
                            note="declined the cookie banner, then a CAPTCHA appeared")
    pace = await _pace(ONE)
    assert pace.stopped_at == NOW and "CAPTCHA" in pace.stopped_for and "stopped" in reply["note"]


async def test_an_unreadable_hand_back_that_names_a_cookie_banner_is_taken_as_the_honest_result(js):
    """Only `blocked` is refused: a banner that really hides the page and cannot be closed is an unreadable page."""
    await browser_world(extra_sites=False)
    await _role(1)
    got = await next_item(WORKER, NOW)
    reply = await hand_back(WORKER, got["item"]["item_id"], "not_found", now=NOW,
                            note="a cookie banner with no option but Accept hides the whole page")
    assert reply["ok"]


@pytest.mark.parametrize("kind, honest, others", [
    ("source", "not_found", ("unreadable", "failed")), ("read", "unreadable", ("not_found", "failed")),
    ("scan", "failed", ("not_found", "unreadable"))])
async def test_the_refusal_of_a_cookie_banner_names_only_the_result_the_items_own_kind_takes(js, kind, honest, others):
    """It told every session to hand back "not_found, unreadable or failed", so a read
    item was told to send a result it does not take and was refused a second time."""
    await browser_world(extra_sites=False)
    if kind == "scan":
        item_id = await lease("scan", ONE, WORKER, NOW)
    else:
        await (_role(1) if kind == "source" else _sourced(1))
        item_id = (await next_item(WORKER, NOW))["item"]["item_id"]
    with pytest.raises(br.BrowserError) as refused:
        await hand_back(WORKER, item_id, "blocked", now=NOW, note="a cookie banner covered the page")
    said = str(refused.value)
    assert f"`{honest}`" in said and not any(f"`{other}`" in said or f"{other}," in said for other in others)
    reply = await hand_back(WORKER, item_id, honest, now=NOW, note="the page could not be read past the banner")
    assert reply["ok"], "and the result it names is one the item takes"


# --- what the sessions are told ---------------------------------------------------------------------------------------


def test_both_sessions_are_told_to_decline_a_cookie_notice_and_that_it_is_never_a_block():
    for name in ("browser.reader", "browser.learner"):
        text = hi.read("instructions", name).lower()
        for rule in ("cookie or consent notice", "reject all", "decline", "necessary only", "only essential",
                     "close it or read past it", "never accept tracking"):
            assert rule in text, (name, rule)
    reader = hi.read("instructions", "browser.reader").lower()
    assert "never a block or a reason to hand an item back" in reader
    assert "except the site's search boxes and a cookie notice's decline option" in reader, "the form rule allows it"
    learner = hi.read("instructions", "browser.learner").lower()
    assert "a cookie or consent notice is never a stop" in learner
    assert "so is declining a cookie notice" in learner, "the form rule allows it"
    # the real stops still stop
    assert "captcha" in reader and "`blocked`" in reader and "real stop" in learner


def test_the_door_and_the_runbook_template_say_the_same():
    assert "cookie notice is declined" in browser_mcp.INSTRUCTIONS and "never a block" in browser_mcp.INSTRUCTIONS
    assert "a cookie banner and the button that declines it" in hi.read("templates", "browser.pathfinder.runbook")
