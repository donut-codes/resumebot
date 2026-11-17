"""Reading a plugin workspace into the owner's documents: the master resume in either of the plugin's layouts, the
form answers and the writing rules. Everything here is made up."""

from pathlib import Path

from app.jobsearch.imports.workspace import parse_form_answers, parse_master, parse_writing, read_workspace
from app.jobsearch.profile import documents
from app.jobsearch.profile import store as profile_store

MASTER = """> **Master document, not for submission.** No graduation years in any cut.
> INTERVIEW-ONLY: the team-size count never goes in writing.

---

# Pat Example
**Oakfield, ST** | 555-010-0199 | [pat@example.test](mailto:pat@example.test) | [boardone.test/in/example](http://boardone.test/in/example)

## Professional Summary

Builds programs that people use.

## Selected Achievements

- Led a statewide program *(approved phrasing)*

## Core Competencies

**Governance:** Risk Frameworks · Policy Design
**Delivery:** Program Governance · Stakeholder Alignment

## Professional Experience

### Northwind Labs, Oakfield, ST | 2020 – 2026

#### Senior Manager, Programs (2022 – 2026)
*Title note: use "Senior Manager" in cuts.*

Sole leader of the programs practice.

**Governance Leadership**
- Led the risk working group across client engagements
  - *Never use an absolute here.*
- Built the practice knowledge base

#### Analyst (2020 – 2022)
- Mapped processes for a state agency

### Fabrikam (fictional) — Riverton, ST
**Engineer** | 2015 – 2020
- Wrote the first test framework

#### Junior Engineer (2012 – 2015) — *optional block*
- Supported releases

## Education

**MS in Leadership**, Example University
*No years.*

## Awards & Recognition

- **Annual reviews:** rated highly

## Evidence Library (for tailoring, not published)

- **Tools:** Visio, Miro.
- **Project X | a state agency:** six analysts, 30 deliverables.

## Tailoring Notes & Guardrails (not published)

No years-of-experience counts.

Standing rule: no em dashes.

## TODO

- [ ] Confirm the start month
"""

ANSWERS = """---
name: form-answers
---
Standing answers:

- Phone (cell): 555-010-0199
- Email: pat@example.test
- Home address: 1 Example Way, Oakfield, ST 00000
- Metro-area answer for "location" fields: Springdale, ST
- Boardone: https://boardone.test/in/example
- Authorized to work in the US: Yes
- Will require visa sponsorship (now or future): No
- Notice period: two weeks
"""

STYLE = """---
name: writing-style
---
Plain, short and optimistic.
"""

RULES = """---
name: content-rules
---
Checklist for every document.

1. **Clients are never named.** Use sector and scale.
2. **Delivered work only.** Nothing unshipped.
4a. **No published work section.**
10. **Banned phrases:**
   - `re:\\b\\d+\\+?\\s+years\\b` -> no years counts
   - `spearheaded` -> puffery
"""

VARIANTS = """---
name: resume-variants
---
## Handcrafted per posting

Every packet is written from the master.

0. **Name the problem first.**
1. **Headline:** function words plus Leader.

## Source of truth
The master.
"""

REVIEW = """---
name: packet-review
---
# Packet review

## The review, in order

1. **Read the sources yourself.**
2. **Claim table.** Every claim against the master.

## Drift patterns reviewers caught

| Pattern | What it looked like | Fix |
|---|---|---|
| **Scope creep by one word** | "led AI for the firm" | Use the master's scope noun |

## Things the reviewer flags instead of fixing

- **Hard gates in the posting**: flag them.
"""


def test_the_master_resume_reads_into_roles_bullets_and_notes_in_either_layout_and_leaves_its_guardrails_out():
    resume, contact = parse_master(MASTER)
    assert contact == {"name": "Pat Example", "location": "Oakfield, ST", "phone": "555-010-0199",
                       "email": "pat@example.test", "links": ["boardone.test/in/example"]}
    assert resume["summary"] == "Builds programs that people use."
    assert [c["area"] for c in resume["competencies"]] == ["Governance", "Delivery"]
    assert resume["competencies"][0]["items"] == ["Risk Frameworks", "Policy Design"]
    roles = resume["roles"]
    assert [(r["employer"], r["title"], r["start"], r["end"]) for r in roles] == [
        ("Northwind Labs", "Senior Manager, Programs", "2022", "2026"),
        ("Northwind Labs", "Analyst", "2020", "2022"),
        ("Fabrikam (fictional)", "Engineer", "2015", "2020"),
        ("Fabrikam (fictional)", "Junior Engineer", "2012", "2015"),
    ]
    lead = roles[0]
    assert lead["location"] == "Oakfield, ST" and lead["intro"] == "Sole leader of the programs practice."
    assert "Senior Manager" in lead["note"]
    assert [b["group"] for b in lead["bullets"]] == ["Governance Leadership", "Governance Leadership"]
    assert lead["bullets"][0]["note"] == "Never use an absolute here."
    assert roles[2]["location"] == "Riverton, ST" and roles[3]["optional"] is True
    assert resume["achievements"][0] == {"id": resume["achievements"][0]["id"], "text": "Led a statewide program",
                                         "note": "approved phrasing"}
    assert resume["education"][0]["school"] == "Example University" and resume["education"][0]["note"] == "No years."
    assert "evidence" not in resume
    # Evidence lives in a role now, and the importer can't tell which: the library's entries are projects, as
    # "Title: text", for the owner to place.
    assert {p["text"] for p in resume["projects"]} == {
        "Tools: Visio, Miro.", "Project X | a state agency: six analysts, 30 deliverables."}
    # The owner decided guardrails are dropped, so the standing notes and the guardrails section are not kept.
    assert "guardrails" not in resume and "INTERVIEW-ONLY" not in str(resume) and "em dashes" not in str(resume)
    assert [n["source"] for n in resume["notes"]] == ["TODO"], "nothing is lost: a section with no field is kept"


