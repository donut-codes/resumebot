"""The owner's profile as structure: what to look for (criteria), the hard rules (gates), how fit is scored (scoring)
and how the search runs (settings).

Each is a document the owner manages in the app, stored as versions (`profile/store.py`). Nothing here holds anyone's
preferences: a new install's documents are empty, every gate is off, and every role gets through. Each kind of gate
and ask is generic; the owner's values fill it in.

This module holds the criteria and the settings and puts the four documents together (`Profile`, `parse`); the rest is
split by job:
- `profile/base.py`: the kinds, `Part`, `Note` and the outline of a posting;
- `profile/gates.py`: the gates, and the sentence the page shows for each;
- `profile/scoring.py`: the asks and rows of scoring, and the sentence the page shows for each;
- `profile/views.py`: the profile as the screening process (`engine_gates`, into `gates.Gates`), the scout
  (`for_scout`) and the readers (`look_for`, `for_titles`) are handed it.
"""

import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.jobsearch import ats
from app.jobsearch.profile.base import KINDS, Note, Part
from app.jobsearch.profile.gates import GateSet
from app.jobsearch.profile.scoring import Ask, Group, Item, Scoring

# --- criteria: what to look for ----------------------------------------------------------------------------------


#: The most phrases a search plan holds: few enough that every one runs on every site that needs a typed search.
MAX_PLAN = 8

#: The most example titles one target role holds, and the most target roles: a role is one real job the owner wants,
#: and a posting is judged against it alone (docs/PRD.md 30.5). (A count of items. No text the owner writes has a
#: limit on its length: not a role's name, not a part, not the company.) The page says both counts where the titles
#: and roles are entered and offers no Add past them (components/profile/targetRoles.ts holds the same two numbers).
MAX_TITLES = 10
MAX_TARGET_ROLES = 20
#: The texts of a target role, in the order a posting reads (the page shows them under these headers).
ROLE_TEXT = ("description", "responsibilities", "qualifications", "preferred", "compensation")


class TargetRole(Part):
    """One job the owner is looking for (docs/PRD.md 30.5). A posting is judged against one role at a time, never a
    blend, and the role it matches is kept with it. `titles` are examples to judge against, not words a posting must
    carry; the texts are the parts of an ideal posting, in the owner's words."""

    name: str = ""
    titles: list[str] = []
    description: str = ""
    responsibilities: str = ""
    qualifications: str = ""
    preferred: str = ""
    #: What the owner wants in pay and benefits. The hard floor is the Compensation gate and the points are Scoring's.
    compensation: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator(*ROLE_TEXT)
    @classmethod
    def _part(cls, v: str) -> str:
        return v.strip()

    @field_validator("titles")
    @classmethod
    def _titles(cls, v: list[str]) -> list[str]:
        titles = [" ".join(t.split()) for t in v if t.strip()]
        if len(titles) > MAX_TITLES:
            raise ValueError(f"a target role holds at most {MAX_TITLES} example titles; it has {len(titles)}")
        return titles

    def says_something(self) -> bool:
        """Whether the role has a name, example titles or a description: what a title can be judged against."""
        return bool(self.name or self.titles or self.description)

    def is_blank(self) -> bool:
        """Whether the role says nothing at all yet (a tab the owner opened and left empty)."""
        return not (self.name or self.titles or any(getattr(self, f) for f in ROLE_TEXT))

    def texts(self) -> dict[str, str]:
        """The role's own texts, in the order a posting reads (`ROLE_TEXT`): each role writes its own, none is shared
        (docs/PRD.md 31.5)."""
        return {f: getattr(self, f) for f in ROLE_TEXT}


