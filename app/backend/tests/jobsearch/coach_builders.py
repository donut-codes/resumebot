"""Made-up material for the tests of interview prep (docs/PRD.md 15.2): a report the checks accept, and the context they
check it against. Every name, employer and address here is made up."""

import copy
from datetime import date

from app.jobsearch.interviews import coach_checks
from app.jobsearch.profile.documents import Resume, Writing

TODAY = date(2026, 10, 29)
SOURCE = {"url": "https://news.example.test/acme-board", "title": "Acme names a board chair",
          "checked_on": "2026-10-29"}

#: A master of three jobs, so the cap on answers for one job applies.
MASTER = {
    "summary": "Builds programs that people use.",
    "achievements": [{"id": "a-grant", "text": "Won a state grant for the program", "note": ""}],
    "competencies": [],
    "roles": [
        {"id": "r-one", "employer": "Northwind Labs", "location": "Oakfield, ST", "title": "Senior Manager",
         "start": "2022", "end": "2026", "tools": [], "bullets": [
             {"id": "b-risk", "text": "Led the risk working group", "note": ""},
             {"id": "b-kb", "text": "Built the knowledge base", "note": ""},
             {"id": "b-train", "text": "Trained forty staff", "note": ""}], "evidence": []},
        {"id": "r-two", "employer": "Contoso Works", "location": "Riverton, ST", "title": "Manager",
         "start": "2018", "end": "2022", "tools": [], "bullets": [
             {"id": "b-open", "text": "Opened two program sites", "note": ""}], "evidence": []},
        {"id": "r-three", "employer": "Fabrikam Group", "location": "Oakfield, ST", "title": "Analyst",
         "start": "2014", "end": "2018", "tools": [], "bullets": [
             {"id": "b-report", "text": "Wrote the quarterly board report", "note": ""}], "evidence": []},
    ],
    "education": [{"id": "ms", "degree": "MS in Leadership", "school": "Example University"}],
}

PEOPLE = [{"id": "p1", "name": "Sam Ortiz", "title": "Director of Programs"}]


def context(**changes) -> coach_checks.Context:
    resume = Resume.model_validate(MASTER)
    base = dict(
        resume=resume, writing=Writing(), stories={"risk": "The risk group"}, people=copy.deepcopy(PEOPLE),
        round_kind="hiring_manager", today=TODAY, has_interview_rules=False)
    return coach_checks.Context.make(**{**base, **changes})


def good_report() -> dict:
    """A report that passes every check, with one job per answer and the checks' own minimums met."""
    return copy.deepcopy({
        "core_need": {"text": "Build the program office the agency does not have yet.", "from_title_only": False},
        "good_looks_like": [{"text": "Know the grant calendar before the first week is out."}],
        "stage_read": {"screens_for": "Whether the owner has run programs like this one.", "interviewers": [{
            "name": "Sam Ortiz", "found": True, "background": "Runs the programs team.",
            "screens_for": "Delivery and staffing.",
            "connection": {"text": "Both have opened new sites.", "from": ["b-open"]},
            "dont": "Do not criticise a past employer.", "questions": ["How do you staff a launch?"]}]},
        "staples": {
            "opener": [{"text": "I build programs that people use, most recently as a manager at Northwind.",
                        "from": ["b-risk"]},
                       {"text": "Before that I opened two program sites.", "from": ["b-open"]}],
            "why_here": [{"text": "The agency is opening a program office.", "source": SOURCE},
                         {"text": "I have opened sites before.", "from": ["b-open"]}],
            "why_leaving": {"ask_owner": "Write one sentence on why you left Northwind."},
            "market": {"low": 90000, "high": 112500, "basis": "base", "source": SOURCE},
        },
        "questions": [
            {"question": "Tell me about a launch.", "asked_by": "Sam Ortiz", "method": "Can you run a launch?",
             "answer": {"kind": "story", "title": "Two sites", "text": "I opened two sites in a year.",
                        "result": "Both were running by June.", "from": ["b-open"]}},
            {"question": "Tell me about a risk you caught.", "method": "Do you see risk early?",
             "answer": {"kind": "story", "title": "The risk group", "text": "I led the risk group.",
                        "result": "The register went to the board each quarter.", "from": ["risk"]}},
            {"question": "Tell me about a report you wrote.", "method": "Can you write for a board?",
             "answer": {"kind": "story", "title": "Board report", "text": "I wrote the quarterly report.",
                        "result": "The board used it to set the budget.", "from": ["b-report"]}},
            {"question": "Tell me about a time you led a team through a crisis.", "method": "Do you stay calm?",
             "gap": "Pick one real crisis from Northwind and add it to the master."},
        ],
        "problem": {"kind": "approach", "approach": "Start with the grant calendar.",
                    "discovery_questions": ["Which grants renew this year?"]},
        "ask": [{"question": "What did the last launch teach you?", "round": "hiring_manager", "priority": 1,
                 "diagnoses": "Whether the team learns from launches.", "good_answer": "A specific change they made.",
                 "bad_answer": "A general answer about teamwork.", "follow_up": "What did you change after it?"}],
        "reference": [{"text": "The board chair changed this autumn.", "source": SOURCE}],
        "open_gaps": [{"text": "No crisis story yet.", "action": "Add one to the master resume."}],
    })
