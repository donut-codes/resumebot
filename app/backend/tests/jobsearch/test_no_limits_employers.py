"""No character limit on an employer's name.

A name an employer is made with (from a role, typed for a split, carried as another name by a merge,
or proposed for a split by the researcher) is kept whole and shown whole. Two names that agree for their first 200
key characters are the same employer for matching, as before: the key is an id, not a name. A site made for the
employer carries the whole name as its label. Synthetic data only."""

import uuid

from sqlalchemy import select

from app import db
from app.inbox import service as inbox
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.employers import records as employer_records
from app.jobsearch.employers.core import key_of
from app.jobsearch.models import Employer
from app.jobsearch.research import employer as employer_research
from app.sites import where
from app.sites.models import Site
from tests.employer_builders import make_employer, make_role
from tests.support import OWNER

LONG = 1_000


def _name(tag: str, n: int = LONG) -> str:
    """A made-up company name of exactly `n` characters: distinct words, no noise words, no outer space."""
    out, i = [], 0
    while sum(len(w) + 1 for w in out) < n:
        i += 1
        out.append(f"{tag}{i}")
    text = " ".join(out)[:n].rstrip()
    return text + "x" * (n - len(text))


def _same_key_other_name(name: str) -> str:
    """Another name that agrees with `name` for its first 200 key characters, and then goes its own way."""
    other = name[:700] + " different ending of its own"
    assert other != name and key_of(other) == key_of(name) and len(key_of(name)) == 200
    return other


async def _all() -> list[Employer]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Employer).where(Employer.owner == OWNER))).all())


async def test_the_name_of_an_employer_made_from_a_role_is_kept_whole_and_a_name_with_the_same_key_finds_it(js):
    name = _name("northwind")
    assert len(name) == LONG
    async with db.session_factory()() as session:
        made = await employers.find_or_make(session, OWNER, name)
        assert made is not None and made.name == name and len(made.name) == LONG
        assert made.key == key_of(name) and len(made.key) == 200
        again = await employers.find_or_make(session, OWNER, _same_key_other_name(name))
        assert again is not None and again.id == made.id, "the first 200 key characters are the match"
        await session.commit()
    [only] = await _all()
    assert only.name == name


async def test_a_name_typed_for_a_split_is_kept_whole_and_a_second_split_to_a_name_with_its_key_joins_it(client):
    name = _name("globex")
    async with db.session_factory()() as session:
        north = await make_employer(session, "Initech")
        roles = [make_role(north, "Lead A"), make_role(north, "Lead B"), make_role(north, "Lead C")]
        session.add_all(roles)
        await session.commit()
        north_id, role_ids = north.id, [str(r.id) for r in roles]
    asked = {"roles": role_ids[0], "name": name}
    looked = await client.get(f"/api/v1/employers/{north_id}/split", params=asked)
    assert looked.status_code == 200, looked.text
    assert looked.json()["to"] == {"id": None, "name": name, "exists": False}, "what would move names it whole"
    first = await client.post(f"/api/v1/employers/{north_id}/split", json={"roles": role_ids[:1], "name": name})
    assert first.status_code == 200, first.text
    other = _same_key_other_name(name)
    again = await client.get(f"/api/v1/employers/{north_id}/split", params={"roles": role_ids[1], "name": other})
    assert again.json()["to"]["exists"] is True and again.json()["to"]["name"] == name
    second = await client.post(f"/api/v1/employers/{north_id}/split", json={"roles": role_ids[1:2], "name": other})
    assert second.status_code == 200, second.text
    made = [e for e in await _all() if e.name != "Initech"]
    assert [e.name for e in made] == [name] and len(made[0].name) == LONG, "one record, with the whole name"
    seen = (await client.get(f"/api/v1/employers/{made[0].id}")).json()
    assert seen["name"] == name


