"""The facts the screening process compares (no database).

Everything here is synthetic (tests/support.py): not the owner's profile, tracker or numbers."""

from datetime import date

import pytest

from app.jobsearch.measures import pay_place as g

TODAY = date(2026, 10, 6)


def make_gates(**over) -> g.Gates:
    places = ["oakfield", "riverton", "lakeside, il"]
    base = {
        "excluded_employers": ["acme holdings", "globex"], "places_gated": True,
        "hybrid_places": places, "onsite_places": places,
    }
    return g.Gates(**{**base, **over})


def test_the_defaults_let_every_role_through():
    gates = g.Gates()
    assert gates.for_process() == {"stale_days": 100_000, "comp_floor": 0, "says": {}}
    assert g.location_ok("Gotham, NY", "onsite", gates) is True, "no place gate: every place is fine"
    assert g.is_excluded("Globex", gates) is False
    assert g.pay_check(10_000, 20_000, gates) == ("unknown", None, ""), "no pay gate: nothing is judged"


def test_the_process_is_handed_only_the_numbers_and_sentences_its_conditions_compare():
    said = make_gates(comp_floor=95_000, stale_days=30, says={"age": "A sentence."})
    assert said.for_process() == {"stale_days": 30, "comp_floor": 95_000, "says": {"age": "A sentence."}}


# --- location --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("location", "mode", "expected"), [
    ("Anywhere", "remote", True),
    ("Gotham, NY", "remote", True),  # remote passes, wherever the company sits
    ("Oakfield, ST", "onsite", True),
    ("Riverton, ST (hybrid)", "hybrid", True),
    ("Lakeside, IL", "onsite", True),  # secondary
    ("Gotham, NY", "hybrid", False),  # not a listed place
    ("Greater Gotham Area", "onsite", False),
    ("Denver, CO", "onsite", False),  # not local, not remote
    ("Oakfield, ST", "restricted_remote", True),  # remote limited to some states reads as its place
    ("Denver, CO", "restricted_remote", False),
    ("Remote - US", "unknown", True),  # the text says remote and the scout couldn't tell the mode
    ("Remote or hybrid in Denver", "unknown", True),  # the text says remote and nothing says remote is closed
    ("Gotham, NY", "unknown", None),  # a place and no work mode: not known to be on site, so unknown, never a fail
    ("Denver, CO", None, None),
    ("Gotham, NY", "remote", True),  # once the work mode is known, the gate can tell: remote passes...
    ("Gotham, NY", "onsite", False),  # ...and on site does not
    ("", "unknown", None),  # no place at all: held, not guessed
    ("United States", "unknown", None),
    (None, "onsite", None),
    # A word that is no place says nothing about where the work is: unknown, whichever way of working the posting names.
    ("26 Locations", "unknown", None),  # all a job system's list says of a posting with several places
    ("2 Locations", "onsite", None),
    ("3 locations", "hybrid", None),
    ("Multiple Locations", "unknown", None),
    ("Various Locations", "onsite", None),
    ("Location", "unknown", None),  # a board's placeholder
    ("N/A", "hybrid", None),
    ("Not stated", "onsite", None),
    (" - ", "unknown", None),
    ("Oakfield, ST and 2 other locations", "onsite", True),  # a place is named: judged on it
    ("Gotham, NY; 2 other locations", "onsite", False),
])
def test_location(location, mode, expected):
    assert g.location_ok(location, mode, make_gates()) is expected


# What a scout or a reader writes when the place is a sentence and not a town (synthetic, in the shapes real ones take).
REMOTE_AMONG_OTHER_WORDS = [
    "Remote / flexible US (listing also says On-site and Gotham)",
    "Remote or Atlanta, GA (listing conflicts: header remote, work-mode field on-site)",
    "Remote (global; London preferred)",
    "Hybrid/remote (US) - LOCATION HOLD",
    "Remote (US); hybrid only if within 30 miles of an Oakfield office",
    "Remote or hybrid in Gotham, NY",
    "United States Work at Home",  # a board's way of saying remote
    "Telecommuter XX",
    "US Telecommuter",
    "Work from home - United States",
]
SAYS_NO_PLACE_IN_WORDS = [
    "Not stated (HOLD)",
    "US, work mode not stated",
    "United States (no city or work mode in JD)",
    "Not stated in posting",
    "Location not stated",
    "Unknown (posting does not say)",
    "Multiple Locations (US)",
    "Hybrid",  # a way of working is no town
    "On-site / Hybrid",
    "United States, in-office",
]


