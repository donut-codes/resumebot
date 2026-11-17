"""The boards' own filters (docs/PRD.md 39.5) and the weekly full read (39.24): a job system that can be asked for
less than its whole board is asked for the owner's places and ways of working and, between full reads, for what is
new; a filter is never narrower than the gates it comes from; a read closes only the postings it could have returned;
and the board is read in full once a week and on the night after the owner's places change. Every board is made up,
behind a mock transport; nothing touches the network."""


from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import adapters
from app.jobsearch.adapters import ADAPTERS, Ask, Asked, Wanted
from app.jobsearch.adapters._ask import saves_a_page
from app.jobsearch.employers import core as employers
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.measures.pay_place import Gates
from app.jobsearch.missions import boards_night
from app.jobsearch.models import Employer, JobPosting
from app.jobsearch.postings import index as postings
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.views import engine_gates
from app.sites import where
from tests.support import EXAMPLE_SYSTEM, OWNER, load_profile

NIGHT = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)


#: An owner who takes remote work in one country and works hybrid or in person in two towns (one of them only hybrid),
#: one who works in a town elsewhere, one who takes only remote work, and one who takes none. Made-up towns; the states
#: are real codes because a town is told from its namesakes by its state.
HERE = Gates(places_gated=True, remote_ok=True, remote_countries=["United States"], hybrid_ok=True,
             hybrid_places=["oakfield, oh", "riverton, oh"], onsite_ok=True, onsite_places=["oakfield, oh"])


ELSEWHERE = Gates(places_gated=True, remote_ok=True, remote_countries=["United States"], hybrid_ok=True,
                  hybrid_places=["maple heights, oh"], onsite_ok=True, onsite_places=["maple heights, oh"])


REMOTE_ONLY = Gates(places_gated=True, remote_ok=True, hybrid_ok=False, onsite_ok=False)


NOT_REMOTE = Gates(places_gated=True, remote_ok=False, hybrid_ok=True, hybrid_places=["oakfield, oh"], onsite_ok=True,
                   onsite_places=["oakfield, oh"])


def _fetcher(handle, robots: str | None = None) -> tuple[Fetcher, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404) if robots is None else httpx.Response(200, text=robots)
        seen.append(request)
        return handle(request)

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(route)), per_host_seconds=0), seen


def test_there_is_something_to_ask_a_board_for_only_when_the_owner_has_a_work_location_gate():
    assert Wanted.of(None) is None
    assert Wanted.of(Gates()) is None, "no gate: every place is fine, so nothing is asked for"
    assert Wanted.of(HERE) is not None


def test_a_place_is_kept_unless_the_gates_rule_it_out_and_a_place_they_cannot_tell_is_kept():
    wanted = Wanted(HERE)
    for place in ("Oakfield, OH", "Riverton, Ohio, US", "US-OH-Oakfield", "Remote - US", "United States", "2 Locations",
                  "Oakfield", "", None):
        assert wanted.keeps(place), place
    for place in ("Oakfield, PA", "Maple Heights, OH", "Remote - Canada", "Berlin, Germany"):
        assert not wanted.keeps(place), place
    assert not Wanted(NOT_REMOTE).keeps("Remote - US"), "remote work is not taken"
    assert Wanted(NOT_REMOTE).keeps("Oakfield, OH")


def test_the_ways_of_working_asked_for_are_the_ones_the_gates_accept():
    assert Wanted(HERE).ways == ("remote", "hybrid", "onsite")
    assert Wanted(REMOTE_ONLY).ways == ("remote",)
    assert Wanted(NOT_REMOTE).ways == ("hybrid", "onsite")


