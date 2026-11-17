"""The job systems a new install starts switched off are the runbooks' own (`starts_off`, the generic-sites design,
Part 6): `init` writes them into the first Settings, and a copy that ships fewer runbooks starts fewer off. Every site
is made up (the keys are the first three of the systems the app has a reader for, which the Settings check names; their
hosts are not). The four a checkout with the restricted folder starts off are the restricted tier's frozen reference."""

import json

import pytest
import sqlalchemy as sa

from app import setup_cli
from app.jobsearch import ats
from app.jobsearch.models import ProfileVersion
from app.jobsearch.profile.schema import Settings
from app.sites import loader, registry


@pytest.fixture(autouse=True)
def _no_real_documents(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_cli, "documents_folder", lambda: tmp_path / "Docs")


def _keys() -> list[str]:
    """Three keys of job systems the app has a reader for, taken from the app and never typed here."""
    return list(ats.SYSTEMS)[:3]


def _registry(tmp_path):
    first, second, third = _keys()
    shipped = tmp_path / "runbooks"
    shipped.mkdir()
    for key, order, off in ((first, 0, False), (second, 1, True), (third, 2, True)):
        doc = {"schema": 1, "key": key, "name": key.title(), "kind": "job_system", "order": order,
               "hosts": [f"{key}.example.test"], "starts_off": off}
        (shipped / f"{key}.json").write_text(json.dumps(doc), encoding="utf-8")
    return loader.load(shipped, tmp_path / "absent", ())


def test_a_new_install_starts_off_the_job_systems_the_runbooks_say_and_no_others(tmp_path):
    folder = tmp_path / "data"
    _, second, third = _keys()
    with registry.replaced(_registry(tmp_path)):
        assert list(ats.NOT_OFFERED) == [second, third]
        assert setup_cli.init(["--owner", "Sam Example", "--timezone", "Asia/Tokyo", "--data", str(folder)]) == 0
    engine = sa.create_engine(f"sqlite:///{setup_cli.database_file(setup_cli.database_url(folder)).as_posix()}")
    try:
        with engine.connect() as c:
            [data] = c.execute(sa.select(ProfileVersion.__table__.c.data)
                               .where(ProfileVersion.__table__.c.kind == "settings")).scalars().all()
    finally:
        engine.dispose()
    assert Settings.model_validate(data).job_systems_off == sorted([second, third])


def test_the_runbooks_that_ship_start_off_only_the_one_whose_terms_are_silent():
    shipped = loader.load(restricted_dir=loader.RESTRICTED_DIR / "absent")
    assert len(shipped.starts_off()) == 1
    assert {shipped.verdict(h) for key in shipped.starts_off() for h in shipped.get(key).hosts} == {"silent"}  # pyright: ignore[reportOptionalMemberAccess]
