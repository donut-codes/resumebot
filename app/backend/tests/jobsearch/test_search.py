"""One search across every role, whatever its status, and the postings that are no role yet (search.py,
GET /api/v1/search). Synthetic roles and postings only."""

import uuid
from datetime import UTC, date, datetime, timedelta

from app import db
from app.jobsearch import ats, stages
from app.jobsearch.models import ADDED_LINK, JobPosting, Opportunity
from tests.support import EXAMPLE_SYSTEM, OWNER


def _role(slug, status, company="Contoso", title="Data Engineer", **extra):
    return Opportunity(owner=OWNER, slug=slug, company=company, title=title, status=status, origin="scout",
                       match_key=f"{slug}|{title}".lower(), found=date.today(), **extra)


def _posting(n, title, company="Contoso", **extra):
    now = datetime.now(UTC)
    extra.setdefault("ats", EXAMPLE_SYSTEM)
    return JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ext_id=f"p{n}", company=company,
                      title=title, match_key=f"{company}|{title}|{n}".lower(), location="Remote",
                      url=f"https://jobs.example.test/{n}", first_seen=now - timedelta(hours=n), last_seen=now,
                      links=[], **extra)


async def _add(*rows):
    async with db.session_factory()() as session:
        session.add_all(rows)
        await session.commit()


async def test_a_search_finds_roles_in_every_status_and_postings_that_are_no_role_yet(client):
    made = _role("made", stages.APPLIED, title="Data Engineer II")
    await _add(
        _role("deck", stages.SECOND_IMPRESSION),
        _role("gated", stages.GATED_OUT, screened_out_reason="Onsite only, you work remote"),
        _role("ended", stages.WITHDREW, ended_reason=stages.WRONG_LEVEL),
        _role("other", stages.FIRST_IMPRESSION, company="Fabrikam", title="Product Manager"),
        made,
        _posting(1, "Data Engineer", filtered_at=datetime.now(UTC), filtered_by="Title says intern"),
        _posting(2, "Senior Data Engineer"),
        _posting(3, "Data Engineer", closed_at=datetime.now(UTC)),  # gone from its board: not searched
        _posting(4, "Data Engineer Lead", company=ADDED_LINK, ats=ats.OWNER, filtered_at=datetime.now(UTC),
                 route="roles"),
    )
    async with db.session_factory()() as session:  # a posting that became a role is found as the role, not twice
        session.add(_posting(5, "Data Engineer II", opportunity_id=made.id))
        await session.commit()

    found = (await client.get("/api/v1/search", params={"q": "data engineer"})).json()
    assert found["roles"] == 4 and found["postings"] == 3 and found["total"] == 7
    rows = {(r["kind"], r["status"]): r for r in found["items"]}
    gated = rows[("role", stages.GATED_OUT)]
    assert gated["lane"] == "closed" and gated["closed_label"] == "Gated out"
    assert gated["reason"] == "Onsite only, you work remote"
    ended = rows[("role", stages.WITHDREW)]
    assert ended["closed_reason"] == "wrong_level" and ended["reason"] == "Wrong level"
    assert rows[("role", stages.SECOND_IMPRESSION)]["label"] == "Second Impression"
    assert rows[("posting", "stopped")]["reason"] == "Title says intern"
    assert rows[("posting", "not_a_role_yet")]["title"] == "Senior Data Engineer"
    assert rows[("posting", "added_links")]["lane"] == "added_links"
    assert [r["kind"] for r in found["items"]] == ["role"] * 4 + ["posting"] * 3, "roles first, then postings"
    assert found["stages"] == {stages.SECOND_IMPRESSION: 1, stages.GATED_OUT: 1, stages.WITHDREW: 1,
                               stages.APPLIED: 1, "added_links": 1, "stopped": 1, "not_a_role_yet": 1}
    assert found["closed"] == 2
    assert set(found["items"][0]) == {"kind", "id", "title", "company", "where", "status", "label", "lane",
                                      "closed_reason", "closed_label", "reason", "favorite", "fit_pct", "found_at",
                                      "url"}


async def test_every_word_must_match_somewhere_in_any_case_the_status_and_the_reason_included(client):
    await _add(
        _role("a", stages.GATED_OUT, company="Northwind", screened_out_reason="Pay below your floor"),
        _role("b", stages.FIRST_IMPRESSION, company="Northwind"),
        _role("c", stages.REJECTED, company="Tailspin", ended_reason=stages.THEY_SAID_NO),
    )

    async def titles(q):
        return sorted(r["company"] + "/" + r["status"] for r in
                      (await client.get("/api/v1/search", params={"q": q})).json()["items"])

    assert await titles("NORTHWIND engineer") == ["Northwind/first_impression", "Northwind/gated_out"]
    assert await titles("northwind floor") == ["Northwind/gated_out"], "a gate's reason is searched"
    assert await titles("gated") == ["Northwind/gated_out"], "the status's name is searched"
    assert await titles("closed") == ["Northwind/gated_out", "Tailspin/rejected"], "Closed names every closed status"
    assert await titles("said no") == ["Tailspin/rejected"], "an ended role's reason in words"
    assert await titles("nothing like this") == []
    empty = (await client.get("/api/v1/search", params={"q": "   "})).json()
    assert empty["total"] == 0 and empty["items"] == []


async def test_a_search_is_paged_across_the_roles_and_then_the_postings(client):
    await _add(*[_role(f"r{i}", stages.FIRST_IMPRESSION, title=f"Analyst {i}") for i in range(3)],
               *[_posting(i, f"Analyst posting {i}") for i in range(1, 4)])
    first = (await client.get("/api/v1/search", params={"q": "analyst", "size": 4})).json()
    second = (await client.get("/api/v1/search", params={"q": "analyst", "size": 4, "page": 2})).json()
    assert first["pages"] == 2 and first["total"] == 6
    assert [r["kind"] for r in first["items"]] == ["role", "role", "role", "posting"]
    assert [r["kind"] for r in second["items"]] == ["posting", "posting"]
    ids = [r["id"] for r in first["items"] + second["items"]]
    assert len(set(ids)) == 6, "no row twice across the pages"
    assert second["items"][0]["title"] == "Analyst posting 2", "postings newest first"


async def test_the_posting_counts_come_in_one_statement_with_the_same_totals(client):
    from tests.support import bound_arguments

    now = datetime.now(UTC)
    await _add(_role("a", stages.FIRST_IMPRESSION, title="Analyst A"), _role("b", stages.APPLIED, title="Analyst B"),
               _posting(1, "Analyst plain"),
               _posting(2, "Analyst stopped", filtered_at=now, filtered_by="Title says intern"),
               _posting(3, "Analyst added", company=ADDED_LINK, ats=ats.OWNER, filtered_at=now, route="roles"))
    with bound_arguments() as seen:
        found = (await client.get("/api/v1/search", params={"q": "analyst"})).json()
    assert found["roles"] == 2 and found["postings"] == 3 and found["total"] == 5
    assert found["stages"] == {stages.FIRST_IMPRESSION: 1, stages.APPLIED: 1, "added_links": 1, "stopped": 1,
                               "not_a_role_yet": 1}
    # one search was 8 statements with three posting counts; one count statement makes it 6
    assert len(seen) <= 6, f"{len(seen)} statements for one search"
