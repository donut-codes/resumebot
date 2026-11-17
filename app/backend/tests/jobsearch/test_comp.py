"""The comp ask: the method of the comp ask (docs/PRD.md), with its own three worked cases (employers made up), the
owner's floor from their gate and the points from their own pay asks."""

import pytest

from app.jobsearch.fit import comp, scoring
from app.jobsearch.profile import gates as pg
from app.jobsearch.profile import schema as p


def owner(floor=112500, tiers=((140000, False, -2), (160000, True, 1), (190000, True, 2)),
          settings=None) -> p.Profile:
    """An owner whose pay method rounds to $5,000 with a $10,000 range (`settings` changes that)."""
    return p.Profile.model_validate({
        "settings": settings if settings is not None else {"comp_round_to": 5000, "comp_range_width": 10000},
        "gates": {"compensation": {"amount": floor}} if floor else {},
        "scoring": {"asks": [
            {"id": f"pay-{amount}", "section": "compensation", "says": f"ask {'over' if above else 'under'} {amount}",
             "points": points, "amount": amount, "above": above}
            for amount, above, points in tiers]},
    })


def required(proven=0, transferable=0, unproven=0):
    return ([{"item": f"p{i}", "class": "P"} for i in range(proven)]
            + [{"item": f"t{i}", "class": "T"} for i in range(transferable)]
            + [{"item": f"u{i}", "class": "U"} for i in range(unproven)])


def test_a_figure_halfway_between_two_steps_rounds_up_not_to_the_even_one():
    """Round() is half-even, so $12,500 to the nearest $5,000 came out $10,000 and $17,500 $20,000."""
    assert comp.round_to(12_500, 5_000) == 15_000 and comp.round_to(17_500, 5_000) == 20_000
    assert comp.round_to(12_499, 5_000) == 10_000 and comp.round_to(7_000, 1_000) == 7_000


def test_a_posted_band_with_a_stance_is_capped_at_the_likely_offer():
    # band 200-245K, 7 of 8 Proven (c = 0.875), S = 1.026 from six siblings: the position gives 230K, the likely
    # offer (midpoint x (0.85 + 0.15 x 0.875), about 218.3K) caps the ask at 215K
    inputs = comp.CompInputs(
        band=[200000, 245000], required=required(proven=7, unproven=1),
        siblings=[{"name": f"s{i}", "low": 102600, "high": 102600, "market": 100000} for i in range(6)])
    r = comp.compute(inputs, owner())
    assert r["coverage"] == 0.875 and abs(r["position"] - 0.626) < 1e-3
    assert r["by_method"] == 230000 and r["ask"] == 215000 and r["range"] == [205000, 215000]
    assert abs(r["likely_offer"] - 218328) < 100 and r["capped_at_likely_offer"] is True
    assert r["expectation"] == 215000 and r["method_version"] == comp.METHOD_VERSION
    # posted band 30; no market sources 0; 8 items 10 + none cited 0; stance from six siblings 21
    assert r["confidence"]["score"] == 61
    assert (r["pay_points"], r["pay_modifier"]) == (3, "ask over 160000; ask over 190000"), (
        "215K is over both tiers, and each tier met earns its points")


def test_a_fixed_grade_holds_the_ask_at_the_band_low_and_clamps_the_range():
    inputs = comp.CompInputs(band=[122004, 128908], clamp_range_to_band=True,
                             required=required(proven=4, transferable=2))
    r = comp.compute(inputs, owner())
    assert r["ask"] == 123000 and r["range"] == [122004, 123000]
    assert r["pay_points"] == 0, "edited 2026-10-27 (PRD 30.10): the under-tier has -2 and earns nothing now (was -2)"


def test_with_no_posted_band_the_band_is_modelled_from_the_market_value():
    inputs = comp.CompInputs(market_value=180000, spread=0.2, required=required(proven=7, transferable=1))
    r = comp.compute(inputs, owner())
    assert r["band_modelled"] is True and r["band"] == [162000, 198000]
    assert r["floor"] <= r["ask"] <= 198000


def test_no_band_and_no_market_value_is_refused_in_words():
    with pytest.raises(ValueError, match="give market_value and spread"):
        comp.compute(comp.CompInputs(required=required(proven=1)), owner())


