"""The packet check with the slice-1 checks wired in: a certification the
master lacks, work written as delivered that did not ship, a line the master keeps out of writing and the owner's style
rules, each blocking and each naming its place in plain words (never a master id). Everything is made up."""

import copy
import inspect
import uuid
from dataclasses import dataclass, field

from app import db
from app.jobsearch.models import Packet
from app.jobsearch.profile import documents, integrity, never_write, style_rules
from app.jobsearch.profile import store as profile_store
from app.jobsearch.writing import packets
from tests.builders import (
    ABOUT,
    FACTS_CLEAN,
    LIBRARY,
    RESUME,
    WRITING,
    compose_reply,
    library_built,
    packet_setup,
    run_seats,
    write_packet,
)
from tests.support import OWNER

ROLE = "Senior Manager at Northwind Labs"
CERTS, SHIPPED, NEVER = (
    integrity.CERTIFICATIONS_CHECK, integrity.UNSHIPPED_CHECK, integrity.NEVER_IN_WRITING_CHECK)
STYLE = style_rules.PACKET_CHECK
NEW = [CERTS, SHIPPED, NEVER, STYLE]
TEXT = [never_write.PACKET_CHECK, *NEW]

#: The made-up master with one line marked never in writing, one with an em dash and a count of years, and a role
#: descriptor and a degree that the app prints as the master holds them (an em dash, and a year beside a degree).
MASTER = copy.deepcopy(RESUME)
MASTER["roles"][0]["descriptor"] = "an example consultancy — founded 2010 by an MBA"
MASTER["roles"][0]["bullets"] += [
    {"id": "b-secret", "text": "Quietly cut the board's review time by running a pilot first",
     "note": "Never in writing: the client asked"},
    {"id": "b-dash", "text": "Led the guild for 12 years — start to finish", "note": ""},
]
MASTER["education"][0]["degree"] = "MS in Leadership, 2014"
SECRET_WORDS = "Quietly cut the board's review time"


def docs(writing: dict | None = None, resume: dict | None = None) -> documents.Documents:
    return documents.Documents.model_validate({"resume": resume or MASTER, "writing": writing or {}})


GOOD_RESUME = {
    "header_location": "Oakfield, ST", "headline": "Programs Leader",
    "summary": "Builds programs that people use, across client engagements.", "summary_from": ["b-risk"],
    "competencies": [{"area": "Governance", "items": ["Risk Frameworks"]}],
    "roles": [{"role": "northwind-lead", "bullets": [
        {"text": "Led the risk working group across client engagements", "from": ["b-risk"]}]}],
    "education": ["ms"],
}


@dataclass
class Made:
    """A packet as the checks take it: the resume's cut, the letter's paragraphs and the lines the letter cites."""

    cut: packets.ResumeCut
    paragraphs: list[str] = field(
        default_factory=lambda: ["Dear hiring team,", "I would like to help you run programs."])
    letter_from: list[str] = field(default_factory=lambda: ["b-risk"])


def packet(**resume) -> Made:
    return Made(packets.ResumeCut.model_validate({**copy.deepcopy(GOOD_RESUME), **resume}))


def bullets(*cited: tuple[str, list[str]]) -> dict:
    return {"roles": [{"role": "northwind-lead", "bullets": [{"text": t, "from": f} for t, f in cited]}]}


def run(sub: Made, documents_=None) -> dict[str, dict]:
    checks = packets.packet_checks(sub.cut, sub.paragraphs, sub.letter_from, documents_ or docs())
    return {c["check"]: c for c in checks}


# --- each new check, refused with its name -------------------------------------------------------------------------


def test_a_packet_clean_of_all_four_says_so_in_its_checks_and_passes():
    got = run(packet())
    assert [got[n]["ok"] for n in NEW] == [True] * 4
    assert got[CERTS]["detail"] == "no certification or licence is named that the master doesn't hold"
    assert got[SHIPPED]["detail"] == "no work that didn't ship is written as delivered"
    assert got[NEVER]["detail"] == "no line the master keeps out of writing is used"
    assert got[STYLE]["detail"] == "none of the style rules you have on was broken"


