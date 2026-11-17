"""The copies the place and pay gates read for several roles, in one read (`cleansed.gate_copies_for`), are the copies
`gate_copy_of` chooses for each of them alone, by the same rule (docs/PRD.md 54.4, `gate_copy`). Everything is made
up."""

import uuid

from app import db
from app.jobsearch import ats
from app.jobsearch.postings import cleansed
from tests.jobsearch.test_gate_copy import bare_role, list_posting
from tests.support import EXAMPLE_SYSTEM, OWNER


async def a_world_of_every_shape(session) -> dict:
    """Roles of every shape the rule has: the employer's own posting over a bigger board's, the newest of two boards,
    the one the role was Sourced to, a lone posting, one whose employer's posting has no copy, one whose newest is a
    stub, one never read, one with no posting, and a posting of another owner."""
    roles = {}
    roles["own"] = await bare_role(session, "own")
    await list_posting(session, roles["own"], "a", system=EXAMPLE_SYSTEM, seen=30, size=300)
    await list_posting(session, roles["own"], "b", system=ats.JOB_SITE, seen=1, size=9000)
    roles["newest"] = await bare_role(session, "newest")
    await list_posting(session, roles["newest"], "a", system=ats.JOB_SITE, seen=20, size=9000)
    await list_posting(session, roles["newest"], "b", system=ats.BROWSER, seen=2, size=400)
    roles["sourced"] = await bare_role(session, "sourced", source_url="https://sourced.example.test/job/771234",
                                       source_kind="employer")
    await list_posting(session, roles["sourced"], "a", system=ats.BROWSER, seen=40,
                       url="https://www.sourced.example.test/job/771234-programs-lead")
    await list_posting(session, roles["sourced"], "b", system=ats.JOB_SITE, seen=1)
    roles["lone"] = await bare_role(session, "lone")
    await list_posting(session, roles["lone"], "a", system=ats.JOB_SITE, seen=3)
    roles["unread"] = await bare_role(session, "unread")
    await list_posting(session, roles["unread"], "a", system=EXAMPLE_SYSTEM, seen=30, copy=False)
    await list_posting(session, roles["unread"], "b", system=ats.JOB_SITE, seen=9)
    roles["thin"] = await bare_role(session, "thin")
    await list_posting(session, roles["thin"], "a", system=ats.JOB_SITE, seen=20, size=900)
    await list_posting(session, roles["thin"], "b", system=EXAMPLE_SYSTEM, seen=1, size=40)
    roles["stub"] = await bare_role(session, "stub")
    await list_posting(session, roles["stub"], "a", system=EXAMPLE_SYSTEM, seen=2, size=15)
    roles["none"] = await bare_role(session, "none")
    theirs = await bare_role(session, "theirs")
    await list_posting(session, theirs, "a", system=EXAMPLE_SYSTEM, seen=3, owner="another")
    return roles


async def test_the_copies_for_several_roles_are_the_copies_chosen_for_each_alone(js):
    async with db.session_factory()() as session:
        roles = await a_world_of_every_shape(session)
        await session.commit()
    ids = [role.id for role in roles.values()]
    async with db.session_factory()() as session:
        together = await cleansed.gate_copies_for(session, OWNER, ids)
        chosen = await cleansed.gate_copy_ids_for(session, OWNER, ids)
        for name, role in roles.items():
            alone = await cleansed.gate_copy_of(session, OWNER, role.id)
            assert (together[role.id].id if role.id in together else None) == (alone.id if alone else None), name
        assert {role_id: copy.id for role_id, copy in together.items()} == chosen
        assert roles["none"].id not in together and len(together) == len(roles) - 1, "a role with no copy is left out"
        assert await cleansed.gate_current_for_roles(session, OWNER) == chosen, "and the owner-wide read agrees"


async def test_only_the_roles_asked_are_answered_and_asking_for_none_reads_nothing(js):
    async with db.session_factory()() as session:
        roles = await a_world_of_every_shape(session)
        await session.commit()
    async with db.session_factory()() as session:
        some = [roles["own"].id, roles["lone"].id, uuid.uuid4()]
        assert set(await cleansed.gate_copy_ids_for(session, OWNER, some)) == {roles["own"].id, roles["lone"].id}
        assert await cleansed.gate_copy_ids_for(session, OWNER, []) == {}
        assert await cleansed.gate_copies_for(session, OWNER, []) == {}
        assert await cleansed.gate_copy_ids_for(session, "another", [roles["own"].id]) == {}, "the owner's own only"


async def test_a_role_asked_twice_is_answered_once_and_the_reads_do_not_grow_with_the_roles(js):
    from sqlalchemy import event

    async with db.session_factory()() as session:
        roles = await a_world_of_every_shape(session)
        await session.commit()
    ids = [role.id for role in roles.values()]
    sent: list[str] = []

    def noted(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if not statement.lstrip().upper().startswith("BEGIN"):  # a writer's session opens with BEGIN IMMEDIATE
            sent.append(statement)

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", noted)
    try:
        async with db.session_factory()() as session:
            assert len(await cleansed.gate_copies_for(session, OWNER, [*ids, *ids])) == len(roles) - 1
    finally:
        event.remove(engine, "before_cursor_execute", noted)
    assert len(sent) == 2, "the postings and their copies in one statement, the copies themselves in another"
