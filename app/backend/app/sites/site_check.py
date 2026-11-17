"""The code Check for one tried item of a site project (docs/PRD.md 29.2): the `site.check` step of
`playbooks/sites/check_one_item.bpmn`. No model and no word from the scout: it runs the site's newest saved skill
through `polite.py` and counts what comes back.

On plan means all of these held:
- postings came back (at least one), unless the item says the site has none (its `result.expected_count` is 0);
- every posting has a title and a link;
- when the item states the number the page itself shows (`result.expected_count`, a field of the `site.read` kind),
  the count is within 10% of it or 2, whichever is larger;
- up to two sample links open (answer 200).

The gateway `Gw_found` reads `found.on_plan`, and the step's output is `found`, so the dict returned here is `found`:
`on_plan` (bool) and `difference` (the findings in the plan's words, empty when on plan, cut to 2,000 characters) are
what the process reads. `verdict` ("on_plan" | "off_plan"), `found` (a list of {what, detail}) and `count` ride along
for the check report. An item that already went off plan (`item.status` "off_plan") is off plan with its own reason
(`item.outcome_reason`), and nothing is fetched. The item arrives with how it ended and what it found
(`round_process.checks_facts`)."""

import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Refused
from app.sites.base import CHECK_DIFFERENCE_MAX_CHARS
from app.sites.models import Project
from app.sites.skills import TRY_SECONDS, SkillError, current_skill, parse_recipe, public_url, run_recipe

SAMPLE_LINKS = 2
#: The count may be off by this fraction of what the item expected, or by this many, whichever is larger.
COUNT_TOLERANCE = 0.10
COUNT_SLACK = 2


def _finding(what: str, detail: str) -> dict:
    return {"what": what, "detail": detail}