def test_the_fingerprint_of_the_owners_places_changes_with_them_and_with_nothing_else():
    same = Gates(places_gated=True, remote_ok=True, remote_countries=["united states"], hybrid_ok=True,
                 hybrid_places=["riverton, oh", "oakfield, oh"], onsite_ok=True, onsite_places=["oakfield, oh"],
                 comp_floor=90_000, stale_days=30)
    assert Wanted(same).fingerprint == Wanted(HERE).fingerprint, "order, case, pay and age are not the places"
    assert Wanted(ELSEWHERE).fingerprint != Wanted(HERE).fingerprint
    assert Wanted(REMOTE_ONLY).fingerprint != Wanted(HERE).fingerprint
    added = Gates(places_gated=True, remote_ok=True, remote_countries=["United States"], hybrid_ok=True,
                  hybrid_places=["oakfield, oh", "riverton, oh", "springdale, oh"], onsite_ok=True,
                  onsite_places=["oakfield, oh"])
    assert Wanted(added).fingerprint != Wanted(HERE).fingerprint, "a place added is a change"


@pytest.mark.parametrize(("total", "kept", "page", "saves"), [
    (15, 0, 20, False),  # the first page, which says what could be asked, already holds the board
    (40, 5, 20, False),  # two pages either way
    (41, 5, 20, True),  # three pages become two
    (60, 0, 20, True),
    (1267, 29, 20, True),  # sixty-four pages become three
    (1000, 990, 20, False),  # most of it is wanted
    (10, 50, 20, False),  # more kept than there is
])
def test_a_question_is_put_only_when_it_makes_the_read_a_page_shorter(total, kept, page, saves):
    assert saves_a_page(total, kept, page) is saves


def test_what_a_read_that_asked_for_less_can_close():
    in_place = JobPosting(location="Oakfield, OH", work_mode=None)
    elsewhere = JobPosting(location="Elmwood, OH", work_mode=None)
    whole = Asked()
    assert whole.can_close(in_place) and whole.can_close(elsewhere)
    places = Asked(within=lambda p: p.location == "Oakfield, OH")
    assert places.can_close(in_place) and not places.can_close(elsewhere)
    new = Asked(new_only=True)
    assert not new.can_close(in_place) and not new.can_close(elsewhere), "a read of what is new closes none"
    assert not Asked(within=lambda p: True, new_only=True).can_close(in_place)


async def test_a_system_with_no_filters_is_read_in_full_whatever_it_is_asked():
    page = {"jobs": [{"id": 4012, "title": "Data Analyst", "location": {"name": "Cleveland, OH"},
                      "absolute_url": "https://job-boards.feedone.test/acme/jobs/4012", "content": ""}]}
    fetch, seen = _fetcher(lambda request: httpx.Response(200, json=page))
    plain = await ADAPTERS[EXAMPLE_SYSTEM].read(fetch, {"slug": "acme"})
    asked = await ADAPTERS[EXAMPLE_SYSTEM].read_asked(fetch, {"slug": "acme"}, Ask(Wanted(HERE), new_days=2))
    assert [p["ext_id"] for p in asked.postings] == [p["ext_id"] for p in plain.postings] == ["4012"]
    assert asked.asked is None and seen[0].url == seen[1].url


@dataclass
class Read:
    """What a stand-in job system answers: a listing, and what it says it asked for."""
    postings: list[dict]
    truncated: bool = False
    note: str | None = None
    asked: Asked | None = None


@dataclass
class FilterBoard:
    """A stand-in for a job system with filters: it answers what it is told, one read at a time, and remembers what it
    was asked (None for a read of the whole board)."""
    filters = True
    answers: list = field(default_factory=list)
    asks: list = field(default_factory=list)

    async def read(self, fetch, board):
        self.asks.append(None)
        return self._answer()

    async def read_asked(self, fetch, board, ask):
        self.asks.append(ask)
        return self._answer()

    def _answer(self):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def detail(self, fetch, board, posting):
        return {}


def _posting(ext_id: str, place: str = "Oakfield, OH", **more) -> dict:
    return {"ext_id": ext_id, "title": f"Role {ext_id}", "location": place, "work_mode": None, "comp": None,
            "comp_min": None, "comp_max": None, "posted": "2026-10-01", "url": f"https://jobs.example.test/{ext_id}",
            "apply_url": None, "text": f"About {ext_id}.", "department": None, **more}


