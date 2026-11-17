"""The integrity checks of a packet (docs/PRD.md 33.5): three checks that are code, always on and not a setting, each
reading the words the writer prints.

- **Certifications and licences.** A name from the shipped list (`CREDENTIALS`) or the owner's own
  (`Writing.credentials`) in the printed text must also be in the master's own words. The master's notes are not its
  words: a note that says "never in writing: a lapsed licence" must not make the name allowed.
- **Work that did not ship.** Three shapes of a sentence that writes proposed, planned or drafted work as delivered
  (`NOT_SHIPPED`). They are tuned to be quiet; a reviewer judges the rest.
- **Never in writing.** A master line whose note says "never in writing" stays out of a packet: not cited, not
  included (a role or a degree), and its words not printed.

Each finder returns lines that name the place in plain words and what was found, never a master id. Nothing here edits
the master or reads anything but the words it is given.
"""

import re
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from app.jobsearch.profile import never_write

if TYPE_CHECKING:
    from app.jobsearch.profile.documents import Education, Resume, Role, Writing

#: The three checks by the names a packet's Checks list them under.
CERTIFICATIONS_CHECK = "Certifications are the master's"
UNSHIPPED_CHECK = "Unshipped work isn't called delivered"
NEVER_IN_WRITING_CHECK = "Lines marked never in writing stay out"


@dataclass(frozen=True)
class Always:
    id: str
    #: The check's name, as a packet's Checks list it.
    name: str
    #: What it asks, in a sentence: what the writer and a reviewer are told.
    rule: str


#: The three checks, for the writer's and the reviewer's briefs and for the names of the checks. The settings page does
#: not list them: they are not a setting.
ALWAYS_CHECKED = (
    Always("certifications", CERTIFICATIONS_CHECK,
           "Name no certification or licence the master doesn't hold: the app knows the common ones, and the owner "
           "adds those of their field."),
    Always("unshipped-work", UNSHIPPED_CHECK,
           "Never write work that was only proposed, planned or drafted as if it was delivered."),
    Always("never-in-writing", NEVER_IN_WRITING_CHECK,
           "Leave out every master line marked never in writing: don't cite it, include its role or degree, or print "
           "its words."),
)


def role_words(role: "Role") -> str:
    """A role in plain words, as the owner reads it: its title at its employer."""
    title, employer = role.title.strip(), role.employer.strip()
    return f"{title} at {employer}" if title and employer else title or employer or "a role in the master"


def education_words(education: "Education") -> str:
    """An education entry in plain words: its degree and its school."""
    return ", ".join(x for x in (education.degree.strip(), education.school.strip()) if x) or (
        "an education entry in the master")


# --- certifications and licences -------------------------------------------------------------------------------------

#: Well-known certifications and licences, each by its full name and, when it has one that is three letters or more
#: and not also a common abbreviation of something else, its acronym (no "PE": private equity; no "CPA": cost per
#: acquisition). One entry is one credential: the master holding any form of it allows every form. Not one person's
#: list: the owner adds the names of their own field under Settings for QA of documentation.
CREDENTIALS: tuple[tuple[str, ...], ...] = (
    # Project, program and product management
    ("Project Management Professional", "PMP"),
    ("Certified Associate in Project Management", "CAPM"),
    ("Program Management Professional",),
    ("PMI Agile Certified Practitioner",),
    ("Certified ScrumMaster", "Certified Scrum Master"),
    ("Professional Scrum Master",),
    ("Certified Scrum Product Owner",),
    ("PRINCE2 Practitioner", "PRINCE2 Foundation"),
    ("Certified Business Analysis Professional", "CBAP"),
    # Quality and process
    ("Six Sigma Yellow Belt",),
    ("Six Sigma Green Belt",),
    ("Six Sigma Black Belt",),
    ("Certified Quality Engineer",),
    ("Certified Quality Auditor",),
    ("ITIL Foundation", "ITIL 4 Foundation"),
    # Finance, accounting and audit
    ("Certified Public Accountant",),
    ("Chartered Financial Analyst", "CFA"),
    ("Certified Management Accountant",),
    ("Certified Internal Auditor",),
    ("Certified Fraud Examiner",),
    ("Certified Financial Planner",),
    ("Chartered Accountant",),
    # Security, risk and audit of systems
    ("Certified Information Systems Security Professional", "CISSP"),
    ("Certified Information Security Manager", "CISM"),
    ("Certified Information Systems Auditor",),
    ("Certified Ethical Hacker", "CEH"),
    ("Offensive Security Certified Professional", "OSCP"),
    ("Certified in Risk and Information Systems Control", "CRISC"),
    ("Certified Cloud Security Professional", "CCSP"),
    # Networks, cloud and systems
    ("CompTIA Security+",),
    ("CompTIA Network+",),
    ("CompTIA A+",),
    ("Cisco Certified Network Associate", "CCNA"),
    ("Cisco Certified Network Professional", "CCNP"),
    ("AWS Certified Solutions Architect",),
    ("AWS Certified Cloud Practitioner",),
    ("AWS Certified Developer",),
    ("Azure Administrator Associate",),
    ("Azure Solutions Architect Expert",),
    ("Google Cloud Professional Cloud Architect",),
    ("Certified Kubernetes Administrator",),
    ("Red Hat Certified Engineer", "RHCE"),
    ("Red Hat Certified System Administrator", "RHCSA"),
    # People and human resources
    ("SHRM Certified Professional", "SHRM-CP"),
    ("SHRM Senior Certified Professional", "SHRM-SCP"),
    ("Professional in Human Resources",),
    ("Senior Professional in Human Resources", "SPHR"),
    # Health
    ("Registered Nurse",),
    ("Licensed Practical Nurse",),
    ("Licensed Clinical Social Worker",),
    ("Advanced Cardiac Life Support", "ACLS"),
    ("Basic Life Support",),
    # Law, privacy and compliance
    ("Certified Information Privacy Professional", "CIPP"),
    ("Certified Compliance and Ethics Professional", "CCEP"),
    ("Notary Public",),
    # Trades and transport
    ("Professional Engineer",),
    ("Commercial Driver's License",),
)
#: An acronym is two to six capitals: matched as whole words and in that case ("pmp" inside a word is not "PMP").
_ACRONYM = re.compile(r"[A-Z]{2,6}")
WHY_CERTIFICATION = "a certification or licence the master doesn't hold"


