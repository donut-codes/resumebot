"""The learner that writes an `adapter` recipe is sent the job systems it may name and the parts of a board each needs
(`skills.adapter_systems`), beside the recipe template: neither the template nor the code lists a system."""

import pytest

from app import db
from app.jobsearch import ats
from app.sites import skills, tools
from tests.jobsearch.example_world import FEEDSYS, example_world  # noqa: F401
from tests.support import OWNER

pytestmark = pytest.mark.usefixtures("example_world")


def test_every_system_with_a_reader_is_listed_with_the_parts_its_board_needs():
    listed = {row["key"]: row for row in skills.adapter_systems()}
    assert FEEDSYS in listed
    assert listed[FEEDSYS] == {"key": FEEDSYS, "name": ats.label(FEEDSYS), "parts": list(ats.SYSTEMS[FEEDSYS].needs)}
    assert all(key in ats.SYSTEMS for key in listed)


async def test_reading_the_site_skill_document_brings_the_list_and_no_other_document_does(js):
    async with db.session_factory()() as session:
        skill = await tools.doc_read(session, OWNER, "site.skill")
        other = await tools.doc_read(session, OWNER, "site.read")
    assert skill["job_systems"] == skills.adapter_systems()
    assert "job_systems" not in other
