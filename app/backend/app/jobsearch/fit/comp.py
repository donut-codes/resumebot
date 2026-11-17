"""The starting-comp expectation (docs/PRD.md 10.3, 25.6, 26): one number for a salary field, worked out by code from
the researched inputs, never by hand; the same inputs always give the same number. Since path 26 its rules of thumb are
the owner's settings, each step says whether it is sourced or a rule of thumb (`steps`), and each expectation has a
confidence worked out from its inputs (`confidence`).

The method:
    c     = (Proven + 0.5 x Transferable) / items, the posting's required items and its responsibilities together
            (the responsibilities count as more required items, by the same classes and weights, as they do in the
            fit: the owner, 2026-10-16; the qualifications gate still reads the required items alone)
    base  = 0.70 when c >= 0.90; 0.60 at 0.75; 0.45 at 0.60; 0.30 at 0.45; else 0.15
    S     = median(sibling band midpoint / sibling market value) with 3 or more sibling bands, else 1.0
    pos   = base + clamp(S - 1, -0.10, +0.10)    (no upward move when the posting says where hires land)
    ask   = L + pos x (H - L), rounded to a step, never above H, and never above the likely offer rounded down to
            that step: the predicted likely ask. It is not raised to the owner's lowest pay (the owner, 2026-10-16); one
            under it says so (`below_floor`), and the pay
            asks and the pay gate judge that honest number.
    range = ask less a width to ask (the low end at least the floor while the floor is under the ask)
    likely offer = P x (low + (high - low) x c), P = (L + H) / 2, kept inside the band (low 0.85, high 1.00)
With no posted band, the band is modelled from the market value: P0 = M x S, [L, H] = P0 x (1 -/+ spread / 2).
The step and the width are the owner's dollar figures when set (settings comp_round_to and comp_range_width); left
blank they are a share of the ask (`ROUND_SHARE`, `RANGE_SHARE`), so one setting fits a $60K search and a $300K one.

The floor is the owner's pay-floor gate (shown beside the ask, never moving it); the points the ask earns are the
owner's own pay asks (scoring): nothing here names a figure.
"""

import math
import re
import statistics

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.jobsearch.fit import scoring
from app.jobsearch.profile.schema import Profile, Settings
from app.jobsearch.profile.views import pay_floor

#: Coverage tiers, highest first: (least c, base position). The default; the owner's are in settings (comp_tiers).
TIERS = ((0.90, 0.70), (0.75, 0.60), (0.60, 0.45), (0.45, 0.30), (0.0, 0.15))
#: Bumped when the method changes, so a stored expectation says which method made it. 3: the responsibilities count in
#: the coverage, and each step's label is worked out from what it rests on (`steps`). 4: the ask is the predicted
#: likely ask, no longer raised to the owner's floor.
METHOD_VERSION = 4

#: The practice the method's rules of thumb follow (docs/PRD.md 26.6), linked from the steps that follow it.
SOURCES = {
    "ranges": ("https://www.complete.so/blog/a-candidates-guide-to-pay-transparency",
               "Pay ranges centre on the market median, about 15 to 25% either side."),
    "placement": ("https://hr.wisc.edu/docs/recruitment/determining-starting-salary-of-a-new-hire.pdf",
                  "Starting pay is placed in the range by how well the candidate meets the requirements."),
    "new_hires": ("https://pave.com/blog-posts/compa-ratios-for-new-hires-vs-existing-employees",
                  "New hires typically land at about 80 to 100% of the range's midpoint."),
}
#: What a step can rest on: a figure with a named source (the posting, or a market survey that gives its sample),
#: an estimate (market research that names no sample), or a rule of thumb (a formula or a judgement of the method's).
SOURCED, ESTIMATE, RULE_OF_THUMB = "sourced", "estimate", "rule of thumb"
#: A market source that says how many data points it rests on ("n=340", "1,200 salaries").
SAMPLE = re.compile(r"\b(?:n\s*=\s*\d+|\d[\d,]*\s+(?:salaries|reports|data points|responses|employees|profiles))", re.I)


def has_sample(source: str) -> bool:
    return bool(SAMPLE.search(source))


def steps(modelled: bool, market_sources: list[str]) -> list[dict]:
    """Each step and what it really rests on (docs/PRD.md 26.6). A posted band is sourced: the posting is its source. A
    band modelled from the market is a rule of thumb. The market figure is sourced only when a source names its sample;
    otherwise it is an analyst's estimate. Every other step is the method's own formula or judgement, so a rule of
    thumb, linked to the practice it follows where there is one."""
    sized = any(has_sample(x) for x in market_sources if x.strip())
    return [
        {"step": "band", "basis": RULE_OF_THUMB, "source": "ranges"} if modelled
        else {"step": "band", "basis": SOURCED},
        {"step": "market", "basis": SOURCED if sized else ESTIMATE},
        {"step": "stance", "basis": RULE_OF_THUMB},
        {"step": "coverage", "basis": RULE_OF_THUMB},
        {"step": "position", "basis": RULE_OF_THUMB, "source": "placement"},
        {"step": "likely_offer", "basis": RULE_OF_THUMB, "source": "new_hires"},
        {"step": "rounding", "basis": RULE_OF_THUMB},
    ]


