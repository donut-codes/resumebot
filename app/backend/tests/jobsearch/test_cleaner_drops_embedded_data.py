"""Embedded data is not posting text (`postings/cleanse.py`, `drop_embedded`). A posting that
carries a picture as a `data:` address (base64 bytes in the text itself) kept the bytes as posting text; the cleansed
copy was then mostly one unbroken run, too big for a rank batch (the role was never ranked) or sent to agents as if it
were words. The cleaner drops the `data:` address and the image or link wrapper that only carried it, says so in
`removed`, and decides on what the run is, never on its length. Every name, sentence and byte here is made up."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.models import JobPosting, Opportunity, ParseJob
from app.jobsearch.postings import cleanse, cleansed, raw_copies
from app.jobsearch.research import rank
from app.settings import settings
from tests.builders import MINUTE, parse_runner
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

BEFORE = ["Programs Lead", "", "About the job",
          "Lead the programs team and run the quarterly review cycle for the whole office."]
AFTER = ["Own the yearly budget and report to the head of operations.",
         "Work with every team on its plan for the year and coach the managers.",
         "Keep the vendor contracts current and present results to the leadership group each quarter.",
         "You have ten years in operations or programs."]
PICTURE = "data:image/png;base64,iVBORw0KGgo" + "AAAAB3NzaC1yc2E/+" * 40 + "=="


def _clean(lines: list[str]) -> cleanse.Cleansed:
    return cleanse.cleanse({"title": "Programs Lead", "company": "Contoso", "text": "\n".join(lines)})


def _gone(chars: int, lines: int = 1) -> list[dict]:
    return [{"rule": "embedded_data", "lines": lines, "chars": chars}]


@pytest.mark.parametrize("tag", [
    f'<img src="{PICTURE}">',
    f'<img alt="Our office" src="{PICTURE}" width="600" />',
    f"<img src='{PICTURE}'/>",
    f'<source srcset="{PICTURE}">',
], ids=["plain", "among other attributes", "single quotes", "source"])
def test_an_image_tag_whose_source_is_a_data_address_is_gone_and_the_words_around_it_stay_in_order(tag):
    got = _clean([*BEFORE, "", tag, "", *AFTER])
    assert got.text_clean == "\n".join([*BEFORE, "", *AFTER])
    assert got.removed == _gone(len(tag))


def test_an_image_tag_inside_a_line_leaves_the_words_of_the_line():
    got = _clean([*BEFORE, f'Our office: <img src="{PICTURE}"> is near the station.', *AFTER])
    assert got.text_clean == "\n".join([*BEFORE, "Our office:  is near the station.", *AFTER])
    assert got.removed == _gone(len(f'<img src="{PICTURE}">'))


def test_an_image_tag_the_text_ends_inside_is_gone_to_the_end():
    """The shape of a text cut by its site: the tag opens, the bytes run, and nothing closes it."""
    cut = f'<img src="{PICTURE}'
    got = _clean([*BEFORE, *AFTER, cut])
    assert got.text_clean == "\n".join([*BEFORE, *AFTER])
    assert got.removed == _gone(len(cut))


def test_a_bare_data_address_in_the_text_is_gone_and_the_words_around_it_stay_in_order():
    got = _clean([*BEFORE, f"See the chart {PICTURE} for the team.", *AFTER])
    assert got.text_clean == "\n".join([*BEFORE, "See the chart  for the team.", *AFTER])
    assert got.removed == _gone(len(PICTURE))


def test_a_data_address_in_square_brackets_on_a_line_of_its_own_is_gone_with_its_brackets():
    """How one job system writes a picture in its plain text: the address alone in brackets, as a paragraph."""
    got = _clean([*BEFORE, "", f"[{PICTURE}]", "", *AFTER, "", f"[{PICTURE}]"])
    assert got.text_clean == "\n".join([*BEFORE, "", *AFTER])
    assert got.removed == _gone(2 * (len(PICTURE) + 2), lines=2)


def test_a_markdown_image_is_gone_and_a_markdown_link_keeps_its_words():
    image, link = f"![Our office]({PICTURE})", f"[the team chart]({PICTURE})"
    got = _clean([*BEFORE, image, f"Read {link} first.", *AFTER])
    assert got.text_clean == "\n".join([*BEFORE, "", "Read the team chart first.", *AFTER])
    assert got.removed == _gone(len(image) + len(link) - len("the team chart"), lines=2)


def test_a_data_address_that_is_not_base64_is_gone_too():
    drawing = "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%2F%3E"
    got = _clean([*BEFORE, f'<img src="{drawing}">', *AFTER])
    assert got.text_clean == "\n".join([*BEFORE, "", *AFTER]), "the line it stood on is a blank line"
    assert got.removed == _gone(len(f'<img src="{drawing}">'))


def test_a_data_address_written_in_capitals_is_gone_too():
    """A scheme is the same in any case (RFC 3986): `DATA:` and `Data:` name the same embedded bytes."""
    shout, mixed = PICTURE.replace("data:image/png;base64", "DATA:IMAGE/PNG;BASE64"), "Data" + PICTURE[4:]
    tag = f'<img src="{shout}">'
    got = _clean([*BEFORE, tag, f"[{mixed}]", *AFTER, "METADATA:image/png;base64,AAAA is a field name in our files."])
    assert got.text_clean == "\n".join([*BEFORE, "", *AFTER,
                                        "METADATA:image/png;base64,AAAA is a field name in our files."])
    assert got.removed == _gone(len(tag) + len(mixed) + 2, lines=2)


def test_embedded_data_is_named_before_the_boilerplate_rules_that_ran_after_it():
    eeo = "We are an equal opportunity employer and do not discriminate on any basis at all."
    got = _clean([*BEFORE, f"[{PICTURE}]", *AFTER, eeo])
    assert got.text_clean == "\n".join([*BEFORE, "", *AFTER])
    assert got.removed == [*_gone(len(PICTURE) + 2), {"rule": "equal_opportunity", "lines": 1, "chars": len(eeo)}]


def test_a_long_ordinary_web_address_is_unchanged_byte_for_byte():
    address = ("https://careers.contoso.example/jobs/programs-lead?" + "&".join(
        f"utm_tag{i}=data{i}&token{i}=iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB{i}" for i in range(400)))
    assert len(address) > 20_000
    lines = [*BEFORE, f'<a href="{address}">Read about the team</a>', f"![Our office]({address})", address, *AFTER]
    got = _clean(lines)
    assert got.text_clean == "\n".join(lines) and got.removed == []


def test_words_about_data_are_unchanged_byte_for_byte():
    lines = [*BEFORE, "Formats of the data: image/png, text/csv and others.",
             "Metadata:image/png;base64,AAAA is a field name in our files.",
             "You keep the reporting data: accurate, current and complete.", *AFTER]
    got = _clean(lines)
    assert got.text_clean == "\n".join(lines) and got.removed == []


def test_a_long_real_description_is_unchanged_byte_for_byte():
    lines = [*BEFORE, *(f"Duty {i}: run program {i} for team {i} and report on it each quarter." for i in range(6000)),
             *AFTER]
    text = "\n".join(lines)
    assert len(text) > 350_000
    got = _clean(lines)
    assert got.text_clean == text and got.removed == []


# --- the ranker ---------------------------------------------------------------------------------------------------

NIGHT = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)


@pytest.fixture
async def agent(js, monkeypatch, scout_token):
    monkeypatch.setattr(settings, "owner", OWNER)
    await parse_runner(seen=MINUTE)


async def test_a_role_whose_posting_holds_a_big_embedded_image_is_queued_for_ranking(agent, monkeypatch):
    """Before, the copy held the picture's bytes, was over what one batch holds, and the role was refused (never
    ranked). The posting itself is short: it is queued whole, with none of the bytes."""
    monkeypatch.setattr(rank, "BATCH_CHARS", 3_000)
    big = "data:image/png;base64,iVBORw0KGgo" + "AAAAB3NzaC1yc2E/+" * 600
    text = "\n".join([*BEFORE, "", f"[{big}]", "", *AFTER, f'<img src="{big}">', "END OF POSTING."])
    assert len(text) > 20_000
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        role = Opportunity(owner=OWNER, slug="pictured", company="Globex", title="Programs Lead", fit=5,
                           origin="scout", match_key="pictured|programs lead", status="second_impression",
                           created_at=NIGHT, found=NIGHT.date())
        session.add(role)
        await session.flush()
        session.add(JobPosting(owner=OWNER, employer_id=role.id, ats=EXAMPLE_SYSTEM, ext_id="pictured",
                               company="Globex", title=role.title, location="Remote", text=text, first_seen=NIGHT,
                               last_seen=NIGHT, links=[], opportunity_id=role.id))
        await session.flush()
        await raw_copies.keep_legacy(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        assert await rank.queue(session, OWNER) == {"version": 1, "roles": 1, "jobs": 1}
        await session.commit()
    async with db.session_factory()() as session:
        (job,) = (await session.scalars(select(ParseJob).where(ParseJob.kind == "rank"))).all()
        (copy,) = (await cleansed.copies_by_role(session, OWNER)).values()
    assert [i["role_id"] for i in job.fields["items"]] == [str(role.id)]
    assert copy.text_clean == "\n".join([*BEFORE, "", *AFTER, "", "END OF POSTING."])
    assert copy.text_clean in job.text and "base64" not in job.text