class Criteria(BaseModel):
    """What the filter reads to decide whether a posting is the owner's kind of job (docs/PRD.md 30.5): the kind of
    employer (`company`, one text for them all) and the target roles, one for each kind of job the owner is looking
    for, each with its own example titles and texts. A posting is judged against one role at a time and the role it
    matches is kept. It says what the owner wants, never a list of counterexamples. `positioning` and `notes` are
    shown on Search Settings under Also sent to agents (docs/PRD.md 30.11): the packet writer and the ranking read the
    first, the search agent is sent the second as written. `search_plan` is stored and not shown: the browser reader
    once read it (it searches each role's titles now)."""

    model_config = ConfigDict(extra="forbid")
    #: How the owner presents themselves: the packet writer and the ranking read it. Shown, and edited, under Also sent
    #: to agents (PRD 30.11).
    positioning: str = ""
    #: The phrases once typed into a site that needs a typed search. Kept as stored and no longer shown or read: the
    #: browser reader searches the target roles' example titles (PRD 30.8).
    search_plan: list[str] = []
    #: What kind of employer the owner wants: industry, size, how it is run.
    company: str = ""
    target_roles: list[TargetRole] = []
    notes: list[Note] = []

    @field_validator("company")
    @classmethod
    def _company(cls, v: str) -> str:
        return v.strip()

    @field_validator("search_plan")
    @classmethod
    def _plan(cls, v: list[str]) -> list[str]:
        phrases = [p.strip() for p in v if p.strip()]
        if len(phrases) > MAX_PLAN:
            raise ValueError(f"a search plan holds at most {MAX_PLAN} phrases, so every one runs on each site")
        return phrases

    @model_validator(mode="after")
    def _roles(self):
        if len(self.target_roles) > MAX_TARGET_ROLES:
            raise ValueError(f"target_roles holds at most {MAX_TARGET_ROLES} roles; it has {len(self.target_roles)}")
        ids = [r.id for r in self.target_roles]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise ValueError(f"target role ids must be unique; repeated: {', '.join(repeated)}")
        return self

    def role(self, role_id: str | None) -> TargetRole | None:
        """The target role with this id, or None."""
        return next((r for r in self.target_roles if r.id == role_id), None) if role_id else None

    def search_phrases(self) -> list[str]:
        """The phrases a site that needs a typed search is searched with, in the owner's browser (docs/PRD.md 30.8):
        each target role's example titles (a role with none is searched by its name), taken one role at a time so every
        role is searched before any is searched twice over; each phrase once, and every one: the titles are the
        owner's, so their number is the owner's."""
        lists = [r.titles or ([r.name] if r.name else []) for r in self.target_roles]
        out: list[str] = []
        seen: set[str] = set()
        for i in range(max(map(len, lists), default=0)):
            for titles in lists:
                if i < len(titles) and titles[i].lower() not in seen:
                    seen.add(titles[i].lower())
                    out.append(titles[i])
        return out


# --- settings: how the search runs ------------------------------------------------------------------------------------


class Rule(Part):
    """An operating rule in words, for the part of the job it belongs to."""

    area: Literal["search", "screening", "packets", "apply", "email"]
    says: str


#: A nightly budget this large or larger is no limit: what the owner saved before there was a way to say so.
NO_BUDGET_FROM = 100000.0


#: What the queue's numbers are when the owner sets none (`Settings`): the numbers `pipeline` and `board` were first
#: written with (`pipeline.STALE_DAYS` and the others stand for these).
STALE_DAYS = 30
STALE_PENALTY = 21
GOLDEN_HOURS = 72
GOLDEN_BONUS = 20
RESEARCH_START_MULTIPLE = 3
#: Days of freshness a role in Followup needed must have left for the app to chase its posting (`CHASE_FRESH_DAYS`).
CHASE_FRESH_DAYS = 3
#: Settings that are gone and may still be in a stored version (nothing reads them): "Apply queue at a time"
#: (2026-10-28), and, with the title pass, the reader hand-out and the packets mission, the titles judged a night, the
#: postings read a night, the packets a run and "Still worth it? after" (2026-10-30), and the lowest score for Ready to
#: Launch (`ready_minimum`, 2026-11-06: Ready is the top read roles by the one number, whatever they score).
RETIRED_SETTINGS = frozenset({"apply_batch", "packets_per_run", "ready_stale_days", "titles_per_night",
                              "readers_per_night", "ready_minimum"})


#: The most places one line of `Settings.employer_places` may give an employer.
EMPLOYER_PLACES_HIGH = 500