def test_the_owners_floor_and_pay_asks_are_theirs_and_none_means_none():
    inputs = comp.CompInputs(band=[60000, 70000], required=required(proven=1))
    low = comp.compute(inputs, owner(floor=90000))
    assert low["ask"] == 65000 and low["below_floor"] is True, "the floor never lifts the ask, even to the band's top"
    r = comp.compute(inputs, owner(floor=0, tiers=()))
    assert r["floor"] == 0 and r["pay_points"] == 0 and r["pay_modifier"] == ""


def test_the_pay_ask_and_the_roles_fit_score_the_pay_tiers_the_same_way():
    """An ask over two tiers earns both (every ask counts on its own, PRD 30.6), in the pay ask's line (`pay_points`)
    and in the role's fit (`scoring.compute`); the most the tiers can add counts each positive tier once."""
    prof = owner()
    # Edited 2026-10-27 (PRD 30.10): the under-140,000 tier has -2 points and earned -2 at 130,000; it earns 0 now.
    for ask, points, says in [(130_000, 0, "ask under 140000"), (150_000, 0, ""), (160_000, 0, ""),
                              (165_000, 1, "ask over 160000"), (190_000, 1, "ask over 160000"),
                              (200_000, 3, "ask over 160000; ask over 190000")]:
        assert comp.pay_points(ask, prof) == (points, says), ask
        fit = scoring.compute({"asks": []}, prof, facts={"ask": ask})
        assert sum(a["earned"] for a in fit["asks"] if a["id"].startswith("pay-")) == points, ask
        assert fit["total"] == points, ask
    assert scoring.possible(prof) == 1 + 2, "each positive tier counts once; the negative one adds nothing"


def test_a_class_must_be_proven_transferable_or_unproven():
    with pytest.raises(ValueError):
        comp.CompInputs(band=[1, 2], required=[{"item": "x", "class": "maybe"}])


def test_with_no_dollar_figure_the_rounding_and_the_range_are_a_share_of_the_ask():
    """A new install's step is the nearest 1, 2 or 5 times a power of ten to 4% of the ask, and its range is 8%
    of the ask rounded to that step, so one setting fits low pay and high."""
    assert [comp.round_step(None, ask) for ask in (30_000, 60_000, 100_000, 120_000, 200_000, 400_000)] == [
        1_000, 2_000, 5_000, 5_000, 10_000, 20_000]
    assert comp.round_step(2_500, 400_000) == 2_500, "a dollar figure the owner set is used as it is"
    assert comp.range_width(None, 120_000, 5_000) == 10_000 and comp.range_width(None, 60_000, 2_000) == 4_000
    assert comp.range_width(0, 120_000, 5_000) == 0 and comp.range_width(7_000, 120_000, 5_000) == 7_000
    new_install = owner(floor=0, tiers=(), settings={})
    low = comp.compute(comp.CompInputs(band=[55_000, 65_000], required=required(proven=4)), new_install)
    high = comp.compute(comp.CompInputs(band=[330_000, 390_000], required=required(proven=4)), new_install)
    assert low["ask"] % 2_000 == 0 and low["ask"] - low["range"][0] == 4_000
    assert high["ask"] % 10_000 == 0 and high["ask"] - high["range"][0] == 30_000


def test_a_new_install_starts_with_no_assumptions_sentence_no_dollar_steps_and_a_pay_gate_on_the_posted_maximum():
    st = p.Settings()
    assert (st.comp_assumptions, st.comp_round_to, st.comp_range_width) == ("", None, None)
    assert pg.Compensation().against == "maximum"


# --- the steps, one at a time ---

#: Likely offer at its highest, so the band's own top is the only cap and the figure the method gives shows as it is.
UNCAPPED = {"comp_likely_low": 1.2, "comp_likely_high": 1.2}


def uncapped(**more) -> dict:
    """Settings with a $5,000 step, a $10,000 range and no cap from the likely offer, and `more`."""
    return {"comp_round_to": 5000, "comp_range_width": 10000, **UNCAPPED, **more}


def siblings(ratio: float, n: int = 3) -> list[dict]:
    """`n` other bands at the employer, each sitting at `ratio` of its market value."""
    return [{"name": f"s{i}", "low": 100 * ratio, "high": 100 * ratio, "market": 100} for i in range(n)]


@pytest.mark.parametrize("c, base", [
    (1.0, 0.70), (0.90, 0.70), (0.8999, 0.60), (0.75, 0.60), (0.7499, 0.45), (0.60, 0.45), (0.5999, 0.30),
    (0.45, 0.30), (0.4499, 0.15), (0.0, 0.15)])