@pytest.mark.parametrize("location", REMOTE_AMONG_OTHER_WORDS)
@pytest.mark.parametrize("mode", ["unknown", None, "local", "lateral"])
def test_a_place_that_says_it_is_remote_with_no_work_mode_passes_when_remote_work_is_on(location, mode):
    """The role can be worked remote, so a hybrid, an on-site or a town beside the word is not held against it."""
    assert g.location_ok(location, mode, make_gates()) is True
    assert g.location_ok(location, mode, make_gates(remote_countries=["United States"])) is True, "no other country"
    role = {"company": "Northwind", "title": "Director", "fit": 5, "location": location, "work_mode": mode}
    assert g.role_facts(role, make_gates(), TODAY)["location_ok"] is True


@pytest.mark.parametrize("location", SAYS_NO_PLACE_IN_WORDS)
@pytest.mark.parametrize("mode", ["unknown", None, "hybrid", "onsite", "restricted_remote", "local"])
def test_a_place_that_says_nothing_in_words_is_unknown_and_never_a_fail(location, mode):
    """A note beside a placeholder or a bare country does not make a town of it: the role is kept with a note to check
    the place, not Gated out."""
    assert g.location_ok(location, mode, make_gates()) is None
    assert g.location_ok(location, mode, make_gates(remote_countries=["United States"])) is None


def test_remote_work_in_named_countries_with_a_place_that_says_nothing_in_words_is_unknown():
    gates = make_gates(remote_countries=["United States"])
    for place in ("Not stated (HOLD)", "Location not stated", "Unknown (posting does not say)"):
        assert g.location_ok(place, "remote", gates) is None, place
    assert g.location_ok("Remote (US)", "remote", gates) is True


def test_a_place_that_is_remote_in_another_country_still_fails_with_countries_named():
    gates = make_gates(remote_countries=["United States"])
    for place in ("Remote - Manila, Philippines", "Remote (Canada)", "Remote - London", "Remote (London preferred)"):
        assert g.location_ok(place, "unknown", gates) is False, place
    assert g.location_ok("Remote (Canada) or hybrid in Oakfield, ST", "unknown", gates) is True, "a listed town"
    assert g.location_ok("Remote (Canada) or hybrid in Denver", "unknown", gates) is False, "and not at an unlisted one"
    # Open in every country is open in the owner's, whatever city the posting prefers.
    assert g.location_ok("Remote (global; London preferred)", "unknown", gates) is True
    assert g.location_ok("Remote (global; London preferred)", "remote", gates) is True
    assert g.location_ok("Remote - worldwide", "unknown", gates) is True
    assert g.location_ok("Remote (global)", "unknown", make_gates(remote_countries=["Canada"])) is True
    assert g.location_ok("Remote (global)", "unknown", make_gates(remote_ok=False)) is False, "remote work is off"


def test_a_place_that_says_it_is_remote_fails_when_remote_work_is_off_unless_it_names_a_town_to_work_from():
    off = make_gates(remote_ok=False)
    for place in ("Remote - US", "United States Work at Home", "Telecommuter XX", "Remote (Oakfield, ST, US)",
                  "Remote or hybrid in Denver", "Remote or hybrid in Gotham, NY"):
        assert g.location_ok(place, "unknown", off) is False, place
    assert g.location_ok("Remote or hybrid in Oakfield, ST", "unknown", off) is True, "hybrid at a listed town"
    assert g.location_ok("Remote or on-site in Oakfield", "unknown", off) is True
    assert g.location_ok("Hybrid/remote (US) - LOCATION HOLD", "unknown", off) is None, "no town named"


