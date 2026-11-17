"""The one place a copy of a posting is made (`postings/raw_copies.py`, the work queue spec D3): the raw and
the cleansed copy are written by `session.add`, and neither model is on the watch list, so the hook and the scan cannot
see a copy being made. Every entry point that makes one therefore posts `role.settle` for the roles of the postings it
made a copy of, and these tests are what would fail if one lost its post: one for each entry point, and a read of the
module that fails on a new function that writes a copy and posts nothing. Everything is made up."""

import ast
import textwrap
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select

from app import db
from app.jobsearch.adapters import _posting
from app.jobsearch.adapters._common import Detailed
from app.jobsearch.models import CleansedPosting, JobPosting, Todo
from app.jobsearch.postings import cleansed, hold, raw_copies
from tests.jobsearch.work_queue import scan
from tests.jobsearch.work_queue.support import OWNER, make_posting, make_role, without_links

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
WHOLE = "The whole text of the posting, long enough to be the text a role settles on. " * 8


async def _a_role_and_its_posting(**posting_fields) -> tuple[uuid.UUID, uuid.UUID]:
    role = make_role()
    posting = make_posting(opportunity_id=role.id, ats="employer", ext_id="1", **posting_fields)
    async with db.session_factory()() as session:
        await without_links(session)
        session.add_all([role, posting])
        await session.commit()
    return role.id, posting.id


async def _posted_by(write) -> list[tuple[str, str, str]]:
    """What `write(session)` posts, as (topic, key, cause); the setup's own to-dos are cleared first."""
    async with db.session_factory()() as session:
        await session.execute(delete(Todo))
        await session.commit()
    async with db.session_factory()() as session:
        await write(session)
        await session.commit()
    async with db.session_factory()() as session:
        return [(t, k, c) for t, k, c in (await session.execute(
            select(Todo.topic, Todo.key, Todo.cause).order_by(Todo.id))).all()]


async def test_the_items_a_read_keeps_settle_the_roles_of_their_postings(js):
    role_id, posting_id = await _a_role_and_its_posting()

    async def write(session):
        posting = await session.get(JobPosting, posting_id)
        listed = _posting(raw={"id": 1, "title": "Lead"}, ext_id="1", title="Lead")
        assert await raw_copies.keep_items(session, OWNER, posting.employer_id, [(posting, listed, "Fabrikam")],
                                           reader="adapter:employer", reader_version=None, now=NOW) == 1

    assert await _posted_by(write) == [("role.settle", str(role_id), "copies.made"),
                                       ("pool.owed", "", "copies.made")]


async def test_a_detail_answer_a_read_keeps_settles_the_role_of_its_posting(js):
    role_id, posting_id = await _a_role_and_its_posting()

    async def write(session):
        more = Detailed(text=WHOLE)
        more.raw = {"id": 1, "content": WHOLE}
        assert await hold.hold(
            session, await session.get(JobPosting, posting_id), hold.answer(more), copy_text=WHOLE, now=NOW) == 1

    assert await _posted_by(write) == [("role.settle", str(role_id), "copies.made"),
                                       ("pool.owed", "", "copies.made")]


async def test_a_report_a_reader_gave_settles_the_role_of_its_posting(js):
    role_id, posting_id = await _a_role_and_its_posting()

    async def write(session):
        assert await raw_copies._keep_report(session, await session.get(JobPosting, posting_id), {"title": "Lead"},
                                            reader="scout", text=WHOLE, now=NOW) == 1

    assert await _posted_by(write) == [("role.settle", str(role_id), "copies.made"),
                                       ("pool.owed", "", "copies.made")]


async def test_a_posting_held_before_the_copies_existed_settles_its_role_when_its_text_is_entered(js):
    role_id, _posting_id = await _a_role_and_its_posting(text=WHOLE)

    async def write(session):
        assert await raw_copies.keep_legacy(session, OWNER, NOW) == 1

    assert await _posted_by(write) == [("role.settle", str(role_id), "copies.made"),
                                       ("pool.owed", "", "copies.made")]


async def test_the_whole_text_a_posting_now_holds_settles_the_role_that_held_a_snippet(js):
    assert len(WHOLE) >= cleansed.MIN_TEXT
    role_id, _posting_id = await _a_role_and_its_posting(text=WHOLE)

    async def write(session):
        assert await raw_copies.keep_whole_text(session, OWNER, [role_id], NOW) == 1

    assert await _posted_by(write) == [("role.settle", str(role_id), "copies.made"),
                                       ("pool.owed", "", "copies.made")]


