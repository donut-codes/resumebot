"""The story library (build_library.bpmn, build_library_part.bpmn, `writing/library.py`): built by its seats from the
master resume, its parts written side by side, checked when it is built (code checks, a facts read), fixed once with
what still fails held out, rebuilt only where the master changed, and read by the owner. Lines of one accomplishment
share a claim. Every seat's answer is scripted; everything is made up."""

import copy

from process_engine.models import ProcInstance
from sqlalchemy import select

from app import db
from app.jobsearch.models import StoryLibrary
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.documents import Documents
from app.jobsearch.writing import library as story_library
from app.jobsearch.writing import seats
from tests.builders import ABOUT, RESUME, WRITING, run_seats
from tests.support import OWNER, load_profile

LINES = {
    "rules": {"protected": ["Initech"], "approved": ["Built the practice knowledge base"],
              "roles": [{"role": "northwind-lead", "min": 1, "max": 4, "scope": True, "firm_first": True},
                        {"role": "nobody", "min": 9}]},
    "scopes": [{"role": "northwind-lead", "text": "Ran the practice's risk work across client engagements.",
                "from": ["b-risk"]}],
    "entries": [
        {"entry": "b-risk", "status": "bullet", "bullets": [{
            "role": "northwind-lead", "claim": "risk", "side": "client", "for": "programs",
            "skills": ["Risk Management", "Governance"],
            "text": "Led the risk working group that reviewed each client engagement every quarter",
            "from": ["b-risk"]}]},
        {"entry": "b-kb", "status": "bullet", "bullets": [{
            "role": "northwind-lead", "work": "kb", "side": "firm", "for": "programs",
            "text": "Built the practice knowledge base", "from": ["b-kb"]}]},
        {"entry": "a-statewide", "status": "bullet", "bullets": [{
            "role": None, "work": "statewide", "for": "programs", "text": "Led a statewide program for Initech",
            "from": ["a-statewide"]}]},
        {"entry": "e-board", "status": "folded", "into": "b-risk"},
    ],
}
LETTERS = {"stories": [{"id": "Risk group", "title": "The risk group", "index": "Risk work clients used", "blocks": [
    {"for": "programs", "sentences": [
        {"text": "The working group met across client engagements.", "from": ["b-risk"]},
        {"text": "The board saw the risk register each quarter.", "from": ["e-board"]}]}]}]}


async def _documents(resume=None, writing=None) -> None:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        for kind, data in {"writing": writing or WRITING, "about": ABOUT, "resume": resume or RESUME}.items():
            await profile_store.save(session, OWNER, kind, data, by="test")
        await session.commit()


async def _newest(status="ready") -> StoryLibrary | None:
    async with db.session_factory()() as session:
        return await story_library.newest(session, OWNER, status)


def _docs() -> Documents:
    return Documents.model_validate({"resume": RESUME, "about": ABOUT, "writing": WRITING})


async def test_saving_the_master_starts_a_build_its_seats_write_check_fix_and_save_a_ready_version(js):
    await _documents()
    building = await _newest("building")
    assert building is not None
    facts = {"failures": [{"id": "L001", "reason": "the master names no count of reviews"}], "dismissed": []}
    fixed = {"lines": [
        {"id": "L001", "text": "Led the risk working group that reviewed the practice's client engagements",
         "from": ["b-risk"]},
        {"id": "L003", "text": "Led a statewide program from start to finish for the agency", "from": ["a-statewide"]}]}
    facts["skills_dropped"] = [{"id": "L001", "skill": "governance"}]
    done = await run_seats({"Write_lines": LINES, "Write_letters": LETTERS, "Check_1": facts,
                            "Fix": fixed, "Check_2": {"failures": []}})
    # the resume lines (one turn, with the owner's rules) and the letter blocks are written side by side
    assert done == ["Write_lines", "Write_letters", "Check_1", "Fix", "Check_2"]
    row = await _newest()
    assert row is not None and row.version == building.version
    data = row.data
    lines = {ln["id"]: ln for ln in data["lines"]}
    assert lines["L001"]["text"] == "Led the risk working group that reviewed the practice's client engagements"
    assert not lines["L001"].get("held") and not lines["L003"].get("held")
    assert lines["S-northwind-lead"]["kind"] == "scope"
    assert lines["L001"]["claim"] == "risk" and lines["L001"]["skills"] == ["Risk Management"]  # the read dropped one
    assert lines["L003"]["kind"] == "achievement" and lines["L003"]["entry"] == "a-statewide"
    # the owner's rules, read by the seat, are kept for code: a role the master lacks is dropped
    assert story_library.role_rules(data, "northwind-lead") == {"min": 1, "max": 4, "firm_first": True, "scope": True}
    assert "nobody" not in data["rules"]["roles"]
    assert data["stories"][0]["id"] == "risk-group" and data["stories"][0]["blocks"][0]["id"] == "risk-group-a"
    assert "e-board" in data["decided"]  # folded entries are decided too: not asked about again
    async with db.session_factory()() as session:
        assert await story_library.is_current(session, OWNER)
        assert await story_library.usable(session, OWNER) is not None


