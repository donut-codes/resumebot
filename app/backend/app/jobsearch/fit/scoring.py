"""A role's fit, worked out from the owner's asks (docs/PRD.md 30.6, on 6.2 to 6.5, 23.4 and 39.20 to 39.22).

The reader answers each of the owner's asks about a posting: `met`, `not` or `unknown`, with a line of evidence (an
answer of `partly`, kept before there was no partial credit, is still read, and earns nothing). The answers are kept on
the role as they were given (`answers`), each under the ask's id and the exact words it was asked in (`says`): reword an
ask and the old answer stays on record but is not used, so the ask counts as can't tell until it is answered in its new
words (39.13). An answer kept before the words were, has no `says` and counts as an answer to the words the ask has now
(39.26). A reader gives no score: the score is worked out last, from the answers and what research found, by
`prioritize.py`, and a role has none until it is researched (39.20). Code does the arithmetic:

- **Rows**: every row adds up with the others (an AND). An ask counts on its own; the owner may put asks in a group
  (docs/PRD.md 30.9): an "any of" row, whose points are earned once when any statement is true, or a "first match"
  row, an ordered list of statements with points of their own where the first true one from the top decides. The
  arithmetic is `score_groups.py` (pure); this module judges each row, from the answers kept on the role and from what
  the app knows, and hands the arithmetic the answers it ended up with. "Grade the requirements for me" is a row the app
  decides itself, from the analyst's classes of the required and preferred items.
- **Earned**: there is no partial credit (the owner, 2026-10-14). An ask that is on earns its points in full when it is
met, and nothing when it is not met, partly met,
  unknown or unanswered. A penalty is the same: it applies when it is met and not otherwise. The classifier's
  probability never enters a score, and a stored answer of "partly" is still read and earns nothing.
- **Possible**: the most the rows can add, each counted once at its top value (a first match its best level, an
  any-of its points, a per-item ask its limit): the positive points only, so penalties are outside it. An answer
  never changes it: an ask nobody could tell stays in it, so a role whose asks have not been checked scores low. An item
  applies to every role (the All tab) or only to the one role whose tab it is on; a role counts All's items and its
  own, and a role with no target role counts only the items for every role (docs/PRD.md 31.5, 31.9). The graded
  requirements add each role's own maximum, since roles list different numbers of items.
- **Fit**: the raw fit stored on a role is the earned points, rounded. The percent is worked out once, from the exact
  earned points, over `possible`, as a whole number held between 0 and 100, and kept with the score with the
  points it was scored against (a tab with no item of its own can reach 100%), under the arithmetic's own number
  (`math`). So editing the asks cannot move a percent on its own: a save of Prioritization works every score out again,
  with the version it was worked out under (39.21), and until then a role's percent is the one it was scored at. A
  score kept before this arithmetic (no `math`) is read as it always was.

Where the app can decide an ask itself (an employer fact the record holds, a posting's age, a pay tier on the likely
ask, the analyst's classes of the required items) it does, and the reader's word on it is set aside. A per-item ask
counts each item up to its limit. A posting-age ask goes one of two ways, as a pay tier does with `above`: "posted over
N days ago" (the default), or, with `above` false, "posted within the last N days", under N x 24 hours old (the owner,
2026-10-13; `freshness.py` says how the age is told).

A role scored before asks (a base score and modifiers, docs/PRD.md 6) keeps its old breakdown and its old fit, shown
as a percent of the old scale the converter kept (`Scoring.old_scale`) until it is scored again.
"""

import math
from collections.abc import Callable, Sequence

from app.jobsearch.fit import freshness, score_groups
from app.jobsearch.measures import dials
from app.jobsearch.profile.base import SECTION_LABEL
from app.jobsearch.profile.schema import Profile
from app.jobsearch.profile.scoring import ANSWERS, GIVEN_ANSWERS, GRADE_ID, MATCH_WEIGHT_DEFAULT, Ask, Group, Item

#: What each answer earns of an ask's points.
WEIGHT = score_groups.WEIGHT
#: What a first-match row's answer (`level`) says besides the id of a statement: no statement is true, or the posting
#: does not say enough to choose.
NO_STATEMENT, CANT_TELL = "none", "unknown"
CLASS_WORDS = {"U": "Unproven", "T": "Transferable", "P": "Proven"}

#: The number of the arithmetic a score is worked out by, kept with it (`math`). A score under another number is stale
#: and is worked out again at the next start (`prioritize.stale_scores`), so the release that changes the arithmetic
#: re-scores every role by itself. 2 was the arithmetic that credited an ask nobody could tell (the classifier's
#: probability times its points) and half of a partly met one; it was never released. 3 is no partial credit
#: (docs/PRD.md 30.9, the owner, 2026-10-14): met earns all, anything else earns nothing. 4 is the same points with the
#: graded row carrying the preferred classes for the queue card whether or not Preferred is on (`CARD_PREFERRED`, the
#: owner, 2026-11-12): the release re-scores every role by itself, which writes the key to the stored rows.
MATH = 4
#: The sentence of the graded-requirements row, as a role page shows it (the app's own words, not the owner's).
#: The key of the graded row that holds the tally of the preferred items' classes for the queue card (`match_share`),
#: kept whether or not the owner's Preferred section is on. `preferred` is what the shown fit counts (None while that
#: section is off); this key never enters the points or the breakdown the owner reads.
CARD_PREFERRED = "card_preferred"
GRADE_SAYS = "Required and preferred items graded by the research"


# --- what a report holds -------------------------------------------------------------------------------------------


#: The evidence of an ask nobody answered: what a score writes for it, and what tells such a row from a real answer when
#: a score kept before the answers were kept apart is read (`answers_in`).
NOT_ANSWERED = "not answered"
#: The most answers kept to one ask: the one in its words now and the ones to the words it had before.
MOST_WORDINGS = 5


def is_asks(breakdown) -> bool:
    """Whether a stored breakdown is the asks kind: it holds the answers to the owner's asks, or the rows that score
    them, or both (a role scored the old way has a base and modifiers instead)."""
    return isinstance(breakdown, dict) and (
        isinstance(breakdown.get("asks"), list) or isinstance(breakdown.get("answers"), list))


def wording(says) -> str:
    """An ask's words as its answers are keyed by them: the sentence with its spaces folded, so only a change a reader
    could see counts as a new question."""
    return " ".join(str(says or "").split())


def ask_words(ask: Ask) -> str:
    """The words an ask (or a statement) is asked in: its sentence and, when the owner wrote one, its Verify line, each
    with its spaces folded. The line is part of the question: a reader is told to apply it exactly, so an ask whose line
    was added, changed or taken away is a new question, asked again. An ask with no line has the words it always had,
    its sentence, so the answers kept to it stay its own."""
    verify = wording(ask.verify)
    return wording(ask.says) + (f" Verify: {verify}" if verify else "")