def test_a_certification_the_master_lacks_fails_its_check_with_the_credential_and_the_place():
    sub = packet(**bullets(("A Project Management Professional who led the guild", ["b-risk"])))
    got = run(sub)[CERTS]
    assert got["ok"] is False and got["blocking"] is True
    assert got["detail"] == (f"bullet 1 under {ROLE}: 'Project Management Professional' "
                             f"(a certification or licence the master doesn't hold)")
    held = copy.deepcopy(MASTER)
    held["roles"][0]["bullets"][0]["text"] = "Earned the PMP in 2018 and led the risk working group"
    assert run(sub, docs(resume=held))[CERTS]["ok"] is True, "a credential the master names is allowed"


def test_the_owners_own_certification_names_are_checked_beside_the_shipped_list():
    sub = packet(summary="A Wine Sommelier Level 3 who leads programs.")
    assert run(sub)[CERTS]["ok"] is True
    writing = {"credentials": [{"id": "wine", "name": "Wine Sommelier Level 3"}]}
    got = run(sub, docs(writing))[CERTS]
    assert got["ok"] is False and got["detail"].startswith("summary: 'Wine Sommelier Level 3'")


def test_work_written_as_delivered_that_did_not_ship_fails_its_check():
    sub = packet(**bullets(("Designed a proposed framework for the board", ["b-risk"])))
    got = run(sub)[SHIPPED]
    assert got["ok"] is False and got["blocking"] is True
    assert got["detail"] == (f"bullet 1 under {ROLE}: 'Designed a proposed' (work that didn't ship, written as "
                             f"delivered)")
    assert run(packet(**bullets(("Launched a pilot for the board", ["b-risk"]))))[SHIPPED]["ok"] is True


def test_a_citation_of_a_line_marked_never_in_writing_fails_its_check_naming_the_line_and_the_place():
    sub = packet(**bullets(("Led the board's work on a quiet pilot", ["b-secret"])))
    got = run(sub)[NEVER]
    assert got["ok"] is False and got["blocking"] is True
    assert got["detail"] == (f"bullet 1 under {ROLE}: cites a line the master marks never in writing "
                             f"({MASTER['roles'][0]['bullets'][2]['text']!r})")
    assert "b-secret" not in got["detail"]


def test_the_summary_the_achievements_and_the_letter_citing_a_marked_line_fail_it_too():
    assert run(packet(summary="Builds programs.", summary_from=["b-secret"]))[NEVER]["detail"].startswith(
        "summary: cites a line")
    sub = packet(achievements=[{"text": "Led programs", "from": ["b-secret"]}])
    assert run(sub)[NEVER]["detail"].startswith("achievement 1: cites a line")
    letter = run(Made(packet().cut, ["I would like to help."], ["b-secret"]))
    assert letter[NEVER]["detail"].startswith("the letter: cites a line")


def test_printing_five_words_of_a_marked_line_fails_it_and_a_marked_role_or_degree_included_fails_it():
    sub = packet(**bullets((f"We {SECRET_WORDS} by running a pilot", ["b-risk"])))
    assert run(sub)[NEVER]["detail"].startswith(f"bullet 1 under {ROLE}: prints words of a line the master marks")
    marked = copy.deepcopy(MASTER)
    marked["roles"][0]["note"] = "never in writing"
    marked["education"][0]["note"] = "never-in-writing"
    got = run(packet(), docs(resume=marked))[NEVER]
    assert got["ok"] is False
    assert f"includes '{ROLE}', which the master marks never in writing" in got["detail"]
    assert "includes 'MS in Leadership, 2014, Example University', which the master marks" in got["detail"]


