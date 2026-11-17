"""The classifier reads a text in one pass, works a posting's rating out once, and is made again only when what it reads
has changed (docs/PRD.md 29.6). What it says is what it said before: the `_first_*` functions below are the classifier's
first writing of `features`, `text_of`, `predict` and `why`, copied, with the constants inlined, and the golden values
were worked out by it. Synthetic text only: every word here is made up."""

import math
import random
import re
from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update

from app import db
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import classifier
from app.jobsearch.gating import sift
from app.jobsearch.models import JobPosting, Opportunity, StageMove
from app.jobsearch.profile import store as profile_store
from tests.support import EXAMPLE_SYSTEM, OWNER, PROFILE, load_profile

NOW = datetime(2026, 11, 6, 5, 30, tzinfo=UTC)
BODY = "You will lead the work with partners across the business and report to the leadership team."

# --- the first writing, word for word ---------------------------------------------------------------------------------

_FIRST_SEGMENT = re.compile(r"[.!?;:,\n\r|/()\[\]{}–—]+|\s-\s")
_FIRST_WORD = re.compile(r"[a-z][a-z0-9+#]*(?:['&][a-z0-9]+)?")


def _first_features(text: str):
    counts: Counter = Counter()
    for segment in _FIRST_SEGMENT.split((text or "").lower()):
        run: list[str] = []
        for word in _FIRST_WORD.findall(segment) + [""]:
            if word and word not in classifier.STOP_WORDS and len(word) > 1:
                run.append(word)
                continue
            counts.update(run)
            counts.update(f"{a} {b}" for a, b in zip(run, run[1:], strict=False))
            run = []
    return Counter({f: min(n, 3) for f, n in counts.items()})


def _first_text_of(title, department, body):
    t = (title or "").strip()
    first = " ".join((body or "").split()[:200])
    return ". ".join(part for part in (t, t, (department or "").strip(), first) if part)


def _first_evidence(model, text):
    out = []
    for feature, count in _first_features(text).items():
        if feature not in model.positive and feature not in model.negative:
            continue
        v = model.vocabulary or len(set(model.positive) | set(model.negative)) or 1
        p = (model.positive.get(feature, 0.0) + 1.0) / (model.positive_total + 1.0 * v)
        n = (model.negative.get(feature, 0.0) + 1.0) / (model.negative_total + 1.0 * v)
        out.append((feature, math.log(p / n) * count))
    return out


def _first_predict(model, text):
    evidence = _first_evidence(model, text)
    prior = math.log(max(model.positive_weight, 1e-9) / max(model.negative_weight, 1e-9))
    tempered = sum(e for _, e in evidence) / math.sqrt(len(evidence)) if evidence else 0.0
    odds = max(-12.0, min(12.0, prior + tempered))
    return 1 / (1 + math.exp(-odds))


def _first_why(model, text, top=3):
    picked: list[str] = []
    used: set[str] = set()
    for feature, contribution in sorted(_first_evidence(model, text), key=lambda fe: (-fe[1], fe[0])):
        if contribution <= 0 or len(picked) >= top:
            break
        words = set(feature.split())
        if words & used:
            continue
        picked.append(feature)
        used |= words
    return picked


# --- what it said, worked out by the first writing --------------------------------------------------------

GOLDEN_FEATURES = {
    "The Director of Data Governance, and a Risk Lead": [
        ("director", 1), ("data", 1), ("governance", 1), ("data governance", 1), ("risk", 1), ("lead", 1),
        ("risk lead", 1)],
    "Senior C++ Engineer - Python & C# (well-known) | O'Reilly's team: R&D; AT&T/Verizon": [
        ("senior", 1), ("c++", 1), ("engineer", 1), ("senior c++", 1), ("c++ engineer", 1), ("python", 1), ("c#", 1),
        ("python c#", 1), ("well", 1), ("known", 1), ("well known", 1), ("o'reilly", 1), ("team", 1), ("r&d", 1),
        ("at&t", 1), ("verizon", 1)],
    "3D printing; A-1 Quality: x y z — Senior — Staff – Principal. 5+ years' experience... (remote)": [
        ("printing", 1), ("quality", 1), ("senior", 1), ("staff", 1), ("principal", 1), ("years", 1),
        ("experience", 1), ("years experience", 1), ("remote", 1)],
    "Lead. Lead. Finance. Lead the lead team; lead leads lead": [
        ("lead", 3), ("finance", 1), ("team", 1), ("lead team", 1), ("leads", 1), ("lead leads", 1),
        ("leads lead", 1)],
    "": [],
}

