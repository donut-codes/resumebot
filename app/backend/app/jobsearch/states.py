"""The words the job-search tables keep in their state columns, written once. Each is a StrEnum, so a member is
equal to the text stored and can be compared, stored and sent as it is; `models.one_of` turns the same list into the
database's CHECK, so a word that is not here cannot be stored."""

from enum import StrEnum


class SearchStatus(StrEnum):
    """A search run (`js_search_runs.status`): the scout has the task, it finished, or it was closed as failed."""

    OPEN = "open"
    DONE = "done"
    FAILED = "failed"


class StartedBy(StrEnum):
    """What started a search run (`js_search_runs.started_by`)."""

    SCHEDULE = "schedule"
    MANUAL = "manual"


class MissionState(StrEnum):
    """A mission (`js_missions.state`): runs on its schedule, or is paused."""

    ON = "on"
    PAUSED = "paused"


class RunOutcome(StrEnum):
    """How a mission's run ended (`js_mission_runs.outcome`); running until what it started ends."""

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PacketState(StrEnum):
    """A packet (`js_packets.status`): the writer is on it, it passed every check, the checks held it, the writer never
    sent it, or a newer version replaced it."""

    WRITING = "writing"
    READY = "ready"
    HELD = "held"
    REPLACED = "replaced"
    FAILED = "failed"


class TagJobStatus(StrEnum):
    """The one-off job that gives the older roles a target role (`js_role_tag_jobs.status`)."""

    OPEN = "open"
    DONE = "done"


class CheckState(StrEnum):
    """A dry run of the title check (`js_check_jobs.state`): waiting for a session, a session has it, done or failed."""

    WAITING = "waiting"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"


class CheckScope(StrEnum):
    """What a dry run of the title check judges (`js_check_jobs.scope`)."""

    PURSUED = "pursued"
    GATED = "gated"
    POSTING = "posting"


class ParseState(StrEnum):
    """A text given to be split into fields (`js_parse_jobs.state`): waiting for a session, a session is reading it,
    done or failed."""

    WAITING = "waiting"
    READING = "reading"
    DONE = "done"
    FAILED = "failed"


class ParseKind(StrEnum):
    """What the text of a parse job is (`js_parse_jobs.kind`)."""

    POSTING = "posting"
    RESUME = "resume"
    #: The master resume read once for its structured record (`structured_profile.py`).
    PROFILE = "profile"
    #: A posting's text read once for its structured record, in shadow (`posting_structure.py`).
    STRUCTURE = "structure"
    #: About 25 cleansed postings ranked against the short profile, in shadow (`rank.py`).
    RANK = "rank"
    #: A site's recorded traffic, trimmed, for a session to write a recipe from (`browser/recipes/record.py`).
    RECIPE = "recipe"
    #: The master resume rewritten as concise atomic claims, one Sonnet job per master version (`cleansed_master.py`).
    CLAIMS = "claims"


class FitsNoneKind(StrEnum):
    """What the owner called fine as it is (`js_fits_none_ok.kind`)."""

    ROLE = "role"
    POSTING = "posting"


class FetchKind(StrEnum):
    """What the last attempt to read or find an employer's job board concluded (`js_employers.fetch_kind`), beside the
    note that says it in words. A failed read is one of the first four (`postings.fault_kind`): the site asks not to be
    read, it changed the way it answers, the board is gone, or it was unreachable. The rest are states the code decides
    on: the read answered none of the roles it had, the careers page was not usable, the scout found no board."""

    REFUSED = "refused"
    SHAPE = "shape"
    GONE = "gone"
    NETWORK = "network"
    EMPTY = "empty"
    CAREERS_PAGE = "careers_page"
    BOARD_NOT_FOUND = "board_not_found"


class StopGate(StrEnum):
    """Which gate stopped a posting (`js_postings.stop_gate`) or a role (`js_opportunities.screened_out_by`), kept
    beside the sentence it gave (docs/PRD.md 39.12). The classifier learns from a stop about the kind of job (`KIND`,
    the title check's "not your kind of job"); a stop for the place, the pay, an employer or the age says nothing of
    it. There is no stop for a role's fit: it never gates. `RULE` is one of the owner's sentences (a reader's answer,
    or the analyst's classes), `SITE` a site the owner avoids, `OTHER` a stop made before gates were kept as data and
    not worked out again yet."""

    EMPLOYER = "employer"
    LOCATION = "location"
    COMPENSATION = "compensation"
    AGE = "age"
    KIND = "kind"
    RULE = "rule"
    SITE = "site"
    OTHER = "other"
    #: The limit of two open applications per employer (`employer_limit.py`), not a gate on the role itself.
    LIMIT = "limit"