def test_the_base_position_changes_at_each_coverage_tier_and_not_a_hair_before(c, base):
    assert comp.base_position(c) == base


def test_the_owners_tiers_are_read_highest_first_and_a_bad_list_is_the_default():
    mine = ((0.8, 0.5), (0.5, 0.3), (0.0, 0.1))
    assert comp.tiers_of("0.5:0.3, 0.8:0.5, 0:0.1") == mine, "in any order"
    assert comp.tiers_of(" 0.8:0.5,0.5:0.3 ,0.0:0.1, ") == mine, "spaces and a trailing comma"
    for bad in ("", "soon", "0.9:0.7", "0.9:0.7, 0.5", "0.9;0.7, 0:0.1", "a:b, 0:0.1"):
        assert comp.tiers_of(bad) == comp.TIERS, bad
    assert comp.base_position(0.5, mine) == 0.3 and comp.base_position(0.95, mine) == 0.5


def test_the_owners_tiers_decide_where_in_the_band_the_ask_starts():
    inputs = comp.CompInputs(band=[100000, 200000], required=required(proven=1, unproven=1))
    default = comp.compute(inputs, owner(floor=0, settings=uncapped()))
    mine = comp.compute(inputs, owner(floor=0, settings=uncapped(comp_tiers="0.5:0.9, 0:0.2")))
    assert (default["coverage"], default["position"], default["by_method"]) == (0.5, 0.3, 130000)
    assert (mine["coverage"], mine["position"], mine["by_method"]) == (0.5, 0.9, 190000)


@pytest.mark.parametrize("weight, coverage, position", [(0.0, 0.5, 0.30), (0.5, 0.75, 0.60), (1.0, 1.0, 0.70)])
def test_a_transferable_item_counts_as_much_of_a_proven_one_as_the_owner_says(weight, coverage, position):
    inputs = comp.CompInputs(band=[100000, 200000], required=required(proven=2, transferable=2))
    r = comp.compute(inputs, owner(floor=0, settings=uncapped(comp_transferable_weight=weight)))
    assert (r["coverage"], r["position"]) == (coverage, position)
    assert (r["proven"], r["transferable"], r["unproven"]) == (2, 2, 0)


def test_the_employers_stance_moves_the_position_by_at_most_the_clamp_either_way():
    inputs = dict(band=[100000, 130000], required=required(proven=4))  # c = 1, base 0.70
    up = comp.compute(comp.CompInputs(siblings=siblings(1.5), **inputs), owner(floor=0, settings=uncapped()))
    down = comp.compute(comp.CompInputs(siblings=siblings(0.5), **inputs), owner(floor=0, settings=uncapped()))
    assert (up["stance"], up["stance_adjustment"], up["position"]) == (1.5, 0.1, 0.8)
    assert (down["stance"], down["stance_adjustment"], down["position"]) == (0.5, -0.1, 0.6)
    assert (up["by_method"], down["by_method"]) == (125000, 120000)
    wide = comp.compute(comp.CompInputs(siblings=siblings(1.5), **inputs),
                        owner(floor=0, settings=uncapped(comp_stance_clamp=0.3)))
    assert (wide["stance_adjustment"], wide["position"], wide["by_method"]) == (0.3, 1.0, 130000)
    flat = comp.compute(comp.CompInputs(siblings=siblings(1.5), **inputs),
                        owner(floor=0, settings=uncapped(comp_stance_clamp=0)))
    assert flat["stance_adjustment"] == 0 and flat["position"] == 0.7


def test_a_posting_that_says_where_hires_land_gets_no_move_up_but_can_still_move_down():
    base = dict(band=[100000, 130000], required=required(proven=4), stance_cap_up=True)
    up = comp.compute(comp.CompInputs(siblings=siblings(1.5), **base), owner(floor=0, settings=uncapped()))
    down = comp.compute(comp.CompInputs(siblings=siblings(0.5), **base), owner(floor=0, settings=uncapped()))
    assert (up["stance"], up["stance_adjustment"], up["position"]) == (1.5, 0, 0.7)
    assert (down["stance_adjustment"], down["position"]) == (-0.1, 0.6)
    assert up["ask_with_no_stance_move"] == up["ask"] == 120000