IN_OAKFIELD = Asked(within=lambda p: p.location == "Oakfield, OH")


NEW = Asked(new_only=True)


@pytest.fixture
def board(monkeypatch):
    fake = FilterBoard()
    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: fake})
    return fake


def _fetch():
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), per_host_seconds=0)


async def _acme(session, name: str = "Acme") -> Employer:
    acme = await employers.find_or_make(session, OWNER, name)
    acme.ats, acme.board = EXAMPLE_SYSTEM, {"slug": name.lower()}
    await session.flush()
    return acme


async def _rows(session) -> dict[str, JobPosting]:
    return {p.ext_id: p for p in (await session.scalars(select(JobPosting))).all()}


async def _night(session, days: float = 0, gates: Gates | None = HERE) -> dict:
    return await postings.refresh(session, OWNER, _fetch(), now=NIGHT + timedelta(days=days), gates=gates)


async def test_a_read_of_what_is_new_closes_nothing_and_a_read_of_the_places_closes_those_places(js, board):
    board.answers = [
        Read([_posting("1"), _posting("2", "Elmwood, OH"), _posting("3")]),
        Read([_posting("4")], asked=NEW),
        Read([_posting("1"), _posting("4")], asked=IN_OAKFIELD),
        Read([_posting("1")]),
    ]
    async with db.session_factory()() as session:
        await _acme(session)
        await _night(session, 0)
        second = await _night(session, 1)
        rows = await _rows(session)
        assert second["closed"] == 0 and second["new"] == 1
        assert all(p.closed_at is None for p in rows.values()), "a read of what is new says nothing of the old postings"
        third = await _night(session, 7)
        assert third["closed"] == 1, "3 is in the owner's places and the read did not return it"
        assert rows["3"].closed_at == NIGHT + timedelta(days=7)
        assert rows["2"].closed_at is None, "Elmwood is not a place asked for: the read could not have returned it"
        fourth = await _night(session, 8, gates=None)
        assert fourth["closed"] == 2, "a read of the whole board closes everything it did not return"
        assert [p.ext_id for p in rows.values() if p.closed_at is None] == ["1"]


async def test_a_read_cut_short_closes_nothing_even_when_it_asked_for_places(js, board):
    board.answers = [Read([_posting("1"), _posting("2")]), Read([_posting("1")], truncated=True, asked=IN_OAKFIELD)]
    async with db.session_factory()() as session:
        await _acme(session)
        await _night(session, 0)
        result = await _night(session, 1)
        assert result["closed"] == 0 and (await _rows(session))["2"].closed_at is None


async def test_a_known_posting_a_filtered_read_did_not_return_stays_open(js, board):
    board.answers = [Read([_posting("1"), _posting("2", "Elmwood, OH")]), Read([_posting("1")], asked=IN_OAKFIELD)]
    async with db.session_factory()() as session:
        await _acme(session)
        await _night(session, 0)
        await _night(session, 1)
        rows = await _rows(session)
        assert rows["2"].closed_at is None and rows["1"].last_seen == NIGHT + timedelta(days=1)
        assert rows["2"].last_seen == NIGHT, "it was not seen again, and not taken to have gone"


async def test_the_first_read_is_a_full_one_then_only_what_is_new_until_a_week_has_gone(js, board):
    board.answers = [Read([_posting("1")], asked=IN_OAKFIELD), Read([_posting("1")], asked=NEW), Read([], asked=NEW),
                     Read([_posting("1")], asked=NEW), Read([_posting("1")], asked=IN_OAKFIELD)]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        assert (acme.read_full_at, acme.read_asked) == (None, None)
        await _night(session, 0)
        assert acme.read_full_at == NIGHT and acme.read_asked
        key = acme.read_asked
        for days in (1, 2, 6, 7):
            await _night(session, days)
        assert [(a.new_days, a.wanted is not None) for a in board.asks] == [
            (None, True), (2, True), (3, True), (7, True), (None, True)], "the days start at the last full read"
        assert acme.read_full_at == NIGHT + timedelta(days=7) and acme.read_asked == key
        assert [p.closed_at for p in (await _rows(session)).values()] == [None]


