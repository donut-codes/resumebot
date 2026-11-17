"""The nightly dollar budget (the owner, 2026-10-19): what the app's own background sessions may cost in one
off-hours window, in dollars at API prices (the setting `Settings.nightly_budget`; none, no limit,
unless the owner sets one).

Each finished session reports what it cost (Claude Code's own `total_cost_usd`, with its token counts) on its `ended`
heartbeat; `record` keeps it. `hold` is asked before the pool hands out work of its own: it is held when the night's
spend, with a guess for the sessions still running, has reached what the clock allows, so the budget is spread
across the window (not burnt in its first hour) and stops for good at the budget. A held pool still hands out what the
owner started (`session_kinds.ORDER`: the same path as outside the off hours), in the same order, so the owner's
priorities and the pull to the right are kept. Work the owner started is never counted against the budget
(`is_background`). Services flush; the route commits."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import roster
from app.jobsearch.models import AgentSpend
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import hours as night
from app.jobsearch.work.session_kinds import of_runner
from app.timeutil import owner_zone, utc_now

#: The head start the pacing gives the window's first minutes, so the first sessions are not held for a rounding.
HEAD_START = timedelta(minutes=30)
#: Rough API prices in dollars per million tokens (input, output), used only when a session's result gave no cost.
FALLBACK_PRICE = (3.0, 15.0)


def _window(s, now: datetime) -> tuple[datetime, timedelta]:
    """The start of the owner's off-hours window that holds `now` (or the last one begun), and its length. A window with
    the same start and end is every hour: counted as the day from its start time."""
    start = night.clock(s.off_hours_start) or time(1, 0)
    end = night.clock(s.off_hours_end) or time(7, 0)
    zone = owner_zone()
    day = now.astimezone(zone).date()
    begun = night.wall_moment(day, start, zone)
    if begun > now:
        day -= timedelta(days=1)
        begun = night.wall_moment(day, start, zone)
    # The length is elapsed time: the night the clocks change is an hour longer or shorter than its wall-clock hours.
    ends = night.wall_moment(day, end, zone)
    if ends <= begun:
        ends = night.wall_moment(day + timedelta(days=1), end, zone)
    return begun, ends - begun


def cost_of(body: dict) -> float:
    """What a session's `ended` heartbeat says it cost: the runner's `cost_usd` (Claude Code's `total_cost_usd`), else a
    guess from its token counts."""
    cost = body.get("cost_usd")
    if isinstance(cost, (int, float)) and cost > 0:
        return float(cost)
    inp, out = (max(int(body.get(k) or 0), 0) for k in ("input_tokens", "output_tokens"))
    return (inp * FALLBACK_PRICE[0] + out * FALLBACK_PRICE[1]) / 1_000_000


async def is_background(session: AsyncSession, owner: str, kind: str | None, now: datetime) -> bool:
    """Whether a session was the app's own pull, counted against the budget: not the owner's asks (a posting to split, a
    check of their roles, Find Gaps), nothing started while their Run now, Wake up or Go stood, and nothing outside the
    off hours (the pool starts only what the owner started then)."""
    from app.jobsearch.pool import core as pool

    pooled = of_runner(kind or "")
    if pooled is None or pooled.group is None or await pool.owner_running(session, owner, pooled.group, now):
        return False
    return night.in_off_hours((await profile_store.current(session, owner)).settings, now)


async def record(session: AsyncSession, owner: str, session_id: str, kind: str | None, spend: object,
                 now: datetime | None = None) -> AgentSpend | None:
    """Keep what a finished session cost (the `spend` of its `ended` heartbeat). Nothing for a session that reported no
    cost or tokens, or one already kept. The caller commits."""
    if not isinstance(spend, dict) or not session_id:
        return None
    now = now or utc_now()
    body = {k: spend.get(k) for k in ("cost_usd", "input_tokens", "output_tokens")}
    cost = cost_of(body)
    if cost <= 0:
        return None
    if await session.scalar(select(AgentSpend.id).where(
            AgentSpend.owner == owner, AgentSpend.session_id == session_id).limit(1)) is not None:
        return None
    row = AgentSpend(owner=owner, session_id=session_id[:80], kind=kind, cost_usd=cost,
                     input_tokens=max(int(body["input_tokens"] or 0), 0),
                     output_tokens=max(int(body["output_tokens"] or 0), 0),
                     background=await is_background(session, owner, kind, now), at=now)
    session.add(row)
    await session.flush()
    from app.jobsearch.browser.recipes import record as recipe_record  # it reads the pool, which reads this

    await recipe_record.add_spend(session, owner, session_id, cost)
    return row


async def spent_since(session: AsyncSession, owner: str, since: datetime) -> tuple[float, int]:
    """What background sessions cost since `since`, and how many."""
    total, n = (await session.execute(
        select(func.coalesce(func.sum(AgentSpend.cost_usd), 0.0), func.count(AgentSpend.id)).where(
            AgentSpend.owner == owner, AgentSpend.background.is_(True), AgentSpend.at >= since))).one()
    return float(total), int(n)


@dataclass
class State:
    budget: float | None  # None: no limit
    spent: float
    running: float  # a guess for the sessions still working
    allowed: float | None  # what the clock allows to be spent by now
    sessions: int

    @property
    def total(self) -> float:
        return self.spent + self.running

    @property
    def reached(self) -> bool:
        return self.budget is not None and self.total >= self.budget

    @property
    def held(self) -> bool:
        return self.allowed is not None and self.total >= self.allowed


async def budget_of(session: AsyncSession, owner: str) -> float | None:
    budget = (await profile_store.current(session, owner)).settings.nightly_budget
    return None if budget is None else float(budget)


async def state(session: AsyncSession, owner: str, now: datetime | None = None) -> State:
    now = now or utc_now()
    s = (await profile_store.current(session, owner)).settings
    start, span = _window(s, now)
    spent, n = await spent_since(session, owner, start)
    working = sum(1 for r in await roster.runner_rows(session, owner)
                  if r.state == "working" and now - r.seen_at <= roster.LIVE_FOR)
    running = working * (spent / n) if n else 0.0
    share = min(1.0, max((now - start + HEAD_START) / span, 0.0))
    budget = None if s.nightly_budget is None else float(s.nightly_budget)
    allowed = None if budget is None else budget * share
    return State(budget=budget, spent=spent, running=running, allowed=allowed, sessions=n)


async def hold(session: AsyncSession, owner: str, now: datetime | None = None) -> str | None:
    """Why the pool starts no work of its own now, or None: the night's budget is reached, or spread across the window
    so far and the clock allows no more yet."""
    st = await state(session, owner, now)
    if st.budget is None:
        return None
    if st.reached:
        return f"the nightly budget of ${st.budget:,.0f} is reached"
    if st.held:
        return f"pacing the nightly budget: ${st.total:,.2f} of ${st.budget:,.0f} is spent for this hour of the night"
    return None


def line(st: State, left: dict[str, str] | None = None) -> str:
    """The night's spend in one line, for the health line at the end of the window: the spend, and what was left
    undone."""
    if st.budget is None:
        words = f"Spent ${st.spent:,.2f} in the night"
    else:
        words = f"Spent ${st.spent:,.2f} of the ${st.budget:,.0f} nightly budget"
    if st.reached:
        words += " (reached: no more background sessions were started)"
    undone = [f"{k.replace('_', ' ')} {v.split(': ', 1)[-1].rstrip('.')}" for k, v in (left or {}).items()
              if not v.endswith("none left.")]
    return words + (". Left undone: " + "; ".join(undone) + "." if undone else ". Nothing was left undone.")
