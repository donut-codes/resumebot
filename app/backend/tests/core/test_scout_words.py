"""What the scout is said to do, in the words the Agents pages show: it follows the search as it is (it finds employers'
job boards and judges no posting; the title pass and the readers are retired), and an install made before those words
changed is brought up to date. Synthetic data only."""

from app import db
from app.agents import roster
from app.models.agent import RoleDef
from tests.support import OWNER

#: What the scout's purpose said when an older install made it, before the title pass.
OLDER_WORDS = ("Searches the job boards and employer career pages for roles that fit your match profile, scores them "
               "and reports them.")


def test_the_scouts_purpose_says_it_finds_employers_job_boards_and_judges_no_posting():
    said = roster.SCOUT_DESCRIPTION
    assert "finds employers' job boards" in said and "judges no posting" in said
    assert "makes a role of what is left" in said, "a posting that passes the gates becomes a role at once"
    assert not [word for word in ("title", "role tab", "likeliest", "read the ones") if word in said], said


#: What the analyst's purpose said before it classed the preferred items too.
OLDER_ANALYST_WORDS = ("Follows behind the scout: researches a found role's employer, pay and full posting, classes "
                       "each required item against the master resume, and re-judges the score.")


def test_the_analysts_purpose_says_it_classes_the_required_and_the_preferred_items():
    assert "classes each required and each preferred item against the master resume" in roster.ANALYST_ROLE.about


async def test_an_older_installs_analyst_is_brought_up_to_date_when_the_app_starts(js):
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        (await session.get(RoleDef, roster.ANALYST)).about = OLDER_ANALYST_WORDS
        await session.commit()
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await session.commit()
        assert (await session.get(RoleDef, roster.ANALYST)).about == roster.ANALYST_ROLE.about


async def test_an_older_installs_scout_is_brought_up_to_date_when_the_app_starts(js):
    async with db.session_factory()() as session:
        conn = await roster.ensure_roster(session, OWNER)
        conn.description = OLDER_WORDS
        await session.commit()
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await session.commit()
        assert (await roster.connection_by_slug(session, OWNER, roster.SCOUT)).description == roster.SCOUT_DESCRIPTION