@pytest.mark.parametrize("location", [
    "Gotham, NY (on-site, not remote)", "Gotham, NY (remote not available)", "No remote work - Gotham, NY",
    "On-site in Gotham, NY; non-remote", "Gotham, NY (hybrid)", "Hybrid - Gotham, NY",
    # Remote some days is hybrid: the days it names are the ones in the office, not a remote role.
    "Gotham, NY (Hybrid - Tues to Thurs onsite, Mon and Fri remote, per the JD)", "Gotham, NY (hybrid, 2 days remote)",
])
def test_a_town_that_is_not_listed_still_fails_when_the_text_does_not_say_remote(location):
    """Remote that is denied is not remote, and a place that names no remote work stays judged on its towns."""
    assert g.location_ok(location, "unknown", make_gates()) is False
    assert g.location_ok(location, "onsite", make_gates()) is False


@pytest.mark.parametrize("location", ["Gotham, NY / Denver, CO", "Greater Gotham Area", "Gotham, NY"])
def test_a_town_that_is_not_listed_is_unknown_with_no_work_mode_and_fails_once_the_work_mode_is_on_site(location):
    """Nothing says the role is worked in person, so the gate waits (and asks again when the work mode is known)."""
    assert g.location_ok(location, "unknown", make_gates()) is None
    assert g.location_ok(location, "onsite", make_gates()) is False
    assert g.location_ok(location, "remote", make_gates()) is True


def test_a_hybrid_place_with_remote_days_is_judged_on_its_town_and_a_remote_that_is_not_stated_is_not_denied():
    assert g.location_ok("Oakfield, ST (Hybrid - Mon and Fri remote)", "unknown", make_gates()) is True
    assert g.location_ok("Gotham, NY (Hybrid - Mon and Fri remote)", "unknown", make_gates()) is False
    # "remote not stated" is not a "no remote": a role the word kept before is kept now.
    assert g.location_ok("Gotham, NY (remote not stated)", "unknown", make_gates()) is not False
    # Days in the office beside a remote role (not a hybrid one) are not a hybrid role.
    assert g.location_ok("Remote US (Gotham-area hires 4 days in office)", "unknown", make_gates()) is True
    # A remote that is said to be unavailable is none, and a bare country beside it names no town.
    assert g.location_ok("United States (remote not available)", "unknown", make_gates()) is None
    assert g.location_ok("Gotham, NY (remote not available)", "unknown", make_gates()) is False


def test_the_board_filter_keeps_a_posting_whose_place_is_a_way_of_saying_remote_or_says_nothing():
    """`Wanted.keeps` asks `location_ok` with no work mode (and then each mode the board might list the posting as): a
    posting is dropped only when every reading fails."""
    for place in ("United States Work at Home", "Telecommuter XX", "US Telecommuter", "Not stated in posting"):
        assert g.location_ok(place, None, make_gates()) is not False, place


def test_a_place_that_says_nothing_is_unknown_for_every_way_of_working_and_never_a_pass_or_a_fail():
    gates = make_gates()
    for place in ("", None, "   ", "26 Locations", "Multiple Locations", "N/A"):
        for mode in ("onsite", "hybrid", "unknown", None, "restricted_remote"):
            assert g.location_ok(place, mode, gates) is None, (place, mode)
    # With no work location gate there is nothing to judge: every place passes, as before.
    assert g.location_ok("26 Locations", "onsite", g.Gates()) is True


def test_remote_work_in_named_countries_with_no_place_stated_is_unknown_and_not_a_pass():
    gates = make_gates(remote_countries=["United States"])
    for place in ("", None, "26 Locations", "Multiple Locations"):
        assert g.location_ok(place, "remote", gates) is None, place
    assert g.location_ok("Remote", "remote", gates) is True, "a place that is stated, with no country in it"
    assert g.location_ok("Remote, US", "remote", gates) is True and g.location_ok("United States", "remote", gates)
    assert g.location_ok("Manila, Philippines", "remote", gates) is False
    assert g.location_ok("", "remote", make_gates()) is True, "no country is asked for: nothing to tell"
    assert g.location_ok("", "remote", make_gates(remote_ok=False)) is False, "remote work is off"


