"""An interview and its prep as the owner reads them (docs/PRD.md 15.2.1 to 15.2.3): the status line in plain words,
what changed since the report was made, and the report with its Quick Sheet (made here, by code, from the rest) and
every citation shown with its words. Pure: no database. The report is stored as the Coach sent it; this is read-time,
so a
master line edited later is shown as it is now."""

import copy
from typing import Any

from app.jobsearch.states import PrepState, RoundKind
from app.timeutil import iso_utc

ROUND_LABELS = {
    RoundKind.RECRUITER_SCREEN.value: "Recruiter screen", RoundKind.HIRING_MANAGER.value: "Hiring manager",
    RoundKind.PANEL.value: "Panel", RoundKind.EXECUTIVE.value: "Executive", RoundKind.OTHER.value: "Interview",
}
#: What the card says of each state; a prep a session holds is "Researching", the second pass "Checking".
WAITING_WORDS = "Waiting for the Coach"
RESEARCHING, CHECKING, READY = "Researching", "Checking", "Ready"
OPEN = (PrepState.WAITING, PrepState.WRITING, PrepState.REVIEW_WAITING, PrepState.REVIEWING)


def round_label(kind: str) -> str:
    return ROUND_LABELS.get(kind, "Interview")


def snapshot_of(interview: Any) -> dict:
    """The interview as a prep was asked for it (what `changed` compares against)."""
    return {"when_at": iso_utc(interview.when_at), "round_kind": interview.round_kind, "label": interview.label or "",
            "people": [{"id": p["id"], "name": p["name"], "title": p.get("title", "")} for p in interview.people or []]}


def step_words(prep: Any) -> str:
    state = prep.state
    if state == PrepState.WAITING:
        return WAITING_WORDS
    if state == PrepState.WRITING:
        return RESEARCHING
    if state in (PrepState.REVIEW_WAITING, PrepState.REVIEWING):
        return CHECKING
    if state == PrepState.READY:
        return READY
    if state == PrepState.LAPSED:
        return "The time passed before the report was made."
    return prep.error or "The prep wasn't made."


def changed_since(snapshot: dict, interview: Any) -> list[str]:
    """What differs between the interview now and the one the report was made for, in words: the time, the round, the
    name the employer gives it, a person added, removed or retitled."""
    now = snapshot_of(interview)
    out: list[str] = []
    if snapshot.get("when_at") != now["when_at"]:
        out.append("the time")
    if snapshot.get("round_kind") != now["round_kind"] or snapshot.get("label") != now["label"]:
        out.append("the round")
    before = {p["id"]: p for p in snapshot.get("people") or []}
    after = {p["id"]: p for p in now["people"]}
    out += [f"a new person, {p['name']}" for i, p in after.items() if i not in before]
    out += [f"{p['name']} is no longer on it" for i, p in before.items() if i not in after]
    out += [f"{after[i]['name']}'s name or title" for i, p in before.items()
            if i in after and (p["name"], p.get("title")) != (after[i]["name"], after[i].get("title"))]
    return out


def _cited(node: Any, texts: dict[str, str]) -> None:
    """Add `cites` beside every `from` list, in place: each id with the words of the master line or story it names."""
    if isinstance(node, dict):
        if isinstance(node.get("from"), list):
            node["cites"] = [{"id": str(i), "text": texts.get(str(i), "(no longer in the master)")}
                             for i in node["from"]]
        for value in node.values():
            _cited(value, texts)
    elif isinstance(node, list):
        for value in node:
            _cited(value, texts)


def _opener(report: dict) -> str:
    return " ".join(str(line.get("text") or "").strip() for line in (report.get("staples") or {}).get("opener") or [])


def quick_sheet(report: dict, interview: Any, comp: dict | None) -> dict:
    """The last half hour: who and when, the core need, the opener, the pay, the first-choice questions and the
    question-to-story map, one line each. Made by code from the report, never written by the Coach."""
    market = (report.get("staples") or {}).get("market")
    questions = report.get("questions") or []
    return {
        "when_at": iso_utc(interview.when_at), "round_label": round_label(interview.round_kind),
        "people": [{"name": p["name"], "title": p.get("title", "")} for p in interview.people or []],
        "core_need": ((report.get("core_need") or {}).get("text") or ""),
        "opener": _opener(report),
        "pay": {"expectation": (comp or {}).get("expectation"),
                "market": {k: market.get(k) for k in ("low", "high", "basis")} if isinstance(market, dict) else None},
        "first_choice": [{"question": q.get("question", ""), "round": q.get("round", "")}
                         for q in report.get("ask") or [] if q.get("priority") == 1],
        "story_map": [{"question": q.get("question", ""),
                       "story": ((q.get("answer") or {}).get("title") or "No story yet") if q.get("answer")
                       else "No story yet"} for q in questions],
    }


def report_view(report: dict, interview: Any, texts: dict[str, str], comp: dict | None) -> dict:
    """The stored report, whole, plus the Quick Sheet, the pay answer and every citation resolved."""
    out = copy.deepcopy(report)
    _cited(out, texts)
    market = (out.get("staples") or {}).get("market")
    out.setdefault("staples", {})["pay"] = {
        "expectation": (comp or {}).get("expectation"), "assumptions": (comp or {}).get("assumptions"),
        "market": market if isinstance(market, dict) else None}
    out["quick_sheet"] = quick_sheet(report, interview, comp)
    return out


def prep_view(prep: Any, interview: Any, texts: dict[str, str], comp: dict | None, *, newest: bool) -> dict:
    ready = prep.state == PrepState.READY
    return {
        "id": str(prep.id), "version": prep.version, "state": prep.state, "step": step_words(prep),
        "error": prep.error if prep.state == PrepState.FAILED else None,
        "made_at": iso_utc(prep.finished_at) if ready and prep.finished_at else None,
        "changed": changed_since(prep.snapshot or {}, interview) if newest else [],
        "report": report_view(prep.report, interview, texts, comp) if ready else None,
        "review": prep.review if ready and prep.review else None,
    }