LIKED = ["Director of Governance and Risk", "Governance Risk Program Lead", "Risk and Compliance Program Director",
         "Head of Risk Governance"]
DISLIKED = ["Warehouse Packing Associate", "Payroll Specialist", "Forklift Operator", "Packing Line Associate"]


def _golden_model() -> classifier.Model:
    return classifier.build(
        [(classifier.text_of(t, "Finance", BODY), True, 2.0) for t in LIKED * 2]
        + [(classifier.text_of(t, None, BODY), False, 1.0) for t in DISLIKED * 2]
        + [(classifier.text_of("Line Cook", None, BODY), False, 0.3),
           (classifier.text_of("Bus Driver", None, "Drive the bus. " * 5), False, 0.3)])


def _golden_questions():
    return [classifier.text_of("Governance and Risk Director", "Finance", BODY),
            classifier.text_of("Payroll Packing Associate", None, BODY),
            classifier.text_of("Quantum Basket Weaver", None, None), "qwertyuiop", "",
            classifier.text_of("Risk Governance Lead Director", None, None)]


#: (probability, the words that put it up) for each of `_golden_questions`, as the first writing rated them.
GOLDEN_RATINGS = [(0.9943656233439158, ["risk", "governance", "director"]), (0.007136988209157801, ["lead"]),
                  (0.6504065040650406, []), (0.6504065040650406, []), (0.6504065040650406, []),
                  (0.9998807996468714, ["risk", "governance", "director"])]


def test_the_features_are_what_they_were_for_texts_worked_out_by_the_first_writing():
    for text, expected in GOLDEN_FEATURES.items():
        assert list(classifier.features(text).items()) == expected, text
    assert classifier.features(None) == {}


_PIECES = ["Lead", "lead", "the", "of", "AND", "a", "I", "x", "C++", "c#", "R&D", "O'Reilly's", "isn't", "well-known",
           "3D", "5+", "A-1", "senior", "Senior", "governance", "risk", "data", "team's", "ß", "İstanbul", "naïve",
           "٣", "über", "x2", "+", "#", "&", "'", "a'", "'b", "q&", "&q", "z9", "9z", "é"]
_BREAKS = [" ", " ", " ", ", ", ". ", "; ", ": ", " | ", "/", "(", ")", "[", "]", "{", "}", " - ", " -", "- ", "-", "–",
           " — ", "\n", "\r\n", "\t", "!", "?", "...", "  ", "\u00a0", "\u3000", ""]


def _made_up_texts(count=3000, seed=2026_11_06):
    rng = random.Random(seed)
    for _ in range(count):
        n = rng.randint(0, 40)
        parts = []
        for _ in range(n):
            parts.append(rng.choice(_PIECES))
            parts.append(rng.choice(_BREAKS))
        yield "".join(parts)


def test_the_features_of_any_text_are_the_same_words_pairs_counts_and_order_as_before():
    """Order matters: the evidence is summed in it, so a different order could differ in the last digit."""
    checked = 0
    for text in _made_up_texts():
        assert list(classifier.features(text).items()) == list(_first_features(text).items()), repr(text)
        checked += 1
    assert checked == 3000


def _words(n):
    return " ".join(f"w{i}" for i in range(n))


def test_the_text_of_a_posting_is_what_it_was():
    for body in (None, "", "   ", _words(1), _words(199), _words(200), _words(201), _words(500),
                 "  lead \n\t the   work\u00a0now  " + _words(300), "a\u3000b\u2003c " * 120):
        for title, department in ((None, None), ("  Lead  ", " Finance "), ("Lead", ""), ("", "Finance")):
            assert classifier.text_of(title, department, body) == _first_text_of(title, department, body)


def test_a_model_made_from_the_same_examples_has_the_same_counts_and_weights():
    model = _golden_model()
    assert (model.positives, model.negatives, model.vocabulary) == (8, 10, 43)
    assert (model.positive_total, model.negative_total) == (336.0, 151.8)
    assert (model.positive_weight, model.negative_weight) == (16.0, 8.600000000000001)


def test_a_rating_and_its_words_are_what_the_first_writing_gave():
    model = _golden_model()
    for question, (probability, words) in zip(_golden_questions(), GOLDEN_RATINGS, strict=True):
        assert model.predict(question) == probability
        assert model.why(question) == words
        assert model.rate(question) == (probability, words)
    assert _golden_model().why(_golden_questions()[0], top=1) == ["risk"]


def test_a_rating_of_any_text_is_the_first_writings_to_the_last_digit():
    model = _golden_model()
    for text in _made_up_texts(1500, seed=7):
        probability, words = model.rate(text)
        assert probability == _first_predict(model, text), repr(text)
        assert words == _first_why(model, text), repr(text)
        assert model.predict(text) == probability and model.why(text) == words