async def test_a_long_name_carried_by_a_merge_stays_whole(js):
    other = _name("hooli", 900)
    async with db.session_factory()() as session:
        keep = await make_employer(session, "Harbor Mutual")
        away = await make_employer(session, "Harbor Mutual Insurance", aliases=[other])
        await session.commit()
        keep_id, away_id = keep.id, away.id
    async with db.session_factory()() as session:
        preview = await employer_records.preview_merge(session, OWNER, keep_id, away_id)
        assert other in preview["names"], "what a merge would call it shows the whole name too"
        await employer_records.merge(session, OWNER, keep_id, away_id, why="a test")
        await session.commit()
    async with db.session_factory()() as session:
        merged = await session.get(Employer, keep_id)
        assert other in (merged.aliases or []), "the other name rides along whole"


async def test_the_researchers_proposed_name_for_a_split_is_kept_whole(js):
    name = _name("umbrella")
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Initrode")
        role = make_role(employer, "Lead A")
        session.add(role)
        await session.flush()
        kept, refused = await employer_research._proposals(
            session, OWNER, employer,
            {"split": [{"roles": [str(role.id)], "new_name": name, "reasons": ["Their postings are on another site"]}]})
    assert refused == []
    assert [s["new_name"] for s in kept["split"]] == [name]


async def test_a_name_the_researcher_proposes_makes_the_new_record_with_the_whole_name_when_the_owner_says_split(js):
    """A name too long to be a domain label is not made by code, so the question here is one raised directly, as an
    earlier release did, and answered."""
    name = _name("umbrella")
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Initrode")
        role = make_role(employer, "Lead A")
        session.add(role)
        await session.flush()
        asked = await inbox.raise_task(
            session, OWNER, key=employer_pairs.split_key(employer.id, [role.id], name), kind=employer_pairs.KIND_SPLIT,
            title="move?", options=[{"id": employer_pairs.SPLIT, "label": "Split them out", "plan": {
                "from": str(employer.id), "roles": [str(role.id)], "names": [], "new_name": name}}])
        await employer_pairs.apply_answer(session, OWNER, asked, employer_pairs.SPLIT)
        await session.commit()
    assert sorted(e.name for e in await _all()) == sorted(["Initrode", name]), "the new record has the whole name"


async def test_a_new_record_is_made_with_the_whole_name_when_the_postings_sit_on_a_site_named_for_it(js):
    name = "umbrella" + "x" * 40
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Initrode")
        role = make_role(employer, "Lead A", apply_url=f"https://careers.{name}.test/jobs/1")
        session.add(role)
        await session.flush()
        done = await employer_pairs.propose_split(
            session, OWNER, employer.id, role_ids=[role.id], new_name=name,
            evidence=employer_pairs.Evidence(employer_pairs.SAME, ("Their postings are on another site",)),
            by=employer_research.BY)
        await session.commit()
    assert done.outcome == employer_pairs.SPLIT_DONE
    assert sorted(e.name for e in await _all()) == sorted(["Initrode", name]), "the new record has the whole name"


async def test_a_site_made_for_an_employer_carries_its_whole_name_as_its_label(js):
    name = _name("stark")
    async with db.session_factory()() as session:
        await make_employer(session, name, careers_url="https://careers.example.test/jobs")
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        [site] = (await session.scalars(select(Site).where(Site.owner == OWNER))).all()
        assert site.label == name and len(site.label) == LONG


async def test_a_rename_keeps_the_whole_name_and_the_key_find_or_make_makes_for_it(client):
    name = _name("wayne")
    async with db.session_factory()() as session:
        employer = await make_employer(session, "Oscorp")
        await session.commit()
        employer_id = employer.id
    res = await client.patch(f"/api/v1/employers/{employer_id}", json={"name": name})
    assert res.status_code == 200, res.text
    assert res.json()["name"] == name
    async with db.session_factory()() as session:
        renamed = await session.get(Employer, employer_id)
        assert renamed.name == name and renamed.key == key_of(name)
        found = await employers.find_or_make(session, OWNER, name)
        assert found is not None and found.id == uuid.UUID(str(employer_id))
