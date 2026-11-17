"""Each of the owner's gates for one role, as the role page shows them (docs/PRD.md 30.2). They are the lines of the
one gate check (`gating/check.py`), so what the page shows is what stops a role. Pure: the role, the profile and an
employer
record in, a list of lines out."""

from app.jobsearch.fit.freshness import PostedDay
from app.jobsearch.gating import check as gating
from app.jobsearch.models import Employer
from app.jobsearch.models import Opportunity as Role


def gate_results(role: Role, profile, on, employer: Employer | None = None, copy=None,
                 posted: PostedDay | None = None) -> list[dict]:
    """Each of the owner's gates for this role: passed, failed, or not decided (the posting didn't say, the reader
    judges it, or nobody has found the fact yet), with the fact it rests on and who checks it. A gate passes until it
    is proven failed (docs/PRD.md 30.2), so a gate that can't tell never fails the role. The pay gate is judged on
    the ask worked out by the analyst once there is one, and a rule set to follow the analyst's classes on how many of
    the role's required items are in its class (unknown until the role is researched). `on` is the day the posting's age
    is counted to; `copy` is the role's cleansed copy, which the gates on stated facts read (`gating.role_subject`) and
    `posted` the day the role was posted (`freshness.posted_of`), counted as every gate counts it."""
    rules = gating.Rules.of(profile, today=on)
    return [line.as_dict() for line in gating.results(gating.role_subject(role, employer, copy, posted), rules)]
