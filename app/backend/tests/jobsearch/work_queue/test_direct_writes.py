"""The scan (the work queue spec E3, a new check the owner approved on 2026-11-02): every direct write in
the app (a Core `update`, `insert` or `delete`) posts a to-do or is listed as exempt, with its reason, in
`direct_writes_baseline.json`. It catches a writer added to a watched table that nobody wired, including one no test
exercises (the tripwire sees only code that runs). Baselines only shrink: a new site is wired, never added here to hide
it. Everything is made up."""

import functools
import textwrap
from pathlib import Path

from process_engine.base import Base as EngineBase

from app.database import Base
from app.jobsearch.work.queue import watch
from tests.jobsearch.work_queue import scan


@functools.cache
def _known() -> dict[str, scan.Site]:
    return scan.scan()


def _tables() -> dict[str, str]:
    """The table of each model by its class name, the app's and the vendored process engine's."""
    return {m.class_.__name__: m.local_table.name for base in (Base, EngineBase) for m in base.registry.mappers}


def test_every_direct_write_to_a_watched_table_posts_or_is_listed():
    found, listed = _known(), scan.baseline()
    unlisted = sorted(set(found) - set(listed))
    assert not unlisted, (
        f"a direct write nobody wired: {unlisted}. Post beside the statement (queue.post, post_roles, ...) or wrap it "
        "in queue.exempt(reason), then list it with `scan.write_baseline(scan.scan())`")
    for key, site in found.items():
        entry = listed[key]
        assert site.statements == entry["statements"], (
            f"{key} writes {site.statements} statements, not {entry['statements']}")
        assert sorted(set(site.targets)) == entry["targets"], f"{key} writes to {sorted(set(site.targets))}"
        if "answered_by" in entry:
            continue
        if "unwatched" in entry:
            tables = _tables()
            assert all(tables.get(t) and tables[t] not in watch.WATCHED_TABLES for t in site.targets), (
                f"{key} says its tables are not watched, and {sorted(set(site.targets))} are or are not known")
            continue
        assert site.calls or site.exempt, f"{key} writes and neither posts nor says why it need not"
        assert sorted(site.posts) == entry["posts"], (
            f"{key} posts {sorted(site.posts)}, the baseline says {entry['posts']}")
        assert site.exempt == entry["exempt"], (
            f"{key} says {site.exempt} for its exemption, the baseline {entry['exempt']}")


def test_the_baseline_lists_no_site_that_has_gone_or_stopped_posting():
    found, listed = _known(), scan.baseline()
    gone = sorted(set(listed) - set(found))
    assert not gone, f"listed and no longer writing directly: {gone}; take them out of the baseline"
    for key, entry in listed.items():
        if "answered_by" in entry:
            other = found.get(entry["answered_by"])
            assert other is not None and other.calls, (
                f"{key} is answered by {entry['answered_by']}, which does not post")
        elif "unwatched" in entry:
            assert len(entry["unwatched"]) >= 20 and not entry["posts"] and not entry["exempt"], (
                f"{key} is unwatched, so it says why and nothing else")
        else:
            assert entry["posts"] or entry["exempt"], f"{key} is listed with neither a post nor an exemption"
            assert all(len(reason) >= 20 for reason in entry["exempt"]), f"{key}: an exemption says why"


def test_every_table_a_direct_write_names_is_a_table_the_tripwire_watches():
    tables = {m.class_.__name__: m.local_table.name for m in Base.registry.mappers}
    for key, site in _known().items():
        for target in site.targets:
            if target in tables:
                assert tables[target] in watch.WATCHED_TABLES, f"{key} writes to {tables[target]}, which nobody watches"


def test_no_raw_sql_writes_to_a_watched_table():
    raw = {key: site.targets for key, site in _known().items() if any(t.startswith("raw:") for t in site.targets)}
    assert not raw, f"raw SQL that writes: {raw}; the tripwire cannot tell what it changes, so write it as a statement"


def test_the_scan_finds_a_writer_that_posts_nothing_and_one_that_exempts(tmp_path: Path):
    package = tmp_path / "app"
    package.mkdir()
    (package / "writers.py").write_text(textwrap.dedent('''
        from sqlalchemy import delete, update
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from app.jobsearch.work import queue

        async def forgets(session):
            await session.execute(update(JobPosting).values(route="roles"))

        async def posts(session):
            await session.execute(pg_insert(Opportunity).values(id=1))
            queue.post_roles(session, "o", [1], cause="x")

        async def exempts(session):
            with queue.exempt("nobody reads these rows, they only say what is open"):
                await session.execute(delete(OpenTab))

        def not_sql(items):
            items.update({}); items.insert(0, 1); items.delete()
    '''), encoding="utf-8")
    found = scan.scan(package)
    assert set(found) == {"app/writers.py::forgets", "app/writers.py::posts", "app/writers.py::exempts"}
    assert not found["app/writers.py::forgets"].posts and not found["app/writers.py::forgets"].exempt
    assert found["app/writers.py::posts"].posts == ["post_roles"]
    assert found["app/writers.py::exempts"].exempt == ["nobody reads these rows, they only say what is open"]