def test_form_answers_fill_the_facts_and_keep_no_standing_answers():
    _, contact = parse_master(MASTER)
    about = parse_form_answers(ANSWERS, contact)
    assert about["phone"] == "555-010-0199" and about["address"].startswith("1 Example Way")
    assert about["metro_location"] == "Springdale, ST" and about["work_authorization"] == "Yes"
    assert about["sponsorship"] == "No" and about["links"] == ["boardone.test/in/example"]
    assert "answers" not in about and "Notice period" not in str(about), "standing answers were dropped"


def test_the_writing_rules_read_into_voice_rules_banned_patterns_format_and_review():
    w = parse_writing(STYLE, RULES, VARIANTS, REVIEW)
    # The voice paragraph and the content rules both go to Rules for your voice, the paragraph first.
    assert [r["id"] for r in w["voice_rules"]] == ["voice", "rule-1", "rule-2", "rule-4a", "rule-10"]
    assert w["voice_rules"][0]["says"] == "Plain, short and optimistic."
    assert w["voice_rules"][1]["says"].startswith("**Clients are never named.**")
    assert "voice" not in w and "rules" not in w
    # A plain phrase is a Never write entry; a pattern waits, unconverted, in the legacy patterns.
    assert [b["phrase"] for b in w["banned"]] == ["spearheaded"]
    assert [(b["pattern"], b["regex"]) for b in w["legacy_patterns"]] == [("\\b\\d+\\+?\\s+years\\b", True)]
    from app.jobsearch.profile.documents import Writing
    assert Writing.model_validate(w).banned[0].phrase == "spearheaded"
    assert [f["id"] for f in w["resume_global"]] == ["format-0", "format-1"]
    assert [s["id"] for s in w["review_steps"]] == ["step-1", "step-2"]
    assert w["drift_patterns"] == [{"id": "scope-creep-by-one-word", "name": "Scope creep by one word",
                                    "looks_like": '"led AI for the firm"', "fix": "Use the master's scope noun"}]
    assert w["review_flags"][0]["says"].startswith("**Hard gates")


def test_a_workspace_folder_is_read_whole_and_says_what_it_found(tmp_path: Path):
    (tmp_path / "Master").mkdir()
    (tmp_path / "Profile").mkdir()
    (tmp_path / "Master" / "Pat_MasterResume.md").write_text(MASTER, encoding="utf-8")
    (tmp_path / "Master" / "Pat_MasterResume.backup_old.md").write_text("# Old", encoding="utf-8")
    for name, text in {"form-answers.md": ANSWERS, "writing-style.md": STYLE, "content-rules.md": RULES}.items():
        (tmp_path / "Profile" / name).write_text(text, encoding="utf-8")
    docs, report = read_workspace(tmp_path)
    assert set(docs) == {"resume", "about", "writing"}
    assert report[0].startswith("Pat_MasterResume.md: 4 roles, 5 bullets")
    assert "no resume-variants.md" in report and "no packet-review.md" in report
    for kind, data in docs.items():
        assert documents.MODELS[kind].model_validate(data)


def test_an_empty_folder_reads_nothing(tmp_path: Path):
    docs, report = read_workspace(tmp_path)
    assert docs == {} and report == ["no master resume (Master/*MasterResume*.md)"]


async def test_the_documents_are_stored_as_versions_and_read_together(js):
    from app import db

    resume, contact = parse_master(MASTER)
    async with db.session_factory()() as session:
        assert await profile_store.save(session, "tester", "resume", resume, by="import") == 1
        assert await profile_store.save(session, "tester", "about", parse_form_answers(ANSWERS, contact),
                                        by="import") == 1
        docs = await profile_store.owner_documents(session, "tester")
        counts = await profile_store.counts(session, "tester")
    assert docs.resume.roles[0].title == "Senior Manager, Programs" and docs.about.phone == "555-010-0199"
    assert docs.writing.voice_rules == [] and counts["resume"] == 1 and counts["writing"] == 0
