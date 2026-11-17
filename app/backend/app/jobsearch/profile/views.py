"""The profile as each reader is handed it: the numbers and lists the screening process compares (`engine_gates`), what
to look for for the title pass and the readers (`look_for`, `for_titles`), and the whole of it, compact, for the scout
(`for_scout`, `scoring_for_agent`). See `profile/schema.py`."""

from app.jobsearch.measures.pay_place import Gates
from app.jobsearch.profile import gates as profile_gates
from app.jobsearch.profile import scoring as profile_scoring
from app.jobsearch.profile.base import SECTIONS
from app.jobsearch.profile.gates import Gate, RoleGates
from app.jobsearch.profile.schema import Criteria, Profile
from app.jobsearch.profile.scoring import RESPONSIBILITIES_QUESTION, Group, Scoring

#: A posting age no role reaches: "no age rule".
NO_AGE_LIMIT = 100_000


# --- the screening process's view (the numbers and lists screen_one_role.bpmn and role_facts compare) ----------------


def _names(locations: list[str]) -> list[str]:
    """What a posting's location line may call these places, lowercased."""
    return sorted({n.strip().lower() for n in locations if n.strip()})


def pay_floor(profile: Profile, target_role: str | None = None) -> int:
    """The amount the compensation gate asks for, a year; 0 when the gate is off (its section is) or has none. For a
    role with a tab of its own that has a lowest pay, the higher of its amount and All's: a posting must clear both
    (docs/PRD.md 31.9)."""
    g = profile.gates
    floor = (g.compensation.amount or 0) if g.checks_pay() else 0
    own = g.role(target_role)
    return max(floor, (own.compensation.amount or 0) if own is not None and own.checks_pay() else 0)


def _gates_of(switches: RoleGates, lines: list[Gate]) -> Gates:
    """The numbers and lists of one scope's gates, as the process compares them: All's (`switches` the whole `GateSet`)
    or one role's own (`switches` its `RoleGates`, `lines` the lines that name it). `says` holds the sentence of each
    gate that is on."""
    out = Gates(stale_days=NO_AGE_LIMIT, comp_floor=0)
    if age := next((x for x in reversed(lines) if x.kind == "posting_age" and x.days), None):
        out.stale_days = age.days
        out.says["age"] = profile_gates.gate_says(age)
    comp = switches.compensation
    if switches.checks_pay():
        out.comp_floor, out.comp_against, out.comp_wide_band_ratio = comp.amount, comp.against, comp.wide_band_ratio
        out.says["compensation"] = profile_gates.compensation_says(comp)
    loc = switches.location
    if switches.checks_location():
        out.places_gated = True
        out.remote_ok, out.remote_countries = loc.remote.on, [c.strip() for c in loc.remote.locations if c.strip()]
        out.hybrid_ok, out.hybrid_places = loc.hybrid.on, _names(loc.hybrid.locations)
        out.onsite_ok, out.onsite_places = loc.in_person.on, _names(loc.in_person.locations)
        out.says["location"] = profile_gates.location_says(loc)
    return out


def engine_gates(profile: Profile) -> Gates:
    """The gates in the shape the screening process compares. A gate that is off, or not there, lets every role
    through. A posting that is old is only ever screened out by a posting-age gate: an ask about age is scored where
    the fit is added up (`scoring.compute`), not here. `says` holds the sentence of each gate that is on: it is the
    reason a posting it stops is given.

    These are All's. A role with gates of its own on Hard pass has its own `Gates` in `by_role` (its work location,
    lowest pay and posting age, only the ones that are on): they ADD to All's, and a posting must pass
    both once the title pass has named the role (`gates.role_facts`, `gating.check`; docs/PRD.md 31.9). A role with
    nothing of its own on is not in `by_role`."""
    g = profile.gates
    out = _gates_of(g, g.checked_lines())
    if g.checks_employers():
        out.excluded_employers = [e.strip().lower() for e in g.employer_names if e.strip()]
    role_ids = dict.fromkeys([*g.by_role, *(r for x in g.other if x.kind == "posting_age" for r in x.roles)])
    for role_id in role_ids:
        own = g.by_role.get(role_id, RoleGates())
        lines = [x for x in g.other if role_id in x.roles and own.section_on(x.section)]
        gates = _gates_of(own, lines)
        if gates.adds_anything():
            out.by_role[role_id] = gates
    return out