async def test_a_read_of_what_is_new_does_not_count_as_the_weekly_full_read(js, board):
    board.answers = [Read([_posting("1")]), Read([_posting("1")], asked=NEW), Read([_posting("1")], asked=NEW),
                     Read([_posting("1")])]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _night(session, 0)
        await _night(session, 1)
        await _night(session, 3)
        assert acme.read_full_at == NIGHT, "the week is counted from the last full read"
        await _night(session, 6.6)
        assert board.asks[-1].new_days is None, "a week less half a day on, the board is read in full"
        assert acme.read_full_at == NIGHT + timedelta(days=6.6)


async def test_a_full_read_that_does_not_end_whole_is_not_counted(js, board):
    board.answers = [Read([_posting("1")], truncated=True), adapters.AdapterError("feedone answered 500", 500),
                     Read([_posting("1")], asked=IN_OAKFIELD), Read([_posting("1")], asked=NEW)]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _night(session, 0)
        assert acme.read_full_at is None, "cut short"
        await _night(session, 1)
        assert acme.read_full_at is None, "failed"
        await _night(session, 2)
        assert acme.read_full_at == NIGHT + timedelta(days=2)
        await _night(session, 3)
        assert [a.new_days for a in board.asks] == [None, None, None, 2]


async def test_a_board_that_goes_empty_is_a_fault_and_not_a_full_read(js, board):
    board.answers = [Read([_posting("1")], asked=IN_OAKFIELD), Read([], asked=IN_OAKFIELD),
                     Read([], asked=IN_OAKFIELD), Read([_posting("1")], asked=IN_OAKFIELD)]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _night(session, 0)
        first = await _night(session, 1)
        assert [f["kind"] for f in first["faults"]] == ["empty"] and first["closed"] == 0
        assert acme.read_full_at == NIGHT, "an empty answer is not a full read"
        assert acme.fetch_note == "had 1 open roles, and this read answered none"
        second = await _night(session, 2)
        assert second["closed"] == 1 and acme.open_roles == 0 and acme.read_full_at == NIGHT
        third = await _night(session, 3)
        assert third["reopened"] == 1 and third["faults"] == [] and acme.fetch_ok is True


async def test_a_board_with_nothing_in_the_owners_places_is_not_empty_and_one_asked_for_what_is_new_never_is(js, board):
    board.answers = [Read([_posting("1", "Elmwood, OH")]), Read([], asked=IN_OAKFIELD), Read([], asked=NEW),
                     Read([], asked=NEW)]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _night(session, 0)
        result = await _night(session, 1)
        assert result["faults"] == [] and result["closed"] == 0 and acme.fetch_ok is True
        assert acme.open_roles == 0, "it counts the postings in the owner's places"
        for day in (2, 3):
            result = await _night(session, day)
            assert result["faults"] == [] and acme.fetch_ok is True and acme.fetch_kind is None


async def test_open_roles_counts_the_postings_a_filtered_read_speaks_for(js, board):
    board.answers = [Read([_posting("1"), _posting("2", "Elmwood, OH"), _posting("3")]),
                     Read([_posting("1"), _posting("4")], asked=IN_OAKFIELD), Read([_posting("5")], asked=NEW)]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _night(session, 0)
        assert acme.open_roles == 3
        await _night(session, 1)
        assert acme.open_roles == 2, "1 and 4 are in the places; 3 is closed; 2 is not one of them"
        await _night(session, 2)
        assert acme.open_roles == 4, "a read of what is new speaks for the whole board: 1, 2, 4 and the new 5"


