"""The gates as structure (docs/PRD.md 30.4): the work location, the lowest pay, the lines of Hard pass, the switches of
their parts and the sentences the page shows for them. Filter what was found, never search. See `profile/schema.py`."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.jobsearch.measures.pay_place import PAY_AGAINST_DEFAULT
from app.jobsearch.profile import gate_words
from app.jobsearch.profile.base import FactOp, Note, Part, SectionId

# --- gates: filter what was found, never search (docs/PRD.md 30.4) ----------------------------------------------------


#: The kinds of gate that sit in a part of a posting: a sentence the reader checks, an employer fact the app checks
#: from the employer's record, and a posting's age the app checks.
GateKind = Literal["rule", "employer_fact", "posting_age", "job_fact"]
#: What the compensation gate compares its amount with.
PayAgainst = Literal["minimum", "maximum", "ask"]


class Way(BaseModel):
    """One way of working (remote, hybrid or in person): accepted or not, and where it must be. `locations` are names a
    posting's location line may use: for remote, where remote work must be open ("United States"); for hybrid and in
    person, the towns or areas. Empty: anywhere."""

    model_config = ConfigDict(extra="forbid")
    on: bool = False
    locations: list[str] = []

    @field_validator("locations")
    @classmethod
    def _locations(cls, v: list[str]) -> list[str]:
        return gate_words.flat_locations(v)


class Location(BaseModel):
    """Where the work happens (the app checks it). Each way of working is accepted or not, with the locations that
    count for it: a posting on site in a place that isn't listed fails, so there is no list of places to avoid. A
    posting that doesn't say where passes: a gate passes until it is proven failed. With no way of working accepted
    there is no work location gate."""

    model_config = ConfigDict(extra="forbid")
    remote: Way = Way()
    hybrid: Way = Way()
    in_person: Way = Way()

    def is_set(self) -> bool:
        """Whether any way of working is accepted: with none there is no work location gate."""
        return self.remote.on or self.hybrid.on or self.in_person.on


class Compensation(BaseModel):
    """One amount a year, compared with the posted minimum, the posted maximum or the estimated ask, the owner's choice
    (the app checks it). With no posted pay it uses the estimated ask; with neither it passes. With no amount there is
    no pay gate."""

    model_config = ConfigDict(extra="forbid")
    amount: int | None = None
    against: PayAgainst = PAY_AGAINST_DEFAULT
    #: A posted band wider than this many times its bottom is too wide to find its middle (the ask is judged later).
    wide_band_ratio: float = 1.5

    def is_set(self) -> bool:
        return bool(self.amount)


#: The part of a posting a gate sits in when none was stored (a profile saved before gates had parts), from what it is
#: and never from its words: a sentence goes to Description, where the owner places it; an employer rule is about the
#: company and a posting age about the posting, so they go there.
GATE_HOME: dict[str, str] = gate_words.GATE_HOME


class Gate(Part):
    """One line of the gates that is not the work location or the pay. It belongs to a `section`, one of the
    parts of a posting (`SECTIONS`); a gate saved without one loads into its kind's home (`GATE_HOME`). It has no
    switch of its own: its section has one (`GateSet.sections_off`). Which fields count depends on `kind`:
    - rule: `text`, a requirement the reader answers pass, fail or unknown from the posting, usually ending in a
      Verify line ("Required travel must be no more than 25% of working time. Verify: ..."; a sentence to avoid, such
      as "travel of 20% or more", works too); a fail screens the role out (`sentence_gates.py`). With `from_classes`
      and `share` the app decides it instead, from the analyst's classes of the role's required items
      (`follows_classes`)
    - employer_fact: `fact`, `op`, `value`: what a role's employer must be ("ownership != pe" for never PE-owned); the
      app checks it from the employer's record, and it passes until the record holds the fact
    - posting_age: `days`; the app checks it
    - job_fact: `fact`, `op`, `value`: a job dial's Must (docs/PRD.md 46.4) the app checks from what is stored of the
      posting (years of experience asked, the degree, the distance from home, the travel asked, whether the employer is
      named ...); it waits, and stops nothing, until the posting states the fact (a posting that states no travel
      passes the Travel dial: it asks none)."""

    kind: GateKind = "rule"
    section: SectionId = "description"
    text: str = ""
    days: int | None = None
    fact: str | None = None
    op: FactOp | None = None
    value: str | int | float | bool | None = None
    #: Title words only (docs/PRD.md 46.4): a comma list of phrases inside which a listed word is no hit ("assistant
    #: vice president" lets that title past "vice president"). Optional and empty by default, so a profile stored
    #: without it loads unchanged: the profile is a JSON document and needs no migration.
    except_words: str | None = None
    #: A rule the app decides itself from the analyst's classes of the role's required items, as an ask's `from_classes`
    #: is: the class that counts ("U" Unproven, "T" Transferable or "P" Proven) and `share`, the percent of the required
    #: items in that class at or above which the gate fails. Both or neither, and only a rule. The reader is not asked
    #: it, and it is unknown (it stops nothing) until the analyst has classed the items (`scoring.class_gate`).
    #: "PT" turns it round: at least `share` percent of the items must be Proven or Transferable together, and the gate
    #: fails below that (the owner's "at least 20% must be Proven or Transferable").
    from_classes: Literal["U", "T", "P", "PT"] | None = None
    share: int | None = Field(None, ge=1, le=100)
    #: The target roles (their ids) this gate is for; empty: every role (the page's All tab). The page writes one role
    #: or none. A role's lines are checked, and sent to a reader, only for a posting matched to that role; the switch
    #: of the part they are in is the role's own (`GateSet.by_role`).
    roles: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _placed(cls, data):
        """A gate stored without a section is put in its kind's home; one that names a section keeps it."""
        if isinstance(data, dict) and not data.get("section"):
            return {**data, "section": GATE_HOME.get(data.get("kind") or "rule", "description")}
        return data

    @field_validator("fact", "op", "from_classes", "share", "except_words", mode="before")
    @classmethod
    def _blank(cls, v):
        return None if v == "" else v

    @field_validator("roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(r.strip() for r in v if r and r.strip()))

    @model_validator(mode="after")
    def _follows_classes(self):
        if (self.from_classes is None) != (self.share is None):
            raise ValueError("a rule the app decides from the analyst's classes needs both from_classes (U, T or P) "
                             "and share (a percent, 1 to 100)")
        if self.from_classes is not None and self.kind != "rule":
            raise ValueError("only a rule (a sentence) can be decided from the analyst's classes")
        return self

    def follows_classes(self) -> bool:
        """Whether the app decides this rule itself, from the analyst's classes of the role's required items
        (`scoring.class_gate`): the reader is not asked it, and its answer, if one comes, is not read."""
        return self.kind == "rule" and self.from_classes is not None and self.share is not None


class RoleGates(BaseModel):
    """The structured gates of one scope and the switches of its parts: Hard pass's All tab (the `GateSet` itself) or
    one role's tab (`GateSet.by_role[role id]`). A role's gates ADD to All's and never replace them: a posting
    matched to that role must pass both (docs/PRD.md 31.9). Each tab's `sections_off` governs that tab's own items:
    the ids of the parts that are off, in the page's order. An off part stops nothing: no code gate and none of its
    sentences sent to a reader. There is no gate on a role's fit (docs/PRD.md 30.4): Prioritization ranks and never
    stops."""

    model_config = ConfigDict(extra="forbid")
    sections_off: list[SectionId] = []
    location: Location = Location()
    compensation: Compensation = Compensation()

    @field_validator("sections_off")
    @classmethod
    def _sections_off(cls, v: list[str]) -> list[str]:
        return gate_words.in_order(v, gate_words.SECTION_IDS)

    def section_on(self, section: str) -> bool:
        return section not in self.sections_off

    def checks_location(self) -> bool:
        return self.section_on("posting") and self.location.is_set()

    def checks_pay(self) -> bool:
        return self.section_on("compensation") and self.compensation.is_set()


class GateSet(RoleGates):
    """The gates, grouped. Employers are not here: each employer's gate is switched on its own record (Employers).
    Each section of the page has one switch (`sections_off`: the ids of the sections that are off, in the page's order;
    empty means every section is on, and a new install's gates are all unset so nothing is checked either way). An off
    section stops nothing: no code gate and none of its sentences sent to a reader. Company covers the employers gated
    on their own records, the employer rules and its sentences; Job posting the work location, the posting age and its
    sentences; Benefits / Compensation the lowest pay and its sentences.

    The document is the All tab: `sections_off`, `location`, `compensation` and the lines in `other` that name no
    role. A role's own tab is `by_role[role id]` (its structured gates and the switches of its parts) with the lines in
    `other` that name it (`Gate.roles`). Saved data with none of this is all All."""

    other: list[Gate] = []
    #: At most two open applications per employer: past two, the other roles there are held out of the deck (PRD 34.2,
    #: the owner, 2026-10-26). On for everyone; it runs while the Company part is on.
    employer_limit: bool = True
    #: One role's own structured gates and switches, by the role's id. They add to All's (a posting matched to the role
    #: must pass both). A role with nothing set has no entry.
    by_role: dict[str, RoleGates] = {}
    #: Names that were gated here before employers had their own switch; they move onto employer records, once.
    employer_names: list[str] = []
    notes: list[Note] = []

    def checks_employers(self) -> bool:
        """Whether the employers gated on their own records, and the names gated here, stop a posting."""
        return self.section_on("company")

    def limits_employers(self) -> bool:
        """Whether an employer's open applications are limited to two."""
        return self.employer_limit and self.checks_employers()

    def role(self, target_role: str | None) -> RoleGates | None:
        """The role's own tab, when it has one with something on it; None for a role with none (or no role)."""
        return self.by_role.get(target_role) if target_role else None

    def checked_lines(self) -> list[Gate]:
        """The All tab's gates in a part of a posting that are being checked: the lines that name no role, in a part
        that is on in All's switches."""
        return [g for g in self.other if not g.roles and self.section_on(g.section)]

    def lines_for(self, target_role: str | None) -> list[Gate]:
        """The gates in a part of a posting that are being checked for a posting matched to this target role, in the
        order they are listed: All's (their part on in All's switches) and the role's own (their part on in the role's
        switches; a role with no tab of its own has every part on). With no target role, All's alone."""
        mine = self.by_role.get(target_role) if target_role else None
        out = []
        for g in self.other:
            if not g.roles:
                if self.section_on(g.section):
                    out.append(g)
            elif target_role in g.roles and (mine is None or mine.section_on(g.section)):
                out.append(g)
        return out


# --- the sentences the page shows for the gates ---------------------------------------------------------------------


def location_says(loc: Location) -> str:
    """The work location as one sentence: the ways of working that are allowed, and where each must be."""
    return gate_words.location_words(
        (loc.remote.on, loc.remote.locations), (loc.hybrid.on, loc.hybrid.locations),
        (loc.in_person.on, loc.in_person.locations))


def compensation_says(comp: Compensation) -> str:
    return gate_words.compensation_words(comp.amount, comp.against)


def gate_says(gate: Gate) -> str:
    """One plain sentence for a gate in `other`, from its kind and values."""
    return gate_words.line_words(gate.kind, gate.text, gate.days, gate.fact, gate.op, gate.value, gate.except_words)


def who_checks(gate: Gate) -> str:
    """Who checks a gate in `other`: the reader, for a sentence; the app, for the kinds it can decide from facts, and
    for a sentence set to follow the analyst's classes (`Gate.follows_classes`): that one is not sent to a reader to
    answer, it is listed as the app's, as the work location and the pay are."""
    return "reader" if gate.kind == "rule" and not gate.follows_classes() else "app"


def gate_lines(gates: GateSet) -> list[dict]:
    """Every gate as a line, in the page's order: id, group (the kind of gate), sentence, whether it is being checked
    (`on`: its section is on and it says something), who checks it, and the target roles it is for (`roles`: empty for
    every role). All's come first; then each role's own structured gates (the id is `<kind>:<role id>`) and its lines. A
    gate in an off section is a line that is not on, so no reader is sent it; one that is on in a role's own tab is
    on for that role only."""
    lines = [
        {"id": "location", "group": "location", "says": location_says(gates.location), "on": gates.checks_location(),
         "who": "app", "roles": []},
        {"id": "compensation", "group": "compensation", "says": compensation_says(gates.compensation),
         "on": gates.checks_pay(), "who": "app", "roles": []},
    ]
    for role_id, own in gates.by_role.items():
        lines += [
            {"id": f"location:{role_id}", "group": "location", "says": location_says(own.location),
             "on": own.checks_location(), "who": "app", "roles": [role_id]},
            {"id": f"compensation:{role_id}", "group": "compensation", "says": compensation_says(own.compensation),
             "on": own.checks_pay(), "who": "app", "roles": [role_id]},
        ]

    def on(g: Gate) -> bool:
        if not g.roles:
            return gates.section_on(g.section)
        return any((gates.by_role[r].section_on(g.section) if r in gates.by_role else True) for r in g.roles)

    return lines + [{"id": g.id, "group": "other", "kind": g.kind, "says": gate_says(g), "on": on(g),
                     "who": who_checks(g), "roles": list(g.roles)} for g in gates.other]