async def test_a_copy_made_again_from_the_raw_rows_settles_the_role_of_its_posting(js):
    role_id, _posting_id = await _a_role_and_its_posting(text=WHOLE)
    async with db.session_factory()() as session:
        assert await raw_copies.keep_legacy(session, OWNER, NOW) == 1
        await session.execute(delete(CleansedPosting))  # as a new cleaner version leaves them: none at its version
        await session.commit()

    async def write(session):
        assert (await raw_copies.recleanse(session, OWNER, NOW))["rebuilt"] == 1

    assert await _posted_by(write) == [("role.settle", str(role_id), "copies.recleanse"),
                                       ("pool.owed", "", "copies.recleanse")]


async def test_a_copy_of_a_posting_with_no_role_settles_nothing(js):
    async with db.session_factory()() as session:
        await without_links(session)
        posting = make_posting(ats="employer", ext_id="1")
        session.add(posting)
        await session.commit()
        posting_id = posting.id

    async def write(session):
        assert await raw_copies._keep_report(session, await session.get(JobPosting, posting_id), {"title": "Lead"},
                                            reader="scout", text=WHOLE, now=NOW) == 1

    assert await _posted_by(write) == []


# --- the module is read, so a new entry point cannot forget -----------------------------------------------------------

MODULE = scan.APP / "jobsearch" / "postings" / "raw_copies.py"
COPY_MODELS = {"RawPosting", "CleansedPosting"}
#: What makes a copy without saving it: a function that calls one of these (or `session.add`) saves a copy.
MAKERS = {"_row", "build"}
SAVES = MAKERS | {"add"}
#: What tells the roles a copy was made.
TELLS = {"_copy_made", "post"}


def _calls(function: ast.AST) -> set[str]:
    """The names a function calls (`name(...)`, `x.name(...)`)."""
    out: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            out.add(node.func.id if isinstance(node.func, ast.Name) else
                    node.func.attr if isinstance(node.func, ast.Attribute) else "")
    return out


def test_every_function_of_raw_copies_that_saves_a_copy_tells_the_roles():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    savers = {name for name, node in functions.items() if name not in MAKERS and _calls(node) & SAVES}
    assert {"keep_items", "keep_legacy", "recleanse", "_keep_sent"} <= savers, "the read of the module finds them"
    silent = sorted(name for name in savers if not _calls(functions[name]) & TELLS)
    assert not silent, f"saves a copy of a posting and does not tell the roles it was made for: {silent}"
    # the entry points that hand the work to `_keep_sent` are told through it
    assert "_copy_made" in _calls(functions["_keep_sent"]) and "post" in _calls(functions["_copy_made"])
    for name in ("_keep_detail", "_keep_report", "keep_whole_text"):
        assert "_keep_sent" in _calls(functions[name]), f"{name} no longer goes through the one that tells the roles"


def _makes_a_copy(tree: ast.Module) -> list[int]:
    """The lines that call a copy model, under the name it was imported by or as `models.RawPosting(...)`."""
    names = set(COPY_MODELS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname for a in node.names if a.name in COPY_MODELS and a.asname}
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and (
        isinstance(n.func, ast.Name) and n.func.id in names
        or isinstance(n.func, ast.Attribute) and n.func.attr in COPY_MODELS)]


def test_no_other_module_makes_a_raw_or_a_cleansed_copy():
    """A copy is made only here, where its to-do is posted: another module that built one would escape it (the models
    are not watched), so every module of the app that names a model is read for a call to it."""
    made: dict[str, list[int]] = {}
    for file in sorted(scan.APP.rglob("*.py")):
        source = file.read_text(encoding="utf-8")
        if file == MODULE or file.name == "models.py" or not any(name in source for name in COPY_MODELS):
            continue
        if lines := _makes_a_copy(ast.parse(source)):
            made[file.relative_to(scan.APP.parent).as_posix()] = lines
    assert not made, f"a copy made outside raw_copies.py, where its to-do is posted: {made}"
    statements = {key: site.targets for key, site in scan.scan().items() if COPY_MODELS & set(site.targets)}
    assert not statements, f"a direct statement on a copy: {statements}"


def test_the_read_for_a_made_copy_sees_a_model_by_any_name():
    probe = ast.parse(textwrap.dedent("""
        from app.jobsearch.models import RawPosting as Raw
        Raw(id=1)
        models.CleansedPosting(id=2)
        other(id=3)
    """))
    assert _makes_a_copy(probe) == [3, 4]