async def test_the_next_read_is_a_full_one_when_the_owners_places_change(js, board):
    board.answers = [Read([_posting("1")], asked=IN_OAKFIELD), Read([_posting("1")], asked=NEW),
                     Read([_posting("1")], asked=IN_OAKFIELD), Read([_posting("1")], asked=NEW),
                     Read([_posting("1")], asked=IN_OAKFIELD)]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _night(session, 0)
        before = acme.read_asked
        await _night(session, 1)
        await _night(session, 2, gates=ELSEWHERE)
        assert board.asks[-1].new_days is None and board.asks[-1].wanted is not None
        assert acme.read_asked != before and acme.read_full_at == NIGHT + timedelta(days=2)
        await _night(session, 3, gates=ELSEWHERE)
        assert board.asks[-1].new_days == 2, "and the night after that is back to what is new"
        await _night(session, 4, gates=Gates())
        assert board.asks[-1].new_days is None and board.asks[-1].wanted is None, "no gate any more: asked in full"


async def test_the_next_read_is_a_full_one_when_the_board_changes(js, board):
    board.answers = [Read([_posting("1")]), Read([_posting("1")], asked=NEW), Read([_posting("1")]),
                     Read([_posting("1")], asked=NEW)]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        await _night(session, 0)
        await _night(session, 1)
        acme.board = {"slug": "acme-careers"}
        await _night(session, 2)
        assert [a.new_days for a in board.asks] == [None, 2, None]
        acme.board = {**acme.board, "api_tenant": "learned", "confirmed": True}
        await _night(session, 3)
        assert board.asks[-1].new_days == 2, "what a read learned about an address is not another board"


async def test_a_board_is_asked_nothing_unless_the_gates_are_given_and_its_system_has_filters(js, board, monkeypatch):
    class Plain:
        filters = False

        async def read(self, fetch, address):
            return Read([_posting("9")])

        async def detail(self, fetch, address, posting):
            return {}

    monkeypatch.setattr(adapters, "ADAPTERS", {EXAMPLE_SYSTEM: board, "feedfour": Plain()})
    board.answers = [Read([_posting("1")], asked=IN_OAKFIELD), Read([_posting("1")])]
    async with db.session_factory()() as session:
        acme = await _acme(session)
        other = await employers.find_or_make(session, OWNER, "Initech")
        other.ats, other.board = "feedfour", {"slug": "initech"}
        await session.flush()
        result = await _night(session, 0)
        assert result["employers"] == 2 and [a.new_days for a in board.asks] == [None]
        assert (other.read_full_at, other.read_asked) == (None, None), "no question, so no full read to count"
        await _night(session, 1, gates=None)
        assert board.asks[-1] is None, "without the owner's gates a board is read whole"
        assert acme.read_full_at == NIGHT, "and that is no full read of the owner's places"


async def test_the_nights_read_asks_each_board_for_the_owners_work_location_from_hard_pass(js, board, monkeypatch):
    monkeypatch.setattr(polite, "make", _fetch)
    board.answers = [Read([_posting("1")], asked=IN_OAKFIELD), Read([_posting("1")], asked=NEW)]
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        fab = await _acme(session, "Fabrikam")
        fab.board_url, fab.check_directly = "https://job-boards.feedone.test/fabrikam", True  # watched: its site is on
        await where.seed_employer_sites(session, OWNER)
        await session.commit()
        gates = engine_gates(await profile_store.current(session, OWNER))
    outcome, _summary, counts = await boards_night.read_boards(OWNER, details_per_night=10)
    assert outcome == "done" and counts["seen"] == 1
    [ask] = board.asks
    assert ask.new_days is None and ask.wanted is not None, "the first read of a board is a full one"
    assert ask.wanted.fingerprint == Wanted.of(gates).fingerprint
    assert ask.wanted.keeps("Oakfield") and ask.wanted.keeps("Remote") and not ask.wanted.keeps("Elmwood, ST")
    await boards_night.read_boards(OWNER, details_per_night=10)
    assert board.asks[-1].new_days == 1, "the second read, the same day, asks only for what is new"
