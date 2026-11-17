"""The watch list: which changes to which models post which to-do.

It is data, read by one `before_flush` listener (`hook.py`) that posts for every ordinary save, present and future. Each
watched model says what its creation posts, which of its columns post when their value changes (`Rule`), what its
deletion posts, and which columns post nothing (`ignore`). **Every column of every watched model is on a rule or on the
ignore list**: a test fails on a column that is on neither, so a new column cannot be forgotten.

A to-do says where to look, never what to do, and a look that finds nothing is harmless; so a column is on a rule when
any loop reads it, and on the ignore list only when none does (a heartbeat, a note the owner reads, a column written
only together with one that already posts).

The writes this cannot see (a Core `update`, `insert` or `delete`) post beside the statement (`actions.store`,
`rank.keep`, ...); `tests/jobsearch/work_queue/direct_writes_baseline.json` lists each with the topic it posts or why it
is exempt, and the scan test keeps that list true."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from operator import attrgetter
from typing import Any

from process_engine.models import ProcWait
from sqlalchemy import inspect
from sqlalchemy.orm.base import PASSIVE_NO_INITIALIZE

from app.jobsearch import stages
from app.jobsearch.models import (
    BrowserDomain,
    BrowserItem,
    CheckJob,
    Employer,
    GapCoachJob,
    GapDraft,
    GapRun,
    HandOut,
    InstructionFile,
    InterviewPrep,
    JobPosting,
    MailMessage,
    Mission,
    Opportunity,
    ParseJob,
    ProfileVersion,
    RoleTagJob,
    SearchRun,
)
from app.jobsearch.work.queue import topics as t
from app.models.agent import AgentConnection, AgentKindPause, AgentRunner
from app.models.work import TrailEvent
from app.settings import settings
from app.sites.models import PlanTask, Project, Site, SiteRecipe, SiteRecording, SiteSkill

#: A role in the deck is read by the employer limit (First Impression to Ready to Launch) and, from Second Impression
#: on,
#: has its employer researched; an application (`classifier.REACHED`) is what the limit counts.
APPLICATIONS = (stages.APPLIED, stages.ACTIVE_DISCUSSION)
LIMITED = (*APPLICATIONS, stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)
ON_DECK = (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH)
#: The stages whose roles have their employer researched by the pool (`research.employer.picks`: a test pins it to
#: `employer.WORTH_RESEARCH`); Second Impression is also where `line.startable` takes a role for the analyst.
WORTH_RESEARCH = (stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH, *APPLICATIONS)
#: The stages whose roles with no text of their posting the browser's queue sources and reads
#: (`postings.unbuilt.STAGES`, the owner, 2026-11-05: a test pins it): a role coming into one, or made in one, may be
#: new work for it.
BUILT_OUT = (stages.APPLIED, stages.ACTIVE_DISCUSSION)


class Change:
    """What a save did to one object's columns, from the session's own history of it."""

    def __init__(self, obj: Any) -> None:
        self.obj = obj
        self.state = inspect(obj)

    def changed(self, column: str) -> bool:
        return (column in self.state.committed_state
                and self.state.get_history(column, PASSIVE_NO_INITIALIZE).has_changes())

    def old(self, column: str) -> Any:
        """The value the column had, or None when it was not loaded (a post that depends on it goes the safe way)."""
        deleted = self.state.get_history(column, PASSIVE_NO_INITIALIZE).deleted
        return deleted[0] if deleted else None

    def new(self, column: str) -> Any:
        return getattr(self.obj, column)


class Created(Change):
    """A new object: nothing was there before. Also the shape of a row about to be inserted by a direct statement (any
    object with the columns as attributes), so a role made either way posts the same."""

    def __init__(self, obj: Any) -> None:
        self.obj = obj
        self.state = None

    def changed(self, column: str) -> bool:
        return True

    def old(self, column: str) -> Any:
        return None


Keys = Callable[[Any, Change], Iterable[object]]
Test = Callable[[Any, Change], bool]


@dataclass(frozen=True)
class Post:
    """One to-do a change posts: its topic (a name, or a function of the object for `ready.<kind>`), the keys (where to
    look), whether it applies to this change, and how long to hold it back."""

    topic: str | Callable[[Any], str | None]
    key: Keys = lambda o, c: [""]
    when: Test | None = None
    after: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class Rule:
    """When any of `columns` changes value, post `posts`."""

    columns: tuple[str, ...]
    posts: tuple[Post, ...]
    when: Test | None = None


@dataclass(frozen=True)
class Watch:
    """A watched model: what its creation posts, its column rules, what its deletion posts, and the columns that post
    nothing."""

    model: type
    created: tuple[Post, ...] = ()
    rules: tuple[Rule, ...] = ()
    deleted: tuple[Post, ...] = ()
    ignore: frozenset[str] = frozenset()
    owner: Callable[[Any], str] = attrgetter("owner")

    def watched_columns(self) -> set[str]:
        return {c for rule in self.rules for c in rule.columns}


# --- keys -------------------------------------------------------------------------------------------------------------


def _id(o: Any, c: Change) -> list[object]:
    return [str(o.id)]


def _whole(o: Any, c: Change) -> list[object]:
    return [""]


def _everything(o: Any, c: Change) -> list[object]:
    return [t.ANY]


def _employer(o: Any, c: Change) -> list[object]:
    return [str(o.employer_id)] if o.employer_id else []


def _employer_old_and_new(o: Any, c: Change) -> list[object]:
    return [str(e) for e in dict.fromkeys((c.old("employer_id"), o.employer_id)) if e]


def _employer_itself(o: Any, c: Change) -> list[object]:
    return [str(o.id)]


def _role_of_posting(o: Any, c: Change) -> list[object]:
    return [str(o.opportunity_id)] if o.opportunity_id else []


def _site(o: Any, c: Change) -> list[object]:
    return [f"site:{o.id}"]


def _site_of(o: Any, c: Change) -> list[object]:
    return [f"site:{o.site_id}"] if getattr(o, "site_id", None) else [t.ANY]


def _link(o: Any, c: Change) -> list[object]:
    return [str(o.link_id)] if o.link_id else []


def _kind_key(o: Any, c: Change) -> list[object]:
    return [o.kind]


def _runner(o: Any, c: Change) -> list[object]:
    return [o.name]


# --- tests ------------------------------------------------------------------------------------------------------------


def _became(column: str, value: object) -> Test:
    return lambda o, c: getattr(o, column) == value and (c.old(column) != value or isinstance(c, Created))


def _is(column: str, *values: object) -> Test:
    return lambda o, c: getattr(o, column) in values


def _set_or_cleared(column: str) -> Test:
    """The column went from none to a value or back, not from one value to a later one (a claim's renewal)."""
    return lambda o, c: (c.old(column) is None) != (getattr(o, column) is None)


def _entered_or_left(column: str, statuses: tuple[str, ...]) -> Test:
    return lambda o, c: getattr(o, column) in statuses or c.old(column) in statuses


def _entered(column: str, statuses: tuple[str, ...]) -> Test:
    return lambda o, c: getattr(o, column) in statuses


def _entered_from_outside(column: str, statuses: tuple[str, ...]) -> Test:
    """The column came into `statuses` from a value outside them (a role back in play), not from one inside them (a role
    moving on). An old value that was not loaded counts as outside: the post goes the safe way."""
    return lambda o, c: getattr(o, column) in statuses and c.old(column) not in statuses


def _either(*tests: Test) -> Test:
    return lambda o, c: any(test(o, c) for test in tests)


def _marked(o: Any, c: Change) -> bool:
    """A mark to read the role next or research it again was added to its analysis (`line.startable` reads them): a
    mark taken away is no new work, and an old value that was not loaded counts as having none."""
    from app.jobsearch.research import analysis, line

    now, was = o.analysis or {}, c.old("analysis") or {}
    return any(key in now and key not in was for key in (line.READ_NEXT, analysis.REANALYSE))


def _lookup_mark_changed(o: Any, c: Change) -> bool:
    """The mark that says a board role's employer posting is looked up next (`models.SOURCE_NEXT`) went on or came
    off its analysis: `models.wants_sourcing` reads it, so the browser's queue is looked at again. An old value that was
    not loaded counts as having none."""
    from app.jobsearch.models import SOURCE_NEXT

    return (SOURCE_NEXT in (o.analysis or {})) != (SOURCE_NEXT in (c.old("analysis") or {}))


def _not_gated(o: Any, c: Change) -> bool:
    return not o.gated


def _linked_to_a_role(o: Any, c: Change) -> bool:
    return o.opportunity_id is not None


def _open_with_no_role(posting: Any) -> bool:
    """A posting the board lists and the app reads (`is_open`) that no role was made from: what `sift` and `make_roles`
    work."""
    return posting.is_open and posting.opportunity_id is None


def _open_with_role(posting: Any) -> bool:
    """A posting of a role that the board still lists and the app still reads (`is_open`): the postings
    `index.fill_role_details` takes the text of, for a role in play. The text itself is not read here: a board's read
    loads the postings it knows with it deferred, and a look that finds nothing owed is harmless."""
    return posting.opportunity_id is not None and posting.is_open


def _thin(o: Any, c: Change) -> bool:
    """A role whose text is thin has its details read (`pool.queue_details`)."""
    from app.jobsearch.postings import cleansed

    return len((o.description or "").strip()) < cleansed.MIN_TEXT


def _status_limited(o: Any, c: Change) -> bool:
    return o.status in LIMITED


def _facts_pending(o: Any, c: Change) -> bool:
    return (o.gate_facts or {}).get("state") == "pending" and (c.old("gate_facts") or {}).get("state") != "pending"


def _waiting_unclaimed(o: Any, c: Change) -> bool:
    return o.analysis_state == "waiting" and o.analysis_claimed_at is None


def _just_named(o: Any, c: Change) -> bool:
    """A job the owner added by its link that has just been named (`reader._name_added`, the app's own read): the
    browser has nothing left to read of it."""
    from app.jobsearch.models import ADDED_LINK

    return c.old("company") == ADDED_LINK


def _added_by_link(o: Any, c: Change) -> bool:
    """A job the owner added by its link and nothing has named yet (`titles.unnamed`): the browser reads its page."""
    from app.jobsearch import ats
    from app.jobsearch.models import ADDED_LINK

    return o.ats == ats.OWNER and o.company == ADDED_LINK


def _left_the_work(o: Any, c: Change) -> bool:
    """A browser item that is over (done, dropped or stuck): the queue may owe it again, or no longer call for it."""
    return o.state in ("done", "dropped", "stuck")


def _a_scan_done(o: Any, c: Change) -> bool:
    return o.kind == "scan" and o.state == "done"


def _scan_of(o: Any, c: Change) -> list[object]:
    """A key of its own (one to-do is posted for each topic and key): a scan owed again is not the look owed now."""
    return [f"scan:{o.target}"]


def _scan_again(o: Any) -> timedelta:
    """A site is scanned again this long after its last scan (`browser.queue.SCAN_EVERY`): no write happens then, so the
    to-do written with the result is what brings the queue back to it."""
    from app.jobsearch.browser import queue as browser_queue

    return browser_queue.SCAN_EVERY


def _paused_cleared(o: Any, c: Change) -> bool:
    return not o.paused and c.old("paused") is not False


def _person_moved_a_role(o: Any, c: Change) -> bool:
    return o.event == "role_moved" and o.actor == "person" and o.link_id is not None


def _agent_wait(o: Any, c: Change) -> bool:
    return o.kind == "agent"


def _browser_topic(o: Any) -> str | None:
    """The browser item's kind of session, or none for the one the app's own code works (`record`)."""
    return t.browser_ready(o.kind)


def _waiting(o: Any, c: Change) -> bool:
    return o.state == "waiting"


def _lease_in(o: Any) -> timedelta | None:
    """How long until the claim's lease runs out: a to-do due then (`ready.<kind>` when the work is free again)."""
    if o.lease_until is None:
        return None
    wait = o.lease_until - datetime.now(UTC)
    return wait if wait > timedelta(0) else None


def _hand_out_topic(o: Any) -> str | None:
    return t.ready(o.kind) if o.kind in t.READY_KINDS else None


def _all_ready() -> tuple[Post, ...]:
    return tuple(Post(t.ready(k)) for k in t.READY_KINDS)


def _one_ready(kind: str, when: Test | None = None) -> Post:
    return Post(t.ready(kind), when=when)


SETTLE = (Post(t.ROLE_SETTLE, _id), Post(t.DECK_DIRTY))
RECONCILE_ROLE = Post(t.BROWSER_RECONCILE, _id)
DECK = Post(t.DECK_DIRTY)
#: The pool's pull reads it (the ranking, the deck's reads, the employers' research): a look at the pull.
POOL = Post(t.POOL_OWED)


# --- the watch list ---------------------------------------------------------------------------------------------------

WATCH: dict[type, Watch] = {}


def _watch(model: type, **kwargs: Any) -> None:
    WATCH[model] = Watch(model, **kwargs)


_watch(
    Opportunity,
    created=(
        *SETTLE,
        Post(t.ROLE_DETAILS, when=_thin),
        Post(t.EMPLOYER_LIMIT, _employer, when=_status_limited),
        Post(t.EMPLOYER_RESEARCH, _employer, when=_entered("status", ON_DECK)),
        Post(t.ready("analysis"), when=_waiting_unclaimed),
        Post(t.ready("titles"), when=_facts_pending),
        # A new role in the stages before the application, or one applied to or in talks (`BUILT_OUT`), may need its
        # posting found or read (`browser.queue._wanted`).
        Post(t.BROWSER_RECONCILE, _id, when=_entered("status", (*stages.PRE_APPLY, *BUILT_OUT))),
        # and may already have a current copy to rank: a listing copied before its role existed has one the moment the
        # role is made (`rank.unranked`; the copy's own post is only for a posting that has a role), and one that
        # went on deck is one the pool researches (`line.startable`, `employer.picks`).
        Post(t.POOL_OWED, when=_either(_entered("status", stages.PRE_READY), _entered("status", WORTH_RESEARCH))),
    ),
    rules=(
        Rule(("status",), (
            *SETTLE,
            Post(t.EMPLOYER_LIMIT, _employer, when=_entered_or_left("status", LIMITED)),
            # The browser's queue reads the stage inside the stages before the application too (a role in Cleansed
            # Data with no text is read in full, one in the Landing zone is not), so a move there asks for it.
            Post(t.BROWSER_RECONCILE, _id, when=_entered_or_left("status", stages.PRE_APPLY)),
            # and so does a role coming into Applied or Active Discussion with no text (`postings.unbuilt`): its
            # posting is looked for and read, the owner's own in talks.
            Post(t.BROWSER_RECONCILE, _id, when=_entered("status", BUILT_OUT)),
            Post(t.EMPLOYER_RESEARCH, _employer, when=_entered("status", ON_DECK)),
            # A role that comes into play is one the pull may rank (`rank.unranked` takes the roles before Ready to
            # Launch), and a role going on is one it may research.
            # or research: a role coming on deck (`line.startable`) or on to Ready to Launch (`employer.picks`) from
            # outside the stages that research its employer.
            Post(t.POOL_OWED, when=_either(_entered_from_outside("status", stages.PRE_READY),
                                           _entered_from_outside("status", WORTH_RESEARCH))),
            # A role back in play (a gate that no longer stops it, a move of the owner's, the limit giving it back)
            # may have text owed by its postings, which `fill_role_details` takes only for roles before Ready to Launch.
            Post(t.ROLE_DETAILS, when=_entered_from_outside("status", stages.PRE_READY)),
        )),
        # What the gate-facts fingerprint reads (`gate_facts.fingerprint`) and what the stage rules read of a role.
        Rule(("title", "company", "description", "description_at", "description_from", "location", "move_type", "comp",
              "comp_source"),
             (Post(t.ROLE_SETTLE, _id), DECK)),
        Rule(("gate_facts",), (Post(t.ROLE_SETTLE, _id), DECK, Post(t.ready("titles"), when=_facts_pending))),
        Rule(("target_role", "matched_by_version"), (Post(t.POOL_OWED, _id), DECK)),
        Rule(("sourced_at", "source_url", "source_kind", "url", "apply_url", "board_url"),
             (Post(t.ROLE_SETTLE, _id), RECONCILE_ROLE)),
        # The line of the analyst: a role in it is no browser read (`to_read_in_full`), and one taken or finished
        # frees the pull's one-ahead slot for the next role.
        Rule(("analysis_state",), (*SETTLE, Post(t.ready("analysis"), when=_waiting_unclaimed), RECONCILE_ROLE, POOL)),
        Rule(("analysis_claimed_at",), (*SETTLE, POOL), when=_set_or_cleared("analysis_claimed_at")),
        # A sourced role is one the pull may research (`analysis.picks`).
        Rule(("sourced_at",), (POOL,)),
        # What the deck decides a board role by (`deck._decide`): Sourced (it is read, not looked up), where it was
        # found (the host of its board link, else its own), and a note the browser left when it gave up looking (such a
        # role is out of the walk and rejoins when the note clears). Without these the deck re-decides on the next
        # dirty turn only.
        Rule(("sourced_at", "board_url", "url", "browser_note"), (DECK,)),
        # The mark the deck puts on a board role for its lookup is what the browser's queue sources it by.
        Rule(("analysis",), (RECONCILE_ROLE,), when=_lookup_mark_changed),
        # What the browser reads of a role: its text (`to_read_in_full` takes the roles that have none) and Go's check
        # that could not tell (`go_checks.browser_checks`).
        Rule(("description",), (RECONCILE_ROLE,)),
        Rule(("go_check", "go_checked_at"), (RECONCILE_ROLE,)),
        Rule(("favorited_at", "ready_asked_at"), (*SETTLE, RECONCILE_ROLE)),
        # The marks the deck puts in the analysis (read next, research again) are what `line.startable` takes.
        Rule(("analysis",), (POOL,), when=_marked),
        Rule(("fit", "fit_breakdown", "queue_rank", "gate_answers", "analysis", "posted", "analysed_at",
              "analysis_asked_at", "analysis_made_under", "packet_complete", "go_opened_at", "go_check", "go_skip"),
             (DECK,)),
        Rule(("employer_id",), (
            Post(t.EMPLOYER_LIMIT, _employer_old_and_new), Post(t.EMPLOYER_RESEARCH, _employer), DECK, RECONCILE_ROLE,
            POOL)),  # `employer.picks` reads which employer a role of the stages worth researching has
    ),
    ignore=frozenset({
        # identity and what a role is called by the tracker
        "id", "owner", "slug", "match_key", "created_at", "created_day", "found", "origin", "source_run",
        # the tracker's columns no loop reads
        "level", "effort", "variant", "sector", "board", "ats", "notes", "col_adjusted", "col_adjusted_comp",
        # written with the status or the re-gate that stops a role, and read by the screens
        "screened_out_reason", "screened_out_by", "screened_out_step", "screened_out_gate_id",
        "screened_out_hard_pass", "screened_out_criteria", "ended_reason", "landed_interview", "fit_evidence",
        # the analyst's and the browser's own bookkeeping
        "analysis_instance_id",
        # the stored order the lanes are read by, written by `stored_order.settle` alone (the triggers of migration 0151
        # name the roles to work out again): no loop reads it
        "queue_points", "queue_day", "queue_moment", "queue_until",
        # which pass matched the role: always written beside `target_role`, which has the rule above
        "matched_by",
    }),
)

_watch(
    JobPosting,
    created=(Post(t.POSTING_SIFT), Post(t.BROWSER_RECONCILE, when=_added_by_link)),
    rules=(
        Rule(("filtered_at",), (Post(t.POSTING_SIFT),), when=lambda o, c: o.filtered_at is None),
        Rule(("route", "filtered_by", "opportunity_id", "owner_override"), (Post(t.POSTING_ROLES),)),
        # A posting that became a role's (the filter's "already a role", `make_roles`, the scout's dedupe, a merge's
        # fold) may hold the text that role lacks: `fill_role_details` joins the posting to its role.
        Rule(("opportunity_id",), (Post(t.ROLE_DETAILS),), when=lambda o, c: _open_with_role(o)),
        # and may give that role a current copy to rank (`cleansed._CURRENT` joins the copies to the role by the link
        # alone): the filter's link, `make_roles`, the scout's dedupe and a merge's fold.
        Rule(("opportunity_id",), (POOL,), when=_linked_to_a_role),
        Rule(("text", "detail_at"), (Post(t.ROLE_SETTLE, _role_of_posting), Post(t.ROLE_DETAILS),
                                     Post(t.BROWSER_RECONCILE, _role_of_posting))),
        # What the browser reads of a posting: a role's own read text is no browser read (`to_read_in_full`), a
        # posting that became a role's is no job of the owner's alone, and one the app or the browser named is no
        # longer unnamed.
        Rule(("opportunity_id",), (Post(t.BROWSER_RECONCILE, _whole),)),
        Rule(("company",), (Post(t.BROWSER_RECONCILE, _whole),), when=_just_named),
        Rule(("url", "apply_url"), (Post(t.BROWSER_RECONCILE, _role_of_posting),)),
        # Closed or Not checked and open again: the old turn reads the posting again (`sift` takes it while it has no
        # filter, `make_roles` once it has passed), and a posting with no role has none for the browser to look at.
        Rule(("closed_at", "not_checked_at"), (
            Post(t.BROWSER_RECONCILE, _role_of_posting),
            Post(t.POSTING_SIFT, when=lambda o, c: _open_with_no_role(o) and o.filtered_at is None),
            Post(t.POSTING_ROLES, when=lambda o, c: _open_with_no_role(o)),
            # and the posting of a role that is open again may hold the text its role lacks
            Post(t.ROLE_DETAILS, when=lambda o, c: _open_with_role(o)),
        )),
    ),
    deleted=(Post(t.BROWSER_RECONCILE, _everything),),
    ignore=frozenset({
        "id", "owner", "employer_id", "ats", "ext_id", "match_key", "title", "location", "work_mode",
        "comp", "comp_min", "comp_max", "posted", "department", "first_seen", "seen_day", "last_seen", "links",
        # the copy a posting points at is written with the copy, whose writer posts (`raw_copies._copy_made`)
        "current_cleansed_id",
        # what the filter and the title check write as they judge a posting
        "fit_prob", "route_why", "target_role", "judged_by", "judged_by_version", "judged_at", "stop_gate",
        "stop_gate_id", "stop_hard_pass", "nearest_role",
    }),
)

_watch(
    Employer,
    created=(DECK,),
    rules=(
        Rule(("gated", "gated_reason", "gated_at", "group", "name", "key", "aliases", "local", "work_style",
              "ownership", "ai_native", "employees", "founded", "industry", "sectors", "headquarters", "offices",
              "events", "owner_fields"),
             (Post(t.EMPLOYER_RESEARCH, _employer_itself), DECK)),
        Rule(("research_state", "researched_at", "research_asked_at"),
             (Post(t.EMPLOYER_RESEARCH, _employer_itself), DECK,
              POOL)),
        # A research the researcher took frees the pull's one-ahead slot for the next employer (`queue_employers`).
        Rule(("research_claimed_at",), (POOL,)),
        # An employer the owner no longer gates may be one the pool researches (`employer.picks` skips a gated one).
        Rule(("gated",), (POOL,), when=_not_gated),
        Rule(("research_state",), (_one_ready("researcher", _is("research_state", "waiting")),)),
        Rule(("ats", "board", "board_url", "careers_url", "website", "check_directly"),
             (Post(t.BROWSER_RECONCILE, _everything),)),
    ),
    deleted=(Post(t.BROWSER_RECONCILE, _everything),),
    ignore=frozenset({
        "id", "owner", "created_at", "updated_at", "notes", "reviews_url", "sources",
        # what a read of its board leaves, and the researcher's claim and its report's own words
        "ats_found_by", "fetched_at", "fetch_ok", "fetch_note", "fetch_kind", "open_roles", "read_full_at",
        "read_asked", "research_instance_id", "research",
    }),
)

_watch(
    Site,
    created=(Post(t.BROWSER_RECONCILE, _site),),
    rules=(Rule(("url", "kind", "on", "employer_id", "list_name", "read_by", "read_found", "off_by", "relearn",
                 "portal_url", "learn_tries"), (Post(t.BROWSER_RECONCILE, _site),)),),
    deleted=(Post(t.BROWSER_RECONCILE, _everything),),
    ignore=frozenset({"id", "owner", "created_at", "label", "added_by", "turned_on_at", "read_note", "off_note",
                      "off_at", "learn_note", "learn_tried_at"}),
)
_watch(
    SiteSkill,
    created=(Post(t.BROWSER_RECONCILE, _site_of),),
    rules=(Rule(("retired_at",), (Post(t.BROWSER_RECONCILE, _site_of),)),),
    ignore=frozenset({"id", "owner", "site_id", "version", "recipe", "made_by_project_id", "created_at"}),
)
_watch(
    SiteRecipe,
    created=(Post(t.BROWSER_RECONCILE, _site_of),),
    rules=(Rule(("state", "retired_at", "health", "bad_nights"), (Post(t.BROWSER_RECONCILE, _site_of),)),),
    ignore=frozenset({"id", "owner", "site_id", "host", "version", "recipe", "recording_id", "created_at"}),
)
# A recording asked is a `record` item of the browser's queue (`recipes.record.wanted`) and one given up or made is
# not: otherwise the owner's ask would wait for the hour's sweep.
_watch(
    SiteRecording,
    created=(Post(t.BROWSER_RECONCILE, _whole),),
    rules=(Rule(("state",), (Post(t.BROWSER_RECONCILE, _whole),)),),
    ignore=frozenset({"id", "owner", "site_id", "host", "reason", "requested_at", "recorded_at", "url", "sha256",
                      "body", "responses", "note"}),
)
# A domain stopped for good ends its waiting items (`host_stops.end_waiting`, part of every refresh; the board's stop
# has an end, `stopped_until`, and ends none: its items wait for it). The pace the
# hand-out keeps (`next_page_at`, `cool_until`, `blocks`) and what the app's own fetch saw (`page_read`) are read by
# no refresh.
_watch(
    BrowserDomain,
    rules=(Rule(("stopped_at", "stopped_until"), (Post(t.BROWSER_RECONCILE, _whole),)),),
    ignore=frozenset({"id", "owner", "domain", "next_page_at", "cool_until", "blocks", "page_read", "page_read_at",
                      "stopped_for"}),
)


def _research_mission(o: Any, c: Change) -> bool:
    from app.jobsearch.missions import record

    return o.key == record.RESEARCH


# A mission's settings are read by the turn (the research mission's again-after days: `deck.employers_for_deck`) and by
# the pool's pull (both missions' targets and the days: `pool._employers_room`, `_research_room`,
# `_employers_to_research`), so a change is a look at the deck and the pool, and at the employers when it is the
# research mission's. It is not on the ignore list: an owner's change would otherwise wait up to an
# hour for the clock.
_watch(
    Mission,
    rules=(
        Rule(("state", "paused_until"), (Post(t.BROWSER_RECONCILE, _everything),)),
        Rule(("settings",), (DECK, Post(t.EMPLOYER_RESEARCH, _everything, when=_research_mission), Post(t.POOL_OWED))),
    ),
    ignore=frozenset({"id", "owner", "key", "name", "purpose", "playbook", "schedule", "checked_at", "created_at",
                      "updated_at"}),
)

# --- what a person or a rule saved ------------------------------------------------------------------------------------

_watch(
    TrailEvent,
    created=(Post(t.ROLE_SETTLE, _link, when=_person_moved_a_role), Post(t.DECK_DIRTY, when=_person_moved_a_role)),
    ignore=frozenset({"id", "owner", "at", "actor", "agent_slug", "who", "event", "sentence", "tier", "mark",
                      "link_kind", "link_id", "sent_run_id", "detail"}),
)
# A saved document asks the turn to look (`rules.changed` is not posted: no handler takes it yet, so it would stay due;
# it is added with its first handler), which the old loop did within 30 seconds of
# any save: the turn reads these documents in every round (`mark_for_reads` the versions a research was made under,
# `rebalance` the Settings, the gate facts the pay floor, `gate` the rules), and nothing else asks for a turn after one
# (the first turn after a save came from the hour's clock). The profile's documents are
# read by the roles' own steps as well (`role.settle`, the whole owner: one to-do, never one for each role); an
# instruction file is read only by the deck's (the analyst's file is one a research is made under).
_watch(
    ProfileVersion,
    created=(Post(t.ROLE_SETTLE, _everything), DECK, POOL),
    ignore=frozenset({"id", "owner", "kind", "version", "data", "saved_by", "why", "saved_at"}),
)
_watch(
    InstructionFile,
    created=(DECK,),
    ignore=frozenset({"id", "owner", "kind", "name", "version", "text", "shipped_sha", "saved_by", "why", "saved_at"}),
)

# --- the pool: who is idle, what is paused, what is waiting for a session ---------------------------------------------

_watch(
    AgentRunner,
    rules=(Rule(("state",), (Post(t.DISPATCH_IDLE, _runner),), when=_is("state", "idle")),),
    ignore=frozenset({"id", "owner", "name", "kind", "session_id", "since", "seen_at"}),
)
_watch(
    AgentConnection,
    rules=(Rule(("paused",), (Post(t.POOL_OWED), *_all_ready()), when=_paused_cleared),),
    ignore=frozenset({"id", "owner", "slug", "name", "description", "roles", "last_seen_at", "checked_in_at",
                      "session_kind", "declared", "declared_at", "usage_limit_until", "created_at"}),
)
_watch(
    AgentKindPause,
    created=(Post(t.POOL_OWED),),
    ignore=frozenset({"id", "owner", "kind", "paused_at"}),
)
_watch(
    SearchRun,
    created=(_one_ready("search"),),
    rules=(Rule(("status",), (_one_ready("search"),)),),
    ignore=frozenset({"id", "owner", "started_by", "focus", "started_at", "ended_at", "instance_id", "session_id",
                      "session_seen_at", "reported", "counts", "summary"}),
)
_watch(
    ParseJob,
    created=(_one_ready("parser"),),
    # A job taken or ended frees the pull's one-ahead slot, and a rank batch that ended may leave the next owed.
    rules=(Rule(("state",), (_one_ready("parser"), POOL)),),
    ignore=frozenset({"id", "owner", "kind", "role_id", "text", "created_at", "taken_at", "finished_at", "fields",
                      "error"}),
)
_watch(
    CheckJob,
    created=(_one_ready("checker"),),
    rules=(Rule(("state",), (_one_ready("checker"),)),),
    ignore=frozenset({"id", "owner", "scope", "text", "created_at", "taken_at", "touched_at", "finished_at", "looks",
                      "items", "answers", "meta", "result", "error"}),
)
_watch(
    GapRun,
    created=(_one_ready("gaps"),),
    rules=(Rule(("state",), (_one_ready("gaps"),)),),
    ignore=frozenset({"id", "owner", "role_id", "opportunity_id", "title", "kind", "again", "created_at", "taken_at",
                      "touched_at", "finished_at", "read", "total", "postings", "available", "themes",
                      "resume_version", "instructions_version", "instance_id", "error"}),
)
_watch(
    GapDraft,
    created=(_one_ready("gaps"),),
    rules=(Rule(("state",), (_one_ready("gaps"),)),),
    ignore=frozenset({"id", "owner", "run_id", "theme", "theme_kind", "words", "created_at", "taken_at", "finished_at",
                      "draft", "error"}),
)
_watch(
    GapCoachJob,
    created=(_one_ready("coach"),),
    rules=(Rule(("state",), (_one_ready("coach"),)),),
    ignore=frozenset({"id", "owner", "run_id", "mode", "items", "attempts", "created_at", "taken_at", "touched_at",
                      "finished_at", "instance_id", "dropped", "error"}),
)
_watch(
    RoleTagJob,
    created=(_one_ready("titles"),),
    rules=(Rule(("status",), (_one_ready("titles"),)),),
    ignore=frozenset({"id", "owner", "started_at", "ended_at", "counts"}),
)
_watch(
    InterviewPrep,
    created=(_one_ready("coach"),),
    rules=(Rule(("state",), (_one_ready("coach"),)),),
    ignore=frozenset({"id", "owner", "interview_id", "version", "snapshot", "report", "review", "error", "claims",
                      "review_claims", "attempts", "asked_at", "taken_at", "written_at", "review_taken_at",
                      "finished_at", "instance_id"}),
)
_watch(
    MailMessage,
    created=(_one_ready("mail", _waiting),),
    rules=(Rule(("state",), (_one_ready("mail"),)),),
    ignore=frozenset({"id", "owner", "message_key", "sender", "sender_key", "subject", "day", "kind", "decided_by",
                      "line", "outcome", "role_id", "trail_event_id", "employer_said", "title_said", "opening",
                      "details", "claimed_at", "claims", "read_at"}),
)
_watch(
    PlanTask,
    created=(_one_ready("site"),),
    rules=(Rule(("status",), (_one_ready("site"),)),),
    ignore=frozenset({"id", "owner", "project_id", "plan_id", "plan_version", "position", "item_key", "title",
                      "group_label", "action", "kind", "data", "mode", "outcome_reason", "context", "findings",
                      "evidence", "result", "superseded", "checked_out_by", "checked_out_at", "created_at",
                      "updated_at"}),
)
# A site project the owner resumes has work for a scout session again (`sites.tools.waiting_projects` reads the open
# projects), with no task written: `completion.resume_project` only clears the stop. A project's work is otherwise its
# tasks (`PlanTask`: made with the project, and with a plan's approval), a finish or a stop only takes work away and
# the priority only orders it. The reads check (`tests/jobsearch/work_queue/test_reads.py`) holds it.
_watch(
    Project,
    rules=(Rule(("stopped_at",), (_one_ready("site"),), when=lambda o, c: o.stopped_at is None),),
    ignore=frozenset({"id", "owner", "site_id", "title", "outcome", "created_by", "stop_reason", "finished_at",
                      "finish_summary", "resolution", "priority", "round_budget", "created_at", "updated_at"}),
)
_watch(
    HandOut,
    created=(Post(_hand_out_topic, after=_lease_in),),
    ignore=frozenset({"id", "owner", "kind", "items", "runner", "created_at", "lease_until", "session_id",
                      "taken_at"}),
)
_watch(
    BrowserItem,
    created=(Post(_browser_topic, when=_waiting),),
    rules=(
        Rule(("state", "next_at"), (Post(_browser_topic),), when=_waiting),
        # An item that is over: the queue may owe it again (the work is still called for) or no longer call for it, and
        # a scan comes due again `SCAN_EVERY` after it, when no write happens (a to-do written with the result).
        Rule(("state",), (Post(t.BROWSER_RECONCILE, _whole),), when=_left_the_work),
        Rule(("state",), (Post(t.BROWSER_RECONCILE, _scan_of, after=_scan_again),), when=_a_scan_done),
    ),
    ignore=frozenset({"id", "owner", "kind", "target", "role_id", "posting_id", "site", "link", "domain", "tries",
                      "last_result", "last_note", "priority", "order_at", "leased_by", "lease_until", "due_by",
                      "code_at", "code_only", "run_id", "done_at", "created_at"}),
)
_watch(
    ProcWait,
    created=(_one_ready("seat", _agent_wait),),
    ignore=frozenset({"id", "instance_id", "token_id", "element_id", "kind", "status", "payload", "due_at", "message",
                      "result", "created_at", "closed_at"}),
    owner=lambda o: settings.owner,
)

#: The tables whose direct statements (a Core `update`, `insert` or `delete`) must post or be exempt: every watched
#: model's, and the few the direct writers touch that no loop reads through the session.
WATCHED_TABLES: frozenset[str] = frozenset(
    {w.model.__tablename__ for w in WATCH.values()}  # type: ignore[attr-defined]
    | {"js_role_ranks", "js_open_tabs", "js_gap_cards", "js_action_runs", "task_handouts", "tasks", "notifications",
       "js_mail_logins", "js_queue_dirty"})


def topics_of(table: str) -> tuple[str, ...]:
    """What a write to `table` that nothing posted for is answered with in production (the tripwire's catch-all `*`):
    every topic the table's model can post, or, for a table nobody has classified, every topic there is."""
    posted: dict[str, None] = {}
    for watch in WATCH.values():
        if watch.model.__tablename__ != table:  # type: ignore[attr-defined]
            continue
        for post in (*watch.created, *watch.deleted, *(p for r in watch.rules for p in r.posts)):
            if isinstance(post.topic, str):
                posted[post.topic] = None
            elif post.topic is _hand_out_topic or post.topic is _browser_topic:
                posted.update({t.ready(k): None for k in t.READY_KINDS})
    return tuple(posted) or t.WRITE_DRIVEN


def posts_for(session: Any, watch: Watch, obj: Any, change: Change, posts: Iterable[Post], cause: str) -> None:
    """Post what `posts` say for `obj`'s change (the hook's step)."""
    from app.jobsearch.work.queue import service

    owner = watch.owner(obj)
    for post in posts:
        if post.when is not None and not post.when(obj, change):
            continue
        topic = post.topic(obj) if callable(post.topic) else post.topic
        if topic is None:
            continue
        keys = list(post.key(obj, change))
        if not keys:
            continue
        after = post.after(obj) if post.after is not None else None
        service.post(session, owner, topic, keys, after=after, cause=cause)