def tiers_of(text: str) -> tuple[tuple[float, float], ...]:
    """The owner's coverage tiers ("0.90:0.70, 0.75:0.60, ..."), highest first; the default when it doesn't read."""
    try:
        pairs = sorted(((float(a), float(b)) for a, b in (x.split(":") for x in text.split(",") if x.strip())),
                       reverse=True)
    except ValueError:
        return TIERS
    return tuple(pairs) if pairs and pairs[-1][0] <= 0 else TIERS


class Required(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str
    #: P (Proven: direct evidence), T (Transferable: a real, specific bridge), U (Unproven).
    cls: str = Field(alias="class")

    @field_validator("cls")
    @classmethod
    def _cls(cls, v: str) -> str:
        v = v.strip().upper()[:1]
        if v not in ("P", "T", "U"):
            raise ValueError("class is P (Proven), T (Transferable) or U (Unproven)")  # never N: it is in no count
        return v


class Sibling(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    low: float
    high: float
    market: float


class CompInputs(BaseModel):
    """What the writer researched for one role."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    band: list[float] | None = Field(default=None, description="[low, high] posted for this role and place")
    market_value: float | None = None
    market_sources: list[str] = []
    spread: float | None = Field(default=None, description="(H - L) / P, to model a band when none is posted")
    siblings: list[Sibling] = []
    required: list[Required] = Field(min_length=1)
    #: The posting's responsibilities, classed the same way: more items in the coverage, never in the confidence.
    responsibilities: list[Required] = []
    #: How many Proven or Transferable required items cite a master line (for the confidence).
    cited: int | None = None
    stance_note: str = ""
    stance_cap_up: bool = False
    clamp_range_to_band: bool = False
    cost_of_living: str = ""
    confidence: str = ""


def round_to(x: float, step: int) -> int:
    """`x` to the nearest multiple of `step`, a half up (round() takes it to the even one: $12,500 to the nearest
    $5,000 is $15,000, as the totals round, not $10,000)."""
    return math.floor(x / step + 0.5) * step


#: The share of the ask the rounding step and the range's width are when the owner sets no dollar figure: the step is
#: the nearest 1, 2 or 5 times a power of ten to this share of the ask, and the width is this share of the ask rounded
#: to that step.
ROUND_SHARE = 0.04
RANGE_SHARE = 0.08


def round_step(setting: int | None, ask: float) -> int:
    """The step the ask is rounded to: the owner's dollar figure, or (blank) the nearest 1, 2 or 5 times a power of ten
    to `ROUND_SHARE` of the ask, never under 1."""
    if setting:
        return max(1, setting)
    target = max(1.0, ROUND_SHARE * ask)
    power = 10 ** math.floor(math.log10(target))
    return int(min((m * power for m in (1, 2, 5, 10)), key=lambda c: abs(c - target)))


def range_width(setting: int | None, ask: int, step: int) -> int:
    """How far under the ask the range reaches: the owner's dollar figure, or (blank) `RANGE_SHARE` of the ask rounded
    to the step (at least one step)."""
    if setting is not None:
        return setting
    return max(step, round_to(RANGE_SHARE * ask, step))


def base_position(c: float, tiers=TIERS) -> float:
    return next(base for least, base in tiers if c >= least)


def confidence(inputs: CompInputs, modelled: bool, single: bool, stance_counted: bool) -> dict:
    """0 to 100 from the inputs, by fixed rules (docs/PRD.md 26.9), with each part and what would raise it."""
    parts = []

    def part(name, points, of, note, raise_it=""):
        parts.append({"name": name, "points": points, "of": of, "note": note, "raise": raise_it if points < of else ""})

    if modelled:
        part("Band", 8, 30, "modelled from the market: no band posted", "a posted band for this role")
    elif single:
        part("Band", 20, 30, "one posted figure, not a range", "a posted range")
    else:
        part("Band", 30, 30, "posted for this role")
    sources = [x for x in inputs.market_sources if x.strip()]
    sized = sum(1 for x in sources if has_sample(x))
    market = min(3, len(sources)) * 6 + (6 if sized else 0)
    part("Market", market, 24, f"{len(sources)} source{'s' if len(sources) != 1 else ''}"
         f"{', with a sample size' if sized else ', no sample size'}",
         "more independent market sources, with their sample sizes" if len(sources) < 3 or not sized else "")
    n = len(inputs.required)
    trans = sum(1 for r in inputs.required if r.cls == "T")
    cited = inputs.cited if inputs.cited is not None else 0
    backed = sum(1 for r in inputs.required if r.cls in ("P", "T"))
    req = (10 if n >= 3 else 5) + round(15 * (cited / backed if backed else 1)) - round(5 * trans / n)
    part("Requirements", max(0, req), 25, f"{n} required item{'s' if n != 1 else ''}, {cited} of {backed} backed "
         f"ones cited, {trans} Transferable",
         "a master line cited for every Proven or Transferable item" if backed and cited < backed else
         ("more of the posting's required items" if n < 3 else ""))
    part("Employer stance", 21 if stance_counted else 6, 21,
         "from the employer's other posted bands" if stance_counted else "assumed neutral: too few other bands",
         "three or more of the employer's other posted bands, with market pay")
    return {"score": min(100, sum(p["points"] for p in parts)), "parts": parts}


def _tally(items: list[Required]) -> dict:
    return {"proven": sum(1 for r in items if r.cls == "P"), "transferable": sum(1 for r in items if r.cls == "T"),
            "unproven": sum(1 for r in items if r.cls == "U")}


def _key(text) -> str:
    """An item's words for matching: lower case, single spaces, no closing stop."""
    return " ".join(str(text or "").lower().split()).rstrip(".;:")


def responsibilities_of(analysis: dict | None) -> list[dict]:
    """The posting's classed responsibilities (`{item, class}`) from the role's research, less a duty that already
    stands in as a required item (`from_duties`, a posting with no qualifications), which counts once, there. The
    one rule for the coverage here and the fit's (`prioritize.responsibility_classes_of`)."""
    a = analysis or {}
    stand_ins = {_key(r.get("item")) for r in a.get("required") or [] if isinstance(r, dict) and r.get("from_duties")}
    return [{"item": str(r.get("item") or ""), "class": str(r.get("class") or "")}
            for r in a.get("responsibilities") or []
            if isinstance(r, dict) and str(r.get("class") or "") in ("P", "T", "U")
            and _key(r.get("item")) not in stand_ins]


def from_research(analysis: dict | None) -> CompInputs | None:
    """The inputs the ask is worked out from, read from a role's research: its pay findings (the band, the market
    value, the spread, the employer's other bands) and its classed required items and responsibilities. None when it
    has no required items classed yet."""
    a = analysis or {}
    pay, required = a.get("pay") or {}, a.get("required") or []
    required = [r for r in required if r.get("class") in ("P", "T", "U")]  # an N line is in no count (26.12)
    if not required:
        return None
    return CompInputs(
        band=pay.get("band"), market_value=pay.get("market_value"), market_sources=pay.get("market_sources") or [],
        spread=pay.get("spread"), stance_note=pay.get("note") or "",
        required=[Required.model_validate({"item": r["item"], "class": r["class"]}) for r in required],
        responsibilities=[Required.model_validate(r) for r in responsibilities_of(a)],
        cited=sum(1 for r in required if r.get("class") in ("P", "T") and r.get("from")),
        siblings=[Sibling.model_validate({"name": x["name"], "low": x["low"], "high": x["high"], "market": x["market"]})
                  for x in pay.get("siblings") or [] if x.get("market")])


def pay_points(ask: int, profile: Profile, target_role: str | None = None) -> tuple[int, str]:
    """The points the owner's pay asks give this ask, and which ones gave them: every pay tier the ask meets, of the
    asks that count for a role with this target role (All's and the role's own), earns its points, the same rule that
    scores the role's fit (`scoring.pay_tiers_met`): an ask over two tiers earns both. The words are the tiers' own
    sentences, joined."""
    met = scoring.pay_tiers_met(ask, profile, target_role)
    return sum(max(0, a.points) for a in met), "; ".join(a.says for a in met)


def compute(inputs: CompInputs, profile: Profile, target_role: str | None = None) -> dict:
    st = profile.settings
    tiers = tiers_of(st.comp_tiers)
    floor = pay_floor(profile, target_role)
    items = [*inputs.required, *inputs.responsibilities]
    n = len(items)
    proven = sum(1 for r in items if r.cls == "P")
    trans = sum(1 for r in items if r.cls == "T")
    c = (proven + st.comp_transferable_weight * trans) / n

    ratios = [((s.low + s.high) / 2.0) / s.market for s in inputs.siblings if s.market]
    counted = len(ratios) >= st.comp_stance_min_siblings
    stance = statistics.median(ratios) if counted else 1.0
    adj = max(-st.comp_stance_clamp, min(st.comp_stance_clamp, stance - 1.0))
    if inputs.stance_cap_up and adj > 0:
        adj = 0.0

    single = False
    if inputs.band and len(inputs.band) in (1, 2):
        # One posted figure is a band of one number (docs/PRD.md 26.8).
        low_band, high_band = float(inputs.band[0]), float(inputs.band[-1])
        single = low_band == high_band
        modelled = False
    else:
        if inputs.market_value is None or inputs.spread is None:
            raise ValueError("no posted band: give market_value and spread so the band can be modelled")
        p0 = inputs.market_value * stance
        low_band, high_band = p0 * (1 - inputs.spread / 2.0), p0 * (1 + inputs.spread / 2.0)
        modelled = True
    mid = (low_band + high_band) / 2.0

    base = base_position(c, tiers)
    pos = base + adj
    raw = low_band + pos * (high_band - low_band)
    step = round_step(st.comp_round_to, raw)

    def rnd(x: float) -> int:
        return round_to(x, step)

    ask = rnd(raw) if single else min(rnd(raw), int(high_band))
    by_method = ask
    likely = max(low_band, min(high_band, mid * (st.comp_likely_low + (st.comp_likely_high - st.comp_likely_low) * c)))
    cap = int(likely // step) * step
    if cap < ask:
        ask = max(cap, -(-int(low_band) // 1000) * 1000)  # never below the band's own low
    low = max(ask - range_width(st.comp_range_width, ask, step), floor if floor < ask else 0)
    if inputs.clamp_range_to_band:
        low = max(low, int(low_band))
    points, why = pay_points(ask, profile, target_role)
    return {
        "ask": ask, "expectation": ask, "method_version": METHOD_VERSION, "basis": st.comp_basis,
        "confidence": confidence(inputs, modelled, single, counted), "single_figure": single,
        "steps": steps(modelled, inputs.market_sources),
        "sources": {k: {"url": u, "says": w} for k, (u, w) in SOURCES.items()},
        "likely_line": [st.comp_likely_low, st.comp_likely_high],
        "range": [low, ask], "likely_offer": round(likely), "floor": floor,
        "band": [round(low_band), round(high_band)], "band_modelled": modelled, "midpoint": round(mid),
        "coverage": round(c, 3), "proven": proven, "transferable": trans, "unproven": n - proven - trans,
        "qualifications": _tally(inputs.required), "responsibilities": _tally(inputs.responsibilities),
        "stance": round(stance, 3), "stance_adjustment": round(adj, 3), "position": round(pos, 3),
        "by_method": by_method, "capped_at_likely_offer": ask < by_method,
        # The predicted ask is under the owner's lowest pay: a signal, said on the page, never hidden by lifting it.
        "below_floor": bool(floor) and ask < floor,
        "ask_with_no_stance_move": min(rnd(low_band + base * (high_band - low_band)), int(high_band)),
        "pay_points": points, "pay_modifier": why,
        "inputs": inputs.model_dump(mode="json", by_alias=True),
    }


#: The method's numbers, by setting name: the ones the settings page's worked example takes (`example`).
NUMBERS = ("comp_tiers", "comp_transferable_weight", "comp_stance_clamp", "comp_stance_min_siblings",
           "comp_likely_low", "comp_likely_high", "comp_round_to")
#: The made-up posting the example is worked on (docs/PRD.md 26.6): a posted range, and five required items, four of
#: them Proven (80% coverage). The same for everyone; it names no employer, place or pay of anyone's.
EXAMPLE_BAND = (150_000, 200_000)
EXAMPLE_PROVEN = 4
EXAMPLE_UNPROVEN = 1


def example(settings: Settings) -> dict:
    """The method's numbers as they stand on the page (a draft, not yet saved) worked on the made-up posting by
    `compute` itself, so the page never carries a copy of the math. Around the settings is an empty profile: no pay
    floor and no pay ask of anyone's, so the figure is the method's alone and nothing of the owner's is read."""
    inputs = CompInputs.model_validate({
        "band": list(EXAMPLE_BAND),
        "required": [{"item": f"requirement {n + 1}", "class": "P" if n < EXAMPLE_PROVEN else "U"}
                     for n in range(EXAMPLE_PROVEN + EXAMPLE_UNPROVEN)]})
    worked = compute(inputs, Profile(settings=settings))
    return {
        "band": worked["band"], "coverage": worked["coverage"], "ask": worked["ask"], "by_method": worked["by_method"],
        "likely_offer": worked["likely_offer"], "capped_at_likely_offer": worked["capped_at_likely_offer"],
    }