def test_the_employers_other_bands_count_only_from_the_owners_minimum_and_a_band_with_no_market_is_ignored():
    inputs = dict(band=[100000, 130000], required=required(proven=4))
    two = comp.CompInputs(siblings=siblings(1.5, n=2), **inputs)
    assert comp.compute(two, owner(floor=0, settings=uncapped()))["stance"] == 1.0, "two are too few by default"
    counted = comp.compute(two, owner(floor=0, settings=uncapped(comp_stance_min_siblings=2)))
    assert counted["stance"] == 1.5 and counted["confidence"]["parts"][-1]["points"] == 21
    no_market = [*siblings(1.5, n=2), {"name": "x", "low": 90, "high": 90, "market": 0}]
    ignored = comp.compute(comp.CompInputs(siblings=no_market, **inputs), owner(floor=0, settings=uncapped()))
    assert ignored["stance"] == 1.0 and ignored["confidence"]["parts"][-1]["points"] == 6
    odd = [{"name": n, "low": r * 100, "high": r * 100, "market": 100} for n, r in (("a", 0.9), ("b", 1.3), ("c", 1.1))]
    assert comp.compute(comp.CompInputs(siblings=odd, **inputs), owner(floor=0, settings=uncapped()))["stance"] == 1.1


def test_the_ask_is_rounded_to_the_owners_step_and_the_range_reaches_the_owners_width_below_it():
    inputs = comp.CompInputs(band=[100000, 130000], required=required(proven=4))  # 0.70 of the band is $121,000
    cases = [
        (5000, 10000, 120000, 120000 - 10000), (1000, 10000, 121000, 111000), (2500, 0, 120000, 120000),
        (5000, 3000, 120000, 117000), (None, None, 120000, 120000 - 10000)]
    for step, width, ask, low in cases:
        settings = {"comp_round_to": step, "comp_range_width": width, **UNCAPPED}
        r = comp.compute(inputs, owner(floor=0, settings=settings))
        assert (r["ask"], r["range"]) == (ask, [low, ask]), (step, width)


def test_the_range_never_reaches_below_a_floor_under_the_ask_and_a_floor_above_never_lifts_it():
    inputs = comp.CompInputs(band=[100000, 130000], required=required(proven=4))
    r = comp.compute(inputs, owner(floor=118000, settings=uncapped()))
    assert r["ask"] == 120000 and r["range"] == [118000, 120000], "the width alone would have gone below the floor"
    above = comp.compute(inputs, owner(floor=125000, settings=uncapped()))
    assert above["ask"] == 120000 and above["range"] == [above["ask"] - 10000, above["ask"]]
    assert above["below_floor"] is True


def test_the_likely_offer_caps_the_ask_and_the_owners_likely_line_moves_the_cap():
    inputs = comp.CompInputs(band=[100000, 130000], required=required(proven=4))  # midpoint 115,000, c = 1
    default = comp.compute(inputs, owner(floor=0))
    assert (default["by_method"], default["likely_offer"], default["ask"]) == (120000, 115000, 115000)
    assert default["capped_at_likely_offer"] is True and default["likely_line"] == [0.85, 1.0]
    low_line = {"comp_round_to": 5000, "comp_range_width": 10000, "comp_likely_low": 0.5, "comp_likely_high": 0.6}
    lower = comp.compute(inputs, owner(floor=0, settings=low_line))
    assert (lower["likely_offer"], lower["ask"]) == (100000, 100000), "the likely offer is kept inside the band"


def test_a_single_posted_figure_is_a_band_of_one_number_and_the_ask_is_that_number():
    r = comp.compute(comp.CompInputs(band=[150000], required=required(proven=2, unproven=2)), owner(floor=0))
    assert r["single_figure"] is True and r["band"] == [150000, 150000] and r["ask"] == 150000
    assert r["confidence"]["parts"][0]["points"] == 20, "one figure is worth less than a posted range"


