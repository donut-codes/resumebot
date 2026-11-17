"""A role's status: each one a step in the process or a handoff.

Followup needed (something must be read or read again) → Landing Zone (the raw copy is read) → Cleansed Data (made
plain; the hard gates on stated facts run) → First Impression (the Haiku rank) → Second Impression (Sonnet line by
line, the top N: "on deck") → Ready to Launch (the best N; packets are built on Go) → Applied (the employer's) →
Active Discussion; it ends Gated out, Rejected, Gone Quiet or Withdrew, which the board and the dashboard show as one
status, Closed, each with its reason. The old tracker's statuses map onto these once (`FROM_TRACKER`), on the way in
from a spreadsheet and in migrations 0006 and 0104.
"""

FOLLOWUP_NEEDED = "followup_needed"
LANDING_ZONE = "landing_zone"
CLEANSED_DATA = "cleansed_data"
FIRST_IMPRESSION = "first_impression"
SECOND_IMPRESSION = "second_impression"
READY_TO_LAUNCH = "ready_to_launch"
GATED_OUT = "gated_out"
APPLIED = "applied"
ACTIVE_DISCUSSION = "active_discussion"
REJECTED = "rejected"
GONE_QUIET = "gone_quiet"
WITHDREW = "withdrew"

#: The statuses before Ready to Launch, in the order of the process: a role here has no decision yet.
PRE_READY = (FOLLOWUP_NEEDED, LANDING_ZONE, CLEANSED_DATA, FIRST_IMPRESSION, SECOND_IMPRESSION)
#: Every status, in the order of the process.
STAGES = (*PRE_READY, READY_TO_LAUNCH, APPLIED, ACTIVE_DISCUSSION, REJECTED, GONE_QUIET, WITHDREW, GATED_OUT)
#: Where a role's story ends.
ENDED = (REJECTED, GONE_QUIET, WITHDREW)
#: Every status that is Closed (shown as one): a gate stopped it, or it ended.
CLOSED = (*ENDED, GATED_OUT)
#: Before the application: what a role with no packet needed yet, or a packet built, can be in.
PRE_APPLY = (*PRE_READY, READY_TO_LAUNCH)
#: Why an Ended role ended (docs/PRD.md 31.7), kept on the role (`Opportunity.ended_reason`) so the classifier learns
#: the right lesson from it: the employer said no, the owner passed on it, the posting came down, or no word came back
#: after they applied. Unknown when none can be told. Only "I passed" is a lesson about the kind of job; "they said no"
#: and "no word came back" say the owner pursued it (they applied), and "the posting went" says nothing about the job
#: at all.
THEY_SAID_NO = "they_said_no"
I_PASSED = "i_passed"
POSTING_GONE = "posting_gone"
NO_REPLY = "no_reply"
UNKNOWN_REASON = "unknown"
#: An outcome the owner records when an employer made an offer (a Closed reason, simplify-plan "Closed reasons").
RECEIVED_OFFER = "received_offer"
#: What the owner can say when they end a role by hand (the move box): the first five are about the kind of job (the
#: classifier learns from them as it does from `i_passed`, which they replaced in the box and which stays readable),
#: duplicate and something else say nothing about it.
NOT_MY_FIELD = "not_my_field"
WRONG_LEVEL = "wrong_level"
PAY_TOO_LOW = "pay_too_low"
LOCATION_OR_MODE = "location_or_mode"
THE_EMPLOYER = "the_employer"
DUPLICATE = "duplicate"
SOMETHING_ELSE = "something_else"
#: A rule's reason (`postings/noise.py`, the owner, 2026-11-13): the role sat in Followup needed and the app's code
#: could not get its original posting. Never the owner's to give, and never a lesson about the kind of job.
NOISE = "noise"
END_REASONS = (THEY_SAID_NO, I_PASSED, POSTING_GONE, NO_REPLY, UNKNOWN_REASON, RECEIVED_OFFER, NOT_MY_FIELD,
               WRONG_LEVEL, PAY_TOO_LOW, LOCATION_OR_MODE, THE_EMPLOYER, DUPLICATE, SOMETHING_ELSE, NOISE)