# --- the scout's view ---------------------------------------------------------------------------------------------

#: What each reader of the gates is told about who checks a gate.
WHO_CHECKS = {
    "app": "the app: report the role anyway, it is screened with the reason",
    "reader": (
        "you: answer pass, fail or unknown from the posting, by the sentence's Verify line, in gate_answers "
        "({id: this gate's id, answer, evidence: one line that quotes or points at the posting}); "
        "the app screens the role out on a fail"),
}


def look_for(c: Criteria) -> dict:
    """What to look for as a reader reads it (docs/PRD.md 30.5): the kind of employer and each target role in full,
    under the names the page uses, in the order a posting reads. Nothing empty. A role is named by its `id`: it is what
    a posting is matched to. Scoring decides the points, so none are here."""
    return _compact({
        "company": c.company.strip(),
        "target_roles": [{"id": r.id, "name": r.name, "titles": r.titles, **r.texts()}
                         for r in c.target_roles if not r.is_blank()],
    })


def look_for_role(c: Criteria, role_id: str | None) -> dict:
    """What to look for as it concerns one target role: the kind of employer and that one role in full, as
    `look_for` gives it; with no such role, the kind of employer alone. The analyst re-judges the asks of one role, so
    it is handed that role's picture, not every role's."""
    role = c.role(role_id)
    return _compact({
        "company": c.company.strip(),
        "target_role": ({"id": role.id, "name": role.name, "titles": role.titles, **role.texts()}
                        if role is not None else None),
    })


def for_titles(profile: Profile) -> dict:
    """What the title pass is handed to judge a title and its employer against (docs/PRD.md 30.5): the company and,
    for each target role, its id, name, example titles and description. Nothing else: a title says nothing of the
    responsibilities, qualifications, preferred list or benefits, so those stay for the readers."""
    c = profile.criteria
    return _compact({
        "company": c.company.strip(),
        "target_roles": [{"id": r.id, "name": r.name, "titles": r.titles, "description": r.description}
                         for r in c.target_roles if r.says_something()],
    })


def for_scout(profile: Profile) -> dict:
    """The profile as a reader or the scout reads it: plain, compact, nothing empty. Who checks each gate is said, so
    the reader knows which postings to report anyway (the app screens them, with the reason, where the owner sees
    it)."""
    c, s = profile.criteria, profile.scoring
    # A gate for some target roles only (a role's tab on Hard pass) says which, as an ask does: the reader checks it
    # only for a posting matched to one of them (docs/PRD.md 31.9).
    gates = [{"id": line["id"], "says": line["says"], "who_checks": WHO_CHECKS[line["who"]],
              "only_for_roles": line["roles"] or None}
             for line in profile_gates.gate_lines(profile.gates) if line["on"]]
    out = {
        "what_to_look_for": look_for(c),
        "hard_gates": gates,
        "scoring": {"sections": scoring_for_agent(s, c)},
        "rules": [r.says for r in profile.settings.rules if r.area in ("search", "screening")],
        "not_placed_yet": [n.text for doc in (c, profile.gates, s, profile.settings) for n in doc.notes],
    }
    return _compact(out)


#: What an agent is told of an ask, a statement or a row the app decides itself.
APP_DECIDES = "the app: leave it out"
#: The question a first-match row puts to an agent, and what an agent is told of the statements of an "any of" row, in
#: plain words (docs/PRD.md 30.9): each statement is answered like an ask, and the app works out the row.
FIRST_MATCH_NOTE = ("answer every statement of the row on its own: the row earns the first one, from the top, that is "
                    "met")
ANY_OF_NOTE = "the row earns once when any one of its statements is true"


def says(profile: Profile) -> dict:
    """Each gate and ask of the profile as the sentence the page shows for it (`GET /api/v1/profile`'s `says`, and the
    owner door's `profile.read`): the work location and the pay floor of All's tab, the lines of the gates in a part of
    a posting by their ids, a role's own tab by the role's id, and each ask by its id."""
    return {
        "gates": {
            "location": profile_gates.location_says(profile.gates.location),
            "compensation": profile_gates.compensation_says(profile.gates.compensation),
            "other": {g.id: profile_gates.gate_says(g) for g in profile.gates.other},
            # A role's own tab on Hard pass: the sentence of each of its structured gates, by the role's id.
            "by_role": {
                r: {"location": profile_gates.location_says(own.location),
                    "compensation": profile_gates.compensation_says(own.compensation)}
                for r, own in profile.gates.by_role.items()},
        },
        "asks": {a.id: profile_scoring.ask_says(a) for a in profile.scoring.asks},
    }


