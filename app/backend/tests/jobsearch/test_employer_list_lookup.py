"""The owner's employer list as a lookup (docs/PRD.md 28.3, the employer list sourcing spec): the owner
loads a file of employer names and websites (`import-employer-list`), and when the scout is handed an employer whose job
board the app does not know, the hand-over carries the list's website for it as a first clue, marked as the owner's and
unchecked. Nothing enters Employers and nothing is researched. Every name, address and file here is made up."""

import csv
import io
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import cli, stages
from app.jobsearch.doors import tools
from app.jobsearch.employers import employer_list as lists
from app.jobsearch.models import Employer, EmployerListEntry
from tests.employer_builders import make_employer, make_role
from tests.support import OWNER

SOURCE = "made-up-list.csv"


def table(*rows, header=("name", "website", "verdict")) -> str:
    """A file's text: the heading and these rows, written as a spreadsheet exports them."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def kept(*rows, header=("name", "website", "verdict")) -> list[lists.Entry]:
    return lists.read(table(*rows, header=header)).entries


async def _stored(session, owner: str = OWNER) -> list[tuple[str, str, str]]:
    rows = (await session.scalars(select(EmployerListEntry).where(EmployerListEntry.owner == owner))).all()
    return sorted((r.source, r.key, r.website) for r in rows)


async def _waiting_employer(session, name: str, **fields) -> Employer:
    """An employer with a role in play and no job system: the scout is handed it."""
    employer = await make_employer(session, name, **fields)
    session.add(make_role(employer, "Programs Lead", stages.SECOND_IMPRESSION))
    await session.flush()
    return employer


async def _handed_over(session) -> dict[str, dict]:
    return {e["company"]: e for e in (await tools.boards_reply(session, OWNER))["employers"]}


# --- reading the file -----------------------------------------------------------------------------------------------


def test_a_row_keeps_the_name_the_key_the_app_matches_by_and_a_website_written_as_an_address():
    [entry] = kept(("Acme, Inc.", "acme.example", "ok"))

    assert entry == lists.Entry(key="acme", name="Acme, Inc.", website="https://acme.example", verdict="ok")


def test_a_website_written_as_an_address_is_kept_as_written():
    [entry] = kept(("Acme", "https://www.acme.example/careers", ""))

    assert entry.website == "https://www.acme.example/careers" and entry.verdict is None


@pytest.mark.parametrize("verdict", ["drop?", "Drop", "probably drop"])
def test_a_row_whose_verdict_says_to_drop_is_left_out_and_counted(verdict):
    read = lists.read(table(("Acme", "acme.example", verdict), ("Fabrikam", "fabrikam.example", "ok")))

    assert [e.key for e in read.entries] == ["fabrikam"]
    assert (read.rows, read.dropped) == (2, 1)


@pytest.mark.parametrize("verdict", ["ok", "check", "", "maybe"])
def test_every_other_verdict_is_kept_and_a_file_with_no_verdict_column_keeps_every_row(verdict):
    assert len(kept(("Acme", "acme.example", verdict))) == 1
    assert len(kept(("Acme", "acme.example"), header=("name", "website"))) == 1


@pytest.mark.parametrize("header", [("Employer", "Domain"), ("COMPANY", "URL"), (" name ", "Website ")])
def test_the_columns_are_found_by_their_headings_in_any_case(header):
    [entry] = kept(("Acme", "acme.example"), header=header)

    assert (entry.key, entry.website) == ("acme", "https://acme.example")


@pytest.mark.parametrize("header", [("website", "verdict"), ("name", "verdict"), ("a", "b")])
def test_a_file_with_no_name_column_or_no_website_column_is_refused_in_words(header):
    with pytest.raises(lists.ListError) as refused:
        lists.read(table(("Acme", "acme.example"), header=header))

    assert "column" in str(refused.value)


def test_a_file_with_nothing_in_it_is_refused_in_words():
    with pytest.raises(lists.ListError):
        lists.read("")


# The cases are named because pytest keeps a test's id in an environment variable, which 200,000 characters overflow.
@pytest.mark.parametrize("text", [
    "name,website\nAcme," + "x" * 200_000 + "\n",
    "name,website," + "x" * 200_000 + "\nAcme,acme.example,\n",
], ids=["a cell over the csv field limit", "a heading over it"])
def test_a_file_the_csv_reader_cannot_read_is_refused_in_words(text):
    with pytest.raises(lists.ListError, match="CSV"):
        lists.read(text)


def test_a_row_with_no_name_that_makes_a_key_or_no_website_to_use_is_left_out_and_counted():
    read = lists.read(table(
        ("Inc.", "nameless.example", "ok"),  # nothing is left of the name once "Inc." is taken out
        ("Acme", "", "ok"),
        ("Fabrikam", "n/a", "ok"),
        ("Globex", "http://localhost/home", "ok"),  # not a public address
        ("Initech", "initech.example", "ok"),
    ))

    assert [e.key for e in read.entries] == ["initech"]
    assert (read.rows, read.dropped, read.unusable, read.repeats) == (5, 0, 4, 0)


def test_the_same_employer_and_website_twice_is_kept_once_and_the_same_employer_at_another_website_is_kept_too():
    read = lists.read(table(
        ("Acme", "acme.example", "check"),
        ("Acme Inc", "acme.example", "ok"),  # the same key and website
        ("Acme", "acme-other.example", "check"),
    ))

    assert sorted(e.website for e in read.entries) == ["https://acme-other.example", "https://acme.example"]
    assert read.repeats == 1


# --- keeping it -----------------------------------------------------------------------------------------------------


async def test_importing_keeps_each_row_for_the_owner_under_the_files_name(js):
    async with db.session_factory()() as session:
        stored = await lists.replace(session, OWNER, SOURCE, kept(("Acme", "acme.example", "ok"),
                                                                  ("Fabrikam", "fabrikam.example", "check")))
        await session.commit()
    async with db.session_factory()() as session:
        assert stored == 2
        assert await _stored(session) == [(SOURCE, "acme", "https://acme.example"),
                                          (SOURCE, "fabrikam", "https://fabrikam.example")]


async def test_importing_a_file_of_the_same_name_again_replaces_what_it_gave_and_leaves_another_file_alone(js):
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, "other.csv", kept(("Globex", "globex.example", "ok")))
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", "acme.example", "ok"),
                                                         ("Fabrikam", "fabrikam.example", "ok")))
        await session.commit()
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", "acme-new.example", "ok")))
        await session.commit()
    async with db.session_factory()() as session:
        assert await _stored(session) == [("made-up-list.csv", "acme", "https://acme-new.example"),
                                          ("other.csv", "globex", "https://globex.example")]


async def test_importing_the_same_file_twice_leaves_the_same_rows(js):
    entries = kept(("Acme", "acme.example", "ok"), ("Fabrikam", "fabrikam.example", "ok"))
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, SOURCE, entries)
        await session.commit()
    async with db.session_factory()() as session:
        before = await _stored(session)
        await lists.replace(session, OWNER, SOURCE, entries)
        await session.commit()
    async with db.session_factory()() as session:
        assert await _stored(session) == before


async def test_another_owners_rows_are_neither_replaced_nor_handed_to_this_owners_scout(js):
    async with db.session_factory()() as session:
        await lists.replace(session, "someone-else", SOURCE, kept(("Acme", "acme.example", "ok")))
        await lists.replace(session, OWNER, SOURCE, kept(("Globex", "globex.example", "ok")))
        await _waiting_employer(session, "Acme")
        await session.commit()
    async with db.session_factory()() as session:
        assert await _stored(session, "someone-else") == [(SOURCE, "acme", "https://acme.example")]
        assert "list_websites" not in (await _handed_over(session))["Acme"]


# --- what the scout is handed ---------------------------------------------------------------------------------------


async def test_an_employer_is_matched_by_its_key_and_by_the_key_of_any_alias(js):
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", "acme.example", "ok"),
                                                         ("Fabrikam Labs", "fabrikam.example", "ok")))
        by_key = await make_employer(session, "Acme Corporation")
        by_alias = await make_employer(session, "Fabrikam", aliases=["Fabrikam Labs, LLC"])
        none = await make_employer(session, "Globex")
        found = await lists.websites_for(session, OWNER, [by_key, by_alias, none])
        assert found == {by_key.id: ["https://acme.example"], by_alias.id: ["https://fabrikam.example"]}


async def test_an_employer_the_list_names_twice_gets_both_websites(js):
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", "acme-b.example", "check"),
                                                         ("Acme", "acme-a.example", "ok")))
        employer = await make_employer(session, "Acme")
        assert (await lists.websites_for(session, OWNER, [employer]))[employer.id] == [
            "https://acme-a.example", "https://acme-b.example"]


async def test_an_employer_the_list_names_many_times_gets_a_few_of_its_websites_in_the_steady_order(js):
    cap = lists.WEBSITES_PER_EMPLOYER
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, SOURCE, kept(
            *((f"Acme{' Inc' if i % 2 else ''}", f"acme-{i:02}.example", "ok") for i in reversed(range(cap + 3)))))
        employer = await make_employer(session, "Acme")
        found = (await lists.websites_for(session, OWNER, [employer]))[employer.id]

    assert found == [f"https://acme-{i:02}.example" for i in range(cap)]


async def test_a_very_long_address_is_no_clue_and_does_not_take_a_place_from_a_short_one(js):
    long_address = "https://acme.example/" + "a" * (lists.LONGEST_WEBSITE + 1)
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", long_address, "ok"),
                                                         ("Acme", "acme-short.example", "ok"),
                                                         ("Fabrikam", long_address, "ok")))
        acme, fabrikam = await make_employer(session, "Acme"), await make_employer(session, "Fabrikam")
        assert await lists.websites_for(session, OWNER, [acme, fabrikam]) == {acme.id: ["https://acme-short.example"]}


async def test_a_website_the_app_already_holds_for_the_employer_is_no_clue(js):
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", "www.acme.example", "ok"),
                                                         ("Fabrikam", "fabrikam.example", "ok")))
        same = await make_employer(session, "Acme", website="http://acme.example/about")
        other = await make_employer(session, "Fabrikam", website="https://fabrikam-group.example")
        assert await lists.websites_for(session, OWNER, [same, other]) == {other.id: ["https://fabrikam.example"]}


async def test_the_scouts_hand_over_carries_the_lists_websites_marked_as_the_owners_and_unchecked(js):
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", "acme.example", "ok")))
        await _waiting_employer(session, "Acme")
        reply = await tools.boards_reply(session, OWNER)

    [acme] = reply["employers"]
    assert acme["list_websites"] == ["https://acme.example"]
    note = reply["how"].lower()
    assert "list_websites" in note and "owner's own employer list" in note and "unchecked" in note
    assert "check" in note and "dead" in note and "wrong" in note


async def test_an_employer_with_no_entry_is_handed_over_as_it_always_was(js):
    async with db.session_factory()() as session:
        await _waiting_employer(session, "Globex")
        before = await tools.boards_reply(session, OWNER)
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", "acme.example", "ok")))
        after = await tools.boards_reply(session, OWNER)

    assert before == after
    assert "list_websites" not in before["employers"][0] and "list_websites" not in before["how"]


async def test_the_list_adds_no_employer_and_changes_nothing_of_one(js):
    async with db.session_factory()() as session:
        employer = await _waiting_employer(session, "Acme")
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", "acme.example", "ok"),
                                                         ("Fabrikam", "fabrikam.example", "ok")))
        await tools.boards_reply(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Employer)) == 1
        employer = await session.get(Employer, employer.id)
        assert (employer.website, employer.careers_url, employer.ats, employer.sources.get("website")) == (
            None, None, None, None)


# --- the numbers a dry run gives ------------------------------------------------------------------------------------


async def test_the_numbers_count_the_employers_in_the_list_the_rows_that_name_them_and_those_waiting_for_the_scout(js):
    entries = kept(("Acme", "acme.example", "ok"), ("Acme", "acme-b.example", "check"),
                   ("Fabrikam Labs", "fabrikam.example", "ok"), ("Nobody", "nobody.example", "ok"))
    async with db.session_factory()() as session:
        waiting = await _waiting_employer(session, "Acme")
        await make_employer(session, "Fabrikam", aliases=["Fabrikam Labs"])
        await make_employer(session, "Globex")
        mine = (await session.scalars(select(Employer).where(Employer.owner == OWNER))).all()

        found = lists.matches(entries, mine, {str(waiting.id)})

    assert (found.employers, found.of, found.rows, found.waiting) == (2, 3, 3, 1)


# --- the command ----------------------------------------------------------------------------------------------------


def _file(tmp_path, *rows, name: str = SOURCE, header=("name", "website", "verdict")):
    path = tmp_path / name
    path.write_text(table(*rows, header=header), encoding="utf-8")
    return path


def _args(path, dry_run: bool = False):
    return SimpleNamespace(csv=str(path), dry_run=dry_run)


async def test_a_dry_run_says_what_would_be_kept_and_matched_and_saves_nothing(js, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli.settings, "owner", OWNER)
    path = _file(tmp_path, ("Acme", "acme.example", "ok"), ("Fabrikam", "fabrikam.example", "drop?"),
                 ("Globex", "globex.example", "check"), ("Initech", "n/a", "ok"))
    async with db.session_factory()() as session:
        await _waiting_employer(session, "Acme")
        await make_employer(session, "Globex", sources={"board_asked": {"at": "2026-11-01T00:00:00+00:00"}})
        await session.commit()

    assert await cli._import_employer_list(_args(path, dry_run=True)) == 0

    out = capsys.readouterr().out
    assert "4 rows read" in out and "would keep 2" in out and "1 marked drop" in out and "1 with no usable" in out
    assert "2 of the app's 2 employers are in it" in out and "1 of the 1 that wait for the scout" in out
    assert "nothing saved" in out
    async with db.session_factory()() as session:
        assert await _stored(session) == []


async def test_the_command_keeps_the_rows_and_a_second_run_leaves_the_same_ones(js, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli.settings, "owner", OWNER)
    path = _file(tmp_path, ("Acme", "acme.example", "ok"), ("Fabrikam", "fabrikam.example", "check"))

    assert await cli._import_employer_list(_args(path)) == 0
    assert await cli._import_employer_list(_args(path)) == 0

    assert "kept 2" in capsys.readouterr().out
    async with db.session_factory()() as session:
        assert await _stored(session) == [(SOURCE, "acme", "https://acme.example"),
                                          (SOURCE, "fabrikam", "https://fabrikam.example")]


async def test_the_command_prints_counts_and_never_a_row_of_the_list(js, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli.settings, "owner", OWNER)
    path = _file(tmp_path, ("Zebulon Quarry", "zebulon-quarry.example", "ok"))
    async with db.session_factory()() as session:
        await make_employer(session, "Zebulon Quarry")
        await session.commit()

    await cli._import_employer_list(_args(path))

    out = capsys.readouterr()
    assert "zebulon" not in (out.out + out.err).lower()


@pytest.mark.parametrize("rows, why", [
    ([("Acme", "n/a", "ok")], "no usable"),
    ([("Acme", "acme.example", "drop?")], "no usable"),
    ([], "no usable"),
])
async def test_a_file_with_no_row_to_keep_is_refused_and_leaves_what_an_earlier_one_gave(
        js, monkeypatch, capsys, tmp_path, rows, why):
    monkeypatch.setattr(cli.settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await lists.replace(session, OWNER, SOURCE, kept(("Acme", "acme.example", "ok")))
        await session.commit()

    assert await cli._import_employer_list(_args(_file(tmp_path, *rows))) == 1

    assert why in capsys.readouterr().err
    async with db.session_factory()() as session:
        assert await _stored(session) == [(SOURCE, "acme", "https://acme.example")]


async def test_a_file_with_no_name_column_or_that_is_not_there_is_refused_in_words(js, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli.settings, "owner", OWNER)

    assert await cli._import_employer_list(_args(_file(tmp_path, ("acme.example", "ok"),
                                                       header=("website", "verdict")))) == 1
    assert "column" in capsys.readouterr().err
    assert await cli._import_employer_list(_args(tmp_path / "missing.csv")) == 1
    assert "error:" in capsys.readouterr().err


# --- the scout's instructions ---------------------------------------------------------------------------------------


def test_the_scout_instructions_say_what_a_list_website_is_and_that_it_must_be_checked():
    from app.harness import instructions

    text = instructions.read("instructions", "jobsearch.scout").lower()
    assert "list_websites" in text
    for word in ("owner's own employer list", "first clue", "unchecked", "dead", "parked", "wrong company"):
        assert word in text, word
