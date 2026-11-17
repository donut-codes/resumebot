"""The estimated ask's worked example (docs/PRD.md 26.6): the settings page shows the method's numbers worked on a
made-up posting, and the figure is whatever `comp.compute` gives for them with the numbers as they stand on the page,
saved or not. The page keeps no copy of the math, and the example reads nothing the owner saved."""

import pytest

from app.jobsearch.fit import comp
from app.jobsearch.profile import schema as p

URL = "/api/v1/profile/comp-example"
BAND = [150_000, 200_000]


def worked(**settings) -> dict:
    """`compute` on the made-up posting (a $150,000 to $200,000 range, four of five required items Proven), with these
    settings and no pay floor, the posting written out here and not taken from the example's own code."""
    inputs = comp.CompInputs.model_validate({
        "band": BAND, "required": [{"item": f"r{n}", "class": "P"} for n in range(4)] + [{"item": "r4", "class": "U"}]})
    return comp.compute(inputs, p.Profile.model_validate({"settings": settings}))


def shown(result: dict) -> dict:
    """What the example gives of a `compute` result."""
    return {k: result[k] for k in ("band", "coverage", "ask", "by_method", "likely_offer", "capped_at_likely_offer")}


async def asked(client, **numbers) -> dict:
    got = await client.get(URL, params=numbers)
    assert got.status_code == 200, got.text
    return got.json()


async def test_with_the_default_numbers_the_example_is_what_compute_gives(client):
    got = await asked(client)
    assert got == shown(worked())
    assert got["band"] == BAND and got["coverage"] == 0.8
    # The likely-offer limit, not the placement in the range, decides the ask with the defaults: a hand-written example
    # that placed the ask by the tiers alone would be wrong.
    assert got["ask"] <= got["likely_offer"] < got["by_method"]
    assert got["capped_at_likely_offer"] is True


@pytest.mark.parametrize("numbers", [
    {"comp_tiers": "0.9:0.2, 0.5:0.1, 0:0.05"},
    {"comp_likely_low": 1.2, "comp_likely_high": 1.2},
    {"comp_likely_low": 0.6, "comp_likely_high": 0.9, "comp_round_to": 1000},
    {"comp_round_to": 10000},
    {"comp_tiers": "0.75:0.9, 0:0.5", "comp_likely_high": 1.2},
])
async def test_a_changed_number_changes_the_example_as_it_changes_compute(client, numbers):
    got = await asked(client, **numbers)
    assert got == shown(worked(**numbers))
    assert got != await asked(client), "the example moved with the number"


async def test_an_ask_the_limit_does_not_hold_down_is_said_so(client):
    got = await asked(client, comp_likely_low=1.2, comp_likely_high=1.2)
    assert got["capped_at_likely_offer"] is False and got["ask"] == got["by_method"]


async def test_the_example_reads_nothing_the_owner_saved(client):
    """A saved pay floor, saved tiers and a saved step change the owner's real asks, never the example: it is worked
    from the numbers in the address alone."""
    before = await asked(client)
    for kind, data in (("gates", {"compensation": {"amount": 190_000, "against": "ask"}}),
                       ("settings", {"comp_tiers": "0.5:0.9, 0:0.9", "comp_round_to": 1000, "comp_likely_low": 1.2,
                                     "comp_likely_high": 1.2})):
        saved = await client.put(f"/api/v1/profile/{kind}", json={"data": data, "why": "made up"})
        assert saved.status_code == 200, saved.text
    assert await asked(client) == before
    # And the numbers given are used, not the saved ones.
    assert await asked(client, comp_round_to=10000) == shown(worked(comp_round_to=10000))


async def test_a_number_that_does_not_fit_is_refused_in_words_and_so_is_a_word_that_is_no_number(client):
    low = await client.get(URL, params={"comp_likely_low": 0.1})
    assert low.status_code == 422 and "comp_likely_low" in low.json()["detail"]
    odd = await client.get(URL, params={"comp_round_to": "soon"})
    assert odd.status_code == 422 and "comp_round_to" in odd.json()["detail"]
    other = await client.get(URL, params={"drafting_bar": 10})
    assert other.status_code == 422 and "drafting_bar" in other.json()["detail"]
    assumed = await client.get(URL, params={"comp_assumptions": "standard benefits"})
    assert assumed.status_code == 422, "the words about the number change none"


def test_the_numbers_the_example_takes_are_settings_of_the_method():
    assert set(comp.NUMBERS) <= set(p.Settings.model_fields)
    assert all(name.startswith("comp_") for name in comp.NUMBERS)
