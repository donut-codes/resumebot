"""A new install's starter writing rules, what the checks page serves, and what the writer and a reviewer are told of
the checks. Everything is made up."""

import logging

from sqlalchemy import select

from app import db
from app.jobsearch.actors import BY_APP
from app.jobsearch.loops.startup import run_startup_fixups
from app.jobsearch.models import ProfileVersion
from app.jobsearch.profile import documents, integrity, never_write, style_rules
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.documents import Writing, rules_for
from app.jobsearch.writing import packets, seats
from tests.builders import packet_setup
from tests.support import OWNER, load_profile

QA = dict(documents.WRITING_SECTIONS)["qa"]
STARTER = [
    "Use one column.",
    "Use the standard section names: Professional Summary, Core Competencies, Professional Experience, Education.",
    "Put nothing in a table, a text box, a header or a footer.",
    "Hide nothing: no white text, no tiny text, no text off the page.",
]
STYLE = [
    ("no-em-dash", "No em dash"), ("no-inline-markup", "No markup"), ("no-year-by-degree", "No year beside a degree"),
    ("no-pre-conceding", "No phrase that concedes a gap first"), ("no-weak-words", "No weak or inflated words"),
]


async def _versions(owner: str = OWNER) -> list[ProfileVersion]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(ProfileVersion).where(
            ProfileVersion.owner == owner, ProfileVersion.kind == "writing").order_by(ProfileVersion.version))).all())


# --- the starter rules for ATS screening -----------------------------------------------------------------------------


def test_the_starter_rules_for_ats_screening_are_four_plain_sentences():
    assert [r.says for r in documents.STARTER_ATS] == STARTER
    assert len({r.id for r in documents.STARTER_ATS}) == 4


async def test_an_owner_with_no_writing_version_gets_version_one_with_the_four_rules_saved_by_the_app(js):
    async with db.session_factory()() as session:
        saved = await profile_store.ensure_starter_writing(session, OWNER)
        await session.commit()
    assert saved == 1
    [row] = await _versions()
    assert (row.version, row.saved_by, row.why) == (1, BY_APP, "starter rules")
    async with db.session_factory()() as session:
        writing = (await profile_store.owner_documents(session, OWNER)).writing
    assert [r.says for r in writing.ats_rules] == STARTER
    assert Writing().model_dump(exclude={"ats_rules"}) == writing.model_dump(exclude={"ats_rules"}), "nothing else"
    assert writing.style_off == [] and writing.credentials == []


async def test_a_second_call_changes_nothing_and_neither_does_one_after_the_owner_edits_the_rules(js):
    async with db.session_factory()() as session:
        await profile_store.ensure_starter_writing(session, OWNER)
        assert await profile_store.ensure_starter_writing(session, OWNER) is None
        await session.commit()
    assert len(await _versions()) == 1
    async with db.session_factory()() as session:  # the owner takes every starter rule out: that is theirs to do
        await profile_store.save(session, OWNER, "writing", {"ats_rules": []}, by="owner")
        assert await profile_store.ensure_starter_writing(session, OWNER) is None
        await session.commit()
        assert [r.says for r in (await profile_store.owner_documents(session, OWNER)).writing.ats_rules] == []
    assert [v.version for v in await _versions()] == [1, 2]


async def test_an_install_that_already_has_writing_rules_gets_nothing_added(js):
    mine = {"voice_rules": [{"id": "plain", "says": "Plain."}], "ats_rules": [{"id": "mine", "says": "My rule."}]}
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "writing", mine, by="owner")
        assert await profile_store.ensure_starter_writing(session, OWNER) is None
        await session.commit()
    [row] = await _versions()
    assert row.version == 1 and row.saved_by == "owner" and row.data["ats_rules"] == mine["ats_rules"]
    async with db.session_factory()() as session:  # an older version with no ATS rules at all is an install with rules
        await profile_store.save(session, OWNER, "writing", {}, by="owner")  # version 2: empty
        assert await profile_store.ensure_starter_writing(session, OWNER) is None
        await session.commit()
    assert [v.version for v in await _versions()] == [1, 2]


async def test_seeding_is_per_owner_and_a_resume_alone_does_not_count_as_writing_rules(js):
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "resume", {"summary": "Builds programs."}, by="owner")
        await profile_store.save(session, "another", "writing", {"voice_rules": []}, by="owner")
        assert await profile_store.ensure_starter_writing(session, OWNER) == 1
        await session.commit()
    assert len(await _versions(OWNER)) == 1 and len(await _versions("another")) == 1


async def test_the_app_start_seeds_the_starter_rules_once_and_a_second_start_changes_nothing(js, caplog):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await session.commit()
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            async with db.session_factory()() as session:
                await run_startup_fixups(session, OWNER)
                await session.commit()
    assert "start-up step" not in caplog.text, "no step failed"
    [row] = await _versions()
    assert (row.version, row.saved_by) == (1, BY_APP)
    assert [r["says"] for r in row.data["ats_rules"]] == STARTER


# --- what the checks page is served ----------------------------------------------------------------------------------


async def test_the_checks_page_gets_the_four_text_kinds_unchanged_and_the_five_style_rules(client):
    got = (await client.get("/api/v1/documents/built-in-checks")).json()
    assert set(got) == {"checks", "style"}
    assert got["checks"] == never_write.built_in_checks() and len(got["checks"]) == 4
    assert [(r["id"], r["name"]) for r in got["style"]] == STYLE
    assert got["style"] == style_rules.shipped()
    assert all(set(r) == {"id", "name", "rule"} and r["rule"] for r in got["style"])
    assert "certification" not in str(got).lower(), "the three integrity checks are not listed on the page"


# --- what the writer and the reviewer are told -----------------------------------------------------------------------


def test_the_qa_part_holds_always_checked_and_the_style_rules_that_are_on_beside_checked_automatically():
    qa = rules_for(Writing(), "review")[QA]
    assert list(qa)[-3:] == [documents.CHECKED_AUTOMATICALLY, "Always checked", "Style rules on"]
    assert [c["name"] for c in qa["Checked automatically"]] == [c.name for c in never_write.BUILT_IN]
    assert qa["Always checked"] == [{"name": c.name, "rule": c.rule} for c in integrity.ALWAYS_CHECKED]
    assert [c["name"] for c in qa["Always checked"]] == [
        "Certifications are the master's", "Unshipped work isn't called delivered",
        "Lines marked never in writing stay out"]
    assert qa["Style rules on"] == [{"name": r.name, "rule": style_rules.told(r)} for r in style_rules.STYLE_RULES]


def test_only_the_style_rules_that_are_on_are_listed_and_none_when_all_are_off():
    some = rules_for(Writing(style_off=["no-em-dash", "no-such-rule"]), "review")[QA]["Style rules on"]
    assert [r["name"] for r in some] == [name for rule_id, name in STYLE if rule_id != "no-em-dash"]
    assert rules_for(Writing(style_off=[i for i, _ in STYLE]), "review")[QA]["Style rules on"] == []


def test_a_job_that_does_not_review_is_not_handed_the_new_lists():
    given = rules_for(Writing(), "resume", "letter")
    assert QA not in given


async def test_the_composer_is_handed_the_checks_in_the_part_it_reads_for_review(js):
    role_id = await packet_setup()
    async with db.session_factory()() as session:
        await packets.start(session, OWNER, role_id, "")
        await session.commit()
    async with db.session_factory()() as session:
        taken = (await seats.take(session, OWNER, "s-1"))["seat"]
        await session.commit()
    for name in [c.name for c in integrity.ALWAYS_CHECKED] + [name for _id, name in STYLE]:
        assert name in taken["system"]