def test_a_count_of_places_is_a_count_and_not_a_place():
    assert (g.counted_places("26 Locations"), g.counted_places("1 location"), g.counted_places(" 2 LOCATIONS. ")) == (
        26, 1, 2)
    assert g.counted_places("Multiple Locations") is None and g.counted_places("Oakfield, ST") is None
    assert g.counted_places("2 Locations; Oakfield, ST") is None and g.counted_places(None) is None
    for nothing in ("", None, "2 locations", "Multiple Locations", "Not specified", "TBD", "n / a", "--"):
        assert g.says_no_place(nothing), nothing
    for place in ("Oakfield, ST", "Remote", "United States", "Oakfield and 2 other locations", "Locations in Ohio"):
        assert not g.says_no_place(place), place


def test_a_roles_place_that_says_nothing_is_not_a_pass_in_what_the_process_compares():
    """The screening process drops a role only for `location_ok = false`; a place that says nothing is `None` (kept,
    with a note to check it), so the role page can say "unknown" instead of "pass"."""
    for place in ("", None, "26 Locations"):
        role = {"company": "Northwind", "title": "Director", "fit": 5, "location": place, "work_mode": "hybrid"}
        assert g.role_facts(role, make_gates(), TODAY)["location_ok"] is None, place


def test_remote_work_that_is_off_fails_a_remote_posting():
    assert g.location_ok("Anywhere", "remote", make_gates(remote_ok=False)) is False
    assert g.location_ok("Remote - US", "unknown", make_gates(remote_ok=False)) is False
    assert g.location_ok("Oakfield, ST", "onsite", make_gates(remote_ok=False)) is True


def test_remote_work_in_named_countries_fails_a_posting_in_another_country():
    gates = make_gates(remote_countries=["United States"])
    assert g.location_ok("Remote, US", "remote", gates) is True
    assert g.location_ok("Remote, Ohio", "remote", gates) is True, "a state of the allowed country"
    assert g.location_ok("Manila, Philippines", "remote", gates) is False
    assert g.location_ok("Remote", "remote", gates) is True, "no country named: not proven abroad"
    assert g.location_ok("Manila, Philippines", "remote", make_gates()) is True, "no countries named: any country"


def test_each_way_of_working_has_its_own_places_and_can_be_switched_off():
    gates = make_gates(hybrid_places=["oakfield"], onsite_places=["riverton"])
    assert g.location_ok("Oakfield, ST", "hybrid", gates) is True
    assert g.location_ok("Riverton, ST", "hybrid", gates) is False, "that place counts for in person only"
    assert g.location_ok("Riverton, ST", "onsite", gates) is True
    assert g.location_ok("Oakfield, ST", "unknown", gates) is True, "no way stated: either's places will do"
    no_hybrid = make_gates(hybrid_ok=False)
    assert g.location_ok("Oakfield, ST", "hybrid", no_hybrid) is False
    assert g.location_ok("Oakfield, ST", "onsite", no_hybrid) is True
    neither = make_gates(hybrid_ok=False, onsite_ok=False)
    assert g.location_ok("Oakfield, ST", "unknown", neither) is None, "no way stated: not known to be in person"
    assert g.location_ok("Anywhere", "remote", neither) is True, "remote is its own switch"


def test_a_way_of_working_that_is_on_with_no_places_takes_any_place():
    gates = make_gates(hybrid_places=[], onsite_places=[])
    assert g.location_ok("Denver, CO", "hybrid", gates) is True
    assert g.location_ok("Gotham, NY", "onsite", gates) is True, "no list of places to avoid: nothing is refused"
    assert g.location_ok("", "onsite", gates) is None


def test_a_posting_on_site_in_a_place_that_is_not_listed_fails_whatever_else_it_lists():
    gates = make_gates()
    assert g.location_ok("Gotham, NY", "onsite", gates) is False, "not a place the owner works from"
    assert g.location_ok("Gotham, NY", "hybrid", gates) is False
    assert g.location_ok("Gotham, NY or Oakfield, ST", "hybrid", gates) is True, "one allowed place is enough"
    assert g.location_ok("Gotham, NY", "unknown", make_gates(hybrid_places=["oakfield"], onsite_ok=False)) is None