def _list_words(group: Group) -> str:
    """The words a first-match row is asked in: its statements, in their order, each under its id. A changed sentence, a
    statement added or removed, one under another id and a row put in another order are a new question; points, the
    part of the posting and the tab are not words (docs/PRD.md 30.9, Q17). A statement's Verify line is part of its
    words (`ask_words`)."""
    return " | ".join(f"{m.id}: {ask_words(m)}" for m in group.members)


def words_of(item: Item) -> str:
    """The words a question is asked in, as its answers are keyed by them: an ask's sentence with its spaces folded, or
    a first-match row's statements (`_list_words`). The statements of an "any of" row are asks, asked by their own
    sentences: the row is no question, and an added or reworded statement is the only new one."""
    return _list_words(item) if isinstance(item, Group) else ask_words(item)


def _words_by_id(profile: Profile) -> dict[str, str]:
    """The words of every question the owner has, by id: each ask, each first-match row, and each statement of a row (an
    answer given to a statement before it was grouped still names it)."""
    out = {a.id: ask_words(a) for a in profile.scoring.asks}
    for g in profile.scoring.groups:
        if g.kind == "best":
            out[g.id] = _list_words(g)
        out.update({m.id: ask_words(m) for m in g.members})
    return out


def _picked(item: dict) -> str | None:
    """The statement a first-match answer names: `level` as the record keeps it, or `statement` as a report sends it
    (the owner's word for it); a statement's id, `none` or `unknown`, without the spaces around it. None when it names
    none."""
    for key in ("level", "statement"):
        if isinstance(item.get(key), str) and item[key].strip():
            return item[key].strip()
    return None


def _answer_word(item: dict) -> str | None:
    """The answer an item of a report or of a role's record gives: the one it says (`met`, `partly`, `not` or
    `unknown`), else the one that follows from the statement a first-match row names (a statement's id: met, `none`:
    not, `unknown`: unknown). None when it gives none."""
    if item.get("answer") in ANSWERS:
        return item["answer"]
    picked = _picked(item)
    if picked is None:
        return None
    return "not" if picked == NO_STATEMENT else "unknown" if picked == CANT_TELL else "met"


def _normal(item: dict) -> dict | None:
    """An item of a report or a record as an answer (a copy): its answer as `_answer_word` gives it, and the statement
    it names (if any) kept as `level`. None for an item that answers nothing."""
    word = _answer_word(item) if isinstance(item, dict) and item.get("id") else None
    if word is None:
        return None
    out = {k: v for k, v in item.items() if k != "statement"}
    out["answer"] = word
    if (picked := _picked(item)) is not None:
        out["level"] = picked
    else:
        out.pop("level", None)
    return out


def problems(raw: dict | None, profile: Profile) -> list[str]:
    """What is wrong with a reader's breakdown against the owner's asks and rows, in words. Empty when it is fine. An
    ask, and each statement of a row (first-match or "any of"), is answered with `answer`, by its own id. A row is never
    answered as a whole, and a statement the app decides itself is left out."""
    if not raw:
        return []
    if not is_asks(raw):
        return ["fit_breakdown is {asks: [{id, answer, evidence}]}: answer each of the owner's asks (the old base and "
                "modifiers are gone)"]
    s = profile.scoring
    out, seen = [], set()
    asks = {a.id: a for a in s.asks}
    boxes = {g.id: g for g in s.groups}
    inside = {m.id: g for g in s.groups for m in g.members}
    for item in raw["asks"]:
        ident = item.get("id") if isinstance(item, dict) else None
        key = ident if isinstance(ident, str) else ""  # no ask, row or statement has an id of nothing
        known = asks.get(key)
        home = inside.get(key) if known is None else None  # the row a statement belongs to
        refused = False
        if known is None and key in boxes:
            kind = "a first-match" if boxes[key].kind == "best" else "an 'any of'"
            out.append(f"fit_breakdown answers {ident!r} as a whole, but it is {kind} row: answer each of its "
                       "statements by its own id")
            refused = True
        elif known is None and home is None:
            out.append(f"fit_breakdown names an ask {ident!r} the owner doesn't have")
        elif (counted := known or home) is not None and not s.item_on_anywhere(counted):
            out.append(f"fit_breakdown names {ident!r}, which the owner turned off")
        elif ident in seen:
            out.append(f"fit_breakdown answers {ident!r} twice")
        if ident in seen:
            continue
        seen.add(ident)
        if not isinstance(item, dict) or refused:
            continue
        if item.get("answer") not in ANSWERS:
            out.append(f"fit_breakdown: the answer to {ident!r} is one of {', '.join(GIVEN_ANSWERS)}")
    return out


def answers_in(source) -> list[dict]:
    """The answers a stored breakdown or a reader's report holds, each `{id, answer, evidence, count?, level?, says?,
    by?}`, in the order they were given. A role keeps them as `answers`, each with the words of the question it answered
    (`says`). A report names no words (its items are `asks`). A first-match row is answered with a statement alone (a
    report's `statement`, kept as `level`), and its `answer` follows from it (`_answer_word`), so everything that reads
    answers reads a row's as it reads an ask's. A score kept before the answers were kept apart holds them among its
    rows (those the reader gave, not the ones the app decided, and not an ask nobody answered), without words: they
    count as answers to the words the ask has now (docs/PRD.md 39.26). Nothing for a role scored the old way."""
    if not isinstance(source, dict):
        return []
    if isinstance(source.get("answers"), list):
        items = source["answers"]
    elif isinstance(source.get("asks"), list):
        items = []
        for row in source["asks"]:
            if isinstance(row, dict) and "earned" in row:  # a scored row, from before the answers were kept apart
                if row.get("by") == "app" or str(row.get("evidence") or "").startswith(NOT_ANSWERED):
                    continue
                row = {k: row[k] for k in ("id", "answer", "evidence", "count") if k in row}
            items.append(row)
    else:
        return []
    return [kept for i in items if (kept := _normal(i)) is not None]


def answer_for(answers: list[dict], item: Item) -> dict | None:
    """The answer on record to this ask (or statement of a row) in the words it has now: the latest answer under its id
    whose words are its (`words_of`), or that names none (given before the words were kept, 39.26). None when it was
    never answered in these words: it counts as can't tell, and an answer to other words stays on record, unused
    (39.13). A row has no answer of its own: its statements are answered one by one (`expand`)."""
    if isinstance(item, Group):
        return None
    now = words_of(item)
    for kept in reversed(answers):
        if kept.get("id") != item.id:
            continue
        said = kept.get("says")
        if said is None or wording(said) == now:
            return kept
    return None