def _found_in(tmp_path: Path, source: str) -> dict[str, scan.Site]:
    """What the scan finds in one made-up module."""
    package = tmp_path / "app"
    package.mkdir(exist_ok=True)
    (package / "probe.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return scan.scan(package)


def test_the_scan_finds_a_writer_whose_only_clue_is_a_parenthesised_import(tmp_path: Path):
    found = _found_in(tmp_path, """
        from sqlalchemy import (
            insert,
            select,
        )

        async def writes(session):
            await session.execute(insert(Thing).values(a=1))
    """)
    assert set(found) == {"app/probe.py::writes"} and found["app/probe.py::writes"].targets == ["Thing"]


def test_the_scan_finds_the_other_ways_to_name_a_statement(tmp_path: Path):
    found = _found_in(tmp_path, """
        import sqlalchemy as sa
        import sqlalchemy.dialects.postgresql as pg
        from sqlalchemy import sql
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.sql import update
        from sqlalchemy.sql.expression import delete as remove

        async def from_sql_module(session):
            await session.execute(update(A).values(a=1))

        async def from_the_expression_module(session):
            await session.execute(remove(B))

        async def through_the_dialects_package(session):
            await session.execute(postgresql.insert(C).values(a=1))

        async def through_an_alias(session):
            await session.execute(pg.insert(D).values(a=1))
            await session.execute(sa.update(E).values(a=1))
            await session.execute(sql.delete(F))

        async def on_the_table(session):
            await session.execute(G.__table__.update().values(a=1))
            await session.execute(H.__table__.delete())

        def on_a_query(session):
            session.query(I).update({"a": 1})
            session.query(J).filter(J.a == 1).delete()

        def in_bulk(session):
            session.bulk_update_mappings(K, [])
            session.bulk_insert_mappings(L, [])
            session.bulk_save_objects([])

        def not_sql(items, cache, session):
            items.update({}); items.insert(0, 1); cache.delete("key"); session.delete(M)
    """)
    expected = {
        "from_sql_module": ["A"], "from_the_expression_module": ["B"], "through_the_dialects_package": ["C"],
        "through_an_alias": ["D", "E", "F"], "on_the_table": ["G", "H"], "on_a_query": ["I", "J"],
        "in_bulk": ["", "K", "L"],
    }
    assert {k.split("::")[1]: sorted(site.targets) for k, site in found.items()} == expected


def test_the_scan_finds_raw_sql_that_does_not_start_with_its_verb(tmp_path: Path):
    found = _found_in(tmp_path, """
        from sqlalchemy import text

        async def in_a_cte(session):
            await session.execute(text("WITH old AS (SELECT id FROM things) UPDATE things SET a = 1 FROM old"))

        async def in_an_f_string(session, table):
            await session.execute(text(f"DELETE FROM {table} WHERE a = 1"))

        async def joined_in_pieces(session):
            await session.execute(text("INSERT INTO " + "things (a) VALUES (1) ON CONFLICT DO UPDATE SET a = 2"))

        async def with_an_alias(session):
            await session.execute(text("UPDATE ONLY app.things AS t SET a = 1"))

        async def only_reads(session):
            await session.execute(text("SELECT id FROM things FOR UPDATE OF things SKIP LOCKED"))
            await session.execute(text("SELECT 1"))
    """)
    assert {k.split("::")[1]: site.targets for k, site in found.items()} == {
        "in_a_cte": ["raw:things"], "in_an_f_string": ["raw:x"], "joined_in_pieces": ["raw:things"],
        "with_an_alias": ["raw:things"]}


def test_a_function_that_only_names_a_topic_is_not_a_function_that_posts(tmp_path: Path):
    found = _found_in(tmp_path, """
        from sqlalchemy import delete
        from app.jobsearch.work import queue

        async def names_a_topic(session):
            await session.execute(delete(A))
            return queue.POSTING_ROLES

        async def posts(session):
            await session.execute(delete(B))
            queue.post(session, "o", queue.POSTING_ROLES)
    """)
    assert found["app/probe.py::names_a_topic"].calls == [] and found["app/probe.py::names_a_topic"].posts
    assert found["app/probe.py::posts"].calls == ["post"]


def test_the_scan_reads_the_packages_the_app_vendors():
    found = _known()
    outside = sorted(key for key in found if key.startswith("packages/"))
    assert outside, "the process engine's own statements are read too (packages/*/src)"
    assert all(key in scan.baseline() for key in outside)