def test_a_model_that_learns_more_after_it_was_used_rates_by_what_it_knows_now():
    model = _golden_model()
    assert model.predict("payroll lead") == _first_predict(model, "payroll lead")  # the rating is remembered from here
    classifier._learn(model, classifier.text_of("Payroll Lead Director", None, BODY), True, 2.0)
    assert model.predict("payroll lead") == _first_predict(model, "payroll lead")
    assert model.why("payroll lead") == _first_why(model, "payroll lead")


def test_a_text_is_read_for_its_words_once_to_rate_it(monkeypatch):
    model = _golden_model()
    reads = []
    real = classifier.features
    monkeypatch.setattr(classifier, "features", lambda text: reads.append(text) or real(text))
    model.rate(_golden_questions()[0])
    assert len(reads) == 1, "the rating and its words come from one reading (the first writing read it twice)"


def test_the_filter_rates_a_posting_and_says_why_from_one_reading(monkeypatch):
    model = _golden_model()
    posting = JobPosting(owner=OWNER, title="Governance and Risk Director", department="Finance", text=BODY)
    reads = []
    real = classifier.features
    monkeypatch.setattr(classifier, "features", lambda text: reads.append(text) or real(text))
    why, probability = sift.rank(model, posting)
    text = classifier.posting_text(posting)
    assert probability == _first_predict(model, text)
    assert why == f"ranked {probability:.2f}: " + ", ".join(_first_why(model, text))
    assert len(reads) == 1


# --- when the model is made again --------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_cache():
    classifier.clear_cache()
    yield
    classifier.clear_cache()


@pytest.fixture
def builds(monkeypatch):
    """How many examples each making of the model was given, in order."""
    made: list[int] = []
    real = classifier.build_giving_the_loop_back

    async def counting(examples):
        made.append(len(examples))
        return await real(examples)

    monkeypatch.setattr(classifier, "build_giving_the_loop_back", counting)
    return made


def _role(n, title, **more):
    return Opportunity(owner=OWNER, slug=f"co-{n}", company=f"Co {n}", title=title, match_key=f"co {n}|{title.lower()}",
                       origin="scout", **more)


def _posting(employer, ext_id, title, **more):
    more = {"location": "Remote", "work_mode": "remote", **more}
    return JobPosting(owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=ext_id, company=employer.name,
                      title=title, first_seen=NOW, last_seen=NOW, links=[], **more)


async def _owner_with_history(session, minimum=5, background=True):
    """Moved-forward roles and a few turned down (so postings that never became roles are learned as background too)."""
    await load_profile(session, OWNER, criteria=PROFILE["criteria"], settings={**PROFILE["settings"],
                                                                               "classifier_min_examples": minimum})
    for r in range(3):
        for i, title in enumerate(LIKED):
            session.add(_role(f"p{r}{i}", title, status="applied", fit=4, description=BODY))
        for i, title in enumerate(DISLIKED[: 1 if background else 4]):
            session.add(_role(f"n{r}{i}", title, status="withdrew", ended_reason="i_passed", fit=1, description=BODY))
    if background:
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add_all([_posting(acme, f"b{i}", f"Night Shift Nurse {i}", text=BODY) for i in range(20)])
    await session.flush()


async def test_a_role_that_teaches_the_model_nothing_does_not_make_it_again(js, builds):
    async with db.session_factory()() as session:
        await _owner_with_history(session)
        first = await classifier.train(session, OWNER)
        assert first is not None and len(builds) == 1
        session.add(_role("idle", "Governance Fellow", status="second_impression", fit=4, description=BODY))
        await session.flush()
        assert await classifier.train(session, OWNER) is first, "a role that is neither moved forward nor turned down"
        assert len(builds) == 1


async def test_a_posting_the_filter_stopped_does_not_make_the_model_again(js, builds):
    async with db.session_factory()() as session:
        await _owner_with_history(session)
        first = await classifier.train(session, OWNER)
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(_posting(acme, "stopped", "Night Shift Nurse", text=BODY, filtered_at=NOW, filtered_by="the place"))
        await session.flush()
        assert await classifier.train(session, OWNER) is first, "the postings count moved; what the model reads did not"
        assert len(builds) == 1


async def test_a_posting_that_joins_the_background_makes_the_model_again(js, builds):
    async with db.session_factory()() as session:
        await _owner_with_history(session)
        first = await classifier.train(session, OWNER)
        acme = await employers.find_or_make(session, OWNER, "Fabrikam")
        session.add(_posting(acme, "new", "Night Shift Nurse", text=BODY))
        await session.flush()
        second = await classifier.train(session, OWNER)
        assert second is not first and len(builds) == 2
        assert second.summary()["negatives"] == first.summary()["negatives"] + 1
        assert await classifier.train(session, OWNER) is second and len(builds) == 2


