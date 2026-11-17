"""Importing the owner's tracker and exporting in the tracker's column order. The workbooks are made here, in memory:
never the owner's file."""

import io
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest
from sqlalchemy import func, select

from app import db
from app.jobsearch import cli
from app.jobsearch.imports import tracker
from app.jobsearch.models import Opportunity
from app.jobsearch.stages import GATED_OUT
from tests.builders import export_workbook
from tests.support import OWNER, tracker_row, tracker_workbook

TRACKER_COLUMNS = [
    "id", "company", "title", "status", "fit", "level", "effort", "variant",
    "comp", "compSource", "location", "moveType", "sector", "found", "board",
    "url", "applyUrl", "ats", "notes", "packetComplete", "queueRank",
    "matchKey", "fitEvidence", "colAdjusted", "colAdjustedComp",
]


def test_the_column_list_is_the_trackers_own_25_columns_in_its_order():
    assert tracker.COLUMNS == TRACKER_COLUMNS and len(tracker.COLUMNS) == 25
    assert set(tracker.ATTR) == set(TRACKER_COLUMNS)


async def _import(rows, header=None):
    async with db.session_factory()() as session:
        counts = await tracker.import_tracker(session, OWNER, tracker_workbook(rows, header))
        await session.commit()
    return counts


async def _all():
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Opportunity).where(Opportunity.owner == OWNER))).all())


#: The counts of an import that did nothing but read the rows it was given.
def _counts(**some) -> dict:
    return {"rows": 0, "added": 0, "already_there": 0, "updated": 0, "left_as_in_app": 0, "repeated": 0,
            "skipped_blank": 0, **some}


async def test_import_reads_every_column_into_the_apps_words(js):
    counts = await _import([
        tracker_row(
            "Northwind Bank", "Director of Programs", id="northwind-director-programs", fit=4, level="Director",
            effort="High", variant="ops", comp="$120K-$150K", compSource="posting", location="Oakfield, ST",
            moveType="local", sector="Finance", found=date(2026, 9, 20), board="Boardtwo", url="https://x.test/1",
            applyUrl="https://x.test/apply", ats="Tenantone", notes="n", packetComplete=True, queueRank=2,
            matchKey="northwind bank|director of programs", fitEvidence="base 4", colAdjusted=False,
            colAdjustedComp=None, status="applied",
        ),
        tracker_row("Fabrikam", "Lead"),
    ])
    assert counts == _counts(rows=2, added=2)
    rows = {r.company: r for r in await _all()}
    nw = rows["Northwind Bank"]
    assert (nw.slug, nw.status, nw.level, nw.effort, nw.variant) == (
        "northwind-director-programs", "applied", "Director", "High", "ops")
    assert nw.fit is None, "the plugin's fit is on a scale the app does not use"
    assert (nw.comp, nw.comp_source, nw.location, nw.move_type, nw.sector) == (
        "$120K-$150K", "posting", "Oakfield, ST", None, "Finance"), "'local' is the plugin's tier, not a way of working"
    assert (nw.found, nw.board, nw.url, nw.apply_url, nw.ats) == (
        date(2026, 9, 20), "Boardtwo", "https://x.test/1", "https://x.test/apply", "Tenantone")
    assert (nw.notes, nw.packet_complete, nw.queue_rank) == ("n", True, 2)
    assert nw.fit_evidence == "Your old tracker scored it 4, on a scale of its own that this app does not use. base 4"
    assert nw.match_key == "northwind bank|director of programs" and nw.origin == "tracker"
    assert rows["Fabrikam"].match_key == "fabrikam|lead"
    assert rows["Fabrikam"].slug == "fabrikam-lead"


async def test_import_names_a_role_once_inside_the_file_and_brings_an_untouched_one_up_to_date(js):
    counts = await _import([
        tracker_row("Northwind", "Director", notes="first"),
        tracker_row("NORTHWIND", " director ", notes="same role, second row"),  # the same key once lowercased
        tracker_row("", "No company"),
        tracker_row("Fabrikam", ""),
    ])
    assert counts == _counts(rows=4, added=1, repeated=1, skipped_blank=2)
    again = await _import(
        [tracker_row("Northwind", "Director", notes="changed in the file"), tracker_row("New", "Role")])
    assert again == _counts(rows=2, added=1, already_there=1, updated=1)
    notes = {r.company: r.notes for r in await _all()}
    assert notes["Northwind"] == "changed in the file", "nothing in the app has touched it, so the file's word stands"


