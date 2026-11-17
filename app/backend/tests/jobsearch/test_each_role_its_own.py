"""Each target role writes its own text (docs/PRD.md 31.5, the owner's decision): Qualifications,
Preferred and Benefits / Compensation are the role's own, because each role is different. A document saved while they
could be written once for every role (it has a `shared` key) loads with that text copied into each role that had none
of its own, and `shared` is gone; nothing is ever saved with it. The agents are sent each role's own text. Everything
here is made up."""

import copy

from app import db
from app.jobsearch.profile import schema as p
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile import views as pv
from tests.builders import COMPENSATION, PREFERRED, QUALIFICATIONS, SHARED, criteria, tab
from tests.support import OWNER, PROFILE, bare, load_profile


def test_a_roles_texts_are_its_own_in_the_order_a_posting_reads():
    role = p.TargetRole.model_validate(tab("builders", qualifications="  A degree.  ", compensation="Pay."))
    assert list(role.texts()) == list(p.ROLE_TEXT)
    assert role.texts() == {"description": "The builders work.", "responsibilities": "What a builders does each day.",
                            "qualifications": "A degree.", "preferred": "", "compensation": "Pay."}


# --- what the agents are sent: each role's own text ------------------------------------------------------------------


def profile_with(doc) -> p.Profile:
    data = copy.deepcopy(PROFILE)
    data["criteria"] = doc
    return p.Profile.model_validate(data)


def test_a_reader_is_sent_each_roles_own_text_and_nothing_it_does_not_have():
    prof = profile_with(criteria(tab("builders", qualifications="A degree.", preferred=PREFERRED), tab("analysts")))
    roles = {r["id"]: r for r in pv.for_scout(prof)["what_to_look_for"]["target_roles"]}
    assert roles["builders"]["qualifications"] == "A degree." and roles["builders"]["preferred"] == PREFERRED
    assert "compensation" not in roles["builders"]
    assert not {"qualifications", "preferred", "compensation"} & set(roles["analysts"]), "it wrote none: none is sent"
    assert "shared" not in str(pv.for_scout(prof))


def test_the_analyst_is_sent_the_one_role_it_judges_with_that_roles_own_text():
    prof = profile_with(criteria(tab("builders", preferred="Mine."), tab("analysts", preferred=PREFERRED)))
    mine = pv.look_for_role(prof.criteria, "analysts")
    assert mine["target_role"]["id"] == "analysts" and mine["target_role"]["preferred"] == PREFERRED
    assert "builders" not in str(mine) and "Mine." not in str(mine)
    assert pv.look_for_role(prof.criteria, "gone") == {"company": "A public agency."}
    assert pv.look_for_role(prof.criteria, None) == {"company": "A public agency."}


def test_the_title_pass_is_sent_each_roles_description_and_nothing_a_title_cannot_judge():
    prof = profile_with(criteria(tab("builders", **SHARED), tab("analysts", **SHARED)))
    sent = pv.for_titles(prof)
    assert [r["id"] for r in sent["target_roles"]] == ["builders", "analysts"]
    assert sent["target_roles"][0]["description"] == "The builders work."
    assert not any(t in str(sent) for t in (QUALIFICATIONS, PREFERRED, COMPENSATION)), "a title says nothing of them"


async def test_the_title_pass_reads_the_roles_as_the_profile_holds_them(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=criteria(tab("builders", **SHARED), tab("analysts", **SHARED)))
        got = pv.for_titles(await profile_store.current(session, OWNER))
        await session.commit()
    assert [r["id"] for r in got["target_roles"]] == ["builders", "analysts"]


# --- over HTTP and in the store ---------------------------------------------------------------------------------------


async def test_the_criteria_are_saved_and_read_back_with_each_roles_own_text_and_no_shared(client):
    doc = criteria(tab("builders", qualifications="A degree."), tab("analysts", preferred=PREFERRED))
    saved = await client.put("/api/v1/profile/criteria", json={"data": doc, "why": "each its own"})
    assert saved.status_code == 200 and saved.json()["saved"] is True
    body = (await client.get("/api/v1/profile")).json()
    assert "shared" not in body["criteria"]
    assert [parts_of(r) for r in body["criteria"]["target_roles"]] == [("A degree.", "", ""), ("", PREFERRED, "")]
    again = await client.put("/api/v1/profile/criteria", json={"data": body["criteria"], "why": "no change"})
    assert bare(again.json()) == {"saved": False, "version": None}


def parts_of(role: dict) -> tuple:
    return role["qualifications"], role["preferred"], role["compensation"]