#: The reasons that are the owner passing on the kind of job.
PASSED_ON_REASONS = (I_PASSED, NOT_MY_FIELD, WRONG_LEVEL, PAY_TOO_LOW, LOCATION_OR_MODE, THE_EMPLOYER)
END_REASON_LABELS = {
    THEY_SAID_NO: "They said no", I_PASSED: "I passed on it", POSTING_GONE: "Expired",
    NO_REPLY: "No word came back", UNKNOWN_REASON: "Not known", RECEIVED_OFFER: "Received offer",
    NOT_MY_FIELD: "Not my field", WRONG_LEVEL: "Wrong level", PAY_TOO_LOW: "Pay too low",
    LOCATION_OR_MODE: "Location or work mode", THE_EMPLOYER: "The employer",
    DUPLICATE: "Duplicate or already applied", SOMETHING_ELSE: "Something else", NOISE: "Noise",
}
#: The same, as a phrase inside a sentence about the role ("You moved it to Withdrew (you passed on it).").
END_REASON_PHRASES = {
    THEY_SAID_NO: "they said no", I_PASSED: "you passed on it", POSTING_GONE: "the posting went",
    NO_REPLY: "no word came back", RECEIVED_OFFER: "you received an offer", NOT_MY_FIELD: "not my field",
    WRONG_LEVEL: "wrong level",
    PAY_TOO_LOW: "pay too low", LOCATION_OR_MODE: "location or work mode", THE_EMPLOYER: "the employer",
    DUPLICATE: "duplicate or already applied", SOMETHING_ELSE: "something else",
    NOISE: "noise: its original posting could not be read by code",
}
#: The reasons the owner is offered when they end a role by hand; none is needed (what is left is worked out, or
#: unknown).
#: `no_reply` is a rule's reason (`quiet.py`): the move box does not offer it, though the API takes it.
OWNER_END_REASONS = (NOT_MY_FIELD, WRONG_LEVEL, PAY_TOO_LOW, LOCATION_OR_MODE, THE_EMPLOYER, DUPLICATE, SOMETHING_ELSE,
                     THEY_SAID_NO, POSTING_GONE, RECEIVED_OFFER)

#: Still in play: what an employer's "open roles" counts.
OPEN = (*PRE_READY, READY_TO_LAUNCH, APPLIED, ACTIVE_DISCUSSION)

LABELS = {
    FOLLOWUP_NEEDED: "Followup needed", LANDING_ZONE: "Landing Zone", CLEANSED_DATA: "Cleansed Data",
    FIRST_IMPRESSION: "First Impression", SECOND_IMPRESSION: "Second Impression", READY_TO_LAUNCH: "Ready to Launch",
    GATED_OUT: "Gated out", APPLIED: "Applied", ACTIVE_DISCUSSION: "Active Discussion", REJECTED: "Rejected",
    GONE_QUIET: "Gone Quiet", WITHDREW: "Withdrew",
}

#: The board's lanes and the dashboard's strip: the statuses in play, the closed ones as one lane (an ended role or one
#: a gate stopped).
LANES = (
    *((s, (s,)) for s in (*PRE_READY, READY_TO_LAUNCH, APPLIED, ACTIVE_DISCUSSION)),
    ("closed", CLOSED),
)
LANE_LABELS = {**LABELS, "closed": "Closed", "ended": "Ended"}

#: The two groups the Pipeline page draws apart (docs/PRD.md 24.17, the owner, 2026-11-06), the same roles as their
#: lanes in another grouping: the roles still being got ready, which have no number yet (work in progress), and the
#: ranked list, First Impression, Second Impression and Ready to Launch as one list sorted by the one number (24.15),
#: each card saying its own stage. `GET /board?column=work` and `?column=ranked` give each as one column.
WORK, RANKED = "work", "ranked"
WORK_IN_PROGRESS = (FOLLOWUP_NEEDED, LANDING_ZONE, CLEANSED_DATA)
RANKED_LIST = (FIRST_IMPRESSION, SECOND_IMPRESSION, READY_TO_LAUNCH)
GROUPS = ((WORK, WORK_IN_PROGRESS), (RANKED, RANKED_LIST))
GROUP_LABELS = {WORK: "Work in progress", RANKED: "Ranked list"}

#: The old tracker's statuses (and the app's before path 23), as statuses.
FROM_TRACKER = {
    "new": LANDING_ZONE, "found": LANDING_ZONE, "needs_drafted": READY_TO_LAUNCH, "ready_to_apply": READY_TO_LAUNCH,
    "ready": READY_TO_LAUNCH, "applied": APPLIED, "interviewing": ACTIVE_DISCUSSION,
    "rejected": REJECTED, "dead": GONE_QUIET, "deferred": WITHDREW, "screened_out": GATED_OUT,
}


def default_end_reason(status: str | None) -> str | None:
    """The reason an Ended stage says by itself, when nothing more is known: Rejected is the employer saying no (the
    rejected-by-employer status of the old tracker and of email); the other ended stages could be any reason, so they
    are unknown until the owner says (a posting that closed is found by `ended.reason_for`). None for a stage that is
    not Ended."""
    if status not in ENDED:
        return None
    return THEY_SAID_NO if status == REJECTED else UNKNOWN_REASON


def from_tracker(status: str | None) -> str:
    """A spreadsheet's status (old or new words) as a stage; anything unknown is Landing Zone."""
    s = (status or "").strip().lower().replace(" ", "_")
    if s in STAGES:
        return s
    return FROM_TRACKER.get(s, LANDING_ZONE)