async def test_what_the_fix_leaves_failing_is_held_out_with_why(js):
    await _documents()
    still = {"failures": [{"id": "L001", "reason": "still says more"}]}
    await run_seats({"Write_lines": LINES, "Write_letters": LETTERS, "Check_1": still,
                     "Fix": {"lines": [{"id": "L001", "text": "Led the risk working group across client engagements",
                                        "from": ["b-risk"]}]},
                     "Check_2": still})
    data = (await _newest()).data
    lines = {ln["id"]: ln for ln in data["lines"]}
    assert lines["L001"]["held"] and "still says more" in lines["L001"]["why_held"]
    # the achievement named a protected client and the fix never returned it: held for the code check
    assert lines["L003"]["held"] and "client rule" in lines["L003"]["why_held"]
    assert all(ln["id"] not in ("L001", "L003") for ln in story_library.usable_lines(data))


def test_the_code_checks_hold_a_line_to_the_owners_rules_and_the_librarys_standard():
    docs = _docs()
    data = {"rules": {"protected": ["Initech"], "approved": ["Built the practice knowledge base"], "roles": {}}}

    def hard(text, kind="bullet", cites=("b-risk",), role="northwind-lead"):
        return story_library.check_line({"id": "x", "kind": kind, "role": role, "text": text, "from": list(cites)},
                                        docs, data)[0]

    assert hard("Led the risk working group across client engagements for the practice") == []
    assert any("too thin" in h for h in hard("Led workshops."))
    assert hard("Built the practice knowledge base", cites=("b-kb",)) == []  # an approved wording may be short
    assert any("client rule" in h for h in hard("Led the risk working group across every Initech client engagement"))
    assert any("first person" in h for h in hard("Led my risk working group across client engagements for years"))
    assert any("past-tense" in h for h in hard("Leading the risk working group across client engagements today"))
    assert any("the most is 18" in h for h in hard(" ".join(["Led"] + ["work"] * 20), kind="achievement", role=None))
    assert any("not a master line" in h for h in hard("Led the risk working group across client engagements",
                                                      cites=("zzz",)))
    assert any("names the employer" in h for h in hard("Ran risk at Northwind for the practice", kind="scope"))
    assert hard("Ran the practice's risk work across client engagements", kind="scope", cites=("northwind-lead",)) == []
    flags = story_library.check_line({"id": "x", "kind": "bullet", "role": "northwind-lead", "from": ["b-risk"],
                                      "text": "Led the risk working group across 40 client engagements"}, docs, data)[1]
    assert any("40" in f for f in flags)  # a number the master lacks is a flag for the facts read, not a refusal


def test_a_block_whose_sentences_mostly_open_with_i_fails_on_voice():
    block = {"id": "b", "sentences": [{"text": "I led the group.", "from": ["b-risk"]},
                                      {"text": "I built the base.", "from": ["b-kb"]},
                                      {"text": "The board used it.", "from": ["e-board"]}]}
    hard, _ = story_library.check_block(block, _docs(), {})
    assert any(h.startswith("voice:") for h in hard)
    block["sentences"][1]["text"] = "The base followed."
    assert not any(h.startswith("voice:") for h in story_library.check_block(block, _docs(), {})[0])


