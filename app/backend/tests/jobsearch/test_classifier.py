"""The classifier (docs/PRD.md 29.5, 29.6 and 31.1): learns from one owner's own roles, says why, and ranks the filter's
survivors for the title pass. It stops none, whatever it rates. Synthetic roles only: every title and word here is made
up."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import classifier
from app.jobsearch.gating import sift
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.schema import Settings
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, gate_says, load_profile

NOW = datetime(2026, 10, 9, 5, 30, tzinfo=UTC)

#: Roles the made-up owner moved forward, and roles they passed on.
LIKED = ["Director of Governance and Risk", "Governance Risk Program Lead", "Risk and Compliance Program Director",
         "Head of Risk Governance", "Compliance Program Manager, Governance", "Governance Lead, Enterprise Risk",
         "Senior Program Director, Risk", "Risk Governance Manager"]
DISLIKED = ["Warehouse Packing Associate", "Payroll Specialist", "Forklift Operator", "Packing Line Associate",
            "Retail Sales Associate", "Warehouse Forklift Lead", "Payroll Clerk", "Store Sales Clerk"]
BODY = "You will lead the work with partners across the business and report to the leadership team."


@pytest.fixture(autouse=True)
def fresh_cache():
    classifier.clear_cache()
    yield
    classifier.clear_cache()


def _role(n, title, **more):
    return Opportunity(owner=OWNER, slug=f"co-{n}", company=f"Co {n}", title=title, match_key=f"co {n}|{title.lower()}",
                       origin="scout", **more)


def _history(liked=LIKED, disliked=DISLIKED, repeat=3):
    """Roles for the owner: the liked ones applied to, the disliked ones passed on."""
    roles = []
    for r in range(repeat):
        for i, t in enumerate(liked):
            roles.append(_role(f"p{r}{i}", t, status="applied", fit=4, description=BODY))
        for i, t in enumerate(disliked):
            roles.append(_role(f"n{r}{i}", t, status="withdrew", ended_reason="i_passed", fit=1, description=BODY))
    return roles


async def _profile(session, criteria=None, **settings):
    await load_profile(session, OWNER, criteria={**PROFILE["criteria"], **(criteria or {})},
                       settings={**PROFILE["settings"], **settings})
    return await profile_store.current(session, OWNER)


def _posting(employer, ext_id, title, **more):
    more = {"location": "Remote", "work_mode": "remote", **more}
    return JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=ext_id, company=employer.name,
                      title=title, first_seen=NOW, last_seen=NOW, links=[], **more)


def test_each_setting_has_its_limits():
    assert Settings(site_projects_per_night=-1).site_projects_per_night == -1, "-1: no limit (limits.py)"
    for bad in ({"classifier_min_examples": 0}, {"classifier_min_examples": 10001}, {"site_projects_per_night": -2}):
        with pytest.raises(ValidationError):
            Settings(**bad)


def test_the_features_are_lowercase_words_and_pairs_without_stop_words():
    f = classifier.features("The Director of Data Governance, and a Risk Lead")
    assert "director" in f and "governance" in f and "data governance" in f and "risk lead" in f
    assert "the" not in f and "of" not in f
    assert "governance risk" not in f, "a pair never spans a comma"
    assert "director data" not in f, "or a stop word"


def test_the_text_is_the_title_the_department_and_only_the_first_words_of_the_body():
    body = " ".join(f"w{i}" for i in range(500))
    text = classifier.text_of("Lead", "Finance", body)
    assert "w199" in text and "w200" not in text and "Finance" in text
    assert classifier.features(text)["lead"] == 2, "the title is said twice"


def test_the_prior_comes_from_the_weights():
    even = classifier.build([("alpha", True, 1.0), ("beta", False, 1.0)])
    heavy = classifier.build([("alpha", True, 2.0), ("beta", False, 1.0)])
    assert even.predict("nothing known") == pytest.approx(0.5)
    assert heavy.predict("nothing known") == pytest.approx(2 / 3)
    assert heavy.predict("alpha") > even.predict("alpha"), "a weight-2 positive counts for more"


def test_a_trained_model_rates_a_matching_title_higher_and_says_why():
    model = classifier.build([(classifier.text_of(t, None, BODY), True, 2.0) for t in LIKED * 3]
                             + [(classifier.text_of(t, None, BODY), False, 1.0) for t in DISLIKED * 3])
    good = classifier.text_of("Governance and Risk Director", None, BODY)
    bad = classifier.text_of("Payroll Packing Associate", None, BODY)
    assert model.predict(good) > 0.8 > 0.2 > model.predict(bad)
    assert 0 < model.predict(good) < 1
    why = model.why(good)
    assert 1 <= len(why) <= 3 and any("governance" in w or "risk" in w for w in why)
    assert len({w for phrase in why for w in phrase.split()}) == sum(len(p.split()) for p in why), "no word twice"
    assert model.why(bad) == [] or not any("governance" in w for w in model.why(bad))
    assert model.why("qwertyuiop") == [], "nothing known, nothing to say"


async def test_the_cold_start_is_no_model(js):
    async with db.session_factory()() as session:
        await _profile(session, classifier_min_examples=10)
        session.add_all(_history(LIKED[:5], DISLIKED, repeat=1))  # 5 positives, 8 negatives
        await session.flush()
        assert await classifier.train(session, OWNER) is None
        await _profile(session, classifier_min_examples=5)
        classifier.clear_cache()
        model = await classifier.train(session, OWNER)
        assert model is not None and model.summary()["positives"] == 5


async def test_it_learns_only_from_the_owners_roles_with_the_agreed_weights(js):
    async with db.session_factory()() as session:
        await _profile(session)
        moved = _role(1, "Moved On", status="applied", fit=1)
        drafted = _role(2, "Drafted", status="ready_to_launch")
        ended = _role(3, "Ended After Applying", status="applied", fit=1)
        # No history, but the employer said no: they had applied (docs/PRD.md 31.7), whatever its fit.
        ended_cold = _role(4, "Ended Cold", status="rejected", fit=1)
        ended_blank = _role(5, "Ended Unscored", status="gone_quiet")  # no history, no fit: says nothing
        # A score, high or low, teaches nothing (docs/PRD.md 39.10): there is no research bar to clear or sit under.
        high = _role(6, "High Fit", status="second_impression", fit=4)
        low = _role(7, "Low Fit", status="second_impression", fit=1)
        passed = _role(9, "Passed On", status="withdrew", ended_reason="i_passed", fit=4)
        # Stopped by a gate that is not about the kind of job (no reason given: it says nothing of the kind of job).
        gated = _role(8, "Gated", status="gated_out", fit=4)
        other = Opportunity(owner="someone-else", slug="x", company="X", title="Not Mine", match_key="x|not mine",
                            status="applied", fit=4)
        session.add_all([moved, drafted, ended, ended_cold, ended_blank, high, low, passed, gated, other])
        await session.flush()
        ended.status = "rejected"
        await session.flush()
        learned = await classifier.examples(session, OWNER)
        got = {text.split(".")[0]: (positive, weight) for text, positive, weight in learned}
    assert got == {
        "Moved On": (True, 2.0), "Drafted": (True, 2.0), "Ended After Applying": (True, 2.0),
        "Ended Cold": (True, 2.0), "Passed On": (False, 1.0),
    }, "a gate stop is no example at all, and neither is a score"


async def test_postings_that_never_became_roles_balance_an_owner_with_few_negatives(js):
    unrelated = ["Night Shift Nurse", "Payroll Specialist", "Line Cook", "Delivery Driver", "Dental Hygienist",
                 "Retail Cashier", "Forklift Operator", "Pharmacy Technician", "Bus Driver", "Barista"]
    async with db.session_factory()() as session:
        await _profile(session, classifier_min_examples=5)
        session.add_all(_history(LIKED, DISLIKED[:1], repeat=3))  # 24 roles moved forward, 3 turned down
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([_posting(acme, f"b{r}{i}", t, text=BODY) for r in range(10) for i, t in enumerate(unrelated)])
        mine = _posting(acme, "linked", "Night Shift Nurse")
        session.add(mine)
        await session.flush()
        mine.opportunity_id = (await session.scalars(select(Opportunity.id).limit(1))).first()
        await session.flush()
        learned = await classifier.examples(session, OWNER)
        pos = sum(w for _t, p, w in learned if p)
        neg = sum(w for _t, p, w in learned if not p)
        extra = await classifier.background(session, OWNER, pos, neg)
        assert len(extra) == 100, "only postings that never became roles"
        assert sum(w for *_x, w in extra) == pytest.approx(pos - neg), "the two sides weigh the same"
        assert await classifier.background(session, OWNER, neg, pos) == [], "nothing when negatives already outweigh"
        model = await classifier.train(session, OWNER)
    assert model.predict(classifier.text_of("Night Shift Nurse", None, BODY)) < 0.2
    assert model.predict(classifier.text_of("Director of Risk Governance", None, BODY)) > 0.8


async def test_a_posting_counts_for_no_more_than_a_turned_down_role(js):
    async with db.session_factory()() as session:
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(_posting(acme, "one", "Line Cook"))
        await session.flush()
        assert [w for *_x, w in await classifier.background(session, OWNER, 50.0, 2.0)] == [1.0]


async def test_the_model_is_kept_until_the_roles_change(js):
    async with db.session_factory()() as session:
        await _profile(session, classifier_min_examples=5)
        session.add_all(_history(repeat=1))
        await session.flush()
        first = await classifier.train(session, OWNER)
        assert first is not None and await classifier.train(session, OWNER) is first
        session.add(_role("late", "Governance Fellow", status="applied", fit=4))
        await session.flush()
        assert await classifier.train(session, OWNER) is not first


#: Three postings a trained model rates high, in the middle and low.
LIKELY, MIDDLING, UNLIKELY = ("Director of Governance and Risk Programs", "Quantum Basket Weaver", "Payroll Clerk")


async def _rated(session, **settings):
    """A made-up owner with history, the three postings, and the model's rating of each. The owner's turned-down roles
    weigh as much as the ones moved forward, so no posting is learned from as background and a title made of words the
    model never saw sits in the middle."""
    await _profile(session, classifier_min_examples=6, **settings)
    session.add_all(_history(LIKED[:4], DISLIKED))
    acme = await employers.find_or_make(session, OWNER, "Fabrikam")
    session.add_all([_posting(acme, "r", LIKELY), _posting(acme, "m", MIDDLING), _posting(acme, "n", UNLIKELY)])
    await session.flush()
    model = await classifier.train(session, OWNER)
    rating = {t: model.predict(classifier.text_of(t, None, None)) for t in (LIKELY, MIDDLING, UNLIKELY)}
    assert rating[LIKELY] > rating[MIDDLING] > rating[UNLIKELY], rating
    return rating


async def _sifted(session, criteria=None, **settings):
    await _profile(session, criteria, classifier_min_examples=6, **settings)
    return await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)


def test_the_classifier_learns_from_twenty_examples_unless_the_owner_says_otherwise():
    assert Settings().classifier_min_examples == 20


def test_the_bar_the_classifier_stopped_under_and_the_share_it_double_checked_are_settings_no_more():
    """The classifier only ranks (docs/PRD.md 31.1): the two settings are gone, and a Settings that still holds one is
    refused, not quietly read (a migration took them out of everything stored)."""
    assert not {"classifier_trim", "classifier_audit_share"} & set(Settings.model_fields)
    assert "classifier_trim" not in Settings().model_dump() and "classifier_audit_share" not in Settings().model_dump()
    for old in ({"classifier_trim": 0.05}, {"classifier_audit_share": 0.02}):
        with pytest.raises(ValidationError):
            Settings(**old)


async def test_without_a_model_every_posting_the_code_lets_through_goes_to_a_role(js):
    async with db.session_factory()() as session:
        await _profile(session, classifier_min_examples=6)
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([_posting(acme, "r", LIKELY), _posting(acme, "n", UNLIKELY)])
        await session.flush()
        result = await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)
        by = {c["posting"].ext_id: c for c in result["candidates"]}
        assert result["model"] is None and set(by) == {"r", "n"}
        assert {c["route"] for c in by.values()} == {"roles"}
        assert by["r"]["posting"].route_why == "no history to learn from yet" and by["r"]["posting"].fit_prob is None
        assert result["routes"] == {"roles": 2} and result["stopped"] == {}


async def test_with_a_model_every_posting_goes_to_a_role_ranked_and_none_is_stopped(js):
    async with db.session_factory()() as session:
        rating = await _rated(session)
        result = await _sifted(session)
        by = {c["posting"].ext_id: c for c in result["candidates"]}
        assert result["model"] is not None and set(by) == {"r", "m", "n"}
        assert {c["route"] for c in by.values()} == {"roles"}, "no posting goes to a reader on the classifier's word"
        assert by["r"]["posting"].fit_prob == pytest.approx(rating[LIKELY]), "the rating is kept, to order the check"
        assert by["m"]["posting"].fit_prob == pytest.approx(rating[MIDDLING])
        assert by["n"]["posting"].fit_prob == pytest.approx(rating[UNLIKELY]), "the unlikely one is ranked as well"
        assert by["r"]["why"].startswith("ranked ") and by["m"]["why"] == f"ranked {rating[MIDDLING]:.2f}"
        assert "governance" in by["r"]["why"] or "risk" in by["r"]["why"], "it says what put it high"
        assert result["routes"] == {"roles": 3} and result["stopped"] == {}
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
        assert all(p.filtered_at and p.filtered_by is None and p.route == "roles" for p in rows.values())
        assert all(p.judged_by is None and p.judged_by_version is None for p in rows.values()), (
            "waiting for the tabs: nothing has judged any of them yet")


async def test_no_posting_is_stopped_by_the_classifier_at_any_rating(js):
    """The classifier used to stop what it rated under a bar (0.05 by default). It stops nothing now: a posting made of
    words the owner only ever turned down, rated far under that bar, is let through like the likeliest one."""
    nearly_never = "Warehouse Forklift Payroll Packing Retail Store Clerk Operator"
    async with db.session_factory()() as session:
        await _rated(session)
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(_posting(acme, "low", nearly_never))
        await session.flush()
        model = await classifier.train(session, OWNER)
        low = model.predict(classifier.text_of(nearly_never, None, None))
        assert low < 0.05, f"a rating under the bar the classifier once stopped by, not {low:.3f}"
        result = await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)
        rows = {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}
    assert result["stopped"] == {} and result["routes"] == {"roles": 4}
    assert {c["posting"].ext_id for c in result["candidates"]} == {"r", "m", "n", "low"}
    assert (rows["low"].filtered_by, rows["low"].route, rows["low"].judged_by) == (None, "roles", None)
    assert rows["low"].fit_prob == pytest.approx(low) and rows["low"].fit_prob < rows["r"].fit_prob
    assert not any(p.filtered_by for p in rows.values())


async def test_the_postings_are_ranked_the_likeliest_first_and_the_least_likely_last(js):
    async with db.session_factory()() as session:
        rating = await _rated(session)
        await _sifted(session)
        ranked = (await session.scalars(select(JobPosting).where(JobPosting.owner == OWNER).order_by(
            JobPosting.fit_prob.desc().nulls_last(), JobPosting.first_seen))).all()
    assert [p.title for p in ranked] == [LIKELY, MIDDLING, UNLIKELY], "the likeliest first, the least likely last"
    assert [p.fit_prob for p in ranked] == pytest.approx([rating[LIKELY], rating[MIDDLING], rating[UNLIKELY]])


async def test_a_gate_stop_records_the_gate_as_the_step(js):
    async with db.session_factory()() as session:
        await _profile(session, classifier_min_examples=6)
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        gone = _posting(acme, "g", LIKELY, location="Greater Gotham", work_mode="onsite")
        session.add(gone)
        await session.flush()
        await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)
        assert (gone.judged_by, gone.judged_by_version) == ("gate", 1)


async def test_a_posting_the_owner_asked_to_read_anyway_counts_as_a_role_they_moved_forward(js):
    async with db.session_factory()() as session:
        await _profile(session, classifier_min_examples=6)
        session.add_all(_history())
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        asked = _posting(acme, "a", "Treasury Analyst", text=BODY, filtered_at=NOW, filtered_by="the place")
        other = _posting(acme, "b", "Treasury Clerk", text=BODY, filtered_at=NOW, filtered_by="the place")
        session.add_all([asked, other])
        await session.flush()
        first = await classifier.train(session, OWNER)
        before = first.predict(classifier.text_of("Treasury Analyst", None, BODY))
        assert not [e for e in await classifier.examples(session, OWNER) if "Treasury" in e[0]]
        asked.owner_override, asked.filtered_by = True, None
        await session.flush()
        learned = [e for e in await classifier.examples(session, OWNER) if "Treasury" in e[0]]
        assert [(positive, weight) for _t, positive, weight in learned] == [(True, classifier.MOVED_FORWARD_WEIGHT)]
        background = await classifier.background(session, OWNER, 40.0, 1.0)
        assert not [e for e in background if "Treasury Analyst" in e[0]], "never learned from as a negative too"
        second = await classifier.train(session, OWNER)
        assert second is not first, "the cached model notices the correction"
        assert second.predict(classifier.text_of("Treasury Analyst", None, BODY)) > before


async def test_a_gate_still_stops_a_posting_the_classifier_likes(js):
    async with db.session_factory()() as session:
        await _profile(session, classifier_min_examples=6)
        session.add_all(_history())
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        gone = _posting(acme, "g", "Director of Governance and Risk Programs", location="Greater Gotham",
                        work_mode="onsite")
        session.add(gone)
        await session.flush()
        result = await sift.sift(session, OWNER, await profile_store.current(session, OWNER), now=NOW)
        assert result["stopped"] == {gate_says()["location"]: 1} and gone.route is None and gone.fit_prob is None