def test_a_style_breach_fails_its_check_with_the_rule_and_the_place_and_passes_with_that_rule_off():
    cases = {
        "no-em-dash": ("Led the guild - start to finish", "No em dash: bullet 1 under " + ROLE + ": ' - '"),
        "no-inline-markup": ("**Programs:** led the guild", "No markup: bullet 1 under " + ROLE + ": '**Programs:**'"),
        "no-pre-conceding": ("Although I lack a degree, I led the guild",
                             "No phrase that concedes a gap first: bullet 1 under " + ROLE + ": 'Although I lack'"),
        "no-weak-words": ("Responsible for the guild", "No weak or inflated words: bullet 1 under " + ROLE
                          + ": 'Responsible for'"),
        "no-year-by-degree": ("Earned an MBA, 2010, and led the guild",
                              "No year beside a degree: bullet 1 under " + ROLE + ": 'MBA, 2010'"),
    }
    for rule_id, (text, said) in cases.items():
        sub = packet(**bullets((text, ["b-risk"])))
        got = run(sub)[STYLE]
        assert got["ok"] is False and got["blocking"] is True, rule_id
        assert got["detail"] == said, rule_id
        assert run(sub, docs({"style_off": [rule_id]}))[STYLE]["ok"] is True, f"{rule_id} off passes it"


def test_with_one_rule_off_the_others_still_fail_and_with_all_off_the_row_says_so():
    text = "Responsible for the guild — and its **members**"
    sub = packet(**bullets((text, ["b-risk"])))
    got = run(sub, docs({"style_off": ["no-em-dash"]}))[STYLE]
    assert got["ok"] is False and "No markup" in got["detail"] and "No weak" in got["detail"]
    assert "No em dash" not in got["detail"]
    everything_off = run(sub, docs({"style_off": [r.id for r in style_rules.STYLE_RULES]}))[STYLE]
    assert everything_off["ok"] is True and everything_off["detail"] == "every style rule is switched off"


def test_every_new_check_is_blocking_and_the_row_names_are_exact():
    got = run(packet())
    assert NEW == ["Certifications are the master's", "Unshipped work isn't called delivered",
                   "Lines marked never in writing stay out", "Your style rules"]
    assert all(got[n]["blocking"] is True for n in NEW)


# --- the owner's own phrases and the copied master line -------------------------------------------------------------


def test_the_owners_phrase_reports_once_in_never_write_with_their_reason_and_not_again_as_a_style_rule():
    sub = packet(**bullets(("Spearheaded the guild", ["b-risk"])))
    got = run(sub, docs(WRITING))
    assert got[never_write.PACKET_CHECK]["ok"] is False
    assert got[never_write.PACKET_CHECK]["detail"] == f"bullet 1 under {ROLE}: 'Spearheaded' (puffery)"
    assert got[STYLE]["ok"] is True, "the starter rule leaves a phrase the owner bans to the owner's rule"


def test_a_claim_that_is_a_cited_master_line_copied_whole_is_not_a_style_breach():
    copied = MASTER["roles"][0]["bullets"][3]["text"]
    sub = packet(**bullets((copied, ["b-dash"])))
    got = run(sub)
    assert got[STYLE]["ok"] is True, "the master is never edited to satisfy a rule"
    # The owner's Never write and the built-in kinds still read it: "12 years" is a built-in check.
    assert got[never_write.PACKET_CHECK]["ok"] is False
    assert "A count of years of experience" in got[never_write.PACKET_CHECK]["detail"]


def test_the_exemption_needs_the_whole_text_and_the_line_to_be_cited():
    copied = MASTER["roles"][0]["bullets"][3]["text"]
    assert run(packet(**bullets((copied + " and more", ["b-dash"]))))[STYLE]["ok"] is False, "not the whole text"
    assert run(packet(**bullets((copied, ["b-risk"]))))[STYLE]["ok"] is False, "the line is not the one cited"
    spaced = copied.replace(" ", "  ")
    assert run(packet(**bullets((spaced, ["b-dash"]))))[STYLE]["ok"] is True, "a run of space is the same words"
    two = bullets((copied, ["b-dash"]), (copied, ["b-risk"]))
    got = run(packet(**two))[STYLE]["detail"]
    assert "bullet 2 under" in got and "bullet 1 under" not in got, "each claim is judged by its own citation"