async def test_a_dash_for_the_trackers_path_reads_the_spreadsheet_from_standard_input(js, monkeypatch):
    """`import-tracker -` takes the file piped in (`type Tracker.xlsx | resumebot import-tracker -`), so a tracker on
    another machine is never written to the one that imports it; any other argument is still a path."""
    assert cli.tracker_source("/data/Tracker/JobSearchTracker.xlsx") == Path("/data/Tracker/JobSearchTracker.xlsx")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=tracker_workbook([tracker_row("Northwind", "Director")])))
    async with db.session_factory()() as session:
        counts = await tracker.import_tracker(session, OWNER, cli.tracker_source("-"))
        await session.commit()
    assert counts == _counts(rows=1, added=1)
    assert [r.company for r in await _all()] == ["Northwind"]


async def test_import_normalizes_odd_cells(js):
    await _import([
        tracker_row("A", "One", status="Applied", fit="4", packetComplete="TRUE", queueRank="3.0", found="2026-09-01"),
        tracker_row("B", "Two", status="mystery", fit=None, packetComplete="no", found="not a date"),
    ])
    rows = {r.company: r for r in await _all()}
    assert (rows["A"].status, rows["A"].fit, rows["A"].packet_complete, rows["A"].queue_rank) == (
        "applied", None, True, 3)
    assert rows["A"].found == date(2026, 9, 1)
    assert rows["A"].fit_evidence.startswith("Your old tracker scored it 4,"), "its own figure is kept, as its figure"
    assert (rows["B"].status, rows["B"].fit, rows["B"].packet_complete, rows["B"].found) == (
        "landing_zone", None, False, None)
    assert rows["B"].fit_evidence is None


async def test_a_file_that_is_not_the_tracker_is_refused_in_plain_words(js):
    with pytest.raises(tracker.TrackerError, match="25 columns"):
        await _import([], header=[*TRACKER_COLUMNS[:-1], "somethingElse"])
    wb = openpyxl.Workbook()
    wb.active.title = "Other"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    async with db.session_factory()() as session:
        with pytest.raises(tracker.TrackerError, match="no sheet named 'Tracker'"):
            await tracker.import_tracker(session, OWNER, buf)
        with pytest.raises(tracker.TrackerError, match="isn't a spreadsheet"):
            await tracker.import_tracker(session, OWNER, io.BytesIO(b"not a workbook"))
    assert await _all() == []


async def test_export_writes_the_trackers_columns_in_the_trackers_order_and_a_screened_out_sheet(js):
    await _import([tracker_row("Northwind", "Director", fit=4, location="Oakfield, ST", found=date(2026, 9, 20))])
    async with db.session_factory()() as session:
        session.add(Opportunity(
            owner=OWNER, slug="globex-vp", company="Globex", title="VP", status=GATED_OUT, match_key="globex|vp",
            location="Denver", comp="$80K", url="https://x.test/g", board="Boardthree", found=date(2026, 10, 1),
            screened_out_reason="location: not remote, local or secondary", origin="scout"))
        await session.commit()
        data = await export_workbook(session, OWNER)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Tracker", "ScreenedOut"]
    sheet = wb["Tracker"]
    assert [c.value for c in sheet[1]] == TRACKER_COLUMNS
    row = dict(zip(TRACKER_COLUMNS, [c.value for c in sheet[2]], strict=True))
    assert (row["company"], row["title"], row["fit"], row["matchKey"], row["found"]) == (
        "Northwind", "Director", None, "northwind|director", "2026-09-20"), "the app has not scored an imported role"
    assert sheet.max_row == 2, "a screened-out role is not in the tracker sheet"
    out = wb["ScreenedOut"]
    assert [c.value for c in out[1]] == ["company", "title", "location", "comp", "url", "board", "found", "reason"]
    assert [c.value for c in out[2]][:3] == ["Globex", "VP", "Denver"]
    assert out[2][7].value == "location: not remote, local or secondary"