def test_a_listed_town_with_its_state_is_named_by_that_town_in_that_state_only():
    """The same town name is in many states: a town listed with its state is not named by another state's."""
    gates = make_gates(hybrid_places=["oakfield, oh", "riverton"], onsite_places=["oakfield, oh", "riverton"])
    for mode in ("onsite", "hybrid", "unknown"):
        assert g.location_ok("Oakfield, OH", mode, gates) is True, mode
        assert g.location_ok("Oakfield, Ohio", mode, gates) is True and g.location_ok("US-OH-Oakfield", mode, gates)
        not_listed = None if mode == "unknown" else False  # no way of working stated: not known to be in person
        assert g.location_ok("Oakfield, NJ", mode, gates) is not_listed, mode
        assert g.location_ok("Oakfield, TN", mode, gates) is not_listed
        assert g.location_ok("US-TN-Oakfield", mode, gates) is not_listed
        assert g.location_ok("Oakfield", mode, gates) is None, "no state beside the town: it cannot be told"
        assert g.location_ok("Oakfield Campus", mode, gates) is None
        assert g.location_ok("Oakfield, NJ; Riverton, ST", mode, gates) is True, "another listed place is named"
        assert g.location_ok("Oakfield, NJ; Oakfield", mode, gates) is None
        assert g.location_ok("Oakfield, NJ; Oakfield, OH", mode, gates) is True, "one allowed place is enough"
    assert g.location_ok("Riverton, NJ", "onsite", gates) is True, "a town listed alone is named in any state"


def test_a_place_the_posting_names_with_no_state_is_not_a_pass_when_the_listed_one_has_one():
    gates = make_gates(hybrid_places=["oakfield, oh"], onsite_places=[])
    assert g.location_ok("Oakfield", "hybrid", gates) is None
    assert g.location_ok("Oakfield", "onsite", gates) is True, "in person has no places listed: anywhere"
    facts = g.role_facts({"company": "Northwind", "title": "Director", "fit": 5, "location": "Oakfield",
                          "work_mode": "hybrid"}, gates, TODAY)
    assert facts["location_ok"] is None, "kept with a note to check the place, not passed and not dropped"


@pytest.mark.parametrize("word", ["local", "high", "unverified", "promotion", "lateral", "Local ", "on-site", "weird"])
def test_a_work_mode_that_is_none_of_the_known_ones_reads_as_unknown_and_not_as_a_mode_that_fails(word):
    """A tracker's imported move types ("local", "lateral" ...) are not ways of working."""
    gates = make_gates()
    for place, expected in (("Remote - US", True), ("Oakfield, ST", True), ("Gotham, NY", None), ("", None),
                            ("26 Locations", None)):
        assert g.location_ok(place, word, gates) is expected is g.location_ok(place, "unknown", gates), (place, word)
    assert g.location_ok("Remote - US", word, make_gates(remote_ok=False)) is False, "remote is off"
    assert g.location_ok("Remote, Manila, Philippines", word, make_gates(remote_countries=["United States"])) is False
    facts = g.role_facts({"company": "Northwind", "title": "Director", "fit": 5, "location": "Remote - US",
                          "work_mode": word}, gates, TODAY)
    assert facts["location_ok"] is True


def test_a_known_work_mode_is_read_without_regard_to_case_or_spaces():
    gates = make_gates()
    assert g.location_ok("Gotham, NY", " Remote ", gates) is True
    assert g.location_ok("Gotham, NY", "ONSITE", gates) is False
    assert g.location_ok("Oakfield, ST", "Restricted_Remote", gates) is True


# --- employers -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("company", "excluded"), [
    ("Globex", True), ("Globex Corporation", True), ("GLOBEX Labs", True), ("Acme Holdings LLC", True),
    ("Globexa Tech", False),  # whole words only
    ("Northwind", False), ("", False), (None, False),
])
def test_excluded_employer(company, excluded):
    assert g.is_excluded(company, make_gates()) is excluded