def employer_places_of(text: str | None) -> list[tuple[str, int]]:
    """`Settings.employer_places` as (employer name, places) pairs, in the order written: "Globex: 4, Initech: 2", one
    pair a line or separated by commas or semicolons. Raises ValueError, in words for the owner, on a part that is not
    a name and a whole number of places, and on a name given twice."""
    pairs: list[tuple[str, int]] = []
    seen: set[str] = set()
    for part in re.split(r"[,;\n]+", text or ""):
        part = part.strip()
        if not part:
            continue
        name, sep, number = part.rpartition(":")
        name, number = name.strip(), number.strip()
        if not sep or not name or not number.isdigit():
            raise ValueError(f"{part!r}: write an employer's name, a colon and a number, like \"Globex: 4\"")
        if not 1 <= int(number) <= EMPLOYER_PLACES_HIGH:
            raise ValueError(f"{name}: 1 to {EMPLOYER_PLACES_HIGH} places (to take no role from an employer, name it "
                             "in Hard pass)")
        if name.lower() in seen:
            raise ValueError(f"{name} is listed twice")
        seen.add(name.lower())
        pairs.append((name, int(number)))
    return pairs


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: The classifier (docs/PRD.md 29.6, 31.1) ranks the postings that pass the gates, the likeliest first, and stops
    #: none: every one waits for the tabs' title check. It learns once the owner has this many positive examples; until
    #: then it ranks nothing and every posting still has its title checked.
    classifier_min_examples: int = Field(20, ge=1, le=10000)
    #: How many sites the Scout mission starts learning to read in a night (docs/PRD.md 29.1), the ones switched on
    #: longest ago first; -1: no limit. At least one: with none a waiting site would never be told when it starts.
    site_projects_per_night: int = Field(3, ge=-1, le=100)
    #: The starting-comp method's numbers (docs/PRD.md 26.6): the rules of thumb, the owner's to change. Coverage
    #: tiers as "least coverage:position" pairs, highest first.
    comp_tiers: str = "0.90:0.70, 0.75:0.60, 0.60:0.45, 0.45:0.30, 0:0.15"
    comp_transferable_weight: float = Field(0.5, ge=0, le=1)
    comp_stance_clamp: float = Field(0.10, ge=0, le=0.5)
    comp_stance_min_siblings: int = Field(3, ge=1, le=20)
    #: The likely offer: this share of the band's midpoint at no coverage, rising to the high one at full coverage
    #: (new-hire offers land at about 80 to 100% of a range's midpoint).
    comp_likely_low: float = Field(0.85, ge=0.5, le=1.2)
    comp_likely_high: float = Field(1.00, ge=0.5, le=1.2)
    #: The dollar step the expectation is rounded to and the width of the range under it. Blank (None): a share of the
    #: ask, so one setting fits low pay and high (`comp.round_step`, `comp.range_width`).
    comp_round_to: int | None = Field(None, ge=1, le=50000)
    comp_range_width: int | None = Field(None, ge=0, le=100000)
    #: What the expectation is (base salary or total compensation) and what it assumes (docs/PRD.md 26.10).
    comp_basis: Literal["base", "total"] = "base"
    comp_assumptions: str = ""
    #: Hold new Claude work during Claude's peak hours (docs/PRD.md 25.8): the window as times of day in its zone.
    avoid_peak: bool = True
    peak_start: str = "05:00"
    peak_end: str = "11:00"
    #: Not anyone's own place: Anthropic publishes Claude's peak hours in Pacific time, so this is the provider's zone
    #: and the same window (in absolute time) for every user wherever they live. The owner's own zone is only used
    #: for the off hours and finish times.
    peak_zone: str = "America/Los_Angeles"
    #: The owner's off hours, in their own time zone (any day): outside them the pool starts no work of its own;
    #: what the owner starts (Run now, an ask, an added link) runs at any time (`night.in_off_hours`).
    off_hours_start: str = "01:00"
    off_hours_end: str = "07:00"
    #: The job systems the owner switched off on Where to look (`ats.SYSTEMS` keys): the night's read skips the
    #: employers that post on one of them.
    job_systems_off: list[str] = []
    #: Watch the job board of every employer with a role in play (docs/PRD.md 29.9), set at the top of Employers. Off
    #: unless the owner switches it on: an install, or an owner, who never touched it watches only what they chose.
    watch_in_play: bool = False
    #: Read the signed-out job search of a board whose runbook asks automated readers to stay away, in the night's read
    #: (docs/PRD.md path 48): the owner's own switch for the one read robots.txt does not allow. Off unless the owner
    #: switches it on (`registry.follow_board_on`).
    read_robots_exempt: bool = False
    #: How many of the session lanes (session-1, session-2 ...) may work at once; one over it is told nothing is waiting
    #: and finishes what it has (`session_kinds.next_kind`). Default 1; a saved Settings version without it reads as 1.
    active_sessions: int = Field(1, ge=1, le=5)
    #: The most the app's own background sessions may cost in one off-hours window, in dollars at API prices
    #: (`budget.py`): paced across the window, and no new background session starts once it is reached. What the owner
    #: starts is never counted. None is no limit, and it is the default (the owner, 2026-10-19); a saved number of
    #: NO_BUDGET_FROM or more, once the stand-in for no limit, reads as None.
    nightly_budget: float | None = Field(None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _drop_retired(cls, data):
        """A saved version may still hold a setting that is gone (`RETIRED_SETTINGS`): it is read past, never refused,
        and the next save leaves it out."""
        if isinstance(data, dict) and RETIRED_SETTINGS & data.keys():
            return {k: v for k, v in data.items() if k not in RETIRED_SETTINGS}
        return data

    @model_validator(mode="before")
    @classmethod
    def _fold_the_daily_number(cls, data):
        """A version saved with the one daily number (`apply_per_day`) reads as that number for each size the version
        does not give, so nothing changes until the owner edits a size (2026-10-28). The number itself reads as None."""
        if not isinstance(data, dict) or not data.get("apply_per_day"):
            return data
        old = data["apply_per_day"]
        data = {**data, "apply_per_day": None}
        if isinstance(old, int) and not isinstance(old, bool) and old > 0:
            for size in ("second_impression_size", "ready_to_launch_size"):
                if data.get(size) is None:
                    data[size] = old
        return data

    @field_validator("nightly_budget")
    @classmethod
    def _no_limit(cls, v: float | None) -> float | None:
        return None if v is not None and v >= NO_BUDGET_FROM else v

    @field_validator("site_projects_per_night")
    @classmethod
    def _some_sites(cls, v: int) -> int:
        if v == 0:
            raise ValueError("1 or more, or -1 for no limit")
        return v

    @field_validator("peak_start", "peak_end", "off_hours_start", "off_hours_end")
    @classmethod
    def _clock(cls, v: str) -> str:
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (v or "").strip())
        if not m:
            raise ValueError("a time of day like 05:00")
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    @field_validator("job_systems_off")
    @classmethod
    def _systems(cls, v: list[str]) -> list[str]:
        unknown = sorted({k for k in v if k not in ats.SYSTEMS})
        if unknown:
            raise ValueError(f"not a job system: {', '.join(unknown)}")
        return sorted(set(v))

    @field_validator("employer_places")
    @classmethod
    def _places(cls, v: str | None) -> str | None:
        pairs = employer_places_of(v)
        return ", ".join(f"{name}: {places}" for name, places in pairs) or None

    @field_validator("peak_zone")
    @classmethod
    def _zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"no time zone called {v!r}") from None
        return v
    #: How many roles Second Impression (the deck Sonnet reads line by line) and Ready to Launch (the day's list Go
    #: builds packets for and opens) each hold (the owner, 2026-10-28; docs/PRD.md 39.32). None, 0 or less: 15
    #: (`pipeline.DAILY_DEFAULT`).
    second_impression_size: int | None = None
    ready_to_launch_size: int | None = None
    #: The numbers the queue's order and its reading line run with (the owner, 2026-10-28; docs/PRD.md 39.32), each
    #: defaulting to what it was when it was written into the code, so a saved version without them changes nothing.
    #: A posting older than `stale_days` (since it was posted, else since the app found it) is stale and loses
    #: `stale_penalty` points in the order; one found in the last `golden_hours` hours gains `golden_bonus`; new reads
    #: wait until First Impression holds `research_start_multiple` times the Second Impression size in ranked roles
    #: (0: they never wait).
    stale_days: int = Field(STALE_DAYS, ge=1, le=3650)
    stale_penalty: int = Field(STALE_PENALTY, ge=0, le=100)
    golden_hours: int = Field(GOLDEN_HOURS, ge=0, le=8760)
    golden_bonus: int = Field(GOLDEN_BONUS, ge=0, le=100)
    research_start_multiple: int = Field(RESEARCH_START_MULTIPLE, ge=0, le=100)
    #: A role in Followup needed has no text of its posting, and the browser chases its original posting only while
    #: the role would still pass the owner's freshness hard pass this many days from now (the owner, 2026-11-04;
    #: `postings/chase.py`). With no freshness hard pass set, every such role is chased. A role past it waits, and is
    #: closed only when the gates are next checked (a save of Hard pass), not as it ages. A saved version without it
    #: reads the default.
    chase_fresh_days: int = Field(CHASE_FRESH_DAYS, ge=0, le=3650)
    #: What the two sizes used to be, one number for both. Only read from a version saved before they were two (see
    #: `_fold_the_daily_number`): it is never set, so a saved version never holds it, and no code reads it.
    apply_per_day: int | None = None
    #: Go, its refill and the 6:30 tabs skip a role at an employer the owner applied to (status Applied, by the day it
    #: became Applied) within this many days (`go_checks.employer_gap`). None, 0 or less: off.
    days_between_employer_applications: int | None = None
    #: The most roles of one employer that hold a place in Second Impression and Ready to Launch together, for the
    #: employers named: "Globex: 4, Initech: 2" (`employer_places_of`). The best roles there by number keep the places
    #: and the rest wait in First Impression, kept and shown (`deck._over_places`). An employer not named has no such
    #: limit. None or blank: no employer has one.
    employer_places: str | None = None
    email_lookback_days: int | None = None
    #: An Applied role with no word for this many days moves to Gone Quiet, "No word came back" (`quiet.py`). None, 0 or
    #: less: never. Not bounded here, so an older stored version still loads (the bounds are the Missions screen's,
    #: `missions/numbers.py`).
    no_reply_days: int | None = None
    rules: list[Rule] = []
    notes: list[Note] = []


