"""The starting-comp expectation (path 26): one number from the inputs, the owner's settings moving it, a single
posted figure, the confidence, the owner correcting a requirement's class, and the answer ready to paste. Everything
is made up."""

import copy

from httpx import ASGITransport, AsyncClient

from app import db
from app.jobsearch.fit import comp
from app.jobsearch.main import app
from app.jobsearch.models import Opportunity
from app.jobsearch.profile import schema as p
from app.jobsearch.research import analysis, settle
from app.settings import settings
from tests.builders import analysis_setup, findings
from tests.support import FIVE, OWNER, PROFILE, load_profile


def owner(**settings_) -> p.Profile:
    data = copy.deepcopy(PROFILE)
    data["settings"] = {**PROFILE["settings"], **settings_}
    return p.Profile.model_validate(data)


def need(proven=0, transferable=0, unproven=0):
    return ([{"item": f"p{i}", "class": "P"} for i in range(proven)]
            + [{"item": f"t{i}", "class": "T"} for i in range(transferable)]
            + [{"item": f"u{i}", "class": "U"} for i in range(unproven)])


def test_the_same_inputs_give_the_same_number_and_the_owners_settings_move_it():
    inputs = comp.CompInputs(band=[200000, 260000], required=need(proven=4))
    a, b = comp.compute(inputs, owner()), comp.compute(inputs, owner())
    assert a["expectation"] == b["expectation"] == 230000, "full coverage: held at the likely offer, the midpoint"
    lower = comp.compute(inputs, owner(comp_likely_high=0.95))
    assert lower["expectation"] == 215000
    assert {s["step"]: s["basis"] for s in a["steps"]}["likely_offer"] == "rule of thumb", "a formula"
    assert a["sources"]["new_hires"]["url"].startswith("https://")


def test_one_posted_figure_is_a_band_of_one_number_and_says_so_in_its_confidence():
    r = comp.compute(comp.CompInputs(band=[200000], required=need(proven=3, transferable=1),
                                     market_sources=["Survey A: $210K (n=340)", "Board B: $195K"], cited=4), owner())
    assert r["single_figure"] is True and r["expectation"] == 200000
    parts = {x["name"]: x for x in r["confidence"]["parts"]}
    assert parts["Band"]["points"] == 20 and "one posted figure" in parts["Band"]["note"]
    assert parts["Market"]["points"] == 18 and parts["Market"]["raise"]
    assert parts["Requirements"]["points"] == 10 + 15 - 1
    assert r["confidence"]["score"] == 20 + 18 + 24 + 6


async def test_the_owner_corrects_a_class_and_the_expectation_follows(js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await analysis.submit(session, OWNER, findings(ids["Fabrikam"]))
        await session.commit()
        before = (await session.get(Opportunity, ids["Fabrikam"])).analysis["ask"]["coverage"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        got = await c.post(f"/api/v1/roles/{ids['Fabrikam']}/requirements",
                           json={"index": 1, "class": "P", "why": "the master holds it"})
        assert got.status_code == 200
        detail = (await c.get(f"/api/v1/roles/{ids['Fabrikam']}")).json()
    reqs = detail["requirements"]
    assert reqs[1]["class"] == "P" and reqs[1]["by"] == "owner" and reqs[1]["owner_why"] == "the master holds it"
    assert reqs[0]["from"] == [{"id": "b-risk", "text": "Led the risk working group across client engagements"}]
    async with db.session_factory()() as session:
        after = (await session.get(Opportunity, ids["Fabrikam"])).analysis["ask"]["coverage"]
    assert before == 0.5 and after == 1.0
    answer = detail["comp_answer"]
    assert answer["expectation"].startswith("My starting base salary expectation is $")
    assert "standard benefits" in answer["expectation"] and answer["assumptions"].startswith("This is base salary")


#: A made-up pay ask: a likely ask over $170,000 earns a point.
OVER = {"id": "pay-over", "section": "compensation", "says": "Likely ask over 170K", "points": 1, "amount": 170_000,
        "above": True}


def _pay_earned(role: Opportunity) -> int:
    return sum(a.get("earned") or 0 for a in role.fit_breakdown.get("asks") or [] if a.get("id") == OVER["id"])


async def test_the_pay_asks_points_are_in_the_roles_stored_fit_and_move_with_the_ask(js, monkeypatch):
    """Step 8 of the ask's math ("+1 point", the owner's Prioritization pay ask) is in the role's stored fit: the ask
    that crosses the owner's pay ask moves the fit by its points, also when a new method works the ask out again."""
    monkeypatch.setattr(settings, "owner", OWNER)
    ids = await analysis_setup()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, scoring={"asks": [*FIVE["asks"], OVER]})
        await session.commit()
    async with db.session_factory()() as session:
        await analysis.start(session, OWNER, await session.get(Opportunity, ids["Fabrikam"]))
        await analysis.submit(session, OWNER, findings(ids["Fabrikam"]))
        await session.commit()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        low = role.analysis["ask"]
        assert low["ask"] < 170_000 and low["pay_points"] == 0 and _pay_earned(role) == 0
        fit_before = role.fit
        # The research's band moves up, under an older method's stamp: the start's rework works the ask out again.
        role.analysis = {**role.analysis, "pay": {**role.analysis["pay"], "band": [200000, 240000]},
                         "ask": {**low, "method_version": comp.METHOD_VERSION - 1}}
        assert await settle.old_asks(session, OWNER) == 1
        await session.commit()
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, ids["Fabrikam"])
        high = role.analysis["ask"]
        assert high["ask"] > 170_000 and high["pay_points"] == 1
        assert _pay_earned(role) == 1 and role.fit == fit_before + 1, "the ask's points are in the stored fit"