def expand(answers: list[dict], profile: Profile) -> list[dict]:
    """The answers with the old single answer to a first-match row put as the answer to the statement it named: a row
    was once answered once, with the id of its first true statement (`level`). One that named a statement, in the
    row's words as they are now, is that statement met; anything else (none, can't tell, other words) says too little
    of the statements to carry over, so they stay unanswered and are asked again. A statement already answered keeps
    its own answer."""
    out = [dict(a) for a in answers]
    for g in profile.scoring.groups:
        if g.kind != "best":
            continue
        old = next((a for a in reversed(answers) if a.get("id") == g.id and isinstance(a.get("level"), str)), None)
        if old is None or (old.get("says") is not None and wording(old["says"]) != _list_words(g)):
            continue
        named = next((m for m in g.members if m.id == old["level"]), None)
        if named is None or old.get("answer") != "met" or answer_for(out, named) is not None:
            continue
        out.append({"id": named.id, "answer": "met", "evidence": old.get("evidence", ""), "says": ask_words(named),
                    "by": old.get("by", "reader")})
    return out


def record(answers: list[dict], new: list[dict], profile: Profile, by: str) -> list[dict]:
    """A role's answers with `new` ones from a reader or the analyst (`by`) put in, each kept under its question's id
    and the question's words as they are in `profile` (an ask's sentence; a row's statements, `words_of`). The same
    question in the same words answered again takes the newer answer's place; an answer to other words stays. An answer
    to a question the profile does not have is kept in the words it came with. At most `MOST_WORDINGS` answers stay to
    one question, the newest. A first-match row's answer keeps its `level`."""
    asks = _words_by_id(profile)
    out = [dict(a) for a in answers]
    for raw in new:
        item = _normal(raw) or raw
        words = asks.get(item["id"], wording(item.get("says")) or None)
        kept = {k: item[k] for k in ("id", "answer", "evidence", "count", "level") if k in item}
        kept["evidence"] = " ".join(str(kept.get("evidence") or "").split())[:400]
        kept.update(says=words, by=by)
        out = [a for a in out if not (a["id"] == kept["id"] and (a.get("says") is None or a["says"] == words))]
        out.append(kept)
    newest_first: list[dict] = []
    counted: dict[str, int] = {}
    for a in reversed(out):
        counted[a["id"]] = counted.get(a["id"], 0) + 1
        if counted[a["id"]] <= MOST_WORDINGS:
            newest_first.append(a)
    return newest_first[::-1]


def stamp(answers: list[dict], profile: Profile) -> list[dict]:
    """The answers with the words of the question put on each that names none: an answer given before the words were
    kept is an answer to the words the ask (or the statement of a row) has in `profile`, from then on (39.26). An answer
    to a question the profile does not have stays as it is."""
    asks = _words_by_id(profile)
    return [{**a, "says": asks[a["id"]]} if a.get("says") is None and a["id"] in asks else dict(a) for a in answers]


# --- what the app decides itself -----------------------------------------------------------------------------------


def compare(have, op: str | None, want) -> bool | None:
    """Whether a fact meets a value, or None when they can't be compared."""
    op = op or "="
    try:
        if op == "in":
            # a dial's pick list: the fact holds one of the listed words ("health" is in "healthcare staffing"; "pe" is
            # in "pe" and not in "public"), the same as a "contains" of one word
            text = str(have).lower()
            return any(item in text for item in dials.values_of(want))
        if op in ("contains", "excludes"):
            hit = str(want).lower() in str(have).lower()
            return hit if op == "contains" else not hit
        if op in (">=", "<=", ">", "<"):
            a, b = float(have), float(want)
            return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]
        same = str(have).lower() == str(want).lower()
        return same if op == "=" else not same
    except (TypeError, ValueError):
        return None


def _decide_fact(ask: Ask, known: dict) -> tuple[bool, str] | None:
    """Whether an employer ask applies, from the record's facts; None when the record doesn't hold the fact."""
    if not ask.fact or ask.fact not in known or known[ask.fact] is None and ask.fact != "years_since_layoff":
        return None
    have = known[ask.fact]
    if have is None:  # researched and none found (a layoff): the "within N years" test fails
        return False, f"from the employer record: no {ask.fact.replace('years_since_', '')} on record"
    hit = compare(have, ask.op or "=", ask.value)
    if hit is None:
        return None
    source = "the posting" if ask.fact in dials.JOB_FACTS else "the employer record"
    return hit, f"from {source}: {ask.fact.replace('_', ' ')} {have}"


def _main_work_gate(known: dict, gate) -> tuple[str, str]:
    """Main work: fails only on a word of the owner's list that a model's read said is the role's main work, with a
    quote of the posting that holds that word too. Anything less (unread, no quote, none, a word outside the quote)
    passes: the posting does not show the role's main work."""
    unseen = "pass", "the posting does not show the role's main work"
    word, why = known.get(dials.MAIN_WORK), known.get(f"{dials.MAIN_WORK}_why")
    if not word or not why:
        return unseen
    in_word = dials.title_hits(str(word), gate.value, None)
    in_quote = dials.title_hits(str(why), gate.value, None)
    hit = next((w for w in in_word if w in in_quote), None)
    if hit is None:
        return unseen
    return "fail", dials.main_work_words(hit, str(why))


def dial_gate(gate, known: dict) -> tuple[str, str]:
    """A dial's Must against the facts it reads: pass, fail, or `waits` while the fact is not stated (docs/PRD.md 46.7:
    the role is held, never stopped), with the fact it rests on or waits for. A dial that `passes_unstated` (Travel,
    Leads business development) passes a posting that states none, and the Employer named flag always has its fact."""
    dial = dials.BY_FACT.get(gate.fact or "")
    have = known.get(gate.fact) if gate.fact else None
    if dial is not None and dial.fact == dials.MAIN_WORK:
        return _main_work_gate(known, gate)
    if dial is not None and dial.kind == "words":
        # Title words: the title is always stated when there is one, so this never waits; with none it passes.
        if not have:
            return "pass", "no title stated"
        found = dials.title_hits(str(have), gate.value, gate.except_words)
        if found:
            return "fail", f"the title \"{have}\" has {", ".join(found)}"
        return "pass", f"the title \"{have}\" has none of the words"
    hit = None if have is None else compare(have, gate.op, gate.value)
    if dial is not None and dial.kind == "flag":
        # Employer named: the fact is always stated (a name, or none); the reason is the words it rests on.
        if hit is None:
            return "waits", f"waiting for {dial.noun}"
        if hit:
            return "pass", "the posting names its employer"
        why = known.get(f"{gate.fact}_why")
        return "fail", f"the posting names no employer: {why}" if why else "the posting names no employer"
    if hit is None and dial is not None and dial.passes_unstated:
        # A posting that says nothing of it asks none (Travel): the Must passes, it does not hold the role.
        if dial.fact == dials.LEADS_BD:  # "states no leads business development" is no sentence
            return "pass", f"the posting does not show {dial.noun}"
        return "pass", f"the posting states no {dial.label.lower()}"
    if hit is None:
        return "waits", f"waiting for {dial.noun if dial else str(gate.fact).replace('_', ' ')}"
    if dial is not None and dial.fact == dials.LEADS_BD:  # the sentence the read rests on, not "... yes"
        return ("pass" if hit else "fail"), dials.leads_bd_words(str(have), known.get(f"{gate.fact}_why"))
    shown = dials.LABELS.get(str(have), have) if dial and dial.kind == "pick" else have
    if dial is not None and dial.unit == "%" and have is not None:
        shown = f"{float(have):g}%"
    return ("pass" if hit else "fail"), f"{dial.noun if dial else gate.fact} {shown}"