async def test_a_title_or_note_that_starts_with_an_equals_sign_is_exported_as_text_never_as_a_formula(js):
    """Openpyxl stores a string starting with `=` as a formula, which Excel runs when the sheet is opened
    (=HYPERLINK to a phishing page, =WEBSERVICE to send cells out). A scout's report is data from the web."""
    async with db.session_factory()() as session:
        session.add_all([
            Opportunity(owner=OWNER, slug="f1", company="=1+1", title='=HYPERLINK("https://x.test","Click")',
                        status="second_impression", match_key="a|b", origin="scout",
                        notes='=WEBSERVICE("https://x.test")'),
            Opportunity(owner=OWNER, slug="f2", company="Globex", title="VP", status=GATED_OUT, match_key="c|d",
                        origin="scout", screened_out_reason="=1+1")])
        await session.commit()
        data = await export_workbook(session, OWNER)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    formulas = [(ws.title, c.coordinate) for ws in wb for row in ws.iter_rows() for c in row if c.data_type == "f"]
    assert formulas == [], "every cell is text"
    row = dict(zip(TRACKER_COLUMNS, [c.value for c in wb["Tracker"][2]], strict=True))
    assert row["company"] == "=1+1" and row["title"] == '=HYPERLINK("https://x.test","Click")'
    assert wb["ScreenedOut"][2][7].value == "=1+1"


async def test_an_exported_tracker_imports_back_as_the_same_roles(js):
    await _import([tracker_row("Northwind", "Director", fit=4), tracker_row("Fabrikam", "Lead", fit=2)])
    async with db.session_factory()() as session:
        data = await export_workbook(session, OWNER)
        counts = await tracker.import_tracker(session, "someone-else", io.BytesIO(data))
        total = await session.scalar(select(func.count()).select_from(Opportunity))
    assert counts["added"] == 2 and total == 4, "the same roles, for another owner, are their own rows"


def _reference_workbook(session_roles) -> openpyxl.Workbook:
    """The export as it was written before the speed fix: whole roles, `sheet.append`, `sheet[max_row]`. Kept as the
    yardstick that the faster one writes the same cells."""
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = tracker.SHEET
    sheet.append(tracker.COLUMNS)
    kept, dropped = session_roles
    for role in kept:
        sheet.append([
            tracker.stages.LABELS.get(role.status, role.status) if c == "status"
            else tracker._cell(getattr(role, tracker.ATTR[c])) for c in tracker.COLUMNS])
        for cell in sheet[sheet.max_row]:
            if cell.data_type == "f":
                cell.data_type = "s"
    out = wb.create_sheet(tracker.SCREENED_SHEET)
    out.append(tracker.SCREENED_COLUMNS)
    for role in dropped:
        out.append([role.company, role.title, role.location, role.comp, role.url, role.board,
                    tracker._cell(role.found), role.screened_out_reason])
        for cell in out[out.max_row]:
            if cell.data_type == "f":
                cell.data_type = "s"
    return wb


def _cells(wb) -> dict:
    return {ws.title: [[(c.value, c.data_type) for c in row] for row in ws.iter_rows()] for ws in wb}