def _pull(points: int) -> str:
    return "adds points" if points > 0 else "noted only"


def _first_match_rows(g: Group) -> list[dict]:
    """The statements of a first-match row as an agent reads them: asks, each with the row it is part of, the note that
    the app takes the first one from the top that is met, and the way it pulls. A statement the app decides carries the
    same leave-it-out note an ask does. Never a point value."""
    return [{"id": m.id, "says": m.says, "verify": profile_scoring.verify_of(m),
             "effect": _pull(m.points), "part_of": g.id,
             "first_match": FIRST_MATCH_NOTE,
             "who_checks": APP_DECIDES if profile_scoring.decided_by_the_app(m) else None,
             "only_for_roles": g.roles or None}
            for m in g.members]


def _any_of_rows(g: Group) -> list[dict]:
    """The statements of an "any of" row as an agent reads them: asks, each with the row it is part of, the note that
    the row earns once when any one of them is true, and the row's effect. A statement the app decides carries the
    same leave-it-out note an ask does. Never a point value."""
    return [{"id": m.id, "says": m.says, "verify": profile_scoring.verify_of(m),
             "effect": _pull(g.points), "part_of": g.id,
             "any_of": ANY_OF_NOTE,
             "who_checks": APP_DECIDES if profile_scoring.decided_by_the_app(m) else None,
             "only_for_roles": g.roles or None}
            for m in g.members]


def scoring_for_agent(scoring: Scoring, criteria: Criteria | None = None) -> list[dict]:
    """The owner's asks and rows as an agent reads them, by the part of a posting they are about, in posting order. No
    points: the agent answers each ask once and never adds anything up; it is told which way an ask or row pulls, and
    which the app decides itself (docs/PRD.md 30.6, 30.9). The statements of a "first match" row (`_first_match_rows`)
    and of an "any of" row (`_any_of_rows`) are asks, each answered by its own id, and the app works the row out. An
    ask, statement or row built from an employer fact or the analyst's classes is the app's: it is marked, and no
    agent is asked it (`decided_by_the_app`). An ask or row on a role's own tab names that role (`only_for_roles`):
    the agent answers it only for a posting matched to it (31.5, 31.9); one on the All tab names none. The built-in
    ask that follows a tab's Responsibilities is put as the question it is (31.6), and only when the owner has kept
    it and some role has Responsibilities written: with no `criteria` to say so, or none written, no role has the
    ask, so the agent is not sent it (an owner who removed it has no such row to send). The app's own row that
    grades the requirements is nobody's question and is not sent."""
    has_responsibilities = criteria is not None and any(
        r.responsibilities for r in criteria.target_roles)
    asks_of = {section: asks for section, _label, asks in profile_scoring.asks_by_section(scoring)}
    out = []
    for section, label in SECTIONS:
        rows = []
        for a in asks_of.get(section, []):
            derived = a.derived == "responsibilities"
            if derived and not has_responsibilities:
                continue
            rows.append({
                "id": a.id, "says": RESPONSIBILITIES_QUESTION if derived else a.says,
                "verify": profile_scoring.verify_of(a),
                "effect": _pull(a.points),
                "count_each_item": a.per_item or None,
                "who_checks": APP_DECIDES if profile_scoring.decided_by_the_app(a) else None,
                "only_for_roles": a.roles or None,
                "also": ("answer it only when the posting's target role has responsibilities written; the app "
                         "leaves it out of the score otherwise") if derived else None,
            })
        for g in scoring.groups:
            if g.section == section and scoring.item_on_anywhere(g):
                rows += _first_match_rows(g) if g.kind == "best" else _any_of_rows(g)
        if rows:
            out.append({"section": label, "id": section, "asks": rows})
    return _compact(out)


def _compact(value):
    """Drop empty values all the way down, so the scout reads only what the owner has set."""
    if isinstance(value, dict):
        kept = {k: _compact(v) for k, v in value.items()}
        return {k: v for k, v in kept.items() if v not in (None, "", [], {}, [None, None])}
    if isinstance(value, list):
        return [_compact(v) for v in value if v not in (None, "", [], {})]
    return value
