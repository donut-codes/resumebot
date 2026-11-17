"""What a report holds of the posting itself: the work location and the pay. The app judges the owner's work location
gate and pay gate on them, so a place or a pay the owner doesn't take screens the role out with that gate as the
reason, and a report whose place or pay figures are of the wrong shape is refused whole. Found 2026-10-13, when
readers wrote "location gate fails" and the pay they read in the posting in `fit_evidence`, which nothing
reads. Synthetic data only."""

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Opportunity, SearchRun
from app.jobsearch.stages import GATED_OUT
from app.jobsearch.stages import LANDING_ZONE as KEPT
from tests.builders import report_to_run
from tests.support import OWNER, load_profile


def _read(title, **more):
    """A report of one posting: its link and its answer to the made-up profile's ask, no total."""
    return {"company": "Fabrikam", "title": title, "url": f"https://jobs.example.test/{title.replace(' ', '-')}",
            "fit_evidence": "Biggest gap: none.",
            "fit_breakdown": {"asks": [{"id": "promotion", "answer": "met", "evidence": "A step up."}]}, **more}


async def _start(**parts):
    """A search round for the made-up owner."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, **parts)
        started = await run.start_round(session, OWNER, started_by="manual")
        await session.commit()
        return started.id


async def _finish(run_id, roles):
    await report_to_run(run_id, roles)
    await report_to_run(run_id, [], done=True)
    async with db.session_factory()() as session:
        return {o.title: o for o in (await session.scalars(select(Opportunity))).all()}


async def _staged(run_id):
    async with db.session_factory()() as session:
        return (await session.get(SearchRun, run_id)).reported


# --- the work location ----------------------------------------------------------------------------------------------


async def test_a_list_of_places_is_one_line_and_other_shapes_are_refused(js):
    run_id = await _start()
    for bad in ({"city": "Oakfield"}, 12, True, ["Oakfield", 3]):
        with pytest.raises(run.RunError, match="location is text, or a list of texts"):
            await report_to_run(run_id, [_read("Reader Role 1", location=bad)])
    assert await _staged(run_id) == []
    stored = await _finish(run_id, [_read("Reader Role 1", location=["Gotham, NY", " Oakfield,  ST ", ""],
                                          work_mode="onsite"),
                                    _read("Reader Role 2", location=["Gotham, NY", "Denver, CO"], work_mode="onsite")])
    assert stored["Reader Role 1"].location == "Gotham, NY; Oakfield, ST"
    assert stored["Reader Role 1"].status == KEPT, "one of the places is the owner's: that is enough"
    assert stored["Reader Role 2"].status == GATED_OUT and stored["Reader Role 2"].screened_out_by == "location"


async def test_a_long_list_of_places_is_kept_whole_up_to_a_generous_limit(js):
    places = [f"Town {n}, ST" for n in range(60)] + ["Oakfield, ST"]  # the owner's place is last
    run_id = await _start()
    stored = await _finish(run_id, [_read("Reader Role 1", location=places)])
    assert stored["Reader Role 1"].location.endswith("Oakfield, ST") and stored["Reader Role 1"].status == KEPT


# --- the pay ----------------------------------------------------------------------------------------------------------


async def test_pay_by_place_is_read_across_every_line_and_a_line_with_no_figure_is_no_pay(js):
    run_id = await _start()
    for_gate = {"location": "Oakfield, ST", "work_mode": "onsite"}
    stored = await _finish(run_id, [
        _read("Reader Role 1", comp=["Zone 1: $143,700-$197,550", "Zone 2: $115,000-$158,050"], **for_gate),
        _read("Reader Role 2", comp="Competitive", **for_gate),
        _read("Reader Role 3", comp="$61,200/yr - $143,700/yr plus bonus", **for_gate),
    ])
    zones = stored["Reader Role 1"]
    assert zones.comp == "Zone 1: $143,700-$197,550; Zone 2: $115,000-$158,050" and zones.status == KEPT
    assert stored["Reader Role 2"].comp is None, "a pay line with no figure states no pay"
    bonus = stored["Reader Role 3"]
    assert bonus.status == KEPT and bonus.comp == "$61,200/yr - $143,700/yr plus bonus"


@pytest.mark.parametrize(("fields", "message"), [
    ({"comp_min": 65}, "comp_min is a yearly figure in whole dollars"),  # an hourly figure sent as a yearly one
    ({"comp_max": 650}, "comp_max is a yearly figure in whole dollars"),
    ({"comp_min": "twelve"}, "comp_min is a yearly figure in whole dollars"),
    ({"comp_max": 150_000_000}, "comp_max is a yearly figure in whole dollars"),
    ({"comp_min": True}, "comp_min is a yearly figure in whole dollars"),
    ({"comp_min": 150_000, "comp_max": 100_000}, "comp_min is over comp_max"),
    ({"comp": {"low": 100_000}}, "comp is text, or a list of texts"),
])
async def test_pay_figures_a_reader_sends_are_checked_at_the_door(js, fields, message):
    run_id = await _start()
    with pytest.raises(run.RunError, match=message):
        await report_to_run(run_id, [_read("Reader Role 1", **fields)])
    assert await _staged(run_id) == [], "a refused batch saves nothing"


async def test_yearly_figures_may_be_sent_as_text_with_a_dollar_sign_and_commas(js):
    run_id = await _start()
    stored = await _finish(run_id, [_read("Reader Role 1", comp_min="$121,869", comp_max="149,188.00",
                                          location="Oakfield, ST", work_mode="onsite")])
    role = stored["Reader Role 1"]
    assert role.comp == "$121,869-$149,188" and role.status == KEPT