def test_a_later_build_writes_again_only_what_the_master_changed():
    docs = _docs()
    before = {"sources": story_library.sources(docs.resume), "decided": ["b-risk", "b-kb", "a-statewide", "e-board"],
              "lines": copy.deepcopy([
                  {"id": "S-northwind-lead", "kind": "scope", "role": "northwind-lead", "text": "Scope.",
                   "from": ["b-risk"]},
                  {"id": "L001", "kind": "bullet", "role": "northwind-lead", "entry": "b-risk", "text": "One.",
                   "from": ["b-risk"]},
                  {"id": "L002", "kind": "bullet", "role": "northwind-lead", "entry": "b-kb", "text": "Two.",
                   "from": ["b-kb"]}]),
              "stories": [{"id": "kb", "blocks": [{"id": "kb-a", "sentences": [{"text": "x", "from": ["b-kb"]}]}]},
                          {"id": "risk", "blocks": [{"id": "risk-a",
                                                     "sentences": [{"text": "x", "from": ["b-risk"]}]}]}]}
    resume = copy.deepcopy(RESUME)
    resume["roles"][0]["bullets"][1]["text"] = "Built and ran the practice knowledge base"
    now = Documents.model_validate({"resume": resume})
    todo = story_library.plan(now, before, rules_changed=False)
    assert todo["entries"] == ["b-kb"]
    assert [ln["id"] for ln in todo["lines"]] == ["S-northwind-lead", "L001"]  # the role's printed facts held
    assert todo["stories"] == ["kb"] and [s["id"] for s in todo["kept_stories"]] == ["risk"]
    assert story_library.plan(docs, before, rules_changed=False)["entries"] == []


async def test_a_build_with_nothing_changed_but_the_rules_reads_the_rules_and_checks_every_line_again(js):
    await _documents()
    await run_seats({"Write_lines": LINES, "Write_letters": LETTERS,
                     "Check_1": {"failures": []}, "Fix": {"lines": []}, "Check_2": {"failures": []}})
    async with db.session_factory()() as session:
        writing = {**WRITING, "banned": [*WRITING["banned"], {"id": "w", "phrase": "working group", "why": "jargon"}]}
        await profile_store.save(session, OWNER, "writing", writing, by="test")
        await session.commit()
    done = await run_seats({"Write_lines": {**LINES, "scopes": [], "entries": []}, "Check_1": {"failures": []},
                            "Fix": {"lines": [], "blocks": []}, "Check_2": {"failures": []}})
    assert done[0] == "Write_lines" and "Write_letters" not in done
    data = (await _newest()).data
    held = {ln["id"] for ln in data["lines"] if ln.get("held")}
    assert "L001" in held  # "working group" is banned now: the line the rules newly break is held out
    assert any(b.get("held") for s in data["stories"] for b in s["blocks"])


async def test_the_owner_reads_the_library_by_role_with_its_stories(client):
    await _documents()
    await run_seats({"Write_lines": LINES, "Write_letters": LETTERS,
                     "Check_1": {"failures": []}, "Fix": {"lines": []}, "Check_2": {"failures": []}})
    got = (await client.get("/api/v1/documents/story-library")).json()
    assert got["current"] is True and got["building"] is False and got["version"] == 1
    role = got["roles"][0]
    assert role["scope"]["text"].startswith("Ran the practice") and len(role["lines"]) == 2
    assert got["stories"][0]["blocks"][0]["text"].startswith("The working group met")
    assert got["achievements"][0]["held"] is True  # the protected client name


async def test_a_part_that_fails_every_try_closes_the_build_and_its_other_parts_and_the_last_library_stays(js):
    await _documents()
    await run_seats({"Write_lines": "no json here", "Write_letters": LETTERS})
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(StoryLibrary).where(StoryLibrary.owner == OWNER))).all()
        assert [r.status for r in rows] == ["failed"]
        assert await story_library.usable(session, OWNER) is None
        build = await session.get(ProcInstance, rows[0].instance_id)
        assert build.status == "cancelled"
        parts = (await session.scalars(select(ProcInstance).where(ProcInstance.parent_id == build.id))).all()
        assert parts and all(p.status in ("completed", "cancelled") for p in parts)
        assert await seats.free(session, OWNER) == []  # no part of the closed build is handed out


