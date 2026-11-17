"""What a NEW install starts with (plan H2): a light nightly spend limit, the owner-only robots switch off, the job
systems whose terms deny the app switched off (docs/PRD.md path 55; the registry names them, the restricted tier holds
the frozen list), and the app's own browser profile (nothing signed in) for the browser jobs. An install that
already exists keeps what it has.
Real SQLite file in a temp folder, no network; every name is made up."""

import pytest
import sqlalchemy as sa

from app import setup_cli
from app.jobsearch.actors import BY_APP
from app.jobsearch.models import ProfileVersion
from app.jobsearch.profile.schema import Settings
from app.sites import registry

OWNER = "Sam Example"
ZONE = "Asia/Tokyo"


@pytest.fixture(autouse=True)
def _no_real_documents(monkeypatch, tmp_path):
    """The machine running the tests has a Documents folder of its own; no test reads it."""
    monkeypatch.setattr(setup_cli, "documents_folder", lambda: tmp_path / "Docs")


def _settings_rows(folder):
    engine = sa.create_engine(f"sqlite:///{setup_cli.database_file(setup_cli.database_url(folder)).as_posix()}")
    try:
        with engine.connect() as c:
            return c.execute(sa.select(ProfileVersion.__table__.c.owner, ProfileVersion.__table__.c.version,
                                       ProfileVersion.__table__.c.data, ProfileVersion.__table__.c.saved_by)
                             .where(ProfileVersion.__table__.c.kind == "settings")).all()
    finally:
        engine.dispose()


def test_a_new_install_starts_with_a_light_spend_limit_the_robots_switch_off_and_the_denying_job_systems_off(tmp_path):
    folder = tmp_path / "data"
    assert setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]) == 0
    [(owner, version, data, by)] = _settings_rows(folder)
    saved = Settings.model_validate(data)
    assert (owner, version, by) == (OWNER, 1, BY_APP)
    assert saved.nightly_budget == setup_cli.STARTER_NIGHTLY_BUDGET > 0
    assert saved.read_robots_exempt is False
    assert saved.job_systems_off == sorted(registry.starts_off())
    assert saved == Settings(nightly_budget=setup_cli.STARTER_NIGHTLY_BUDGET,
                             job_systems_off=sorted(registry.starts_off()))  # nothing else was set


def test_a_new_install_writes_no_browser_mode_so_the_apps_own_profile_stays_the_default(tmp_path):
    folder = tmp_path / "data"
    setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)])
    assert "SCOUT_BROWSER" not in setup_cli.read_env(folder / ".env")


def test_init_run_again_leaves_the_owners_saved_settings_alone(tmp_path):
    folder = tmp_path / "data"
    args = ["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)]
    setup_cli.init(args)
    setup_cli.init(args)
    assert len(_settings_rows(folder)) == 1


def test_an_existing_database_with_no_settings_is_given_the_starter_limit_once(tmp_path):
    """H4, on purpose: a run that stopped after making the database used to leave it with no starter settings for
    good (the denying job systems on, no spend limit); init now seeds a database that holds no Settings row, and
    never one that holds a saved version (test_init_run_again_leaves_the_owners_saved_settings_alone)."""
    folder = tmp_path / "data"
    folder.mkdir()
    url = setup_cli.database_url(folder)
    assert setup_cli.create_database(url) is True  # an install made before this change: a database with no settings
    (folder / ".env").write_text(
        f"JOBSEARCH_OWNER={OWNER}\nJOBSEARCH_TIMEZONE={ZONE}\nJOBSEARCH_DATABASE_URL={url}\n", encoding="utf-8")
    setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)])
    [(_, version, data, _)] = _settings_rows(folder)
    assert version == 1 and Settings.model_validate(data).nightly_budget == setup_cli.STARTER_NIGHTLY_BUDGET
    setup_cli.init(["--owner", OWNER, "--timezone", ZONE, "--data", str(folder)])
    assert len(_settings_rows(folder)) == 1