MODELS: dict[str, type[BaseModel]] = {"criteria": Criteria, "gates": GateSet, "scoring": Scoring, "settings": Settings}


def parse(kind: str, data: dict | None) -> BaseModel:
    """A document of this kind from stored data (empty when there is none). Raises pydantic's ValidationError."""
    if kind not in MODELS:
        raise ValueError(f"no profile document called {kind!r}; the kinds are {', '.join(KINDS)}")
    return MODELS[kind].model_validate(data or {})


class Profile(BaseModel):
    """All four documents together, as the screening process and the scout read them."""

    criteria: Criteria = Criteria()
    gates: GateSet = GateSet()
    scoring: Scoring = Scoring()
    settings: Settings = Settings()

    def is_set(self) -> bool:
        """Whether there is anything to judge a posting against: the kind of employer, or a target role with a name,
        example titles or a description (docs/PRD.md 30.5)."""
        c = self.criteria
        return bool(c.company.strip() or any(r.says_something() for r in c.target_roles))

    def ask_applies(self, ask: Ask, target_role: str | None) -> bool:
        """Whether an ask is asked of, and counted for, a role with this target role (docs/PRD.md 31.5): one limited to
        some roles applies to those only (a role with no target role is in none of them), one that follows a tab's
        text (31.6) only where the tab has that text written. An ask for every role applies to every role."""
        if ask.roles and target_role not in ask.roles:
            return False
        if ask.derived == "responsibilities":
            role = self.criteria.role(target_role)
            return role is not None and bool(role.responsibilities)
        return True

    def asks_for(self, target_role: str | None) -> list[Ask]:
        """The asks that count for a role with this target role, in the owner's order: All's and the role's own
        (docs/PRD.md 31.9), those in a section that is on (in All's switches for All's asks, in the role's own for its
        asks) and that apply to the role. The independent asks only: the statements of a group are in `items_for`."""
        return [a for a in self.scoring.asks
                if self.scoring.ask_on(a, target_role) and self.ask_applies(a, target_role)]

    def item_applies(self, item: Item, target_role: str | None) -> bool:
        """Whether an ask or a group is asked of, and counted for, a role with this target role (`ask_applies`): a group
        on some roles' tabs applies to those only, and has no built-in ask in it."""
        if isinstance(item, Group):
            return not item.roles or target_role in item.roles
        return self.ask_applies(item, target_role)

    def items_for(self, target_role: str | None) -> list[Item]:
        """The items that count for a role with this target role (docs/PRD.md 30.9, 31.5, 31.9): its asks
        (`asks_for`), then its groups, each in the owner's order. An item counts when it is on All's tab or on the
        role's own, its part is switched on for that tab, and it applies to the role; a role with no target role counts
        All's items only. A group counts as one item, with its statements inside it."""
        s = self.scoring
        return [*self.asks_for(target_role),
                *(g for g in s.groups if s.item_on(g, target_role) and self.item_applies(g, target_role))]