def employer_gate(gate, known: dict) -> tuple[str, str]:
    """An employer-fact gate against the record's facts: pass, fail, or unknown (the record doesn't hold the fact
    yet: research hasn't filled it), with the fact it rests on."""
    if not gate.fact or gate.fact not in known:
        return "unknown", "not researched yet"
    have = known[gate.fact]
    if have is None:
        return "unknown", f"no {gate.fact.replace('years_since_', '').replace('_', ' ')} on record"
    hit = compare(have, gate.op, gate.value)
    if hit is None:
        return "unknown", f"{gate.fact.replace('_', ' ')} {have}"
    return ("pass" if hit else "fail"), f"{gate.fact.replace('_', ' ')} {have}"


def class_gate(gate, classes: list[str]) -> tuple[str, str]:
    """A rule set to follow the analyst's classes (`Gate.from_classes`, `Gate.share`) against a role's required items:
    fail when that share of the items or more are in the class, pass when fewer are, unknown while none is classed (the
    analyst has not researched the role), with the figures it rests on. The role's share is rounded down to a whole
    percent, the figure it is shown with, so 4 of 5 is 80% and 7 of 9 is 77%: it never reaches 78."""
    classed = [c for c in classes if c in ("U", "T", "P")]
    if not classed:
        return "unknown", "no required items classed yet"
    if gate.from_classes == "PT":
        # The other way round: at least `share` percent must be Proven or Transferable; fewer fails. Exact, in whole
        # items, so 4 of 21 (19.05%) fails a 20% rule.
        count = sum(1 for c in classed if c in ("P", "T"))
        fact = (f"{count} of {len(classed)} required item{'s' if len(classed) != 1 else ''} "
                f"Proven or Transferable, {count * 100 // len(classed)}%")
        return ("pass" if count * 100 >= gate.share * len(classed) else "fail"), fact
    count = sum(1 for c in classed if c == gate.from_classes)
    fact = (f"{count} of {len(classed)} required item{'s' if len(classed) != 1 else ''} "
            f"{CLASS_WORDS[gate.from_classes]}, {count * 100 // len(classed)}%")
    return ("fail" if count * 100 >= gate.share * len(classed) else "pass"), fact


def _from_classes(ask: Ask, classes: list[str]) -> tuple[bool, str, int]:
    """Whether a requirements ask applies, from the analyst's classes; and how many items it counts."""
    if ask.from_classes == "all_P":
        hit = bool(classes) and all(c == "P" for c in classes)
        return hit, f"from the research: {'every' if hit else 'not every'} required item Proven", 1
    count = sum(1 for c in classes if c == ask.from_classes)
    return count > 0, f"from the research: {count} required item{'s' if count != 1 else ''} " \
                      f"{CLASS_WORDS[ask.from_classes]}", count


def _by_the_app(ask: Ask, known: dict, classes: list[str] | None, facts: dict) -> tuple[bool, str, int | None] | None:
    """The app's own answer to an ask: (applies, evidence, items counted), or None when it can't tell yet."""
    if ask.from_classes and classes is not None:
        return _from_classes(ask, classes)
    if ask.fact:
        decided = _decide_fact(ask, known)
        return (decided[0], decided[1], None) if decided else None
    if ask.days is not None:
        aged = _by_age(ask, ask.days, facts)
        if aged is not None:
            return aged
    if ask.amount is not None and facts.get("ask") is not None:
        have = facts["ask"]
        return pay_tier_met(ask, have), f"likely ask ${have:,}", None
    return None


def _by_age(ask: Ask, days: int, facts: dict) -> tuple[bool, str, int | None] | None:
    """The app's answer to a posting-age ask, or None when it can't tell the posting's age yet. By default the ask is
    "posted over `days` days ago" (older than, from the employer's date, else the day the role was found). With `above`
    false it is "posted within the last `days` days": under `days` x 24 hours old (`freshness.within`), counted from the
    hour the app first saw the posting when it has that, else from the date."""
    age = facts.get("age_days")
    if ask.above:
        if age is None:
            return None
        return age > days, f"posted {age} day{'s' if age != 1 else ''} ago", None
    hours = facts.get("seen_hours")
    inside = freshness.within(days, age, hours)
    if inside is None:
        return None
    return inside, freshness.evidence(days, age, hours, facts.get("age_from") or "posted"), None


def pay_tier_met(ask: Ask, have: float) -> bool:
    """Whether an ask's pay tier is met by this likely ask: over its amount for an `above` tier, under it otherwise."""
    if ask.amount is None:
        return False
    return have > ask.amount if ask.above else have < ask.amount


def _is_pay_tier(ask: Ask) -> bool:
    """Whether an ask is a pay tier and nothing else: it is decided on the likely ask alone."""
    return ask.amount is not None and not ask.from_classes and not ask.fact and ask.days is None


def pay_tiers_met(have: float, profile: Profile, target_role: str | None = None) -> list[Ask]:
    """The owner's pay tiers (asks with an `amount`) that this likely ask meets, for a role with this target role. Tiers
    that are asks of their own each count on their own and earn their points (docs/PRD.md 30.6): an ask over two tiers
    earns both, the owner's choice. Tiers the owner put in a group (30.9) count as the group does: of a first match
    the app judges, the level that holds from the top; of an any-of, the row once (its points, under the sentences of
    its statements). This is the one rule for the role's fit (`compute`) and for the pay ask's own line
    (`comp.pay_points`), so they cannot disagree."""
    met = [ask for ask in profile.asks_for(target_role) if _is_pay_tier(ask) and pay_tier_met(ask, have)]
    for group in profile.items_for(target_role):
        if not isinstance(group, Group) or not all(_is_pay_tier(m) for m in group.members):
            continue
        if group.kind == "best":
            held = next((m for m in group.members if pay_tier_met(m, have)), None) if group.by_the_app() else None
            if held is not None:
                met.append(held)
        elif any(pay_tier_met(m, have) for m in group.members):
            met.append(Ask(id=group.id, section=group.section, roles=list(group.roles), points=group.points,
                           says=" or ".join(m.says for m in group.members)))
    return met


# --- adding it up --------------------------------------------------------------------------------------------------


def _reach(ask: Ask) -> int:
    """The most an ask can add to the earned points: its points, or a per-item ask's limit
    (`score_groups.reach_single`)."""
    return score_groups.reach_single(ask.points, ask.per_item, ask.limit)


def reach(item: Item) -> int:
    """The most an ask or a group can add to the points a role can earn, counted once at its top value: a group's best
    level or its row's points, and nothing for a penalty (docs/PRD.md 30.9)."""
    if isinstance(item, Group):
        return score_groups.reach_group(item.kind, item.points, [m.points for m in item.members])
    return _reach(item)