async def test_the_lines_are_one_turn_on_the_whole_master_beside_the_letters(js):
    await _documents()
    async with db.session_factory()() as session:
        texts = [(await seats.take(session, OWNER, f"s{n}"))["seat"]["system"] for n in range(2)]
        await session.commit()
    lines, letters = texts
    assert lines.startswith("# You write a candidate's resume lines") and "## The owner's rules, for code" in lines
    to_decide = lines.split("## ENTRIES TO DECIDE")[1].split("\n## ")[0]
    assert '"b-risk"' in to_decide and '"a-statewide"' in to_decide
    assert letters.startswith("# You write a candidate's letter blocks")


def test_the_parts_of_a_build_follow_what_it_writes():
    docs = _docs()
    first = story_library.plan(docs, None, rules_changed=True)
    assert story_library.plan_parts(docs, first) == [{"kind": "lines"}, {"kind": "letters"}]
    rules_only = {"entries": [], "scopes": [], "stories": [], "new_material": [], "rules_changed": True}
    assert story_library.plan_parts(docs, rules_only) == [{"kind": "lines"}]


def test_a_figure_the_master_hedges_keeps_its_hedge_on_every_line_and_block():
    resume = copy.deepcopy(RESUME)
    resume["roles"][0]["bullets"][1]["text"] = ("Built the practice knowledge base, cutting ramp-up from about six "
                                                "months to about two")
    docs = Documents.model_validate({"resume": resume, "about": ABOUT, "writing": WRITING})

    def hard(text):
        return story_library.check_line({"id": "x", "kind": "bullet", "role": "northwind-lead", "text": text,
                                         "from": ["b-kb"]}, docs, {})[0]

    assert any("'six months'" in h and "hedge" in h for h in hard(
        "Built the practice knowledge base, cutting new-hire ramp-up from six months to about two"))
    assert any("'two'" in h for h in hard("Built the practice knowledge base, cutting ramp-up from about six months "
                                          "to two"))
    assert not any("hedge" in h for h in hard(
        "Built the practice knowledge base, cutting ramp-up from roughly six months to approximately two"))
    # a figure the master does not hedge is not held to one, nor the same number word counting something else
    assert story_library.dropped_hedges("Managed two analysts on a two-phase platform", "about two months") == []
    assert story_library.dropped_hedges("Led 30 deliverables", "about 30 deliverables, and 30 deliverables") == []
    assert story_library.dropped_hedges("a $2.2M program", "a roughly $2.2M program") == ["$2.2m program"]
    block = {"id": "b", "sentences": [{"text": "Ramp-up fell from six months to two.", "from": ["b-kb"]}]}
    assert any("hedge" in h for h in story_library.check_block(block, docs, {})[0])


def test_lines_of_one_accomplishment_share_a_claim():
    lines = [
        {"id": "L1", "kind": "bullet", "entry": "e-build", "claim": "practice-build", "from": ["e-build"]},
        # written for the same entry under another claim: still the same accomplishment
        {"id": "L2", "kind": "bullet", "entry": "e-build", "claim": "practice-standards", "from": ["e-build"]},
        # rests on the entry another line was written for: it tells part of that line's story
        {"id": "L3", "kind": "achievement", "entry": "a-team", "claim": "team", "from": ["a-team", "e-build"]},
        # the writer gave it the same claim as another line (an older library calls it "work")
        {"id": "L4", "kind": "bullet", "entry": "e-course", "claim": "course", "from": ["e-course"]},
        {"id": "L5", "kind": "bullet", "entry": "e-course-2", "work": "course", "from": ["e-course-2"]},
        {"id": "L6", "kind": "bullet", "entry": "e-forecast", "claim": "forecast", "from": ["e-forecast", "note"]},
        {"id": "S-x", "kind": "scope", "role": "x", "from": ["e-build"]},
    ]
    got = story_library.claims(lines)
    assert got["L1"] == got["L2"] == got["L3"]
    assert got["L4"] == got["L5"] != got["L1"]
    assert got["L6"] not in (got["L1"], got["L4"])
    assert "S-x" not in got