def test_the_summary_an_achievement_and_a_letter_paragraph_copied_from_a_cited_line_are_exempt_too():
    marked = copy.deepcopy(MASTER)
    marked["achievements"] = [{"id": "a-dash", "text": "Led it — well", "note": ""}]
    marked["summary_variants"] = [{"id": "sv-dash", "text": "Runs it — well", "note": ""}]
    sub = packet(summary="Runs it — well", summary_from=["sv-dash"],
                 achievements=[{"text": "Led it — well", "from": ["a-dash"]}])
    assert run(sub, docs(resume=marked))[STYLE]["ok"] is True
    got = run(Made(sub.cut, ["Led it — well", "Thanks — truly"], ["a-dash"]), docs(resume=marked))[STYLE]
    assert got["ok"] is False and "letter paragraph 2" in got["detail"] and "letter paragraph 1" not in got["detail"]


def test_the_checks_read_only_the_writers_own_words_never_what_the_app_prints_from_the_master():
    # The master's descriptor has an em dash and a year beside a degree, and so has its degree line; the app prints
    # both as the master holds them, and the writer's own words are clean.
    sub = packet(education=["ms"])
    got = run(sub)
    assert got[STYLE]["ok"] is True and got[NEVER]["ok"] is True and got[CERTS]["ok"] is True


# --- plain places, no ids ------------------------------------------------------------------------------------------


def test_no_detail_of_any_check_holds_a_role_id_and_each_says_title_and_employer():
    sub = packet(
        headline="Programs Leader - at scale", summary="Responsible for a PMP guild.", summary_from=["b-risk"],
        competencies=[{"area": "Governance", "items": ["Multiple Risk Frameworks"]}],
        achievements=[{"text": "Launched a draft policy", "from": []},
                      {"text": "Led a statewide program", "from": ["a-statewide"]}],
        roles=[{"role": "northwind-lead", "intro": "Runs the **practice** for 12 years",
                "bullets": [
                    {"text": "Leveraged " + "word " * 50, "from": ["b-invented"]},
                    {"text": "A bullet with no source", "from": []},
                    {"text": "Quietly cut the board's review time and more", "from": ["b-secret"]},
                    {"text": "Designed a proposed plan", "from": ["b-risk"]}]}])
    checks = packets.packet_checks(sub.cut, sub.paragraphs, sub.letter_from, docs(WRITING))
    assert len(checks) >= 9
    for c in checks:
        assert "northwind-lead" not in c["detail"], c
    said = " | ".join(c["detail"] for c in checks)
    assert f"bullet 1 under {ROLE}" in said and f"bullet 2 under {ROLE}" in said and f"scope line under {ROLE}" in said
    assert "achievement 1" in said and "headline" in said and "summary" in said


def test_the_places_of_the_older_checks_name_the_role_by_its_words_and_keep_their_own_words():
    sub = packet(**bullets(("Did one thing", ["b-invented"]), ("Did more", [])))
    got = run(sub)
    detail = got["Every claim cites the master"]["detail"]
    assert f"bullet 2 under {ROLE} cites no master line" in detail
    assert f"bullet 1 under {ROLE} cites 'b-invented', which the master doesn't have" in detail
    long = packet(**bullets(("word " * 60, ["b-risk"])))
    assert run(long)["Bullets are short"]["detail"] == f"bullet 1 under {ROLE} is 60 words"
    assert run(long)["Bullets are short"]["blocking"] is False


def test_the_words_bullet_one_and_achievement_and_the_summary_stay_in_the_details():
    sub = packet(summary="Leveraging programs.", summary_from=[],
                 achievements=[{"text": "Leveraged it", "from": []}, {"text": "Led programs", "from": []}])
    cites = run(sub)["Every claim cites the master"]["detail"]
    assert "the summary cites no master line" in cites
    assert "achievement 1 cites no master line" in cites and "achievement 2 cites no master line" in cites
    banned = run(sub, docs({"banned": [{"id": "lev", "phrase": "leverage", "why": "jargon"}]}))
    assert "summary: 'Leveraging' (jargon)" in banned[never_write.PACKET_CHECK]["detail"]
    assert "achievement 1: 'Leveraged' (jargon)" in banned[never_write.PACKET_CHECK]["detail"]
    bullet = run(packet(**bullets(("Leveraged it", ["b-risk"]))), docs({"banned": [{"id": "l", "phrase": "leverage"}]}))
    assert f"bullet 1 under {ROLE}: 'Leveraged'" in bullet[never_write.PACKET_CHECK]["detail"]