def possible(profile: Profile, target_role: str | None = None) -> int:
    """The most the owner's rows can earn for a role with this target role: the reach of the asks and groups that are on
    and apply to it, All's and the role's own (docs/PRD.md 30.9, 31.5, 31.9), each counted once. It leaves out the
    graded requirements, whose most is each role's own (`compute` adds it to the role's score)."""
    return sum(reach(item) for item in profile.items_for(target_role))


def half_up(x: float) -> int:
    return math.floor(x + 0.5)


_WAITS_FOR_FACTS = "the app decides this once it has the facts"


def _judge(ask: Ask, answers: list[dict], known: dict, classes: list[str] | None, facts: dict) -> dict:
    """How one ask stands: its answer, the evidence for it, who decided it (the app, from a fact the record holds, the
    posting's age, a pay tier or the analyst's classes, over the reader's word) and, for an ask counted for each item,
    the items found. An ask in a group is judged the same way: a statement is an ask."""
    reader = answer_for(answers, ask) or {}
    decided = _by_the_app(ask, known, classes, facts)
    count = 1
    if decided is not None:
        answer, evidence, by = ("met" if decided[0] else "not"), decided[1], "app"
        count = decided[2] if decided[2] is not None else 1
    elif ask.checked_by == "reader" and reader.get("answer") in ANSWERS:
        answer, evidence, by = reader["answer"], " ".join(str(reader.get("evidence") or "").split())[:400], "reader"
        try:
            count = max(1, int(reader.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
    else:
        answer, by = "unknown", "app" if ask.checked_by == "app" else "reader"
        evidence = _WAITS_FOR_FACTS if ask.checked_by == "app" else _not_answered(answers, ask.id)
    return {"answer": answer, "evidence": evidence, "by": by, "count": count}


def _not_answered(answers: list[dict], ident: str) -> str:
    return NOT_ANSWERED + (" in its present words" if any(a.get("id") == ident for a in answers) else "")


def _holds_from_answers(given: Sequence[dict | None]) -> list[bool | None]:
    """Which statements of a first match the reader's answers make true: the row earns the first statement from the top
    that is met; one not met or that can't be told is skipped (no partial credit: if you can't tell, it is a zero). When
    none is met the row is a zero, unless a statement is still unanswered, which leaves it undecided."""
    met = next((i for i, g in enumerate(given) if g and g["answer"] == "met"), None)
    holds: list[bool | None] = [False] * len(given)
    if met is not None:
        holds[met] = True
    elif any(g is None for g in given):
        holds = [None] * len(given)
    return holds


#: What `_single`, `_first_match`, `_any_of` and `_graded` hand `compute`: the row for the arithmetic, and how to write
#: its line of the breakdown once it is scored.
Built = tuple[score_groups.Item, Callable[[score_groups.ItemScore], dict]]


def _single(ask: Ask, answers: list[dict], known: dict, classes: list[str] | None, facts: dict) -> Built:
    judged = _judge(ask, answers, known, classes, facts)
    item = score_groups.Single(ask.id, ask.points, judged["answer"], ask.per_item, ask.limit, judged["count"])

    def row(s: score_groups.ItemScore) -> dict:
        out = {"id": ask.id, "section": ask.section, "says": ask.says, "points": ask.points,
               "answer": judged["answer"], "earned": round(s.earned, 2), "evidence": judged["evidence"],
               "by": judged["by"], "decided": s.decided}
        if ask.per_item:
            out["count"] = judged["count"]
        return out

    return item, row


def _first_match(group: Group, answers: list[dict], known: dict, classes: list[str] | None, facts: dict) -> Built:
    """A first match is judged by the app or by the reader, never both. The app tests the statements from the top (a
    fact the record does not hold yet stops the scan). The reader answers each statement on its own (`answer_for`, in
    the words it has now) and the row earns the first one from the top that is met (`_holds_from_answers`)."""
    members = group.members
    if group.by_the_app():
        stands = [_by_the_app(m, known, classes, facts) for m in members]
        holds = [None if d is None else d[0] for d in stands]
        said, by = [d[1] if d is not None else _WAITS_FOR_FACTS for d in stands], "app"
    else:
        given = [answer_for(answers, m) for m in members]
        holds = _holds_from_answers(given)
        by = "reader"
        said = [" ".join(str(g.get("evidence") or "").split())[:400] if g else _not_answered(answers, m.id)
                for m, g in zip(members, given, strict=True)]
    item = score_groups.FirstMatch(group.id, tuple(score_groups.Level(m.id, m.points, h)
                                                   for m, h in zip(members, holds, strict=True)))

    def row(s: score_groups.ItemScore) -> dict:
        shown = members[s.held] if s.held is not None else members[0]
        level = members[s.held].id if s.held is not None else "none" if s.decided else "unknown"
        if by == "app":
            stopped = next((i for i, h in enumerate(holds) if h is None), None)
            evidence = (said[s.held] if s.held is not None else "; ".join(said) if s.decided else
                        said[stopped if stopped is not None else 0])
        elif s.held is not None:
            evidence = said[s.held]
        else:
            evidence = "; ".join(w for w in said if w) if s.decided else next(
                (w for g, w in zip(given, said, strict=True) if g is None), "")
        return {"id": group.id, "kind": "best", "section": group.section, "says": shown.says, "points": shown.points,
                "answer": ("met" if s.held is not None else "not") if s.decided else "unknown", "level": level,
                "levels": [{"id": m.id, "says": m.says, "points": m.points} for m in members],
                "earned": round(s.earned, 2), "decided": s.decided, "evidence": evidence, "by": by}

    return item, row


def _any_of(group: Group, answers: list[dict], known: dict, classes: list[str] | None, facts: dict) -> Built:
    """An any-of is judged by its statements, each as an ask is (the app's own answer over the reader's): the row earns
    its points once, at the best statement's weight."""
    members = group.members
    judged = [_judge(m, answers, known, classes, facts) for m in members]
    item = score_groups.AnyOf(group.id, group.points, tuple(j["answer"] for j in judged))

    def row(s: score_groups.ItemScore) -> dict:
        gave = next((j for j in judged if s.decided and WEIGHT.get(j["answer"]) == s.weight), None)
        unsaid = next((j for j in judged if j["answer"] == "unknown"), judged[0])
        answer = ("met" if s.weight == 1.0 else "not") if s.decided and s.weight is not None else "unknown"
        return {"id": group.id, "kind": "any", "section": group.section, "says": " or ".join(m.says for m in members),
                "points": group.points, "answer": answer,
                "parts": [{"id": m.id, "says": m.says, "answer": j["answer"], "evidence": j["evidence"], "by": j["by"]}
                          for m, j in zip(members, judged, strict=True)],
                "earned": round(s.earned, 2), "decided": s.decided, "evidence": (gave or unsaid)["evidence"],
                "by": "app" if all(j["by"] == "app" for j in judged) else "reader"}

    return item, row


def _tally(classes: list[str] | tuple[str, ...]) -> dict[str, int]:
    return {"proven": classes.count("P"), "transferable": classes.count("T"), "unproven": classes.count("U")}


def _graded(profile: Profile, classes: list[str] | None, preferred: list[str] | None,
            responsibilities: list[str] | None = None) -> Built | None:
    """"Grade the requirements for me": the row the app decides itself, from the analyst's classes of the required items
    (when Qualifications is on) and of the preferred ones (when Preferred is on). The posting's responsibilities,
    classed the same way, count with the required items, as more of them, by the same points (the owner, 2026-10-16).
    None when it is off, or both parts are. It is never asked of an agent, and it is decided whenever the role is
    scored: a role has no score before it is researched, and one researched with no item to class earns nothing from it
    and can reach nothing."""
    settings = profile.scoring
    grades_required, grades_preferred = settings.grades_required(), settings.grades_preferred()
    if not (grades_required or grades_preferred):
        return None
    g = settings.grade_requirements
    if g is None:  # `grades_required` and `grades_preferred` hold only with the setting on: here for the type
        return None
    duties = tuple(c for c in responsibilities or () if c in CLASS_WORDS) if grades_required else ()
    stated = tuple(c for c in classes or () if c in CLASS_WORDS) if grades_required else ()
    required = stated + duties
    liked = tuple(c for c in preferred or () if c in CLASS_WORDS) if grades_preferred else ()
    weights = score_groups.GradeWeights(g.required_proven, g.required_transferable, g.required_unproven,
                                        g.preferred_proven_or_transferable, g.preferred_unproven)
    item = score_groups.Graded(GRADE_ID, required, liked, weights)

    def words(side: str, tally: dict[str, int]) -> str:
        return f"{side} " + ", ".join(f"{n} {CLASS_WORDS[k[0].upper()]}" for k, n in tally.items())

    def row(s: score_groups.ItemScore) -> dict:
        seen = [words("required", _tally(stated))] if grades_required else []
        seen += [words("responsibilities", _tally(duties))] if duties else []
        seen += [words("preferred", _tally(liked))] if grades_preferred else []
        answer = "met" if s.reach > 0 and s.earned >= s.reach else "partly" if s.earned > 0 else "not"
        return {"id": GRADE_ID, "kind": "grade", "section": "qualifications" if grades_required else "preferred",
                "says": GRADE_SAYS, "points": s.reach, "answer": answer, "earned": round(s.earned, 2), "decided": True,
                "evidence": "from the research: " + "; ".join(seen), "by": "app",
                "required": _tally(stated) if grades_required else None,
                "responsibilities": _tally(duties) if grades_required else None,
                "preferred": _tally(liked) if grades_preferred else None,
                CARD_PREFERRED: _tally(tuple(c for c in preferred or () if c in CLASS_WORDS))}

    return item, row


def compute(raw: dict | None, profile: Profile, employer_facts: dict | None = None, classes: list[str] | None = None,
            facts: dict | None = None, target_role: str | None = None, version: int | None = None,
            preferred: list[str] | None = None, responsibilities: list[str] | None = None) -> dict | None:
    """The breakdown and the fit it adds up to, or None when the report has no answers (the report's own fit stands).

    `raw` is a reader's report (`{asks: [{id, answer, evidence}]}`) or a role's stored answers (`{answers: [...]}`). An
    answer counts only for the ask in the words it was given for: one to other words is not used (`answer_for`), and
    the ask counts as can't tell. A first match is answered once, `{id, answer, level, evidence}` with `level` the id
    of its first true statement, `none` or `unknown`; the statements of an any-of are answered as asks are.
    `employer_facts` are what the employer record holds, `classes` the analyst's classes of the required items and
    `preferred` of the preferred ones (they decide an ask or a statement set to follow them, and the graded
    requirements), `responsibilities` of the posting's duties (they count in the graded requirements only, as more
    required items), `facts` what the app knows of the posting (`age_days`, `ask`: the likely ask). Only a met ask earns
    its points: the rest, unknown and partly met among them, earn nothing, and the classifier's probability is not read.
    `target_role` is the role the posting matched: only the items that apply to it are scored (docs/PRD.md 31.5), so an
    answer to an ask that does not apply is set aside, and `possible` is that role's own. `version` is the version of
    Prioritization the score is worked out under (39.21), kept with it. The same inputs always give the same
    breakdown: the rows are `asks` (an ask's, a group's, and the graded requirements'), then `earned` (two decimals),
    `decided` (the points the decided rows can add), `possible`, `coverage` (a whole percent), `percent` (worked out
    once from the exact points; None when there is none), `total` (the whole points), `version` and `math`."""
    if not is_asks(raw):
        return None
    known, facts = employer_facts or {}, facts or {}
    answers = expand(answers_in(raw), profile)
    built: list[Built] = []
    for item in profile.items_for(target_role):
        if isinstance(item, Group):
            built.append((_first_match if item.kind == "best" else _any_of)(item, answers, known, classes, facts))
        else:
            built.append(_single(item, answers, known, classes, facts))
    if (graded := _graded(profile, classes, preferred, responsibilities)) is not None:
        built.append(graded)
    scored = score_groups.score([item for item, _row in built])
    out = {"asks": [row(s) for (_item, row), s in zip(built, scored.items, strict=True)], "earned": scored.earned,
           "decided": scored.decided, "possible": scored.possible, "coverage": scored.coverage,
           "percent": scored.percent, "total": scored.total}
    return {**out, "version": version, "math": MATH} if version is not None else {**out, "math": MATH}


def answers_line(source: dict | None, profile: Profile, target_role: str | None = None, limit: int = 300) -> str:
    """The same line for a role that has answers and no score yet: how many of the asks that count for it were met,
    partly met, not met or can't be told, and the biggest gap. The statements of an any-of count as asks, and a first
    match the reader has answered counts once (met when a statement is met, not when none is). It adds nothing up.
    Empty when the role has no answers."""
    answers = answers_in(source)
    if not answers:
        return ""
    counts = {"met": 0, "partly": 0, "not": 0, "unknown": 0}
    gaps: list[tuple[int, str]] = []
    for item in profile.items_for(target_role):
        given_to: list[tuple[dict, int, str]] = []  # (answer, points, sentence)
        if isinstance(item, Group) and item.kind == "best":
            held = None if item.by_the_app() else _holds_from_answers([answer_for(answers, m) for m in item.members])
            if held is not None and None not in held:
                met = {"answer": "met" if True in held else "not"}
                given_to.append((met, reach(item), item.members[0].says))
        elif isinstance(item, Group):
            parts = [(given, m) for m in item.members
                     if m.checked_by == "reader" and (given := answer_for(answers, m)) is not None]
            met = any(given["answer"] == "met" for given, _m in parts)  # a part met: no part is a gap
            given_to += [(given, 0 if met else item.points, m.says) for given, m in parts]
        elif item.checked_by == "reader" and (given := answer_for(answers, item)) is not None:
            given_to.append((given, item.points, item.says))
        for given, points, says in given_to:
            counts[given["answer"]] += 1
            if given["answer"] == "not" and points > 0:
                gaps.append((points, says))
    words = (("met", "met"), ("partly", "partly met"), ("not", "not met"), ("unknown", "can't tell"))
    line = ", ".join(f"{counts[k]} {w}" for k, w in words if counts[k]) or "no asks answered"
    if gaps:
        line += f"; biggest gap: {max(gaps, key=lambda g: g[0])[1]}"
    return line[:limit]


# --- the role panel ------------------------------------------------------------------------------------------------


def _group_view(group: Group, stored: dict | None, answers: list[dict]) -> dict:
    """A group's row for the role panel: the row it was scored as (a first match's held level and every level, an
    any-of's statements), or, before the role is scored, what the group is and the answer a reader gave it."""
    base = {"id": group.id, "kind": group.kind, "section": group.section, "section_label": SECTION_LABEL[group.section]}
    if stored is not None:
        return {**stored, **base, "state": "unknown" if stored.get("answer") == "unknown" else "checked"}
    by = "app" if group.by_the_app() else "reader"
    if group.kind == "best":
        top = group.members[0]
        held = None if group.by_the_app() else _holds_from_answers([answer_for(answers, m) for m in group.members])
        at = held.index(True) if held and True in held else None
        decided = held is not None and None not in held
        given = answer_for(answers, group.members[at]) if at is not None else None
        return {**base, "says": top.says, "points": top.points,
                "answer": ("met" if at is not None else "not") if decided else None,
                "level": (group.members[at].id if at is not None else "none") if decided else None,
                "levels": [{"id": m.id, "says": m.says, "points": m.points} for m in group.members],
                "earned": None, "evidence": (given.get("evidence") if given else "") or "", "by": by,
                "state": "unscored"}
    parts = []
    for m in group.members:
        given = answer_for(answers, m) if m.checked_by == "reader" else None
        parts.append({"id": m.id, "says": m.says, "answer": given["answer"] if given else None,
                      "evidence": (given.get("evidence") if given else "") or "",
                      "by": "app" if m.checked_by == "app" else "reader"})
    return {**base, "says": " or ".join(m.says for m in group.members), "points": group.points, "answer": None,
            "parts": parts, "earned": None, "evidence": "", "by": by, "state": "unscored"}


def view(breakdown: dict | None, profile: Profile, target_role: str | None = None) -> list[dict]:
    """Every ask and group the owner has that is on and applies to the role's target role, with the section it is
    about, for the role panel: its answer, the points it earned and why, who decided it, and whether it was checked,
    can't be told, or was added after this role was scored. A group is one row, with its statements inside it (a first
    match's levels with the held one, an any-of's parts), and the graded requirements, when the role was scored with
    them, one more. As the role was scored: the answer and the points are the stored ones, so changing a weight shows in
    the next score, not in this one. A role with answers and no score yet (it waits for its research) shows each answer
    it holds to the ask in its present words, with no points. In posting order, then by points."""
    rows_kept = breakdown.get("asks") if isinstance(breakdown, dict) else None
    stored = {a["id"]: a for a in rows_kept} if isinstance(rows_kept, list) else {}
    answers = expand(answers_in(breakdown), profile)
    rows = []
    for ask in profile.items_for(target_role):
        if isinstance(ask, Group):
            rows.append(_group_view(ask, stored.get(ask.id), answers))
            continue
        a = stored.get(ask.id)
        if a is None and (given := answer_for(answers, ask)) is not None and ask.checked_by == "reader":
            a = {"points": ask.points, "answer": given["answer"], "earned": None,
                 "evidence": given.get("evidence") or "", "by": "reader"}  # answered, and not yet scored
        row = {
            "id": ask.id, "section": ask.section, "section_label": SECTION_LABEL[ask.section], "says": ask.says,
            "points": a["points"] if a else ask.points,
            "answer": a["answer"] if a else None, "earned": a["earned"] if a else None,
            "evidence": (a["evidence"] if a else "") or "",
            "by": a["by"] if a else ("app" if ask.checked_by == "app" else "reader"),
        }
        row["state"] = "unscored" if a is None else "unknown" if a["answer"] == "unknown" else "checked"
        rows.append(row)
    if (graded := stored.get(GRADE_ID)) is not None and graded.get("section") in SECTION_LABEL:
        rows.append({**graded, "section_label": SECTION_LABEL[graded["section"]], "state": "checked"})
    order = {s: i for i, s in enumerate(SECTION_LABEL)}
    return sorted(rows, key=lambda r: (order[r["section"]], -r["points"]))


# --- fit as a percent (docs/PRD.md 23.4) ----------------------------------------------------------------------------


class Span(tuple):
    """The lowest and highest raw fit the owner's asks allow, `(0, possible)` for a role with no target role (only the
    asks for every role), and as `legacy` the scale roles scored before asks are shown on (None when there was none).
    Unpacks like that pair. `by_role` holds each target role's own possible points (docs/PRD.md 31.5): a role's span is
    `for_role(its target role)`. `match_weight` is the owner's share of a read role's queue score that is its match
    (`Scoring.match_weight`, `queue_percent`)."""

    legacy: tuple[int, int] | None
    by_role: dict[str, int]
    match_weight: float

    def __new__(cls, low: int, high: int, legacy: tuple[int, int] | None = None,
                by_role: dict[str, int] | None = None, match_weight: float = MATCH_WEIGHT_DEFAULT):
        out = super().__new__(cls, (low, high))
        out.legacy = legacy
        out.by_role = dict(by_role or {})
        out.match_weight = match_weight
        return out

    def for_role(self, target_role: str | None) -> tuple[int, int]:
        """The lowest and highest raw fit for a role with this target role: its own possible points when it is one of
        the owner's, else the all-roles ones."""
        high = self.by_role.get(target_role) if target_role else None
        return (self[0], self[1] if high is None else high)


def possible_by_role(profile: Profile) -> dict[str, int]:
    """The most each target role's asks can earn, by the role's id (what the profile page shows for a tab)."""
    return {r.id: possible(profile, r.id) for r in profile.criteria.target_roles}


def grading_best(profile: Profile) -> dict[str, int]:
    """The best positive number the owner set for one required requirement and for one preferred requirement, 0 for a
    side with none (or when the requirements are not graded): what each requirement a posting lists can add to a role's
    most."""
    g = profile.scoring.grade_requirements
    if g is None or not g.on:
        return {"required": 0, "preferred": 0}
    return {"required": max(0, g.required_proven, g.required_transferable, g.required_unproven),
            "preferred": max(0, g.preferred_proven_or_transferable, g.preferred_unproven)}


def bounds(profile: Profile) -> Span | None:
    """The span the owner's asks allow, or None when they say nothing (no ask with positive points for any role, no old
    scale)."""
    old = profile.scoring.old_scale
    legacy = (old.low, old.high) if old is not None else None
    top, own = possible(profile), possible_by_role(profile)
    if top > 0 or any(v > 0 for v in own.values()) or legacy is not None:
        return Span(0, top, legacy, own, profile.scoring.match_weight)
    return None


def percent(raw: int | None, span: tuple[int, int] | None) -> int | None:
    """A raw fit as a share of what was possible, 0 to 100."""
    if raw is None or span is None:
        return None
    low, high = span[0], span[1]
    if high <= low:
        return None
    return max(0, min(100, half_up((raw - low) * 100 / (high - low))))  # half up, as the totals are: 12.5 is 13


def _scale_of(role, span: tuple[int, int] | None) -> tuple[int, int] | None:
    """The lowest and highest raw fit a role's fit is measured against: what `percent_of` takes its percent of. None
    when there is no scale to make a percent of."""
    breakdown = getattr(role, "fit_breakdown", None)
    if isinstance(breakdown, dict) and is_asks(breakdown):
        against = breakdown.get("possible")
        if isinstance(against, int | float) and not isinstance(against, bool) and against > 0:
            return (0, int(against))
        if span is None:
            return None
        for_role = getattr(span, "for_role", None)
        return for_role(getattr(role, "target_role", None)) if for_role else span
    if span is None:
        return None
    return getattr(span, "legacy", None)


def _stored_percent(role) -> tuple[bool, int | None]:
    """`(worked out by `compute`, its percent)`: a score that carries the number of the arithmetic that worked it out
    (`math`) holds its own percent, worked out once from the exact earned points and kept with it (None when it has
    none: too little decided, or nothing it could earn)."""
    breakdown = getattr(role, "fit_breakdown", None)
    if not (isinstance(breakdown, dict) and is_asks(breakdown)):
        return False, None
    stamp = breakdown.get("math")
    if not isinstance(stamp, int) or isinstance(stamp, bool):
        return False, None
    stored = breakdown.get("percent")
    return True, stored if isinstance(stored, int) and not isinstance(stored, bool) else None


def percent_of(role, span: tuple[int, int] | None) -> int | None:
    """A role's fit as a percent. A role scored by the arithmetic of docs/PRD.md 30.9 holds its percent, worked out once
    from the exact earned points over the points it was scored against, so editing the asks cannot move it before the
    score is worked out again (39.21). A role scored on the asks before that (no `math`) has it from its whole-point fit
    against the points it was scored against (`possible`); one with none kept is judged against the points its own
    target role's asks can earn now (31.5). A role scored the old way, or that came from the tracker, is judged against
    the old scale."""
    if role.fit is None:
        return None
    worked, stored = _stored_percent(role)
    if worked:
        return stored
    return percent(role.fit, _scale_of(role, span))


# --- the score the best-of queue sorts a read role by ---------------------------------------------------------------

# The owner sets how much of a read role's queue score is its match, the Proven share of its required items
# (`Scoring.match_weight`, half unless they say); the owner's other asks make up the rest, by their own points. The fit
# as a percent sorted the roles applied to barely better than chance (the owner, 2026-10-23), because the graded row's
# share of it grows with the number of items a posting lists (on a short posting the asks were half the score, on a long
# one a tenth), and a Transferable item counted as half met. A Proven class was the most accurate read of "met", and
# counting Transferable as met the least.


def _grade_row(breakdown: dict) -> dict | None:
    rows = breakdown.get("asks")
    if not isinstance(rows, list):
        return None
    return next((r for r in rows if isinstance(r, dict) and r.get("kind") == "grade"), None)


def _count(tally, key: str) -> int:
    value = tally.get(key) if isinstance(tally, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def match_share(grade_row: dict) -> float | None:
    """How well a role's requirements match, 0 to 1: the share of its classed required items, the posting's
    responsibilities and its preferred items (the owner, 2026-11-12) that the analyst classed Proven. A Transferable or
    Unproven item is not met; N is in no count. The preferred items count whether or not the owner's Preferred section
    is on (that setting decides only what the shown fit counts): the row keeps their tally apart for the card
    (`CARD_PREFERRED`), and a row kept before that key reads `preferred` as it is. A share, so a posting that lists many
    items weighs no more than one that lists few. None when no item was classed."""
    liked = grade_row[CARD_PREFERRED] if CARD_PREFERRED in grade_row else grade_row.get("preferred")
    tallies = (grade_row.get("required"), grade_row.get("responsibilities"), liked)
    proven = sum(_count(t, "proven") for t in tallies)
    total = proven + sum(_count(t, "transferable") + _count(t, "unproven") for t in tallies)
    return proven / total if total else None


def asks_share(breakdown: dict, grade_row: dict) -> float | None:
    """What the owner's asks other than the graded requirements earned, as a share of what the ones with a known answer
    can earn, held between 0 and 1. An ask nobody could tell (undecided) is left out of both: it earns nothing and its
    points leave the room, so a reader's silence neither lowers a role nor raises it. A penalty is outside the room, as
    in the fit, and a met one takes its points off what was earned (so it can take the share to 0). None when the
    decided asks can earn nothing."""
    numbers = [breakdown.get("decided"), breakdown.get("earned"), grade_row.get("points"), grade_row.get("earned")]
    decided, earned, reach, got = (float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None
                                   for v in numbers)
    if decided is None or earned is None or reach is None or got is None:
        return None
    room = decided - reach  # the graded row is always decided: what is left is the decided asks' reach
    return max(0.0, min(1.0, (earned - got) / room)) if room > 0 else None


def queue_percent(role, span: tuple[int, int] | None) -> int | None:
    """The number the best-of queue sorts a read role by (`pipeline.score`), 0 to 100: w x the Proven share of its
    required items, duties and preferred items (`match_share`) + (1 - w) x the share of the owner's other asks with a
    known answer it earned (`asks_share`: their own points, pay and employer facts among them, so those stay the owner's
    priorities; the hard gates close roles before any of this). w is the owner's (`Scoring.match_weight`, carried on the
    span; half on a plain pair). A role with no percent has no queue score either. A role whose score holds
    no graded requirements (the owner does not grade them, or nothing was classed), or that was scored before this
    arithmetic, is sorted by its percent (`percent_of`) as before; one with graded requirements and no decided ask
    that can earn is its match."""
    shown = percent_of(role, span)
    if shown is None:
        return None
    worked, _ = _stored_percent(role)
    if not worked:
        return shown
    breakdown = role.fit_breakdown
    grade = _grade_row(breakdown)
    match = match_share(grade) if grade is not None else None
    if grade is None or match is None:
        return shown
    w = getattr(span, "match_weight", MATCH_WEIGHT_DEFAULT)
    asks = asks_share(breakdown, grade)
    value = match if asks is None else w * match + (1 - w) * asks
    return max(0, min(100, half_up(value * 100)))


def has_no_percent(role) -> bool:
    """Whether a role was scored by the arithmetic of docs/PRD.md 30.9 and that gave it no percent (nothing decided, or
    nothing it could earn). Its points are over no scale, so a list shows it the dash a role with no fit gets, and never
    its raw points."""
    worked, stored = _stored_percent(role)
    return worked and stored is None
