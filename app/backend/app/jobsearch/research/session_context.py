"""What a session is told once, in its system prompt, instead of in every tool reply.

The analyst got the owner's stable material back in its first tool reply, every session: the master resume, the
owner's scoring and the response shape. Tool replies are written to Claude's prompt cache fresh each session, while the
system prompt is the cached prefix, so the same text sent there is read from the cache for a tenth of the price.

The runner asks `GET /api/v1/agent/context?kind=` when a session starts and appends the text to the session's system
prompt file (`scout.mjs`), then names the text's digest in the session's MCP configuration (`X-Context`). A tool leaves
the material out of its reply only when the session names the digest the app holds now (`held`); a session that could
not fetch it, or whose owner has edited it since, is sent the material in the reply as before. Nothing the owner wrote
is dropped, and `jobs.analysis` part `context` and `jobs.profile` full return it on demand.

Services flush, routes commit: nothing here writes.
"""

import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.views import scoring_for_agent
from app.jobsearch.resume import cleansed_master
from app.jobsearch.work import instruction_files

ANALYSIS = "analysis"
KINDS = (ANALYSIS,)


async def resume_part(session: AsyncSession, owner: str) -> dict:
    """The master resume as the analyst reads it (`jobs.analysis` part `resume`). Once the master's claims are made
    (`cleansed_master`), the line-by-line read (Second Impression) is given them first: the claims with their line
    ids and the full text of every line they cite, the whole master still after them."""
    docs = await profile_store.owner_documents(session, owner)
    r = docs.resume.model_dump(mode="json")
    out = {k: r[k] for k in ("summary", "achievements", "competencies", "roles", "education")}
    # A competency is not a line a requirement cites (`_master_ids` leaves its id out), so the analyst is not shown
    # the id: citing it refused the whole submission.
    out["competencies"] = [{k: v for k, v in c.items() if k != "id"} for c in out["competencies"]]
    master = await cleansed_master.second_impression_input(session, owner)
    if master is not None:
        return {"claims": master.text, "cited_lines": master.cited, **out}
    return out


async def parts(session: AsyncSession, owner: str, kind: str) -> dict:
    """The named pieces of a kind's stable material, in the order they are written to the system prompt."""
    if kind == ANALYSIS:
        profile = await profile_store.current(session, owner)
        return {
            "master_resume": await resume_part(session, owner),
            "scoring": {"sections": scoring_for_agent(profile.scoring, profile.criteria)},
            "response_template": await instruction_files.read(session, owner, "templates", "jobsearch.analysis"),
        }
    raise ValueError(f"no context for {kind!r}")


INTRO = {
    ANALYSIS: ("The owner's material for this role, read once. It is the same for every role you research: the master "
               "resume (what jobs.analysis part 'resume' returns, so do not call it), the owner's scoring and the "
               "response shape (what jobs.analysis returns as `scoring` and `response_template`). The role itself and "
               "its employer, gates and pay come from jobs.analysis."),
}


def render(kind: str, material: dict) -> str:
    """The system-prompt section: each piece under its name, JSON for data and the text as written for a template.
    Deterministic, so the prefix is the same from one session to the next until the owner edits something."""
    out = [f"# Owner context\n\n{INTRO[kind]}"]
    for name, value in material.items():
        body = value if isinstance(value, str) else "```json\n" + json.dumps(
            value, ensure_ascii=False, sort_keys=True, indent=1) + "\n```"
        out.append(f"## {name}\n\n{body}")
    return "\n\n".join(out)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def text(session: AsyncSession, owner: str, kind: str) -> str:
    return render(kind, await parts(session, owner, kind))


async def held(session: AsyncSession, owner: str, kind: str, named: str | None) -> bool:
    """Whether the session names the digest of what the app holds now: it has the material in its system prompt, so
    a tool reply may leave it out. False when it names none (a session that could not fetch it, or a caller of
    another kind), or an older one (the owner edited it since)."""
    return bool(named) and named == digest(await text(session, owner, kind))
