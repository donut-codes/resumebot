"""The code checks on the Coach's report (docs/PRD.md 15.2.5 to 15.2.10). Code decides what the report may claim; the
Coach judges what to research and how to say it. Each check is one plain sentence naming the item and what to do, so the
Coach fixes it and sends again, and all of them are named at once.

- **Shape:** every section is there; a question has a story or says what is missing; a question to ask has what it
diagnoses, a good and a bad answer and a follow-up; every named interviewer has a card with a "don't" and questions; an
open gap names what the owner can do. - **Consistency:** a claim about the owner cites master lines or library stories
that exist (why leaving may ask the owner instead); no phrase the owner banned is spoken (the four built-in kinds of
instead); no phrase the owner banned is spoken (the four built-in kinds of text are for documents a screener reads, not
for a spoken answer); no certification the master does not hold; a story is one thing that happened (its lines are of
one job); when the master holds three or more jobs no job carries more than two answers. - **Recency:** a fact about
the employer or the market, and the pay range, carries an https source, its title and the day it was checked (today's,
it was checked (today's, or the day before). - **Budget:** the first-choice questions to ask for a round stay within
it, unless the owner has rules for interviews (they are the Coach's to follow, and code cannot read them).

Pure: no database. `Context.make` takes what the checks need from the master and the owner's rules."""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.jobsearch.profile import integrity, never_write
from app.jobsearch.profile.documents import Resume, Writing, master_texts

SECTIONS = ("core_need", "good_looks_like", "stage_read", "staples", "questions", "problem", "ask", "reference",
            "open_gaps")
