"""The gates a reader judges: the sentences in the profile's Other (kind `rule`), such as a travel limit.

The app cannot decide one of these from a fact, so the reader answers each from the posting, by the sentence's own
Verify line: `pass`, `fail` or `unknown`, with one line that quotes or points at the posting. The answers come in a
reader's report as `gate_answers: [{id, answer, evidence}]`, beside `fit_breakdown`. They are kept on the role
(`js_opportunities.gate_answers`), and the role page shows each (`role_gates.gate_results`).

A `fail` screens the role out with that gate's sentence as the reason (`actions.store`, as an employer's gate does);
a `pass` and an `unknown` do not: a gate passes until it is proven failed (docs/PRD.md 30.2). Every role is kept.
This module only checks and reads the answers; it never touches the database.

An answer is kept under the rule's id and the words the rule had when it was asked (`says`, docs/PRD.md 39.13): the
same id in other words is a new question, so the old answer stays on record and is not used (`answer_of`), and the
roles hold no answer to the words now. An answer kept
before the words were (no `says`) counts as an answer to the words the rule has now (39.26); the migration that added
the words wrote them in.

A sentence the owner set to follow the analyst's classes (`Gate.follows_classes`: a class and a share) is not the
reader's: the app decides it after research, from how many of the role's required items are in that class
(`scoring.class_gate`, run by `role_gates.gate_results`). The reader is not asked it (`profile_gates.who_checks`), and
an answer to it that comes anyway is kept on the role and read for nothing.
"""

from app.jobsearch.fit import scoring
from app.jobsearch.profile.gates import Gate
from app.jobsearch.profile.schema import Profile

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
ANSWERS = (PASS, FAIL, UNKNOWN)
#: What an answer the analyst gave carries as `by` (a reader's carries none), so a stop made on it names the research
#: as its step (`gating.results`) and not the reader.
BY_ANALYST = "analyst"
#: What an answer the gate-facts step's short Haiku read gave carries (`gate_facts.py`): only a fail on a quote of the
#: posting's lines is ever kept, and a stop made on it is the app's own gate step's.
BY_FACTS = "facts"
#: What an answer carries once it was given with the research done and the whole posting in front of the reader: a
#: can't-tell then is final, and is not asked again. A can't-tell given before it (from a summary) is asked once more.
AFTER_RESEARCH = "after_research"
#: The most of a reader's line that is kept.
EVIDENCE_CHARS = 400
#: The most answers one role may hold (a profile has a handful of sentences; this only stops a runaway report).
MOST = 200


def clean(raw) -> tuple[list[dict], list[str]]:
    """A reader's `gate_answers` tidied: `({id, answer, evidence}, ...)` and what is wrong with it, in words. Nothing
    (None, "", an empty list) is fine. An entry that is not an object, or names no gate, is passed over, as an ask's
    answer is; an answer that is not one of `ANSWERS` is a problem."""
    if raw in (None, "", [], {}):
        return [], []
    if not isinstance(raw, list):
        return [], ["gate_answers is a list of {id, answer, evidence}: an answer to each sentence gate you were given"]
    out, problems = [], []
    for item in raw[:MOST]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        entry = {"id": str(item["id"]).strip()[:80], "answer": str(item.get("answer") or "").strip().lower(),
                 "evidence": " ".join(str(item.get("evidence") or "").split())[:EVIDENCE_CHARS]}
        if entry["answer"] not in ANSWERS:
            problems.append(f"gate_answers: the answer to gate {entry['id']!r} is one of {', '.join(ANSWERS)}")
            continue
        out.append(entry)
    return out, problems


def sentence_gates(profile: Profile) -> dict[str, Gate]:
    """The profile's sentence gates (kind `rule`) by id, on or off."""
    return {g.id: g for g in profile.gates.other if g.kind == "rule"}


def problems(answers: list[dict] | None, profile: Profile) -> list[str]:
    """What is wrong with a reader's cleaned answers against the owner's gates, in words. Empty when it is fine. A gate
    that was turned off since the reader was sent it, or that the app has since been set to decide from the analyst's
    classes, is not a problem: its answer is kept and judged nothing."""
    out, seen = [], set()
    known = sentence_gates(profile)
    for item in answers or []:
        ident = item["id"]
        if ident in seen:
            out.append(f"gate_answers answers {ident!r} twice")
            continue
        seen.add(ident)
        if ident not in known:
            out.append(f"gate_answers names a gate {ident!r} that is not one of the owner's sentence gates (the app "
                       "checks the others itself: leave them out)")
    return out


def wording(gate: Gate) -> str:
    """A sentence rule's words as its answers are keyed by them: the sentence with its spaces folded, as an ask's are,
    so only a change a reader could see makes it a new question."""
    return scoring.wording(gate.text)


def answer_of(answers, gate: Gate) -> dict | None:
    """The reader's answer to this rule in its words now, from a role's stored `gate_answers`: the latest answer under
    the rule's id whose words are the rule's, or that names none (given before the words were kept, docs/PRD.md
    39.26). None when the rule was never answered in these words: it passes until proven failed (30.2), and an answer to
    other words stays on record, unused (39.13)."""
    now = wording(gate)
    for item in reversed(answers or []):
        if isinstance(item, dict) and item.get("id") == gate.id and item.get("answer") in ANSWERS:
            said = item.get("says")
            if said is None or scoring.wording(said) == now:
                return item
    return None


def stamp(answers, profile: Profile) -> list[dict]:
    """The answers with the words of the rule put on each that names none: an answer given before the words were kept is
    an answer to the words the rule has in `profile`, from then on (docs/PRD.md 39.26). An answer to a rule the profile
    does not have stays as it is."""
    words = {g.id: wording(g) for g in sentence_gates(profile).values()}
    return [{**a, "says": words[a["id"]]} if isinstance(a, dict) and a.get("says") is None and a.get("id") in words
            else a for a in answers or []]


def record(answers, new: list[dict], profile: Profile) -> list[dict]:
    """A role's answers with `new` ones from a reader put in, each kept under its rule's id and the rule's words as they
    are in `profile`. The same rule in the same words answered again takes the newer answer's place; an answer to other
    words stays. An answer to a rule the profile does not have is kept in the words it came with. At most
    `scoring.MOST_WORDINGS` answers stay to one rule, the newest."""
    words = {g.id: wording(g) for g in sentence_gates(profile).values()}
    out = [dict(a) for a in answers or [] if isinstance(a, dict)]
    for item in new:
        says = words.get(item["id"], scoring.wording(item.get("says")) or None)
        kept = {"id": item["id"], "answer": item["answer"], "evidence": item.get("evidence") or "", "says": says}
        if item.get("by"):  # an answer of the analyst's says so; a reader's carries nothing
            kept["by"] = item["by"]
        if item.get(AFTER_RESEARCH):
            kept[AFTER_RESEARCH] = True
        out = [a for a in out if not (a.get("id") == kept["id"] and (a.get("says") is None or a["says"] == says))]
        out.append(kept)
    newest_first: list[dict] = []
    counted: dict[str, int] = {}
    for a in reversed(out):
        counted[a.get("id", "")] = counted.get(a.get("id", ""), 0) + 1
        if counted[a.get("id", "")] <= scoring.MOST_WORDINGS:
            newest_first.append(a)
    return newest_first[::-1]
