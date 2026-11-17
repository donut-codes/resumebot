"""What marks a posting as stopped by the filter (docs/PRD.md 30.2): open, filtered, with a reason, and no role made
from it (`stopped_where`), and the words the Gated out column says when the title check stopped it."""

from app.jobsearch.models import JobPosting

#: What a posting the title check stopped says, in the Gated out column.
TITLE_DID_NOT_MATCH = "Title didn't match your role tabs"


def stopped_where(owner: str):
    """The postings that are stopped: open, filtered, with a reason, and not a role."""
    return (JobPosting.owner == owner, JobPosting.is_open, JobPosting.filtered_at.is_not(None),
            JobPosting.filtered_by.is_not(None), JobPosting.opportunity_id.is_(None))


_stopped = stopped_where