#: The id of the line the employer's own gate is (`js_postings.stop_gate_id`, `js_opportunities.screened_out_gate_id`).
#: It is not a line of Hard pass: it is the owner's switch on an employer's page, or a name on the list of employers.
EMPLOYER_LINE = "employer"


class StopStep(StrEnum):
    """The step of the work that made a stop (`js_opportunities.screened_out_step`; a posting's is `judged_by`, which
    has the first two): a code gate, the title check, a reader's answer to a sentence rule, the analyst's answer to one
    after research (docs/PRD.md 39.12). `TRACKER` is the old tracker's own screening, read in by the import: it was not
    one of the app's steps, so no re-run moves the role."""

    GATE = "gate"
    TITLES = "titles"
    READER = "reader"
    TRACKER = "tracker"
    RESEARCH = "research"


class GapState(StrEnum):
    """A Find Gaps run or a drafted line (`js_gap_runs.state`, `js_gap_drafts.state`): waiting for a session, a session
    has it, done or failed."""

    WAITING = "waiting"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"


class GapKind(StrEnum):
    """What a Find Gaps run reads (`js_gap_runs.kind`): the stored postings of a target role, or postings an agent found
    on the public web for a role title that is not a target role."""

    ROLE = "role"
    NEW = "new"


class GapAnswer(StrEnum):
    """What the owner said of a theme (`js_gap_answers.answer`): they have not done it (a real gap), or it is not
    relevant (hidden for that role on later runs)."""

    NOT_DONE = "not_done"
    NOT_RELEVANT = "not_relevant"


class CardState(StrEnum):
    """A Find Gaps question card (`js_gap_cards.state`): waiting for the owner, answered in their words, skipped, or
    answered "not true" (the theme is remembered as one the owner has not done)."""

    OPEN = "open"
    ANSWERED = "answered"
    SKIPPED = "skipped"
    NOT_TRUE = "not_true"


class CoachMode(StrEnum):
    """What a Find Gaps Coach job is (`js_gap_coach_jobs.mode`): questions for a run's themes, or master lines proposed
    from the owner's answers."""

    ASK = "ask"
    PROPOSE = "propose"


class ProposalStatus(StrEnum):
    """Where a proposal is (`js_proposals.status`, docs/PRD.md 8.3's minimal shape)."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class BrowserKind(StrEnum):
    """`BrowserItem.kind`: the kinds of browser work."""

    SOURCE = "source"
    READ = "read"
    SCAN = "scan"
    MAP = "map"
    #: The app's own code loads a site's listings in its Chrome and keeps the traffic (`browser/recipes/record.py`); no
    #: session.
    RECORD = "record"
    #: The app's own code reads a board role's posting page in its Chrome and keeps the text for the stage, the gates
    #: and the rank (`browser/code.py` `listing_by_code`, docs/PRD.md 48.14); no session.
    LISTING = "listing"


class BrowserState(StrEnum):
    """`BrowserItem.state`."""

    WAITING = "waiting"
    LEASED = "leased"
    DONE = "done"
    STUCK = "stuck"
    DROPPED = "dropped"


class RoundKind(StrEnum):
    """What kind of round an interview is (`js_interviews.round_kind`, docs/PRD.md 15.2.1)."""

    RECRUITER_SCREEN = "recruiter_screen"
    HIRING_MANAGER = "hiring_manager"
    PANEL = "panel"
    EXECUTIVE = "executive"
    OTHER = "other"


class PrepState(StrEnum):
    """A prep of one interview (`js_interview_preps.state`, `coach.py`): waiting for a Coach session -> writing (a
    session took it) -> review_waiting (the report passed the code checks) -> reviewing (a second session took it) ->
    ready; or failed, or lapsed (the interview's time passed while it waited)."""

    WAITING = "waiting"
    WRITING = "writing"
    REVIEW_WAITING = "review_waiting"
    REVIEWING = "reviewing"
    READY = "ready"
    FAILED = "failed"
    LAPSED = "lapsed"


class TabKind(StrEnum):
    """Whose a tab in the app's Chrome is (`js_open_tabs.kind`, docs/PRD.md path 52): a browser session's, a code job's,
    or one Launch opened for the owner (kept open until its role leaves Ready to Launch)."""

    SESSION = "session"
    CODE = "code"
    GO = "go"