ROUNDS = ("recruiter_screen", "hiring_manager", "panel", "executive")
#: First-choice questions to ask, by round (docs/PRD.md 15.2.8).
BUDGET = {"recruiter_screen": 2, "hiring_manager": 5, "panel": 1, "executive": 2}
#: The most answers one job may carry when the master holds at least `CAP_FROM` jobs (docs/PRD.md 15.2.7).
MOST_ON_ONE_JOB, CAP_FROM = 2, 3
ASK_PARTS = ("question", "diagnoses", "good_answer", "bad_answer", "follow_up")
#: Words that make a line about the employer or the market time-sensitive: a year, or what news and law are made of.
_CUE = re.compile(
    r"\b(?:19|20)\d{2}\b|\b(?:announced|acquired|acquisition|merger|raised|funding|ipo|went public|layoffs?|laid off"
    r"|ceo|cfo|cto|coo|chief|president|chair|chairman|chairwoman|deadline|takes effect|new law|regulation|recently|"
    r"this year|last year|this quarter)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Context:
    """What the checks run against."""

    resume: Resume
    writing: Writing
    master: dict[str, str]
    #: A master id to the id of its job (the role, its bullets and its evidence); lines of no job are not in it.
    jobs: dict[str, str]
    job_names: dict[str, str]
    stories: dict[str, str]
    people: list[dict]
    round_kind: str
    today: date
    has_interview_rules: bool

    @staticmethod
    def make(*, resume: Resume, writing: Writing, stories: dict[str, str], people: list[dict], round_kind: str,
             today: date, has_interview_rules: bool) -> "Context":
        jobs: dict[str, str] = {}
        names: dict[str, str] = {}
        for role in resume.roles:
            names[role.id] = f"{role.title} at {role.employer}"
            jobs[role.id] = role.id
            jobs.update({b.id: role.id for b in role.bullets})
            jobs.update({e.id: role.id for e in role.evidence})
        return Context(resume=resume, writing=writing, master=master_texts(resume), jobs=jobs, job_names=names,
                       stories=stories, people=people, round_kind=round_kind, today=today,
                       has_interview_rules=has_interview_rules)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _cited(line: Any, ctx: Context, where: str, out: list[str], *, may_ask: bool = False) -> list[str]:
    """The ids a line of the owner's voice cites, with a problem for none, or for one that is not in the master or the
    library. A line that asks the owner for the words (`ask_owner`) is allowed where `may_ask`."""
    if not isinstance(line, dict) or not _text(line.get("text")):
        if not (may_ask and isinstance(line, dict) and _text(line.get("ask_owner"))):
            out.append(f"{where} needs its words in text.")
        return []
    ids = [str(i) for i in line.get("from") or [] if _text(i)]
    if not ids:
        out.append(f"{where} says something about the owner but cites no master line or story: add `from` with the "
                   "ids that back it, or take it out." + (" Or send ask_owner with what the owner must write."
                                                          if may_ask else ""))
        return []
    unknown = [i for i in ids if i not in ctx.master and i not in ctx.stories]
    if unknown:
        out.append(f"{where} cites {', '.join(unknown)}, which is neither a master line nor a story in the library: "
                   "cite ids the resume and stories parts gave.")
    return ids


def _source_problem(source: Any, ctx: Context) -> str | None:
    if not isinstance(source, dict):
        return "has no source: send {url, title, checked_on}"
    url = _text(source.get("url"))
    if not url.lower().startswith("https://") or len(url) < 12:
        return "has a source that is not an https address"
    if not _text(source.get("title")):
        return "has a source with no title"
    try:
        day = date.fromisoformat(_text(source.get("checked_on")))
    except ValueError:
        return "has a source with no date checked (checked_on, YYYY-MM-DD)"
    if not ctx.today - timedelta(days=1) <= day <= ctx.today:
        return (f"has a source whose date checked ({day.isoformat()}) is not today's or yesterday's "
                f"(the owner's today is {ctx.today.isoformat()}, which may differ from your machine's date): "
                f"look again and date the day you looked by the owner's today")
    return None


def _fact(item: Any, ctx: Context, where: str, out: list[str], *, always: bool = False) -> None:
    """A line about the employer or the market: a source is needed when the line is time-sensitive (`_CUE`) or `always`;
    one that is given is checked either way."""
    if not isinstance(item, dict) or not _text(item.get("text")):
        out.append(f"{where} needs its words in text.")
        return
    cue = _CUE.search(item["text"])
    if item.get("source") is None and not (always or cue):
        return
    problem = _source_problem(item.get("source"), ctx)
    if problem:
        out.append(f"{where} {problem}" + (f" ({cue.group(0)!r} makes it time-sensitive)" if cue else "") + ".")


def _spoken(report: dict) -> list[tuple[str, str]]:
    """Each line the owner would say, with where it is: where banned phrases and certifications are searched."""
    out: list[tuple[str, str]] = []
    staples = report.get("staples") or {}
    for n, line in enumerate(staples.get("opener") or []):
        out.append((f"opener, line {n + 1}", _text((line or {}).get("text"))))
    for n, line in enumerate(staples.get("why_here") or []):
        if isinstance(line, dict) and line.get("from"):
            out.append((f"why here, line {n + 1}", _text(line.get("text"))))
    leaving = staples.get("why_leaving") or {}
    out.append(("why leaving", _text(leaving.get("text"))))
    for person in (report.get("stage_read") or {}).get("interviewers") or []:
        connection = person.get("connection") or {}
        out.append((f"connection to {_text(person.get('name'))}", _text(connection.get("text"))))
    for q in report.get("questions") or []:
        answer = q.get("answer") or {}
        for part in ("title", "text", "result"):
            out.append((f"answer to {_text(q.get('question'))[:40]!r}, {part}", _text(answer.get(part))))
    return [(where, text) for where, text in out if text]


def _answers_by_job(report: dict, ctx: Context, out: list[str]) -> None:
    by_job: dict[str, list[str]] = {}
    for q in report.get("questions") or []:
        answer = q.get("answer")
        if not isinstance(answer, dict):
            continue
        ids = [str(i) for i in answer.get("from") or []]
        jobs = {ctx.jobs[i] for i in ids if i in ctx.jobs}
        if len(jobs) > 1:
            out.append(f"The answer to {_text(q.get('question'))!r} cites lines from two jobs: a story is one thing "
                       "that happened, so split it or cite one job.")
            continue
        job = next((ctx.jobs[i] for i in ids if i in ctx.jobs), None)
        if job is not None:
            by_job.setdefault(job, []).append(_text(q.get("question")))
    if len(ctx.resume.roles) >= CAP_FROM:
        for job, questions in by_job.items():
            if len(questions) > MOST_ON_ONE_JOB:
                out.append(f"{ctx.job_names[job]} carries {len(questions)} answers ({'; '.join(questions)}): with "
                           f"three or more jobs on the master, no job carries more than {MOST_ON_ONE_JOB}. Use "
                           "another job's story, or say there is none and add it to open gaps.")


def check_report(report: Any, ctx: Context) -> list[str]:
    """Every problem with a report, in words; none means it may go to the second pass."""
    if not isinstance(report, dict):
        return ["The report is an object: {core_need, good_looks_like, stage_read, staples, questions, problem, ask, "
                "reference, open_gaps}."]
    out = [f"The section {name} is missing." for name in SECTIONS if report.get(name) in (None, "")]
    if out:
        return out

    if not _text((report["core_need"] or {}).get("text")):
        out.append("core_need needs one sentence in text.")
    for n, item in enumerate(report["good_looks_like"] or []):
        _fact(item, ctx, f"good_looks_like, item {n + 1}", out)

    staples = report["staples"] or {}
    opener = staples.get("opener") or []
    if not opener:
        out.append("staples.opener needs the lines of the opener, each citing the master.")
    for n, line in enumerate(opener):
        _cited(line, ctx, f"The opener, line {n + 1},", out)
    here = staples.get("why_here") or []
    if not any(isinstance(h, dict) and h.get("source") for h in here) or not any(
            isinstance(h, dict) and h.get("from") for h in here):
        out.append("staples.why_here needs a line about the employer (with its source) and a line about the owner's "
                   "relevant past (citing the master).")
    for n, line in enumerate(here):
        if isinstance(line, dict) and line.get("from"):
            _cited(line, ctx, f"Why here, line {n + 1},", out)
        else:
            _fact(line, ctx, f"Why here, line {n + 1},", out, always=True)
    leaving = staples.get("why_leaving")
    if not isinstance(leaving, dict):
        out.append("staples.why_leaving is missing: the owner's reason, citing the master, or ask_owner.")
    else:
        _cited(leaving, ctx, "why_leaving", out, may_ask=True)
    market = staples.get("market")
    if market is not None:
        if not isinstance(market, dict) or not all(isinstance(market.get(k), int | float) for k in ("low", "high")):
            out.append("The market range needs low and high as numbers.")
        else:
            problem = _source_problem(market.get("source"), ctx)
            if problem:
                out.append(f"The market range {problem}.")

    cards = {_text(p.get("name")).lower(): p for p in report["stage_read"].get("interviewers") or []
             if isinstance(p, dict)}
    for person in ctx.people:
        name = _text(person.get("name"))
        card = cards.get(name.lower())
        if card is None:
            out.append(f"No card for {name}: every person in the interview needs one in stage_read.interviewers.")
            continue
        if not _text(card.get("dont")):
            out.append(f"The card for {name} has no don't: one thing not to do with this person.")
        if not card.get("questions"):
            out.append(f"The card for {name} has no questions: two or three this person is likely to ask.")
        if card.get("connection"):
            _cited(card["connection"], ctx, f"The connection on the card for {name}", out)
        if card.get("found") is False and _text(card.get("background")):
            out.append(f"The card for {name} says nothing was found but holds a background: leave it empty.")

    for q in report["questions"] or []:
        label = _text((q or {}).get("question")) or "A question"
        if not isinstance(q, dict) or not _text(q.get("question")) or not _text(q.get("method")):
            out.append(f"{label!r} needs the question and the method (what the interviewer is really asking for).")
            continue
        answer = q.get("answer")
        if answer is None:
            if not _text(q.get("gap")):
                out.append(f"{label!r} has no story and no gap: send answer, or gap with what the owner can still do. "
                           "Never invent a story.")
            continue
        if not isinstance(answer, dict) or answer.get("kind") not in ("story", "judgement") or not _text(
                answer.get("text")):
            out.append(f"The answer to {label!r} is a story or a judgement, with its words in text.")
            continue
        if answer["kind"] == "story" and not _text(answer.get("result")):
            out.append(f"The story for {label!r} needs a result someone could check.")
        _cited(answer, ctx, f"The answer to {label!r}", out)
    _answers_by_job(report, ctx, out)

    problem = report["problem"]
    if not isinstance(problem, dict) or problem.get("kind") not in ("approach", "first_engagement") or not _text(
            problem.get("approach")):
        out.append("problem is an approach or a first_engagement, with the approach in words.")

    firsts: dict[str, int] = {}
    for q in report["ask"] or []:
        label = _text((q or {}).get("question")) or "A question to ask"
        missing = [part for part in ASK_PARTS if not isinstance(q, dict) or not _text(q.get(part))]
        if missing:
            out.append(f"{label!r} is missing {', '.join(missing)}: each question to ask carries what it diagnoses, a "
                       "good and a bad answer and the one follow-up.")
            continue
        if q.get("round") not in ROUNDS:
            out.append(f"{label!r} names a round that is none of {', '.join(ROUNDS)}.")
        elif q.get("priority") == 1:
            firsts[q["round"]] = firsts.get(q["round"], 0) + 1
    if not ctx.has_interview_rules:
        for kind, count in firsts.items():
            if count > BUDGET[kind]:
                out.append(f"{count} first-choice questions for the {kind.replace('_', ' ')} round: the budget is "
                           f"{BUDGET[kind]}. Make the rest second choice.")

    for n, item in enumerate(report["reference"] or []):
        _fact(item, ctx, f"reference, item {n + 1}", out, always=True)
    for n, gap in enumerate(report["open_gaps"] or []):
        if not isinstance(gap, dict) or not _text(gap.get("text")) or not _text(gap.get("action")):
            out.append(f"Open gap {n + 1} needs its text and the action the owner can take before the call.")

    spoken = _spoken(report)
    out += [f"Never write: {hit}" for hit in never_write.find_hits(ctx.writing, spoken, built_in=False)]
    out += [f"Certification: {hit}" for hit in integrity.certification_hits(ctx.writing, ctx.resume, spoken)]
    return out


def check_review(review: Any) -> list[str]:
    """The second pass's reply: a summary, and findings each naming where, the problem and the fix."""
    if not isinstance(review, dict):
        return ["The review is an object: {summary, findings: [{where, problem, fix, action?}]}."]
    out = []
    if not _text(review.get("summary")):
        out.append("The review needs a summary: one or two sentences on whether the report holds up.")
    findings = review.get("findings")
    if not isinstance(findings, list):
        return [*out, "findings is a list (empty when you found nothing)."]
    for n, f in enumerate(findings):
        if not isinstance(f, dict) or not _text(f.get("where")) or not _text(f.get("problem")):
            out.append(f"Finding {n + 1} needs where it is and what the problem is.")
    return out