def _as_id(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def _site_id(session: AsyncSession, data: dict) -> uuid.UUID | None:
    """The site: `data["site"]`, else `data["project"]["site_id"]`, else the site of the project `data["project"]` names
    (the check file's data model gives the project as its id)."""
    site = data.get("site")
    if isinstance(site, dict):
        site = site.get("id")
    if _as_id(site):
        return _as_id(site)
    project = data.get("project")
    if isinstance(project, dict):
        if _as_id(project.get("site_id")):
            return _as_id(project["site_id"])
        project = project.get("id")
    pid = _as_id(project)
    if pid is None:
        return None
    row = await session.get(Project, pid)
    return row.site_id if row else None


def expected_count(data: dict) -> int | None:
    """The number of postings the page itself shows, which the item reports as `result.expected_count` (0 when the page
    says there are none), or None."""
    item = data.get("item") if isinstance(data.get("item"), dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    value = result.get("expected_count")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _tolerance(expected: int) -> float:
    return max(COUNT_SLACK, COUNT_TOLERANCE * expected)


def _result(findings: list[dict], count: int, **more: Any) -> dict:
    difference = "; ".join(f"{f['what']}: {f['detail']}" if f["detail"] else f["what"] for f in findings)
    on_plan = not findings
    return {
        "on_plan": on_plan, "difference": difference[:CHECK_DIFFERENCE_MAX_CHARS],
        "verdict": "on_plan" if on_plan else "off_plan", "found": findings, "count": count, **more,
    }


def _public_links(postings: list[dict]) -> list[str]:
    return [p["url"] for p in postings if p.get("url") and public_url(p["url"])]


def _samples(postings: list[dict]) -> list[str]:
    """Up to two links to open, taken from the start and the middle of the list."""
    links = list(dict.fromkeys(_public_links(postings)))
    if len(links) <= SAMPLE_LINKS:
        return links
    return [links[0], links[len(links) // 2]]


def _name(p: dict) -> str:
    return str(p.get("title") or p.get("url") or p.get("ext_id") or "a posting")[:60]


def _count(listing, expected: int | None) -> tuple[list[dict], int, list[str]]:
    findings: list[dict] = []
    postings = listing.postings
    count = len(postings)
    if count == 0:
        if expected != 0:
            detail = listing.note or "the skill read the site and found no postings"
            findings.append(_finding("Nothing came back", detail))
        return findings, 0, []
    if expected == 0:
        findings.append(_finding("Postings came back from a site the item says has none", f"{count} came back"))
    no_title = [p for p in postings if not p.get("title")]
    no_link = [p for p in postings if not p.get("url")]
    if no_title:
        findings.append(_finding("Postings with no title", f"{len(no_title)} of {count}"))
    if no_link:
        names = ", ".join(_name(p) for p in no_link[:3])
        findings.append(_finding("Postings with no link", f"{len(no_link)} of {count}, such as {names}"))
    if expected and abs(count - expected) > _tolerance(expected):
        more = " (the read was cut short)" if listing.truncated and count < expected else ""
        findings.append(_finding(
            "The number of postings is not what the item expected",
            f"{count} came back, {expected} were expected{more}"))
    return findings, count, _samples(postings)


async def _open_samples(fetch, links: list[str]) -> list[dict]:
    findings: list[dict] = []
    for link in links:
        try:
            reply = await fetch.get(link)
        except Refused as e:
            findings.append(_finding("A sample link was refused", f"{link}: {e}"))
            continue
        except httpx.HTTPError as e:
            findings.append(_finding("A sample link did not open", f"{link}: {type(e).__name__}"))
            continue
        if reply.status_code != 200:
            findings.append(_finding("A sample link did not open", f"{link} answered {reply.status_code}"))
    return findings


async def _read_site(recipe) -> tuple[Any, list[dict]]:
    """The network part of a check: the site read through `recipe` (its `Listing`), and what opening its sample links
    found. A refusal, a skill that no longer reads the site or a site that did not answer comes back in the listing's
    place, so the check says it the same way wherever the site was read."""
    fetch = polite.give_time(polite.make(), TRY_SECONDS)  # the check reads like a try: a long site is cut short
    try:
        try:
            listing = await run_recipe(fetch, recipe, keep_bad=True)
            return listing, await _open_samples(fetch, _samples(listing.postings))
        except (Refused, SkillError, httpx.HTTPError) as e:
            return e, []
    finally:
        await fetch.close()


def _ahead_key(site_id: uuid.UUID, version: Any) -> tuple:
    return "site_check", site_id, version


async def read_ahead(session: AsyncSession, project: Project) -> dict:
    """The read every check of `project`'s round makes (its site through its newest saved skill), made now with no
    transaction open, for `polite.read_ahead`: the checks run inside the round's run, which cannot end its transaction.
    `session` is released first (`db.release`: what it did so far is committed). Empty, with the session left as it
    is, when there is nothing to read (no site, no skill, a recipe the app cannot run) or it cannot be released."""
    if not db.can_release(session) or project.site_id is None:
        return {}
    skill = await current_skill(session, project.site_id)
    if skill is None:
        return {}
    try:
        recipe = parse_recipe(skill.recipe)
    except ValueError:
        return {}
    await db.release(session)
    return {_ahead_key(project.site_id, skill.version): await _read_site(recipe)}


async def site_check(session: AsyncSession, instance, data: dict) -> dict:
    """Check one tried item of a site project, in code. `data` is the check's data (`playbooks/sites/
    check_one_item.bpmn`): `owner`, the project's `site` (or `project`, whose site is looked up), the `item` and the
    check record `made`. Returns `found` (see the module's docstring)."""
    item = data.get("item") if isinstance(data.get("item"), dict) else {}
    if item.get("status") == "off_plan":
        reason = item.get("outcome_reason") or "It didn't go to plan"
        return _result([_finding(str(reason)[:CHECK_DIFFERENCE_MAX_CHARS], "")], 0)

    site_id = await _site_id(session, data)
    if site_id is None:
        return _result([_finding("Nothing came back", "the check does not say which site it is for")], 0)
    skill = await current_skill(session, site_id)
    if skill is None:
        return _result([_finding("Nothing came back", "the site has no saved skill yet")], 0)
    try:
        recipe = parse_recipe(skill.recipe)
    except ValueError as e:
        return _result([_finding("The saved skill is not a recipe the app can run", str(e))], 0)

    expected = expected_count(data)
    ready = polite.ahead(_ahead_key(site_id, skill.version))  # read before the round's run began (`read_ahead`)
    if not isinstance(ready, tuple):
        await db.release_if_free(session)  # called on its own: the site is read with no transaction open
        ready = await _read_site(recipe)
    listing, opened = ready
    if isinstance(listing, Refused):
        return _result([_finding("Nothing came back", f"the site asks not to be read: {listing}")], 0)
    if isinstance(listing, SkillError):
        return _result([_finding("Nothing came back", str(listing))], 0)
    if isinstance(listing, httpx.HTTPError):
        return _result([_finding("Nothing came back", f"the site did not answer ({type(listing).__name__})")], 0)
    findings, count, samples = _count(listing, expected)
    findings += opened
    return _result(findings, count, skill_version=skill.version, sampled=samples)
