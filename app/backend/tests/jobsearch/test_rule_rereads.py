"""A sentence rule's answer is kept under the rule's id and its words, so a rule reworded since is a new question
(docs/PRD.md 39.13, 39.15, 39.26). The app decides every line of Hard pass and Prioritization now (PRD 30, 46): no
reader is sent back for a rule, so what stays here is how an answer already on a role is kept, matched to words and
counted, and that saving Hard pass with a rule queues nobody. Every name, number and sentence is made up."""

from datetime import UTC, date, datetime

from app import db
from app.jobsearch import judged, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import sentence_gates
from app.jobsearch.measures.pay_place import match_key
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

NOW = datetime.now(UTC)
TODAY = date.today()
TRAVEL = {"id": "travel", "kind": "rule", "section": "responsibilities", "text": "Travel of 20% or more."}
BADGE = {"id": "badge", "kind": "rule", "section": "responsibilities", "text": "The role needs an on-site badge."}
TABS = {"company": "A public agency.", "target_roles": [
    {"id": "builders", "name": "Builders", "titles": ["Director"], "description": "Builds programs."},
    {"id": "analysts", "name": "Analysts", "titles": ["Analyst"], "description": "Looks at programs."}]}
#: No asks to answer, so a reader's report is only about the rules.
NO_ASKS = {"asks": []}


def rules(*lines: dict, **more) -> dict:
    """Hard pass with these sentence rules and nothing else on."""
    return {"other": list(lines), **more}


def given(rule: dict, answer: str = "pass", evidence: str = "made-up evidence", **more) -> dict:
    """An answer to this rule in its words, as a role keeps it."""
    return {"id": rule["id"], "answer": answer, "evidence": evidence, "says": rule["text"], **more}


async def _read(session, company, answers, *, prob=None, target=None, status=stages.SECOND_IMPRESSION,
                route=judged.READER, posting=True, **more) -> Opportunity:
    """A role a reader has read, with its posting and these answers to the rules (kept as given)."""
    employer = await employers.find_or_make(session, OWNER, company)
    role = Opportunity(
        owner=OWNER, slug=company.lower(), company=company, title="Programs Lead", status=status, origin="scout",
        match_key=match_key(company, "Programs Lead"), found=TODAY, employer_id=employer.id, target_role=target,
        location="Remote, US", move_type="remote", url=f"https://jobs.example.test/{company.lower()}", fit=3,
        gate_answers=answers, **more)
    session.add(role)
    await session.flush()
    if posting:
        session.add(JobPosting(
            owner=OWNER, employer_id=employer.id, ats=EXAMPLE_SYSTEM, ext_id=f"R-{company}", company=company,
            title="Programs Lead", match_key=match_key(company, "Programs Lead"), url=role.url, location="Remote",
            work_mode="remote", text=f"The whole posting for {company}.", route=route, judged_by=judged.TITLE_CHECK,
            first_seen=NOW, last_seen=NOW, filtered_at=NOW, links=[], fit_prob=prob, opportunity_id=role.id,
            target_role=target))
    return role


async def _world(session, *lines, **parts) -> p.Profile:
    await load_profile(session, OWNER, scoring=NO_ASKS, gates=rules(*lines), **parts)
    return await profile_store.current(session, OWNER)


async def _put_gates(client, doc: dict):
    saved = await client.put("/api/v1/profile/gates", json={"data": doc, "why": "a test"})
    assert saved.status_code == 200, saved.text
    return saved.json()


# --- the words an answer was given for ------------------------------------------------------------------------------


def test_an_answer_is_kept_under_the_rules_id_and_words_and_one_to_other_words_stays_on_record_unused():
    rule = pg.Gate(**TRAVEL)
    now = [given(TRAVEL, "fail")]
    assert sentence_gates.answer_of(now, rule)["answer"] == "fail"
    reworded = pg.Gate(**{**TRAVEL, "text": "Travel of 30% or more."})
    assert sentence_gates.answer_of(now, reworded) is None, "a reworded rule is a new question"
    assert now == [given(TRAVEL, "fail")], "and the answer to the old words is still on record"
    spaced = pg.Gate(**{**TRAVEL, "text": "  Travel of   20%\tor more. "})
    assert sentence_gates.answer_of(now, spaced) is not None, "only a change a reader could see is a new question"
    assert sentence_gates.answer_of([{**given(TRAVEL), "id": "badge"}], rule) is None, "another rule's answer"
    assert sentence_gates.answer_of([given(TRAVEL, "fail"), given(TRAVEL, "pass")], rule)["answer"] == "pass", (
        "the latest answer in these words")
    assert sentence_gates.answer_of([given(TRAVEL, "pass"), {**given(BADGE), "id": "travel"}], rule)["answer"] == "pass"