@cache
def _form(name: str) -> re.Pattern:
    """The matcher for one name: an acronym as whole words in its own case; a longer name as whole words, ignoring case
    and across any run of space."""
    words = name.split()
    if len(words) == 1 and _ACRONYM.fullmatch(words[0]):
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(words[0])}(?![A-Za-z0-9])")
    body = r"\s+".join(never_write.literal(word) for word in words)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _entries(writing: "Writing") -> list[tuple[str, ...]]:
    """The credentials to look for: the shipped list, then the owner's own names (a blank one is nothing, and a name the
    app already knows or the owner listed twice is looked for once)."""
    entries = list(CREDENTIALS)
    seen = {" ".join(form.split()).lower() for entry in CREDENTIALS for form in entry}
    for credential in writing.credentials:
        name = " ".join(credential.name.split())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            entries.append((name,))
    return entries


def master_words(resume: "Resume") -> str:
    """The master's own words, for what it holds: its summary and variants, achievements, competencies, tools, each
    role's employer, title, scope line, descriptor, bullets and evidence, its projects, awards and philosophy, and each
    degree and school. Not a line's note, a role's or degree's note, nor the master's notes."""
    parts = [resume.summary, resume.philosophy]
    parts += [x.text for x in (*resume.summary_variants, *resume.achievements, *resume.projects, *resume.awards)]
    parts += [x for c in resume.competencies for x in (c.area, *c.items)]
    for role in resume.roles:
        parts += [role.employer, role.title, role.intro, role.descriptor, *role.tools]
        parts += [b.text for b in role.bullets]
        parts += [x for e in role.evidence for x in (e.title, e.text)]
    parts += [x for e in resume.education for x in (e.degree, e.school)]
    return "\n".join(p for p in parts if p)


def certification_hits(writing: "Writing", resume: "Resume", texts: list[tuple[str, str]]) -> list[str]:
    """Each certification or licence named in the printed text that the master does not hold, one line for each in each
    place: where, and the name as it was printed. `writing` holds the owner's own names, `resume` is the master, and
    `texts` is each piece of printed text with where it is (the shape `never_write.find_hits` takes). This is the one
    finder: every check of text in the owner's voice calls it."""
    master = master_words(resume)
    entries = _entries(writing)
    held: dict[tuple[str, ...], bool] = {}
    found: list[str] = []
    for where, text in texts:
        for entry in entries:
            named = [(m.start(), m.group(0)) for form in entry if (m := _form(form).search(text))]
            if not named:
                continue
            if entry not in held:
                held[entry] = any(_form(form).search(master) for form in entry)
            if not held[entry]:
                found.append(f"{where}: {min(named)[1]!r} ({WHY_CERTIFICATION})")
    return found


# --- work that did not ship ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    id: str
    pattern: re.Pattern


#: A verb that says something was delivered.
_DELIVERED = (
    "deliver", "implement", "launch", "ship", "deploy", "build", "built", "design", "develop", "create", "execute",
    "complete", "release", "roll out", "establish", "introduce", "produce",
)
_VERBS = "|".join(p.pattern for verb in _DELIVERED if (p := never_write.phrase_pattern(verb)) is not None)
#: One word of a stretch that stays inside a sentence: no full stop, semicolon, ! or ? in it.
_WORDS_BETWEEN = r"(?:[^\s.;!?]+\s+)"
#: The verbs of the last two shapes, which say it was delivered.
_DELIVERED_AFTER = r"(?:delivered|implemented|launched|shipped|deployed)(?!\w)"