# --- pay -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "low", "high"), [
    ("$120,000 - $160,000", 120_000, 160_000),
    ("$120K-$160K", 120_000, 160_000),
    ("up to $150K", 150_000, 150_000),
    ("$65 - $80 per hour", 135_200, 166_400),
    # an hourly range in every wording a held posting uses, as a 2,080-hour year (made-up figures)
    ("$40 - $52 hourly", 83_200, 108_160),
    ("$40 - $520 hourly", 83_200, 1_081_600),
    ("USD 40–50/hour", 83_200, 104_000),
    ("USD 400-500/hour", 832_000, 1_040_000),
    ("USD 48.00 - 50.00 per hour", 99_840, 104_000),
    ("USD 45.50 per hour", 94_640, 94_640),
    ("USD48.0/HOURLY - USD50.0/HOURLY", 99_840, 104_000),
    ("USD 40 to 50 an hour", 83_200, 104_000),
    ("$40 — 52 per hour", 83_200, 108_160),
    ("USD 120,000 - 150,000", 120_000, 150_000),
    ("$6,000 - 7,000 a month", 72_000, 84_000),
    # a number with no currency mark in its range is not pay
    ("5 days a week", None, None),
    ("401k match", None, None),
    ("2,080 hours a year", None, None),
    ("$40 - $52 hourly plus a $5,000 signing bonus", 83_200, 108_160),
    ("$40 - 52 hourly plus a signing bonus of 5,000", 83_200, 108_160),
    # A bonus is not the pay, and a month's pay is twelve of them
    ("$5,000 signing bonus", None, None),
    ("$120,000 - $140,000 plus a $5,000 signing bonus", 120_000, 140_000),
    ("Salary $120K, signing bonus of $10,000", 120_000, 120_000),
    ("$130,000 base, bonus up to $20,000, $5,000 relocation stipend", 130_000, 130_000),
    ("$120,000 plus bonus", 120_000, 120_000),
    ("$6,500 per month", 78_000, 78_000),
    ("$5,000 - $6,000 a month", 60_000, 72_000),
    ("$120,000 a year ($10,000 per month)", 120_000, 120_000),
    ("Competitive", None, None),
    ("", None, None),
    (None, None, None),
])
def test_comp_text(text, low, high):
    assert g.parse_comp_text(text) == (low, high)


def test_a_match_key_folds_every_run_of_whitespace_so_every_copy_of_a_role_has_the_same_key():
    """A posting's key was only stripped, a reported role's was folded: a double space or a non-breaking space
    in an ATS title left the role unlinked from its posting, which went to a reader again and was dropped as a
    duplicate."""
    assert g.match_key("Acme  Co", " Lead\u00a0Programs\t") == "acme co|lead programs"
    assert g.match_key("Acme Co", "Lead Programs") == g.match_key("ACME\u00a0CO", "lead  programs")
    assert g.match_key(None, "Lead") == "|lead"


def test_the_ask_is_the_middle_of_the_band_and_a_very_wide_band_is_not_judged():
    assert g.provisional_ask(100_000, 140_000) == 120_000
    assert g.provisional_ask(100_000, 150_000) == 125_000  # exactly 1.5x is still judged
    assert g.provisional_ask(100_000, 150_001) is None  # over 1.5x: too wide, kept
    assert g.provisional_ask(130_000, None) == 130_000
    assert g.provisional_ask(None, None) is None


def pay_gates(against: str, amount: int = 100_000) -> g.Gates:
    return g.Gates(comp_floor=amount, comp_against=against)


def test_the_pay_gate_compares_the_amount_with_the_figure_the_owner_chose():
    band = (95_000, 130_000)  # the middle, 112,500
    assert g.pay_check(*band, pay_gates("minimum")) == ("fail", 95_000, "posted minimum")
    assert g.pay_check(*band, pay_gates("maximum")) == ("pass", 130_000, "posted maximum")
    assert g.pay_check(*band, pay_gates("ask")) == ("pass", 112_500, "estimated ask")
    assert g.pay_check(77_500, 112_500, pay_gates("ask")) == ("fail", 95_000, "estimated ask")