def test_an_answer_kept_before_the_words_were_counts_for_the_words_the_rule_has_now():
    bare = {"id": "travel", "answer": "fail", "evidence": "Heavy travel."}
    rule = pg.Gate(**TRAVEL)
    assert sentence_gates.answer_of([bare], rule) == bare, "no words kept: it answers the words the rule has"
    assert sentence_gates.answer_of([bare], pg.Gate(**{**TRAVEL, "text": "Other words."})) == bare
    profile = p.Profile.model_validate({"gates": rules(TRAVEL)})
    stamped = sentence_gates.stamp([bare, {"id": "gone", "answer": "pass", "evidence": ""}, given(TRAVEL)], profile)
    assert stamped == [{**bare, "says": TRAVEL["text"]}, {"id": "gone", "answer": "pass", "evidence": ""},
                       given(TRAVEL)], "words on the answers that have none, for the rules there are"
    reworded = pg.Gate(**{**TRAVEL, "text": "Travel of 30% or more."})
    assert sentence_gates.answer_of(stamped, reworded) is None, "reworded afterwards: stamped for the old words"


def test_recording_an_answer_puts_the_rules_words_on_it_and_keeps_the_old_words_answer_beside_it():
    profile = p.Profile.model_validate({"gates": rules(TRAVEL)})
    first = sentence_gates.record([], [{"id": "travel", "answer": "fail", "evidence": "Heavy."}], profile)
    assert first == [{"id": "travel", "answer": "fail", "evidence": "Heavy.", "says": TRAVEL["text"]}]
    again = sentence_gates.record(first, [{"id": "travel", "answer": "pass", "evidence": "None."}], profile)
    assert [a["answer"] for a in again] == ["pass"], "the same rule in the same words, answered again, takes its place"
    newer = p.Profile.model_validate({"gates": rules({**TRAVEL, "text": "Travel of 30% or more."})})
    kept = sentence_gates.record(again, [{"id": "travel", "answer": "fail", "evidence": "Heavy."}], newer)
    assert [(a["answer"], a["says"]) for a in kept] == [("pass", TRAVEL["text"]), ("fail", "Travel of 30% or more.")]
    old = {"id": "old", "answer": "pass", "evidence": "", "says": " Old   words "}
    gone = sentence_gates.record([], [old], profile)
    assert gone[0]["says"] == "Old words", "an answer to a rule the profile has not keeps the words it came with"
    many = []
    for n in range(9):
        many = sentence_gates.record(
            many, [{"id": "travel", "answer": "pass", "evidence": str(n)}],
            p.Profile.model_validate({"gates": rules({**TRAVEL, "text": f"Words {n}."})}))
    assert [a["evidence"] for a in many] == ["4", "5", "6", "7", "8"], "the newest few stay"


async def test_a_reworded_rule_stops_nothing_on_the_answer_to_its_old_words_and_an_unchanged_one_does(js):
    reader = [given(TRAVEL, "fail", "Heavy travel.")]
    async with db.session_factory()() as session:
        profile = await _world(session, TRAVEL)
        rules_now = await gating.rules(session, OWNER, profile)
        role = await _read(session, "Fabrikam", reader)
        subject = gating.role_subject(role, None)
        stop = gating.check(subject, rules_now)
        assert stop is not None and (stop.gate, stop.gate_id) == ("rule", "travel"), "the rule, in its own words"
        reworded = rules({**TRAVEL, "text": "Travel of 30% or more."})
        await profile_store.save(session, OWNER, "gates", reworded, by="test")
        assert gating.check(subject, await gating.rules(session, OWNER)) is None, "the new words are not answered"
