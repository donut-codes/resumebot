"""Who last set an employer's watch (docs/PRD.md 29.9): the owner, or the app because the employer has a role in play.

The watch is the switch on the employer's own site (`Site.on`, which `Employer.check_directly` follows). It is the
owner's to set; with "Watch the job board of every employer with a role in play" on, the app sets it too. To tell the
two apart the one who last set it is kept with the employer's other facts, as the source of its `check_directly` fact
(`Employer.sources`, the way `employers.edit` keeps who set a fact): `owner` for the owner's own switch, `role in play`
for the app's. Nothing is kept for a watch nobody has set since this record began, which is the owner's: an employer
the owner watched before is never unwatched by the app.

A leaf module (it imports only the employer record), so the switch (`where.py`), the rule that uses it
(`auto_watch.py`) and the employer's page (`employers/core.py`) can each read it without importing one another.
"""

from datetime import UTC, datetime

from app.jobsearch.models import Employer

#: The employer fact the watch is, and so the key its source is kept under in `Employer.sources`.
FACT = "check_directly"
#: Who set it: the owner (`employers.edit` keeps the same word for a fact they set), or the app for a role in play.
BY_OWNER, BY_PLAY = "owner", "role in play"
#: The `source` the record gives for each, as a fact's source is worded everywhere else ("set by you").
SOURCE_WORDS = {BY_OWNER: "you", BY_PLAY: "a role in play"}


def note(employer: Employer, by: str) -> None:
    """Keep that `by` (`BY_OWNER` or `BY_PLAY`) set this employer's watch, now."""
    employer.sources = {**(employer.sources or {}), FACT: {
        "source": SOURCE_WORDS[by], "at": datetime.now(UTC).isoformat(), "by": by}}


def set_by(employer: Employer) -> str | None:
    """Who last set the employer's watch: `BY_PLAY` when the app did, `BY_OWNER` when anyone else did (the owner, or a
    record whose maker is not named), None when nothing has been kept."""
    entry = (employer.sources or {}).get(FACT)
    if not isinstance(entry, dict):
        return None
    return BY_PLAY if entry.get("by") == BY_PLAY else BY_OWNER


#: What the employer's page is told when its watch is on because of a role in play (`watched_for`).
FOR_PLAY = "in_play"


def watched_for(employer: Employer, watched: bool) -> str | None:
    """Why the employer is watched, for its page to say: `FOR_PLAY` while the watch is on and the app set it; None for
    a watch the owner set, or none."""
    return FOR_PLAY if watched and set_by(employer) == BY_PLAY else None