async def test_a_role_moved_forward_makes_the_model_again(js, builds):
    async with db.session_factory()() as session:
        await _owner_with_history(session)
        first = await classifier.train(session, OWNER)
        session.add(_role("late", "Governance Fellow", status="applied", fit=4, description=BODY))
        await session.flush()
        second = await classifier.train(session, OWNER)
        assert second is not first and second.summary()["positives"] == first.summary()["positives"] + 1
        assert len(builds) == 2


async def test_the_model_kept_is_the_model_that_would_be_made(js):
    async with db.session_factory()() as session:
        await _owner_with_history(session)
        first = await classifier.train(session, OWNER)
        session.add(_role("idle", "Governance Fellow", status="second_impression", fit=4, description=BODY))
        await session.flush()
        kept = await classifier.train(session, OWNER)
        classifier.clear_cache()
        made = await classifier.train(session, OWNER)
        assert kept is first and made is not first
        assert (made.positive, made.negative, made.vocabulary) == (kept.positive, kept.negative, kept.vocabulary)
        assert (made.positive_total, made.negative_total) == (kept.positive_total, kept.negative_total)
        assert (made.positive_weight, made.negative_weight) == (kept.positive_weight, kept.negative_weight)


async def test_a_new_minimum_gives_the_model_or_the_cold_start_without_clearing_anything(js, builds):
    async with db.session_factory()() as session:
        await _owner_with_history(session, minimum=10, background=False)  # 12 positives
        assert (await classifier.train(session, OWNER)) is not None
        await _owner_with_history_settings(session, 13)
        assert await classifier.train(session, OWNER) is None, "fewer positives than the minimum: the cold start"
        await _owner_with_history_settings(session, 12)
        model = await classifier.train(session, OWNER)
        assert model is not None and model.summary()["positives"] == 12


async def _owner_with_history_settings(session, minimum):
    await load_profile(session, OWNER, criteria=PROFILE["criteria"],
                       settings={**PROFILE["settings"], "classifier_min_examples": minimum})
    assert (await profile_store.current(session, OWNER)).settings.classifier_min_examples == minimum


async def test_a_cold_start_is_kept_while_nothing_it_reads_changes(js, builds):
    async with db.session_factory()() as session:
        await _owner_with_history(session, minimum=500)
        assert await classifier.train(session, OWNER) is None and len(builds) == 1
        session.add(_role("idle", "Governance Fellow", status="second_impression", fit=4, description=BODY))
        await session.flush()
        assert await classifier.train(session, OWNER) is None
        assert len(builds) == 1


async def test_an_example_whose_sign_and_weight_change_but_not_its_text_makes_the_model_again(js, builds):
    """A role the owner pursued and then passed on teaches the same words the other way round: the model is not the one
    made already. The role is as an imported one is (applied, no stage history) and is flipped in the database, and
    there are no postings that never became roles: the one example's sign and weight is the only thing that differs."""
    async with db.session_factory()() as session:
        await _owner_with_history(session, background=False)
        role_id = await session.scalar(select(Opportunity.id).where(Opportunity.slug == "co-p00"))
        await session.execute(delete(StageMove).where(StageMove.role_id == role_id))
        before = await classifier.examples(session, OWNER)
        first = await classifier.train(session, OWNER)
        assert first is not None and len(builds) == 1
        await session.execute(update(Opportunity).where(Opportunity.id == role_id).values(
            status="withdrew", ended_reason="i_passed"))
        after = await classifier.examples(session, OWNER)
        assert [text for text, _, _ in after] == [text for text, _, _ in before], "the same texts, in the same order"
        text = classifier.text_of(LIKED[0], None, BODY)
        assert [(was, now) for was, now in zip(before, after, strict=True) if was != now] == [
            ((text, True, classifier.MOVED_FORWARD_WEIGHT), (text, False, classifier.NEGATIVE_WEIGHT))]
        second = await classifier.train(session, OWNER)
        assert second is not first and len(builds) == 2
        assert (second.positives, second.negatives) == (first.positives - 1, first.negatives + 1)
        assert (second.positive_weight, second.negative_weight) == (
            first.positive_weight - classifier.MOVED_FORWARD_WEIGHT, first.negative_weight + classifier.NEGATIVE_WEIGHT)
        assert await classifier.train(session, OWNER) is second and len(builds) == 2
