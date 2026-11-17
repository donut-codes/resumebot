"""Scoring as structure (docs/PRD.md 30.6, 30.9): the asks, the rows that group them, the suggestions, the graded
requirements, and the sentences and tests the asks go by. See `profile/schema.py`."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.jobsearch.profile import gate_words
from app.jobsearch.profile.base import SECTIONS, FactOp, Note, Part, SectionId

# --- scoring: how good a fit ----------------------------------------------------------------------------------------


#: How the reader (or the app) answers an ask about a posting.
ANSWERS = ("met", "partly", "not", "unknown")
#: The answers an agent is told to give. "partly" is still read when an answer kept before the rule of no partial credit
#: (docs/PRD.md 30.9, 2026-10-14) holds it, and it earns nothing, so `ANSWERS` keeps it.
GIVEN_ANSWERS = ("met", "not", "unknown")


class Ask(Part):
    """One sentence about a posting, with the points it adds (or takes off). The reader answers it for each posting:
    met, not met or can't tell; the points earned are all of them when it is met and none otherwise: there is no
    partial credit (docs/PRD.md 30.6, 30.9). An answer of "partly", kept before that rule, is still read and earns none.
    Each ask counts on its own, and every row adds up with the others (an AND), unless the owner puts it in a group
    (`Group`, 30.9: an "any of" row or a "first match" row, whose statements are asks too); an older ask's `group`, the
    meaning the word had before 2026-10-10, is dropped when it is read (31.9).
    `per_item` with `limit`: the points
    count for each item found, up to the limit (for example −1 for each unproven required item, down to −3).
    `checked_by` is who decides: the reader, or the app (only an ask the app can decide itself: it needs one of the
    mechanisms below). A reader's ask that has a mechanism is decided by the app instead once the app holds the
    facts. Mechanisms: `fact`/`op`/`value` (an employer fact the record holds, docs/PRD.md 21.4), `days`/`above` (a
    posting's age: older than so many days, or with `above` false within the last so many, docs/PRD.md 30.6),
    `amount`/`above` (a pay tier on the likely ask) and `from_classes` (the analyst's classes of the required items,
    docs/PRD.md 25.5). An ask has no switch of its own: its section has one (`Scoring.sections_off`)."""

    section: SectionId = "description"
    says: str
    #: The owner's Verify line (the owner, 2026-10-14): exactly how to check this ask against a posting, so the same
    #: posting gets the same answer every time. The reader is sent it and applies it exactly: met only when the posting
    #: shows what it names, not met when it shows otherwise, unknown when it does not say. Empty: no line, and the ask
    #: is read as it always was. The owner's text, with no limit on its length; an older document has none.
    verify: str = ""
    points: int = 0
    checked_by: Literal["reader", "app"] = "reader"
    per_item: bool = False
    limit: int | None = None
    #: An employer fact the app decides this ask on when the employer record holds it: years_since_founded,
    #: employees, ownership, ai_native, industry, local, work_style, years_since_layoff, layoffs_in_2_years.
    fact: str | None = None
    op: FactOp | None = None
    value: str | int | float | bool | None = None
    #: A posting's age, in days, and which way it goes with `above`: the default is "posted over this many days ago"
    #: (older than); false is "posted within the last this many days", under days x 24 hours old, so 1 is under 24 hours
    #: (`freshness.within` says how the age is told). The days of a "within" ask are at least 1.
    days: int | None = None
    amount: int | None = None
    #: Which way `amount` or `days` goes. A pay tier: true (the default) is the likely ask over the amount, false under
    #: it. A posting's age: true (the default) is older than the days, false is within them.
    above: bool = True
    #: "U", "T" or "P" counts the items of that class (per item, to `limit`); "all_P" applies when every one is Proven.
    from_classes: Literal["U", "T", "P", "all_P"] | None = None
    #: The target roles (their ids) this ask is asked of and counted for; empty: every role (docs/PRD.md 31.5), which
    #: is Prioritization's All tab; one id is that role's own tab. A role's possible points and its fit percent count
    #: the asks that apply to every role and its own, and a role with no target role counts only the asks that apply
    #: to every role. The page writes one role or none (31.9).
    roles: list[str] = []
    #: A built-in ask whose question comes from the role's own tab: "responsibilities" asks whether the posting's work
    #: matches the Responsibilities written for the role (docs/PRD.md 31.6). It applies only to a role whose tab has
    #: Responsibilities written. The owner sets its points, turns it off, limits it to some roles or removes it like any
    #: ask (a removed one stays removed: `Scoring.built_ins_given`).
    derived: Literal["responsibilities"] | None = None

    @field_validator("from_classes", "fact", "op", "derived", mode="before")
    @classmethod
    def _blank(cls, v):
        return None if v == "" else v

    @field_validator("roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(r.strip() for r in v if r and r.strip()))

    def mechanism(self) -> str | None:
        """What the app can decide this ask from: classes, employer, age or pay; None when only a reader can."""
        if self.from_classes:
            return "classes"
        if self.fact:
            return "employer"
        if self.days is not None:
            return "age"
        if self.amount is not None:
            return "pay"
        return None

    @model_validator(mode="after")
    def _app_needs_a_way(self):
        if self.checked_by == "app" and self.mechanism() is None:
            raise ValueError(
                "the app can only check an ask it can decide itself: an employer fact, a posting age, a pay amount "
                "or the requirement classes")
        return self

    @model_validator(mode="after")
    def _within_needs_a_day(self):
        """A posting "within the last N days" is under N x 24 hours old, so N is 1 or more (within 0 days is no time at
        all). An ask on a posting's age that goes the other way (older than, the default) keeps any number."""
        if self.days is not None and not self.above and self.days < 1:
            raise ValueError(
                "an ask on a posting posted within the last so many days needs 1 day or more (within 1 day is under "
                "24 hours old)")
        return self


#: The built-in ask that follows each tab's Responsibilities (docs/PRD.md 31.6): its id, its sentence and the points a
#: new install starts with. It is an ask like the others (points, which roles, and the switch of its section,
#: Responsibilities), so the owner decides what it is worth, turns it off with its section, or removes it. A scoring is
#: given it once (`Scoring.built_ins_given`), so a removed one stays removed.
RESPONSIBILITIES_ID = "matches-responsibilities"
RESPONSIBILITIES_SAYS = "Matches the role's responsibilities"
RESPONSIBILITIES_POINTS = 3
#: What a reader is asked for that ask, since its sentence is about the role the posting was matched to.
RESPONSIBILITIES_QUESTION = ("Does this posting's work match the responsibilities written for the target role it was "
                             "matched to (that role's `responsibilities` in what_to_look_for)?")


def built_in_asks(taken: set[str]) -> list["Ask"]:
    """The built-in asks a scoring is given once (`Scoring.built_ins_given`). An id the owner already uses for another
    ask, group or statement of a group is not taken over (`taken` holds them all, `Scoring.ids_in_use`): the built-in
    one gets a free id."""
    ident, n = RESPONSIBILITIES_ID, 2
    while ident in taken:
        ident, n = f"{RESPONSIBILITIES_ID}-{n}", n + 1
    return [Ask(id=ident, section="responsibilities", says=RESPONSIBILITIES_SAYS, points=RESPONSIBILITIES_POINTS,
                derived="responsibilities")]


# --- groups: one row that holds several statements (docs/PRD.md 30.9) ------------------------------------------------

#: The two kinds of group (the owner, 2026-10-14: every row is already an AND, so there is no "all of"):
#: - `best`, the screen's "first match": an ordered list of statements, each with the points of its own; the first true
#:   one from the top decides the points. A statement here is a level.
#: - `any`, the screen's "any of": one row with several statements and one set of points, earned once when any of them
#:   is true. A statement here is a part, and carries no points of its own.
#: A statement is an `Ask`, so each can be answered by an agent (a reader, the analyst) or decided by the app from a
#: fact, the posting's age, a pay amount or the analyst's classes, exactly as an independent ask is (`Ask.mechanism`).
GroupKind = Literal["best", "any"]


#: The sentence for an id that an ask, a group or a statement of a group already has (one id space for them all).
_USED_TWICE = "'{id}' is used twice: every ask, row and statement needs an id of its own"


def _row(kind: str) -> str:
    """What a refusal calls a group of this kind: the words the screens use."""
    return "a first-match row" if kind == "best" else "an 'any of' row"


class Group(Part):
    """One row of Prioritization that holds several statements: a "first match" (`best`) or an "any of" (`any`). It
    sits in one part of a posting (`section`) and on one tab (`roles`: empty is All's, one id is that role's), as an ask
    does; its statements (`members`) take the part and the tab of the group when the document is read. `points` is the
    "any of" row's points, earned once, and 0 for a first match, whose statements carry the points. The model refuses a
    group with fewer than two statements, a blank statement, a statement that counts for each item or is the built-in
    ask, points on a statement of an "any of" (they are on the row), points on a first match (they are on its
    statements), a first match whose statements are judged by both Claude and the app, and a statement id used twice."""

    kind: GroupKind
    section: SectionId = "description"
    roles: list[str] = []
    points: int = 0
    members: list[Ask]

    @field_validator("roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(r.strip() for r in v if r and r.strip()))

    @field_validator("points")
    @classmethod
    def _points_on_the_row(cls, v: int, info: ValidationInfo) -> int:
        if info.data.get("kind") == "best" and v != 0:
            raise ValueError("the points of a first-match row are on its statements, not on the row")
        return v

    @field_validator("members")
    @classmethod
    def _statements(cls, v: list[Ask], info: ValidationInfo) -> list[Ask]:
        kind = info.data.get("kind")
        if kind is None:  # the kind was refused: its own message says so
            return v
        row = _row(kind)
        if len(v) < 2:
            raise ValueError(f"{row} needs at least two statements")
        if any(not m.says.strip() for m in v):
            raise ValueError(f"every statement of {row} needs a sentence")
        if any(m.per_item or m.derived for m in v):
            raise ValueError("an ask counted for each item, or the built-in responsibilities ask, cannot be in a "
                             "first-match or 'any of' row")
        if kind == "any" and any(m.points for m in v):
            raise ValueError("the points of an 'any of' row are on the row, not on its statements")
        if kind == "best" and len({decided_by_the_app(m) for m in v}) > 1:
            raise ValueError("a first-match row is judged by Claude or by the app, not both: every statement needs a "
                             "test, or none does")
        ids = [m.id for m in v]
        if twice := next((i for i in ids if ids.count(i) > 1), None):
            raise ValueError(_USED_TWICE.format(id=twice))
        section, roles = info.data.get("section"), info.data.get("roles")
        if section is None or roles is None:  # one of them was refused: its own message says so
            return v
        return [m.model_copy(update={"section": section, "roles": list(roles)}) for m in v]

    def by_the_app(self) -> bool:
        """Whether the app decides every statement of the group itself (from a fact, the posting's age, a pay amount or
        the analyst's classes): then no agent is asked it. A first match is judged by one of them, never both
        (`members` refuses a mix); an "any of" may have some statements for an agent and some for the app."""
        return all(decided_by_the_app(m) for m in self.members)


#: What an item of Prioritization is: an ask that counts on its own, or a group.
Item = Ask | Group

#: What a suggestion proposes: a group of one of the two kinds, or the removal of asks that grading counts again.
SuggestionKind = Literal["best", "any", "remove"]


class Suggestion(Part):
    """A change an agent or the profile API proposes for the owner to use or dismiss (docs/PRD.md 30.9): it is a note,
    and changes nothing by being stored. `kind` is `best` or `any`, to put the asks in a first-match or an "any of" row,
    or `remove`, to offer the asks for removal because grading the requirements counts the same things (the owner
    removes them with one click and a save; nothing is ever removed automatically). `asks` are the ids of asks the
    owner already has (at least one), in the order proposed (best first for a first match); `points` is the "any of"
    row's points (0 for a first match, whose statements keep the points of the asks, and for a removal); `why` is one
    plain sentence for the card. No text has a limit on its length. The app never writes one itself."""

    kind: SuggestionKind
    asks: list[str]
    points: int = 0
    why: str = ""

    @field_validator("asks")
    @classmethod
    def _asks(cls, v: list[str]) -> list[str]:
        ids = list(dict.fromkeys(a.strip() for a in v if a and a.strip()))
        if not ids:
            raise ValueError("a suggestion names at least one ask")
        return ids

    @field_validator("points")
    @classmethod
    def _points_on_the_row(cls, v: int, info: ValidationInfo) -> int:
        kind = info.data.get("kind")
        if kind == "best" and v != 0:
            raise ValueError("a first-match suggestion has no points of its own: its statements keep the points of "
                             "the asks")
        if kind == "remove" and v != 0:
            raise ValueError("a remove suggestion has no points: it only offers the asks for removal")
        return v


#: How much of a read role's queue score is its match (the Proven share of its graded required items) when the owner
#: has not said: half, the other half the share of the owner's other asks it earned (`scoring.queue_percent`). The
#: product's own starting point, not anyone's preference: the owner sets it on Prioritization (`Scoring.match_weight`).
MATCH_WEIGHT_DEFAULT = 0.5

#: The id of the graded-requirements row in a role's score (`scoring.compute`). It has a colon, which no ask's, group's
#: or statement's id can have (`Part.id`), so it never takes another row's id.
GRADE_ID = "grade:requirements"


class RequirementsGrade(BaseModel):
    """"Grade the requirements for me" (the owner, 2026-10-14): one row of Prioritization the app decides itself, from
    the analyst's classes (P Proven, T Transferable, U Unproven) of a role's required and preferred items, and never
    asks an agent. Off by default: with it off nothing changes. With it on, each required item earns the points of its
    class (`required_proven`, `required_transferable`, `required_unproven`) and each preferred item
    `preferred_proven_or_transferable` or `preferred_unproven`; the row earns their sum. The five weights are the
    owner's own: the model holds no default for any of them (built to share: the page's starting numbers are the page's
    own), so an object that lacks one, or holds one that is not a whole number, is refused.

    The row's share of what a role can earn is the role's own maximum, since roles list different numbers of items:
    the most each of its required and preferred items can earn, added up (the best positive weight of each side). A
    class that costs points (Unproven required) is a penalty and stays out of what a role can earn, as every penalty
    does. The required items count when Qualifications is on, the preferred ones when Preferred is on. The posting's
    responsibilities, classed the same way, count as more required items, by the same points (the owner, 2026-10-16);
    the gates never read them."""

    model_config = ConfigDict(extra="forbid")
    on: bool = False
    required_proven: int
    required_transferable: int
    required_unproven: int
    preferred_proven_or_transferable: int
    preferred_unproven: int


class RoleSwitches(BaseModel):
    """One role's tab on Prioritization: the switches of its parts (`sections_off`, the ids of the parts that are off,
    in the page's order). They govern the asks that name that role (`Ask.roles`); All's switches govern the asks that
    name none."""

    model_config = ConfigDict(extra="forbid")
    sections_off: list[SectionId] = []

    @field_validator("sections_off")
    @classmethod
    def _sections_off(cls, v: list[str]) -> list[str]:
        return gate_words.in_order(v, gate_words.SECTION_IDS)


class OldScale(BaseModel):
    """The lowest and highest raw fit the owner's scoring allowed before it was asks (a base score and modifiers):
    roles scored then keep their old score on this scale until they are scored again (docs/PRD.md 30.6)."""

    model_config = ConfigDict(extra="forbid")
    low: int
    high: int


class Scoring(BaseModel):
    """The asks, by the part of a posting they are about. Each of the seven sections has one switch (`sections_off`: the
    ids of the sections that are off, in the page's order; empty means every section is on). An ask in an off section
    counts for nothing: no points and no possible points. It stays on the page.

    The page has a tab for All and one for each target role (docs/PRD.md 31.9). `sections_off` is the All tab's, and the
    asks that name no role (`Ask.roles` empty) are its asks; a role's own tab is `by_role[role id]` (its switches) with
    the asks that name it. A role counts All's asks and its own (`Profile.asks_for`). Saved data with none of this is
    all All.

    Every ask counts on its own and every row adds up with the others, unless the owner puts asks in a group
    (docs/PRD.md 30.9): an "any of" row or a "first match" row (`groups`, each in one part and on one tab, as an ask
    is). The items of a role are its asks and groups together (`Profile.items_for`). A group is the owner's own:
    nothing here ever makes one, and `suggestions` are notes for the owner to use or dismiss. `grade_requirements` is
    the one row the app decides itself, from the analyst's classes (`RequirementsGrade`)."""

    model_config = ConfigDict(extra="forbid")
    sections_off: list[SectionId] = []
    asks: list[Ask] = []
    #: "Any of" and "first match" rows, each holding several statements (docs/PRD.md 30.9). Empty for every document
    #: saved before groups, which read as they did.
    groups: list[Group] = []
    #: Groupings proposed for the owner (by an agent through the profile API, named in the history).
    #: Notes: nothing counts, scores or changes by their being here.
    suggestions: list[Suggestion] = []
    #: "Grade the requirements for me": the app's own row over the analyst's classes, off until the owner turns it on.
    #: Null until the owner first ticks the box; null means off.
    grade_requirements: RequirementsGrade | None = None
    #: One role's own switches, by the role's id; a role with every part on has no entry.
    by_role: dict[str, RoleSwitches] = {}
    old_scale: OldScale | None = None
    #: Anything from an older scoring that no field holds now (the base levels, the order of work), kept word for word
    #: until the owner turns it into asks.
    notes: list[Note] = []
    #: The built-in asks (an `Ask.derived`) this scoring has been given, by kind. A built-in ask is made once, for a
    #: scoring that has never had it; one that was given it and lacks it had it removed by the owner, and it is not made
    #: again. Only a scoring saved before built-in asks existed has no record, and it is given the ask when it is read.
    built_ins_given: list[Literal["responsibilities"]] = []
    #: How much "can I do it" counts against "does it fit what I want" in the order of the read roles, 0 to 1
    #: (`scoring.queue_percent`): the share of a read role's queue score that is its match, the Proven share of its
    #: graded required items; the rest is the share of the owner's other asks it earned. A saved version without it
    #: reads as `MATCH_WEIGHT_DEFAULT`.
    match_weight: float = Field(default=MATCH_WEIGHT_DEFAULT, ge=0, le=1)

    @field_validator("sections_off")
    @classmethod
    def _sections_off(cls, v: list[str]) -> list[str]:
        return gate_words.in_order(v, gate_words.SECTION_IDS)

    @field_validator("built_ins_given")
    @classmethod
    def _given_once(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @field_validator("groups")
    @classmethod
    def _one_id_space(cls, v: list[Group], info: ValidationInfo) -> list[Group]:
        """An ask, a group and a statement of a group each have an id of their own: a group's or a statement's id that
        anything else has too is refused, in words. (Two asks that share an id, in a document saved before groups,
        stay as they were: they were never refused.)"""
        ids = [a.id for a in info.data.get("asks") or []] + [g.id for g in v] + [m.id for g in v for m in g.members]
        if twice := next((i for i in [g.id for g in v] + [m.id for g in v for m in g.members] if ids.count(i) > 1),
                         None):
            raise ValueError(_USED_TWICE.format(id=twice))
        return v

    def ids_in_use(self) -> set[str]:
        """Every id an ask, a group or a statement of a group has: what a new id must not take."""
        return {a.id for a in self.asks} | {g.id for g in self.groups} | {m.id for g in self.groups for m in g.members}

    def section_on(self, section: str, target_role: str | None = None) -> bool:
        """Whether a part is on: in All's switches, or in the role's own when a role is given (a role with no entry has
        every part on)."""
        if target_role is None:
            return section not in self.sections_off
        mine = self.by_role.get(target_role)
        return mine is None or section not in mine.sections_off

    def item_on(self, item: "Item", target_role: str | None = None) -> bool:
        """Whether an ask's or a group's part is on for a role with this target role: the role's own switches for an
        item that names roles, All's for one that names none."""
        return self.section_on(item.section, target_role if item.roles else None)

    def item_on_anywhere(self, item: "Item") -> bool:
        """Whether an ask or a group counts for anyone: its part is on in All's switches (it names no role), or in the
        switches of any role it names."""
        if not item.roles:
            return self.section_on(item.section)
        return any(self.section_on(item.section, r) for r in item.roles)

    def ask_on(self, ask: "Ask", target_role: str | None = None) -> bool:
        """Whether an ask's part is on for a role with this target role (`item_on`)."""
        return self.item_on(ask, target_role)

    def ask_on_anywhere(self, ask: "Ask") -> bool:
        """Whether an ask counts for anyone (`item_on_anywhere`)."""
        return self.item_on_anywhere(ask)

    def grades_required(self) -> bool:
        """Whether the app grades a role's required items: "grade the requirements for me" is on and Qualifications, the
        part the required items belong to, is on (in All's switches: the row names no role)."""
        grade = self.grade_requirements
        return grade is not None and grade.on and self.section_on("qualifications")

    def grades_preferred(self) -> bool:
        """Whether the app grades a role's preferred items: the setting is on and Preferred is on."""
        grade = self.grade_requirements
        return grade is not None and grade.on and self.section_on("preferred")

    @model_validator(mode="after")
    def _built_in(self):
        """A scoring is given each built-in ask once (docs/PRD.md 31.6), after the owner's own, and the owner may remove
        it. One with the ask has been given it; one with no record of it is given it when it is read (a scoring saved
        before built-in asks existed, or a new one); one that was given it and lacks it had it removed, and stays so."""
        if "responsibilities" not in self.built_ins_given:
            if not any(a.derived == "responsibilities" for a in self.asks):
                self.asks = [*self.asks, *built_in_asks(self.ids_in_use())]
            self.built_ins_given = [*self.built_ins_given, "responsibilities"]
        return self


# --- the sentences the page shows for the asks, and who decides them -------------------------------------------------


def ask_says(ask: Ask) -> str:
    """One plain sentence for an ask: what it says and the points it adds or takes off ("noted" when none)."""
    if not ask.points:
        return f"{ask.says} (noted)"
    sign = "+" if ask.points > 0 else ""
    each = ""
    if ask.per_item:
        each = f" each, down to {ask.limit}" if ask.limit is not None else " each"
    return f"{ask.says}: {sign}{ask.points}{each}"


def asks_by_section(scoring: Scoring) -> list[tuple[str, str, list[Ask]]]:
    """The asks that count for someone (those in a section that is on, for All or for a role they name), grouped by the
    part of a posting they are about, in posting order, each section's asks by points (the highest first; between
    equals, the order they were added). A section with none is left out."""
    out = []
    for section, label in SECTIONS:
        mine = [a for a in scoring.asks if a.section == section and scoring.ask_on_anywhere(a)]
        if mine:
            out.append((section, label, sorted(mine, key=lambda a: -a.points)))
    return out


def verify_of(ask: Ask) -> str | None:
    """The ask's Verify line as an agent is sent it: the owner's text as written, whole; None when there is none (a line
    of only spaces is none), so the ask is sent as it always was."""
    return ask.verify if ask.verify.strip() else None


def decided_by_the_app(ask: Ask) -> bool:
    """Whether the app decides this ask itself, so that no agent is asked it: the owner left it to the app
    (`checked_by`), or it is built from an employer fact, the posting's age, a pay amount or the analyst's classes
    (`Ask.mechanism`), where the app's own answer is the one that counts (`scoring.compute`)."""
    return ask.checked_by == "app" or ask.mechanism() is not None
