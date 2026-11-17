"""The registry writes nothing and the shipped tests know nothing of the restricted folder (the generic-sites design,
2.2 and 3.6). Every site is made up."""

import ast
import json
from pathlib import Path

from sqlalchemy import func, select

from app import db
from app.sites import loader, registry, where
from app.sites.models import Site, SiteSkill
from tests.support import OWNER

BACKEND = Path(__file__).resolve().parents[2]
SITES = BACKEND / "app" / "sites"
HOST = "careers.undecided.example.test"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_the_modules_that_read_the_runbooks_import_nothing_that_could_write_a_row():
    for name in ("registry.py", "loader.py", "schema.py"):
        for module in _imports(SITES / name):
            writes = ("sqlalchemy", "app.db", "app.sites.models", "app.sites.skills")
            assert not module.startswith(writes), (name, module)


def test_no_shipped_test_imports_the_restricted_folder():
    """The folder is absent from an exported tree: a test that imported it would fail to collect there."""
    named = []
    for path in (BACKEND / "tests").rglob("*.py"):
        if any(m == "restricted_runbooks" or m.startswith("restricted_runbooks.") for m in _imports(path)):
            named.append(path.name)
    assert named == []


def test_no_module_of_the_app_imports_the_restricted_folder_by_name():
    named = [p.name for p in (BACKEND / "app").rglob("*.py")
             if any(m == "restricted_runbooks" or m.startswith("restricted_runbooks.") for m in _imports(p))]
    assert named == [], "hooks are loaded by the bare name of a runbook's reader, never imported"


async def _counts(session) -> tuple[int, int]:
    return (await session.scalar(select(func.count()).select_from(SiteSkill)) or 0,
            await session.scalar(select(func.count()).select_from(Site)) or 0)


async def test_a_runbook_for_an_undecided_sites_host_leaves_the_site_off_and_writes_no_skill_row(js, tmp_path):
    from app.jobsearch.employers import core as employers

    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / "known.json").write_text(json.dumps({
        "schema": 1, "key": "known", "name": "Known", "kind": "host", "hosts": ["example.test"],
        "pace_seconds": 5}), encoding="utf-8")
    mine = loader.load(shipped, tmp_path / "absent", ())
    async with db.session_factory()() as session:
        employer = await employers.find_or_make(session, OWNER, "Undecided")
        employer.careers_url = f"https://{HOST}/"
        await session.flush()
        with registry.replaced(mine):
            await where.seed_employer_sites(session, OWNER)
            before = await _counts(session)
            for ask in (registry.order, registry.starts_off, registry.board_hosts, registry.shared_hosts,
                        registry.mail_sets, registry.own_careers_paths):
                ask()
            assert registry.pace_for_domain("example.test") == 5.0
            assert registry.stop_key(HOST) == HOST
            assert await _counts(session) == before
        [site] = (await session.scalars(select(Site).where(Site.owner == OWNER))).all()
        assert (site.on, site.turned_on_at) == (False, None)
        assert before[0] == 0, "no skill row was written for it, whatever the runbook says of its host"
