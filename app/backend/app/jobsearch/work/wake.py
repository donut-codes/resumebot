"""Knock on the scout's door when a search starts: the signed wake the planner runner already accepts
(`deploy/planner/planner.mjs`: HMAC-SHA256 over `<timestamp>.<body>`, headers `x-webhook-timestamp` and
`x-webhook-signature-v2`), with the mode `scout`. Best effort and never raises: with no wake configured, or the scout
down, its own check-in timer finds the open search (`jobs.profile` says whether one is open)."""

import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from app import db
from app.settings import settings

log = logging.getLogger(__name__)

#: The reasons a wake carries, each the kind of work the scout is knocked for (the runner picks its session by it,
#: `app/scout/scout.mjs`). `site_project` is a site project's task waiting for the scout (a plan to write, an item to
#: do, an act to run); its `project_id` is the project's. Sent with retries by `app.sites.wake_ops`. `parse_asked` is a
#: posting the owner pasted, waiting to be split into a role's fields; its `project_id` is the parse job's.
#: `check_asked` is a check of the owner's role tabs (Check my roles, `jobs/check.py`), waiting for a small session;
#: its `project_id` is the check job's.
#: `gaps_asked` is a Find Gaps run, or a line to draft from the owner's words (`gaps/runs.py`, `gaps/drafts.py`),
#: waiting for a session; its `project_id` is the run's or the line's.
#: `employers_asked` is an employer waiting for the employer researcher (`research/employer.py`, the Research employers
#: mission); its `project_id` is the first employer's.
SEARCH_STARTED, PACKET_ASKED, ANALYSIS_ASKED, SITE_PROJECT, TAG_ROLES, PARSE_ASKED, CHECK_ASKED, GAPS_ASKED = (
    "search_started", "packet_asked", "analysis_asked", "site_project", "tag_roles", "parse_asked", "check_asked",
    "gaps_asked")
#: `coach_asked` is the prep of an interview waiting for the Coach (`coach.py`); its `project_id` is the prep's.
EMPLOYERS_ASKED = "employers_asked"
#: `mail_asked` is mail waiting for the mail reader (`mail/haiku.py`, the Read the mail mission).
MAIL_ASKED = "mail_asked"
COACH_ASKED = "coach_asked"
#: The owner's Run now: what it queued (or the browser work it asked for) is waiting for the session pool. The lanes
#: look for work only every five minutes, so the first lane is knocked and asks the app at once.
RUN_NOW = "run_now"


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()


async def knock(run_id: str, reason: str = SEARCH_STARTED) -> bool:
    """True when the scout accepted the wake. `run_id` is the id of what the wake is for (a search run, a packet, a
    role, a site project). Sent once the caller's work is committed, never inside a transaction
    (`db.outside_transaction`, which the tests turn into an error even when no wake address is set)."""
    db.outside_transaction(f"a wake of the scout ({reason})")
    if not settings.wake_url:
        return False
    try:
        secret = Path(settings.wake_secret_file).read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("no wake secret to sign with; the scout will find the search on its next check-in")
        return False
    body = json.dumps({"mode": "scout", "reason": reason, "project_id": run_id}).encode("utf-8")
    stamp = str(int(time.time()))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                settings.wake_url, content=body,
                headers={"Content-Type": "application/json", "x-webhook-timestamp": stamp,
                         "x-webhook-signature-v2": sign(secret, stamp, body)},
            )
        return res.status_code == 202
    except httpx.HTTPError as exc:
        log.warning("couldn't wake the scout: %s", type(exc).__name__)
        return False


#: The reason of a command (`command`): the dispatcher claimed an item for the runner and tells it to start a session.
DISPATCH = "dispatch"


def runner_url(runner: str) -> str | None:
    """Where a pool runner (`session-<n>`) takes its command: the wake address is the first session's, and the lanes
    listen on consecutive ports (`scout/lanes-local.mjs`). None for a runner that is no session of the pool, or with no
    wake address set."""
    named = re.fullmatch(r"session-(\d+)", runner or "")
    if not settings.wake_url or named is None:
        return None
    parts = urlsplit(settings.wake_url)
    try:
        port = (parts.port or 80) + int(named[1]) - 1
    except ValueError:
        return None
    host = parts.hostname or ""
    return urlunsplit(parts._replace(netloc=f"[{host}]:{port}" if ":" in host else f"{host}:{port}"))


async def command(runner: str, kind: str, claim: str, item: dict, model: str | None = None) -> bool:
    """Tell a waiting runner to start a session for the item the app claimed for it (`dispatcher.py`): the same signed
    wake the runner already takes, carrying the claim, the kind and the item's ids. True only when the runner accepted
    it (202): a runner that is busy, held at Claude's usage limit or down says so, and the claim is given back. Never
    raises; sent once the caller's transaction is committed (`db.outside_transaction`)."""
    db.outside_transaction(f"a command to the {runner} lane ({kind})")
    url = runner_url(runner)
    if url is None:
        return False
    try:
        secret = Path(settings.wake_secret_file).read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("no wake secret to sign with; the %s lane finds its work on its next check-in", runner)
        return False
    body = json.dumps({"mode": "scout", "reason": DISPATCH, "project_id": claim, "claim": claim, "kind": kind,
                       "item": item, **({"model": model} if model else {})}).encode("utf-8")
    stamp = str(int(time.time()))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                url, content=body,
                headers={"Content-Type": "application/json", "x-webhook-timestamp": stamp,
                         "x-webhook-signature-v2": sign(secret, stamp, body)})
        return res.status_code == 202
    except httpx.HTTPError as exc:
        log.warning("couldn't command the %s lane: %s", runner, type(exc).__name__)
        return False