def test_the_ask_is_the_predicted_likely_ask_and_a_floor_above_it_never_lifts_it():
    """The owner, 2026-10-16: the ask is what we predict, the spot in the range capped at the likely offer, never
    raised to the lowest pay; one under it says so (`below_floor`) and the pay asks score that number."""
    # c = (3 + 0.5 x 2) / 6; position 0.45 gives 127,000, rounded 125,000; the likely offer is the midpoint
    # 130,000 x (0.85 + 0.15 x c) = 123,500, which caps the ask at 120,000
    inputs = comp.CompInputs(band=[100000, 160000], required=required(proven=3, transferable=2, unproven=1))
    r = comp.compute(inputs, owner(floor=140000))
    assert (r["by_method"], r["ask"], r["likely_offer"]) == (125000, 120000, 123500)
    assert r["below_floor"] is True and r["capped_at_likely_offer"] is True
    assert r["range"] == [r["ask"] - 10000, r["ask"]]
    assert r["pay_points"] == 0, "the ask meets the -2 under-tier, which earns nothing (edited 2026-10-27, PRD 30.10)"
    under = comp.compute(inputs, owner(floor=90000))
    assert under["ask"] == 120000 and under["below_floor"] is False
    assert comp.compute(inputs, owner(floor=0))["below_floor"] is False


def test_the_responsibilities_count_in_the_coverage_as_more_items_and_not_in_the_confidence():
    inputs = comp.CompInputs(band=[100000, 130000], required=required(proven=2), cited=2,
                             responsibilities=required(transferable=2, unproven=2))
    r = comp.compute(inputs, owner(floor=0))
    assert r["coverage"] == round((2 + 0.5 * 2) / 6, 3), "same classes and weight, as more items"
    assert (r["proven"], r["transferable"], r["unproven"]) == (2, 2, 2)
    assert r["qualifications"] == {"proven": 2, "transferable": 0, "unproven": 0}
    assert r["responsibilities"] == {"proven": 0, "transferable": 2, "unproven": 2}
    parts = {x["name"]: x for x in r["confidence"]["parts"]}
    assert parts["Requirements"]["note"] == "2 required items, 2 of 2 backed ones cited, 0 Transferable"
    alone = comp.compute(comp.CompInputs(band=[100000, 130000], required=required(proven=2)), owner(floor=0))
    assert alone["coverage"] == 1.0 and alone["responsibilities"] == {"proven": 0, "transferable": 0, "unproven": 0}


def _basis(r: dict) -> dict:
    return {s["step"]: s["basis"] for s in r["steps"]}


def test_each_step_is_labelled_by_what_it_rests_on():
    estimate = comp.compute(comp.CompInputs(band=[100000, 130000], required=required(proven=1),
                                            market_sources=["An analyst's estimate for the title and place"]),
                            owner(floor=0))
    assert _basis(estimate) == {"band": "sourced", "market": "estimate", "stance": "rule of thumb",
                                "coverage": "rule of thumb", "position": "rule of thumb",
                                "likely_offer": "rule of thumb", "rounding": "rule of thumb"}
    sized = comp.compute(comp.CompInputs(band=[100000, 130000], required=required(proven=1),
                                         market_sources=["Survey A: $115K (n=340)"]), owner(floor=0))
    assert _basis(sized)["market"] == "sourced", "a source that names its sample"
    modelled = comp.compute(comp.CompInputs(market_value=120000, spread=0.2, required=required(proven=1)),
                            owner(floor=0))
    assert _basis(modelled)["band"] == "rule of thumb", "a band modelled from the market is the method's"


def test_a_duty_that_stands_in_as_a_required_item_counts_once_and_the_confidence_counts_the_cited_ones():
    """The posting's responsibilities from the research leave out a duty that stands in as a required item
    (`from_duties`); the confidence note counts how many of the backed required items cite a master line."""
    research = {
        "required": [{"item": "Runs a program", "class": "P", "from": ["b-1"]},
                     {"item": "Leads a team", "class": "T", "from": ["b-2"]},
                     {"item": "Holds a licence", "class": "U"},
                     {"item": "Plans the work", "class": "P", "from": ["b-3"], "from_duties": True}],
        "responsibilities": [{"item": "Plans the work.", "class": "P", "from": ["b-3"]},
                             {"item": "Reports to the board", "class": "U"}],
    }
    assert comp.responsibilities_of(research) == [{"item": "Reports to the board", "class": "U"}], (
        "a duty that stands in as a required item counts once, there")
    done = comp.CompInputs(band=[100000, 130000], cited=2, required=[
        {"item": "runs a program", "class": "P"}, {"item": "Leads a team.", "class": "T"},
        {"item": "Holds a licence", "class": "U"}])
    r = comp.compute(done, owner(floor=0))
    assert "2 of 2 backed ones cited" in r["confidence"]["parts"][2]["note"]