def test_a_role_the_master_does_not_have_is_named_as_the_writer_wrote_it():
    ghost = {"roles": [{"role": "ghost", "bullets": [{"text": "Led things - badly", "from": ["b-risk"]}]}]}
    got = run(packet(**ghost))
    assert got["Roles and education are the master's"]["detail"] == "role 'ghost' isn't in the master"
    assert "bullet 1 under role 'ghost'" in got[STYLE]["detail"]


def test_a_scope_line_and_the_headline_and_a_competency_are_read_with_their_own_places():
    bullet = {"text": "Led the risk working group across client engagements", "from": ["b-risk"]}
    sub = packet(headline="Leader — Programs", competencies=[{"area": "Risk", "items": ["Various Tools"]}],
                 roles=[{"role": "northwind-lead", "intro": "Runs it - and more", "bullets": [bullet]}])
    got = run(sub)[STYLE]["detail"]
    assert "No em dash: headline: '—'" in got
    assert "No weak or inflated words: competencies: Risk: 'Various'" in got
    assert f"No em dash: scope line under {ROLE}: ' - '" in got


# --- the text part, and who calls it ------------------------------------------------------------------------------


def test_text_checks_is_the_text_part_of_packet_checks_and_takes_only_the_packet_and_the_documents():
    assert list(inspect.signature(packets.text_checks).parameters) == ["cut", "paragraphs", "letter_from", "docs"]
    sub = packet(**bullets(("Responsible for a PMP guild - quietly", ["b-risk"])))
    documents_ = docs(WRITING)
    checks = packets.packet_checks(sub.cut, sub.paragraphs, sub.letter_from, documents_)
    text = packets.text_checks(sub.cut, sub.paragraphs, sub.letter_from, documents_)
    assert [c["check"] for c in text] == TEXT
    assert [c for c in checks if c["check"] in TEXT] == text
    names = [c["check"] for c in checks]
    start = names.index(TEXT[0])
    assert names[start:start + 5] == TEXT, "the text checks sit together, after the roles check and before the letter"
    assert names[:start] == ["Every claim cites the master", "Roles and education are the master's"]


def test_text_checks_alone_reads_a_corrected_packet_without_the_other_checks():
    sub = packet(**bullets(("A **bold** thing", ["b-risk"])))
    text = packets.text_checks(sub.cut, sub.paragraphs, sub.letter_from, docs())
    assert [c["check"] for c in text if not c["ok"]] == [STYLE]


# --- through the writer ---------------------------------------------------------------------------------------------

FAILING = {"claims": [], "failures": []}
RULING = {"rulings": [{"id": "F1", "real": True, "status": "open"}], "fix_list": []}
#: The library of the made-up master: a line that cites the line the master marks never in writing, beside the others.
SECRET_LIBRARY = copy.deepcopy(LIBRARY) | {"sources": {}}
SECRET_LIBRARY["lines"].append({"id": "L004", "kind": "bullet", "role": "northwind-lead", "entry": "b-secret",
                                "work": "pilot", "side": "client", "for": "programs",
                                "text": "Cut the board's review time by running a pilot first.", "from": ["b-secret"]})


async def _with_master(writing: dict | None = None, library: dict | None = None):
    """A role to write for, under the master that marks one line never in writing."""
    role_id = await packet_setup(with_resume=False)
    async with db.session_factory()() as session:
        for kind, data in {"resume": MASTER, "about": ABOUT, "writing": writing or WRITING}.items():
            await profile_store.save(session, OWNER, kind, data, by="test")
        await _master_read(session)
        await library_built(session, data=library)
        await session.commit()
    return role_id


async def _master_read(session) -> None:
    from tests.builders import _master_read as read

    await read(session)


async def _start(role_id) -> str:
    async with db.session_factory()() as session:
        started = await packets.start(session, OWNER, role_id, "")
        await session.commit()
        return str(started.id)


