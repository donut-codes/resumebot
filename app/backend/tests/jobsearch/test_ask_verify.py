"""An ask's Verify line (docs/PRD.md 30.9, the owner 2026-10-14: the agent has a plan for how it scores, and
it repeats it): each ask and each statement of a first-match or "any of" row may carry a Verify text that says exactly
how to check it against a posting, so the same posting gets the same answer every time. The model keeps it (the
owner's text, no limit on its length; an older document loads with none), the reader is sent it and told to apply it
exactly, and a changed Verify line is a reworded ask, asked again. Made-up asks, ids and sentences only."""

import copy

from app import db
from app.harness import instructions as harness
from app.jobsearch.fit import scoring
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import scoring as ps
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile import views as pv
from tests.support import FIVE, OWNER

VERIFY = "The posting names a budget of 1 million dollars or more, in the work or the requirements."

#: A fourth ask, on the All tab, that no role has an answer to yet.
GRANTS = {"id": "ask-d", "section": "preferred", "says": "The work includes writing grants", "points": 1}
#: A first-match row of three statements, best first, and an "any of" row of two, both on the All tab.
FIRST = {"id": "row-a", "kind": "best", "section": "responsibilities", "members": [
    {"id": "stmt-a", "says": "The work is mostly building", "points": 4},
    {"id": "stmt-b", "says": "The work is partly building", "points": 3},
    {"id": "stmt-c", "says": "The work is a little building", "points": 2}]}
ANY = {"id": "box-a", "kind": "any", "section": "company", "points": 2, "members": [
    {"id": "stmt-d", "says": "The employer is a public agency"},
    {"id": "stmt-e", "says": "The employer is a nonprofit"}]}


def scoring_of(*asks: dict, groups=()) -> ps.Scoring:
    return ps.Scoring.model_validate({"asks": list(asks), "groups": list(groups)})


def asks_sent(profile: p.Profile) -> dict[str, dict]:
    """What a reader is sent, by id (asks, first-match rows and the statements of an "any of" row)."""
    return {a["id"]: a for s in pv.scoring_for_agent(profile.scoring)
            for a in s["asks"]}


# --- the model ------------------------------------------------------------------------------------------------------


def test_an_ask_and_the_statements_of_both_kinds_of_row_keep_their_verify_line():
    first = {**FIRST, "members": [{**m, "verify": f"Verify {m['id']}"} for m in FIRST["members"]]}
    anyof = {**ANY, "members": [{**m, "verify": f"Verify {m['id']}"} for m in ANY["members"]]}
    s = scoring_of({**GRANTS, "verify": VERIFY}, groups=(first, anyof))
    assert s.asks[0].verify == VERIFY
    assert [m.verify for m in s.groups[0].members] == ["Verify stmt-a", "Verify stmt-b", "Verify stmt-c"]
    assert [m.verify for m in s.groups[1].members] == ["Verify stmt-d", "Verify stmt-e"]
    again = ps.Scoring.model_validate(s.model_dump(mode="json"))
    assert again == s, "it dumps and loads again as it was"


def test_an_older_document_with_no_verify_loads_with_none():
    old = copy.deepcopy({**FIVE, "groups": [FIRST, ANY]})
    assert all("verify" not in a for a in old["asks"])
    s = ps.Scoring.model_validate(old)
    assert [a.verify for a in s.asks] == [""] * len(s.asks) and s.asks
    assert all(m.verify == "" for g in s.groups for m in g.members)


async def test_a_verify_line_is_stored_in_the_versioned_document_and_read_back(js):
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "scoring", {"asks": [{**GRANTS, "verify": VERIFY}]}, by="test")
        await session.commit()
    async with db.session_factory()() as session:
        profile = await profile_store.current(session, OWNER)
    assert profile.scoring.asks[0].verify == VERIFY


async def test_a_verify_line_has_no_limit_on_its_length(js):
    long = ("The posting says so, in these words or others that mean the same. " * 4000).strip()
    assert len(long) > 200_000
    s = scoring_of({**GRANTS, "verify": long})
    assert s.asks[0].verify == long, "kept whole, never cut"
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "scoring", {"asks": [{**GRANTS, "verify": long}]}, by="test")
        await session.commit()
    async with db.session_factory()() as session:
        assert (await profile_store.current(session, OWNER)).scoring.asks[0].verify == long


# --- what the reader is sent ----------------------------------------------------------------------------------------


def test_the_reader_is_sent_each_asks_verify_line_and_an_ask_with_none_is_sent_none():
    first = {**FIRST, "members": [{**FIRST["members"][0], "verify": "Verify the top one"}, *FIRST["members"][1:]]}
    anyof = {**ANY, "members": [{**ANY["members"][0], "verify": "Verify the agency one"}, ANY["members"][1]]}
    prof = p.Profile.model_validate({"scoring": {"asks": [
        {**GRANTS, "verify": VERIFY}, {"id": "ask-x", "section": "description", "says": "Plain ask", "points": 1}],
        "groups": [first, anyof]}})
    sent = asks_sent(prof)
    assert sent["ask-d"]["verify"] == VERIFY
    assert "verify" not in sent["ask-x"], "no Verify line: sent as it is today"
    assert sent["stmt-a"]["verify"] == "Verify the top one"
    assert all("verify" not in sent[i] for i in ("stmt-b", "stmt-c"))
    assert sent["stmt-d"]["verify"] == "Verify the agency one" and "verify" not in sent["stmt-e"]
    assert pv.for_scout(prof)["scoring"]["sections"], "the reader's profile carries the same rows"
    assert VERIFY in str(pv.for_scout(prof)["scoring"])


def test_a_verify_line_of_only_spaces_is_sent_as_none():
    prof = p.Profile.model_validate({"scoring": {"asks": [{**GRANTS, "verify": "   "}]}})
    assert "verify" not in asks_sent(prof)["ask-d"]


def test_the_agents_are_told_to_apply_a_verify_line_exactly():
    shared = harness.read("instructions", "jobsearch.scoring").lower()
    assert "verify" in shared
    for rule in ("exactly", "met", "not", "unknown", "no `verify`"):
        assert rule in shared, rule


# --- a changed Verify line is a reworded ask ------------------------------------------------------------------------


def test_an_ask_with_no_verify_line_has_the_words_it_always_had():
    prof = p.Profile.model_validate({"scoring": {"asks": [GRANTS]}})
    ask = prof.scoring.asks[0]
    assert scoring.words_of(ask) == scoring.wording(ask.says), "an old answer's words still match"
    assert scoring.stamp([{"id": "ask-d", "answer": "met"}], prof)[0]["says"] == scoring.wording(ask.says)


def test_a_verify_line_is_part_of_the_words_an_answer_is_kept_under_and_spaces_in_it_do_not_count():
    a = ps.Ask(id="ask-d", says="Writes grants", verify="Names grant writing")
    assert scoring.words_of(a) != scoring.words_of(a.model_copy(update={"verify": "Names grant making"}))
    assert scoring.words_of(a) == scoring.words_of(a.model_copy(update={"verify": "  Names   grant writing "}))
    assert scoring.words_of(a) != scoring.words_of(a.model_copy(update={"verify": ""}))