#: The three shapes, quiet by design: "launched a pilot" is not one. The reviewer judges the rest.
NOT_SHIPPED = (
    # A delivery verb, then "a", "an" or "the" proposed, planned or draft: "designed a proposed".
    Shape("delivered-a-proposal", re.compile(rf"(?:{_VERBS})\s+(?:a|an|the)\s+(?:proposed|planned|draft)(?!\w)",
                                             re.IGNORECASE)),
    # A proposal for or to something, then it was delivered, implemented, launched, shipped or deployed.
    Shape("proposal-delivered", re.compile(
        rf"(?<!\w)proposal\s+(?:for|to)\s+{_WORDS_BETWEEN}{{0,12}}?{_DELIVERED_AFTER}", re.IGNORECASE)),
    # Proposed, planned or staged work that "was" or "were" delivered, implemented, launched, shipped or deployed.
    Shape("proposed-was-delivered", re.compile(
        rf"(?<!\w)(?:proposed|planned|staged)\s+{_WORDS_BETWEEN}{{0,6}}?(?:was|were)\s+{_DELIVERED_AFTER}",
        re.IGNORECASE)),
)
WHY_NOT_SHIPPED = "work that didn't ship, written as delivered"


def unshipped_hits(texts: list[tuple[str, str]]) -> list[str]:
    """Each place the printed text writes work that did not ship as delivered: where, and the words."""
    return [f"{where}: {m.group(0)!r} ({WHY_NOT_SHIPPED})" for where, text in texts
            for shape in NOT_SHIPPED for m in shape.pattern.finditer(text)]


# --- never in writing ------------------------------------------------------------------------------------------------

#: A note that says it, with a hyphen (of either kind) or spaces, in any case ("Never in writing", "never-in-writing").
_GAP = rf"[\s{chr(0x2010)}{chr(0x2011)}\-]+"
_NEVER_IN_WRITING = re.compile(rf"never{_GAP}in{_GAP}writing", re.IGNORECASE)
#: Five words of a marked line, in a row, in one printed piece are the line used; a line of fewer words must appear
#: whole.
WORDS_THAT_COUNT = 5
#: The curly apostrophes and the modifier letter, which read as the plain one.
_APOSTROPHES = {0x2019: "'", 0x2018: "'", 0x02BC: "'"}
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.translate(_APOSTROPHES).lower())


def marked_lines(resume: "Resume") -> tuple[dict[str, str], dict[str, str]]:
    """What the master marks never in writing: its lines by id, each as its own text (a bullet, achievement, project,
    award or summary variant), and its roles and education entries by id, each in plain words. The master's free notes
    are not read."""
    lines = {
        x.id: x.text
        for x in (*(b for r in resume.roles for b in r.bullets), *resume.achievements, *resume.projects, *resume.awards,
                  *resume.summary_variants)
        if _NEVER_IN_WRITING.search(x.note)}
    others = {r.id: role_words(r) for r in resume.roles if _NEVER_IN_WRITING.search(r.note)}
    others |= {e.id: education_words(e) for e in resume.education if _NEVER_IN_WRITING.search(e.note)}
    return lines, others


def _printed(line: list[str], words: list[str]) -> bool:
    """Is the marked line's text in these words: five in a row, or the whole of a line under five words?"""
    if not line:
        return False
    size = min(WORDS_THAT_COUNT, len(line))
    grams = {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}
    return any(tuple(line[i:i + size]) in grams for i in range(len(line) - size + 1))


def never_in_writing_hits(resume: "Resume", cites: list[tuple[str, str]], includes: list[tuple[str, str]],
                          texts: list[tuple[str, str]]) -> list[str]:
    """Each place a packet uses a master line marked never in writing. `cites` is each citation as (where, master id):
    a bullet's, an achievement's, the summary's or the letter's `from`. `includes` is each role or education entry the
    packet lists, as (where, master id). `texts` is each piece of printed text with where it is. One line for each
    marked line in each place, naming the place and the line by its own words."""
    lines, others = marked_lines(resume)
    if not lines and not others:
        return []
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    for where, ident in cites:
        words = lines[ident] if ident in lines else others.get(ident)
        if words is not None and (where, ident) not in seen:
            seen.add((where, ident))
            found.append(f"{where}: cites a line the master marks never in writing ({words!r})")
    for where, ident in includes:
        if ident in others and (where, ident) not in seen:
            seen.add((where, ident))
            found.append(f"{where}: includes {others[ident]!r}, which the master marks never in writing")
    spoken = {ident: _words(text) for ident, text in lines.items()}
    for where, text in texts:
        words = _words(text)
        for ident, line in lines.items():
            if (where, ident) not in seen and _printed(spoken[ident], words):
                seen.add((where, ident))
                found.append(f"{where}: prints words of a line the master marks never in writing ({line!r})")
    return found
