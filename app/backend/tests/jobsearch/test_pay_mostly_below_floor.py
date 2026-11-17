"""A role whose posted pay has its middle under the owner's floor but its top at or over it stays open (the pay gate
closes only a top under the floor); its card says so in the next-step line and the ranker is given the fact as input.
Synthetic data only."""

from app import db
from app.jobsearch.measures.pay_place import mostly_below_floor
from app.jobsearch.models import CleansedPosting, Opportunity
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.research.rank import HOW, block
from tests.support import OWNER, load_profile

GATES = {"compensation": {"amount": 120_000, "against": "maximum"}}


def test_the_middle_under_the_floor_with_the_top_reaching_it_is_mostly_below():
    assert mostly_below_floor(70_000, 150_000, 120_000) == (70_000, 150_000)


def test_a_middle_at_the_floor_a_top_under_it_a_single_figure_or_no_floor_is_not():
    assert mostly_below_floor(100_000, 140_000, 120_000) is None  # the middle reaches it
    assert mostly_below_floor(50_000, 100_000, 120_000) is None  # the top is under: the gate closes it
    assert mostly_below_floor(None, 150_000, 120_000) is None and mostly_below_floor(70_000, 150_000, 0) is None


def test_the_ranker_is_given_the_pay_note_as_a_field_and_told_to_weigh_it():
    copy = CleansedPosting(title="T", employer="E", locations=[], pay_min=70_000, pay_max=150_000, text_clean="Body")
    note = "Pay note: the middle of $70,000 to $150,000 is under the candidate's $120,000 floor"
    assert note in block(1, copy, 120_000)
    assert "Pay note" not in block(1, copy, 100_000) and "Pay note" not in block(1, copy)
    assert "Pay note" in HOW and "never scores 0" in HOW


async def _card(comp: str, status="second_impression") -> dict:
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=GATES)
        role = Opportunity(owner=OWNER, slug="a", company="Acme", title="Lead", fit=5, origin="scout",
                           match_key="a|lead", status=status, comp=comp)
        session.add(role)
        await session.flush()
        return await role_row.row(session, OWNER, role.id)


async def test_the_card_of_an_open_role_with_pay_mostly_below_the_floor_says_so_in_its_note_line(js):
    card = await _card("$70,000-$150,000")
    assert card["next"].endswith("Pay mostly below your floor: $70,000\u2013$150,000")


async def test_a_card_whose_pay_reaches_the_floor_in_the_middle_has_no_such_note(js):
    assert "Pay mostly" not in ((await _card("$115,000-$150,000"))["next"] or "")