async def _held(packet_id: str, **replies) -> Packet:
    """Write the packet with these composer replies, the gate failing and nothing fixed; returns it as filed."""
    reply = {"Compose": replies["compose"], "Facts_1": FAILING,
             "Fix": {"resume": {}}, "Facts_2": FAILING, "Arbitrate": RULING}
    await run_seats(reply)
    async with db.session_factory()() as session:
        return await session.get(Packet, uuid.UUID(packet_id))


async def test_a_packet_naming_a_certification_the_master_lacks_is_held_with_the_check_named(js):
    packet_id = await _start(await _with_master())
    said = "A Project Management Professional who builds programs."
    held = await _held(packet_id, compose=compose_reply(summary=said, summary_from=["L001"]))
    failed = {c["check"]: c for c in held.checks if c["blocking"] and not c["ok"]}
    assert held.status == "held" and CERTS in failed
    assert "summary: 'Project Management Professional'" in failed[CERTS]["detail"]


async def test_a_packet_citing_a_line_marked_never_in_writing_is_held(js):
    packet_id = await _start(await _with_master(library=SECRET_LIBRARY))
    held = await _held(packet_id, compose=compose_reply(roles=[{"role": "northwind-lead", "intro": "",
                                                                "lines": ["L001", "L004"]}]))
    failed = {c["check"]: c for c in held.checks if c["blocking"] and not c["ok"]}
    assert held.status == "held" and NEVER in failed
    assert "b-secret" not in failed[NEVER]["detail"] and ROLE in failed[NEVER]["detail"]


async def test_a_style_breach_is_found_in_the_filed_packets_checks(js):
    packet_id = await _start(await _with_master())
    held = await _held(packet_id, compose=compose_reply(headline="Leader — Programs"))
    failed = {c["check"]: c for c in held.checks if c["blocking"] and not c["ok"]}
    assert held.status == "held" and STYLE in failed and "No em dash" in failed[STYLE]["detail"]


async def test_a_packet_that_breaks_a_style_rule_the_owner_switched_off_is_filed_ready(js):
    packet_id = await _start(await _with_master({**WRITING, "style_off": ["no-em-dash"]}))
    await write_packet(Compose=compose_reply(headline="Leader — Programs"), Facts_1=FACTS_CLEAN)
    async with db.session_factory()() as session:
        stored = await session.get(Packet, uuid.UUID(packet_id))
    assert stored.status == "ready"


async def test_a_clean_packet_is_filed_ready_with_the_four_new_rows_passing(js):
    packet_id = await _start(await _with_master())
    await write_packet()
    async with db.session_factory()() as session:
        stored = await session.get(Packet, uuid.UUID(packet_id))
    assert stored.status == "ready"
    rows = {c["check"]: c for c in stored.checks}
    assert all(rows[n]["ok"] and rows[n]["blocking"] for n in NEW)


# --- the letter's length, from the owner's own letter rules --------------------------------------------------------

def _letter_of(words: int) -> Made:
    sub = packet()
    sub.paragraphs = [" ".join(["word"] * words)]
    return sub


def _length_docs(*says: str) -> documents.Documents:
    return docs(writing={"letter_global": [{"id": f"r{i}", "says": s} for i, s in enumerate(says)]})


def test_the_letter_length_check_reads_the_owners_range_and_refuses_a_letter_outside_it():
    d = _length_docs("The letter must be about 250 to 400 words long. Verify: the count is about 250 to 400.")
    assert run(_letter_of(300), d)["The letter's length"]["ok"]
    for n in (200, 450):
        row = run(_letter_of(n), d)["The letter's length"]
        assert not row["ok"] and row["blocking"] and f"{n} words" in row["detail"] and "250 to 400" in row["detail"]


def test_the_letter_length_check_reads_a_ceiling_and_a_floor_in_the_owners_words():
    d = _length_docs("Never more than 350 words.", "At least 100 words.")
    assert run(_letter_of(200), d)["The letter's length"]["ok"]
    assert not run(_letter_of(351), d)["The letter's length"]["ok"]
    assert not run(_letter_of(99), d)["The letter's length"]["ok"]


def test_no_letter_length_check_when_the_owner_sets_no_number():
    assert "The letter's length" not in run(_letter_of(900), _length_docs("Keep the letter short."))
    assert "The letter's length" not in run(_letter_of(900))