async def test_the_export_asks_the_database_a_fixed_number_of_times_and_writes_the_same_cells_as_before(js):
    from sqlalchemy import event

    statuses = ["second_impression", "applied", "landing_zone", GATED_OUT]
    async with db.session_factory()() as session:
        session.add_all([
            Opportunity(
                owner=OWNER, slug=f"co{n}-role", company=f"=Co {n}" if n % 7 == 0 else f"Co {n}", title=f"Role {n}",
                status=statuses[n % 4], match_key=f"co{n}|role", origin="scout", fit=n % 5 or None,
                location=None if n % 3 else "Remote", found=date(2026, 9, 1 + n % 28), packet_complete=n % 2 == 0,
                queue_rank=n if n % 4 == 1 else None, notes=f"=SUM({n})" if n % 5 == 0 else "a note",
                description="posting text " * 50, analysis={"pay": ["x"] * 20},
                screened_out_reason="location" if n % 4 == 3 else None)
            for n in range(80)])
        await session.commit()

    async def export_with_count(owner):
        async with db.session_factory()() as session:
            statements: list[str] = []

            def noted(_conn, _cursor, statement, _parameters, _context, _many):
                if not statement.startswith(("BEGIN", "COMMIT", "ROLLBACK")):  # the transaction's own, not a query
                    statements.append(statement)

            engine = db.engine().sync_engine
            event.listen(engine, "before_cursor_execute", noted)
            try:
                data = await export_workbook(session, owner)
            finally:
                event.remove(engine, "before_cursor_execute", noted)
        return data, statements

    data, statements = await export_with_count(OWNER)
    assert len(statements) == 2, f"one query for each sheet, however many roles: {statements}"
    for statement in statements:
        assert "description" not in statement and "analysis" not in statement, "the posting's text is not read"

    async with db.session_factory()() as session:
        from app.jobsearch import stages

        order = (Opportunity.created_at, Opportunity.slug)
        kept = (await session.scalars(select(Opportunity).where(
            Opportunity.owner == OWNER, Opportunity.status != stages.GATED_OUT).order_by(*order))).all()
        dropped = (await session.scalars(select(Opportunity).where(
            Opportunity.owner == OWNER, Opportunity.status == stages.GATED_OUT).order_by(*order))).all()
    got = _cells(openpyxl.load_workbook(io.BytesIO(data)))
    # Both are saved and read back, so what is compared is what Excel would open.
    buffer = io.BytesIO()
    _reference_workbook((kept, dropped)).save(buffer)
    expected = _cells(openpyxl.load_workbook(io.BytesIO(buffer.getvalue())))
    assert len(got["Tracker"]) == 61 and len(got["ScreenedOut"]) == 21
    assert got == expected


def test_writing_the_workbook_takes_time_in_proportion_to_its_rows():
    """Found when a large export took minutes: each row cost a pass over every row before it."""
    import gc
    import time

    def seconds(rows: int) -> float:
        """Best of three, with the cyclic collector paused: a full collection over a big heap, or a busy
        machine, lands on one sample and says nothing about how the writing grows with its rows."""
        body = [[f"v{r}-{c}" for c in range(len(tracker.COLUMNS))] for r in range(rows)]
        best = float("inf")
        for _ in range(3):
            gc.collect()
            gc.disable()
            try:
                started = time.perf_counter()
                tracker._workbook_bytes(body, [])
                best = min(best, time.perf_counter() - started)
            finally:
                gc.enable()
        return best

    seconds(200)  # warm up
    small, large = seconds(1000), seconds(4000)  # still 4x; the same bound
    assert large < small * 8, f"4x the rows took {large / small:.1f}x as long (in proportion is about 4)"


def _ref_write(sheet, number, row):
    for column, value in enumerate(row, 1):
        cell = sheet.cell(row=number, column=column, value=value)
        if cell.data_type == "f":
            cell.data_type = "s"


def test_the_workbook_is_written_in_streaming_mode_with_the_same_cells_as_the_whole_workbook_way(monkeypatch):
    """Found when the export held the whole workbook in memory (hundreds of megabytes for tens of thousands of rows):
    every cell of a
    fixture with a guarded `=` text, a date and blanks equals what the old whole-workbook writer stored."""
    made = []
    real = openpyxl.Workbook

    def spy(*args, **kwargs):
        made.append(kwargs.get("write_only"))
        return real(*args, **kwargs)

    monkeypatch.setattr(openpyxl, "Workbook", spy)
    tracker_rows = [["=1+1", "Role", None, "2026-09-20", 4, True, ""], ["Plain", None, "=SUM(1)", "", 0, False, "x"]]
    screened_rows = [["=cmd", "VP", None, "$1", "https://x.test", "Boardthree", "2026-10-01", "=1+1"]]
    data = tracker._workbook_bytes(tracker_rows, screened_rows)
    assert made == [True], "the workbook is written in streaming mode"
    reference = real()
    sheet = reference.active
    sheet.title = tracker.SHEET
    sheet.append(tracker.COLUMNS)
    out = reference.create_sheet(tracker.SCREENED_SHEET)
    out.append(tracker.SCREENED_COLUMNS)
    for target, rows in ((sheet, tracker_rows), (out, screened_rows)):
        for number, row in enumerate(rows, 2):
            _ref_write(target, number, row)
    buffer = io.BytesIO()
    reference.save(buffer)
    got = _cells(openpyxl.load_workbook(io.BytesIO(data)))
    assert got == _cells(openpyxl.load_workbook(io.BytesIO(buffer.getvalue())))
    assert all(kind != "f" for ws in got.values() for row in ws for _v, kind in row)