def test_the_estimated_ask_is_the_analysts_once_there_is_one_and_the_middle_of_the_band_until_then():
    assert g.pay_check(90_000, 130_000, pay_gates("ask"), worked=95_000) == ("fail", 95_000, "estimated ask")
    assert g.pay_check(90_000, 130_000, pay_gates("maximum"), worked=95_000)[0] == "pass", "posted pay was chosen"


def test_with_no_posted_pay_the_estimated_ask_is_used_whatever_was_chosen_and_with_neither_it_passes():
    for against in ("minimum", "maximum", "ask"):
        assert g.pay_check(None, None, pay_gates(against), worked=90_000) == ("fail", 90_000, "estimated ask")
        assert g.pay_check(None, None, pay_gates(against)) == ("unknown", None, "")


def test_one_posted_figure_is_both_the_minimum_and_the_maximum_and_a_wide_band_with_a_low_top_still_fails():
    assert g.pay_check(None, 99_000, pay_gates("minimum")) == ("fail", 99_000, "posted minimum")
    assert g.pay_check(120_000, None, pay_gates("maximum")) == ("pass", 120_000, "posted maximum")
    wide = g.pay_check(40_000, 99_000, pay_gates("ask"))  # too wide to find the middle, but no ask can top 99,000
    assert wide == ("fail", 99_000, "posted maximum")
    assert g.pay_check(60_000, 200_000, pay_gates("ask"))[0] == "unknown", "too wide, and the top clears it"


# --- age -------------------------------------------------------------------------------------------------------------


def test_age_comes_from_the_postings_own_date_else_the_day_it_was_found_else_nothing():
    assert g.age_in_days(date(2026, 9, 6), None, TODAY) == 30
    assert g.age_in_days(None, date(2026, 10, 1), TODAY) == 5
    assert g.age_in_days(date(2026, 9, 6), date(2026, 10, 5), TODAY) == 30, "the employer's date wins"
    assert g.age_in_days(None, None, TODAY) is None, "an unknown age is never stale"
    assert g.age_in_days(date(2026, 10, 9), None, TODAY) == 0, "a date in the future is not negative"


def test_a_roles_facts_are_what_the_process_compares():
    gates = make_gates()
    facts = g.role_facts({
        "company": "Globex", "title": "Director, Programs", "fit": 5, "location": "Oakfield, ST",
        "work_mode": "onsite", "comp": "$100,000 - $125,000", "posted": "2026-09-01",
    }, gates, TODAY)
    assert facts == {
        "key": "globex|director, programs", "fit": 5, "age_days": 35, "excluded": True,
        "location_ok": True, "location": "Oakfield, ST", "comp_max": 125_000, "ask": 112_500, "comp_judged": None,
        "comp_from": "", "comp_floor": 0, "stale_days": 100_000, "says": {},
    }
    judged = g.role_facts({"company": "Northwind", "title": "Lead", "comp": "$100,000 - $120,000"},
                          make_gates(comp_floor=115_000, comp_against="maximum"), TODAY)
    assert (judged["comp_judged"], judged["comp_from"]) == (120_000, "posted maximum")


def test_the_match_key_is_the_trackers_lowercased_company_and_title():
    assert g.match_key("  Northwind Bank ", "Senior Director") == "northwind bank|senior director"
    assert g.match_key(None, None) == "|"


@pytest.mark.parametrize("place", [
    "Indiana", "Indiana, United States", "Indiana, US", "Texas, USA", "IN", "Ohio (no city in JD)",
])
def test_a_state_alone_names_no_town_so_the_place_is_unknown_not_a_fail(place):
    assert g.names_a_town(place) is False
    assert g.location_ok(place, "onsite", make_gates()) is None
    assert g.location_ok(place, "hybrid", make_gates()) is None


@pytest.mark.parametrize(
    "place", ["Oakfield, OH", "Riverton, IN", "Lakeside, MA", "New York, NY", "Ohio, PA", "Oakfield, Ohio"])
def test_a_town_with_its_state_still_names_a_town(place):
    assert g.names_a_town(place) is True
