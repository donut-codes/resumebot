"""The owner's own documents (docs/PRD.md paths 8 and 9): the master resume, application info, and the writing rules
anything in their name follows. Stored as versions like the profile (`profile/store.py`), shown under Me.

- **resume**: the master every tailored resume is cut from. Each bullet keeps its note: the confirmation that says
  how it may be used (a packet writer obeys it, a reader sees it on request). Each role lists the tools used in it.
  Evidence lives in the role where it was
  earned: each role has its own list, never printed, and an entry used in several jobs is listed under each of them
  (the owner decided this). Anything not tied to work is a project.
- **about**: the facts forms ask for (contact, work authorization). They are shown and saved with the master resume,
  on one page, with no standing answers.
- **writing**: five sections, in the order Me shows them: rules for your voice, rules for ATS
  screening, the rules for the resume (Global, then one list for each section of the resume, then a list for each target
  role), the rules for cover letters (the same shape: Global, then one list for each part of the letter, then a list
  for each target role), and settings for QA of documentation (the review steps, the drift patterns, what a reviewer
  flags, the phrases that must never appear, which of the app's style rules are switched off, the owner's own
  certifications and licences, and rules for humanizing). Each rule is one point. `rules_for` hands a job the sections
  it needs.

Nothing here holds anyone's data: a new install's documents are empty, apart from the four starter rules for ATS
screening that `profile_store.ensure_starter_writing` saves once (`STARTER_ATS`).
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from app.jobsearch.profile import integrity, style_rules
from app.jobsearch.profile.base import Note, Part
from app.jobsearch.profile.never_write import BUILT_IN

DOCUMENT_KINDS = ("resume", "about", "writing")
#: The page margin the Word export may be given, in inches: the least and the most.
MARGIN_LEAST, MARGIN_MOST = 0.25, 1.5


class Line(Part):
    """A line of the resume with the note that governs its use ("approved phrasing", "never in writing")."""

    text: str
    note: str = ""


class Bullet(Line):
    #: The heading it sat under inside the role ("Team leadership"), for grouping; never printed as a subhead.
    group: str = ""


class Evidence(Part):
    title: str
    text: str


class Role(Part):
    employer: str
    location: str = ""
    title: str
    start: str = ""
    end: str = ""
    #: One plain line of scope under the title.
    intro: str = ""
    #: What the employer is, in a half line ("regional logistics company").
    descriptor: str = ""
    #: Left off a resume unless something in it serves a qualification the posting asks for.
    optional: bool = False
    note: str = ""
    #: The tools used in this job, so a form's "years with this tool" is answerable from the role's dates.
    tools: list[str] = []
    bullets: list[Bullet] = []
    #: Detail from this job for tailoring, never printed. An id is cited like a bullet's.
    evidence: list[Evidence] = []


class Competency(Part):
    area: str
    items: list[str] = []


class Education(Part):
    degree: str
    school: str = ""
    note: str = ""


class Rule(Part):
    says: str


class Resume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = ""
    summary_variants: list[Line] = []
    achievements: list[Line] = []
    philosophy: str = ""
    competencies: list[Competency] = []
    roles: list[Role] = []
    education: list[Education] = []
    projects: list[Line] = []
    awards: list[Line] = []
    notes: list[Note] = []


def master_texts(r: Resume) -> dict[str, str]:
    """The text of each master resume line, by its id: a bullet, an achievement, a project, an award, a summary variant,
    a role's evidence entry, a role (its title and employer) and an education entry. What a class or a grade cites by id
    is shown as these words (on a role's Requirements, and in Find Gaps)."""
    text = {b.id: b.text for role_ in r.roles for b in role_.bullets}
    text |= {x.id: x.text for x in [*r.achievements, *r.projects, *r.awards, *r.summary_variants]}
    text |= {e.id: f"{e.title}: {e.text}" for role_ in r.roles for e in role_.evidence}
    text |= {x.id: f"{x.title}, {x.employer}" for x in r.roles}
    text |= {e.id: f"{e.degree}, {e.school}" for e in r.education}
    return text


class About(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = ""
    location: str = ""
    #: The location to give on a form for a remote or out-of-town role, when it differs ("the metro area").
    metro_location: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    links: list[str] = []
    work_authorization: str = ""
    sponsorship: str = ""
    current_company: str = ""
    notes: list[Note] = []


class Banned(Part):
    """A phrase that must never appear in anything written in the owner's name, in plain words. The packet check matches
    it ignoring case, as whole words, with its simple variants (`never_write.phrase_pattern`)."""

    phrase: str
    why: str = ""


class LegacyPattern(Part):
    """A "Never write" entry from before phrases were plain words that was a regular expression. Kept as it was and
    enforced exactly as before, until the owner places it (a built-in check, a plain phrase, or dropped). `regex`:
    `pattern` is a case-insensitive regular expression; otherwise a plain phrase, matched ignoring case."""

    pattern: str
    regex: bool = False
    why: str = ""


class Drift(Part):
    name: str
    looks_like: str = ""
    fix: str = ""


class Credential(Part):
    """The name of a certification or licence of the owner's field, checked beside the app's own list
    (`integrity.CREDENTIALS`): a packet that names it when the master does not hold it is refused."""

    name: str


class Writing(BaseModel):
    """The writing rules, one list of one-point rules for each section (docs/PRD.md 19.5)."""

    model_config = ConfigDict(extra="forbid")
    #: Rules for your voice.
    voice_rules: list[Rule] = []
    #: Rules for ATS screening.
    ats_rules: list[Rule] = []
    #: Rules for interviews (docs/PRD.md 15.2.14): the owner's own interview habits, one point to a rule. Only interview
    #: prep reads them, with the voice rules.
    interview_rules: list[Rule] = []
    #: The resume rules, Global: what applies to the whole resume (length, margins, fonts, file type, section order,
    #: dates). The document rules (margins, page layout) that Settings once held are here too.
    resume_global: list[Rule] = []
    #: The rules for each section of the resume, in the order it prints them.
    resume_header: list[Rule] = []
    resume_summary: list[Rule] = []
    resume_competencies: list[Rule] = []
    resume_achievements: list[Rule] = []
    resume_experience: list[Rule] = []
    resume_education: list[Rule] = []
    #: Rules that apply only when tailoring a resume for one target role, by the role's id (`profile.TargetRole`). A
    #: role deleted in Search Settings keeps its rules here: they are the owner's words, shown with a note, never
    #: dropped.
    role_rules: dict[str, list[Rule]] = {}
    #: The page margin in inches, on all four sides of the Word resume and cover letter (under Global). Empty: the
    #: export keeps its own margins. A version saved before this was added loads without it.
    page_margin_inches: float | None = None
    #: The cover letter rules, Global: what applies to the whole letter (length, tone, when a letter is written at all,
    #: file type).
    letter_global: list[Rule] = []
    #: The rules for each part of the letter, in the order it prints them.
    letter_header: list[Rule] = []
    letter_greeting: list[Rule] = []
    letter_opening: list[Rule] = []
    letter_body: list[Rule] = []
    letter_closing: list[Rule] = []
    letter_signoff: list[Rule] = []
    #: Rules that apply only when writing a letter for one target role, by the role's id: a map of its own, apart from
    #: the resume's. Kept for a role since deleted, as the resume's are.
    letter_role_rules: dict[str, list[Rule]] = {}
    #: Settings for QA of documentation: the phrases that must never appear (checked by code), the steps of the review,
    #: the patterns reviewers catch, what a reviewer flags for the owner instead of fixing, and rules for humanizing.
    banned: list[Banned] = []
    #: The "Never write" entries that were regular expressions, from before phrases were plain words: kept as they were
    #: and enforced as before, shown read-only until the owner places them.
    legacy_patterns: list[LegacyPattern] = []
    review_steps: list[Rule] = []
    drift_patterns: list[Drift] = []
    review_flags: list[Rule] = []
    humanizing: list[Rule] = []
    #: The ids of the app's style rules (`style_rules.STYLE_RULES`) the owner has switched off. Empty is every rule on,
    #: which is how a version saved before the switches loads. An id this release does not know is kept and ignored.
    style_off: list[str] = []
    #: The owner's own certification and licence names, checked beside the app's list.
    credentials: list[Credential] = []
    notes: list[Note] = []

    @field_validator("page_margin_inches")
    @classmethod
    def _margin(cls, v: float | None) -> float | None:
        # Written this way round so that NaN is refused too.
        if v is not None and not MARGIN_LEAST <= v <= MARGIN_MOST:
            raise PydanticCustomError(
                "page_margin", f"page margins must be between {MARGIN_LEAST} and {MARGIN_MOST} inches")
        return v


#: The sections of the writing rules, in the order Me shows them: the key, then the name it goes by on the page and in
#: what a writer or reviewer is handed.
WRITING_SECTIONS = (
    ("voice", "Rules for your voice"),
    ("ats", "Rules for ATS screening"),
    ("format", "Rules for the resume, by section"),
    ("letters", "Rules for cover letters"),
    ("interview", "Rules for interviews"),
    ("qa", "Settings for QA of documentation"),
)
#: What is under Settings for QA of documentation, in order: the field and the name it goes by.
QA_PARTS = (
    ("review_steps", "The review every packet gets"),
    ("drift_patterns", "Drift reviewers catch"),
    ("review_flags", "Flagged for you, not fixed"),
    ("banned", "Never write"),
    ("humanizing", "Humanizing documents"),
)
#: What the writer is told of the checks that ship with the app, beside Never write (the page shows them as "Checked
#: automatically"), then the three integrity checks it is always held to (the page does not list them) and the style
#: rules that are on.
CHECKED_AUTOMATICALLY = "Checked automatically"
ALWAYS_CHECKED_NAME = "Always checked"
STYLE_ON_NAME = "Style rules on"
_SECTION_RULES = {"voice": "voice_rules", "ats": "ats_rules", "interview": "interview_rules"}
#: The four rules a new install's Rules for ATS screening start with (saved once by
#: `profile_store.ensure_starter_writing`). The owner edits them like any rule.
STARTER_ATS = (
    Rule(id="one-column", says="Use one column."),
    Rule(id="section-names", says="Use the standard section names: Professional Summary, Core Competencies, "
                                  "Professional Experience, Education."),
    Rule(id="no-tables", says="Put nothing in a table, a text box, a header or a footer."),
    Rule(id="hide-nothing", says="Hide nothing: no white text, no tiny text, no text off the page."),
)
#: The parts of the rules for the resume, in the order the resume prints them after Global: the field and the name the
#: section goes by (the resume's own headings; Header is the name and contact line).
RESUME_SECTIONS = (
    ("resume_header", "Header"),
    ("resume_summary", "Summary"),
    ("resume_competencies", "Core Competencies"),
    ("resume_achievements", "Selected Achievements"),
    ("resume_experience", "Experience"),
    ("resume_education", "Education"),
)
#: The parts of the rules for the cover letter, in the order the letter prints them after Global: the field and the name
#: the part goes by.
LETTER_SECTIONS = (
    ("letter_header", "Header"),
    ("letter_greeting", "Greeting"),
    ("letter_opening", "Opening"),
    ("letter_body", "Body"),
    ("letter_closing", "Closing"),
    ("letter_signoff", "Sign-off"),
)
GLOBAL_NAME = "Global"


def role_rules_name(role_name: str) -> str:
    """What the rules for one target role are called wherever they are shown."""
    return f"For {role_name} roles"


#: Which sections each job is handed: drafting a resume, drafting a cover letter, reviewing a
#: document.
JOB_SECTIONS = {
    "resume": ("voice", "ats", "format"),
    "letter": ("voice", "letters"),
    "review": ("qa",),
    "interview": ("voice", "interview"),
}


def rules_for(writing: Writing, *jobs: str, role: tuple[str, str] | None = None) -> dict:
    """The writing rules these jobs need, each section under its name, in the page's order: a section's rules as their
    words, the rules for the resume and for the cover letter each as Global and then each part under its name, and
    Settings for QA of documentation as its named parts. A writer session that does several jobs (drafts a resume and a
    letter, and reviews them) is handed them together, and a section two of them need comes once. `role` is the target
    role the posting matched, as (id, name): its rules come last in the rules for the resume and in the rules for the
    cover letter, each when it has any."""
    unknown = [j for j in jobs if j not in JOB_SECTIONS]
    if unknown:
        raise ValueError(f"no job called {unknown[0]!r}; the jobs are {', '.join(JOB_SECTIONS)}")
    wanted = {key for job in jobs for key in JOB_SECTIONS[job]}
    out: dict = {}
    for key, name in WRITING_SECTIONS:
        if key not in wanted:
            continue
        if key == "qa":
            out[name] = _qa(writing)
        elif key == "format":
            out[name] = _by_part(writing, writing.resume_global, RESUME_SECTIONS, writing.role_rules, role)
        elif key == "letters":
            out[name] = _by_part(writing, writing.letter_global, LETTER_SECTIONS, writing.letter_role_rules, role)
        else:
            out[name] = [r.says for r in getattr(writing, _SECTION_RULES[key])]
    return out


def _qa(writing: Writing) -> dict:
    """Settings for QA of documentation as the writer is handed them. Under Never write come the owner's phrases and
    then any older pattern kept from before phrases were plain words; the checks the app runs on every draft follow
    (the four kinds of text, the three integrity checks, and the style rules that are on), so a draft is written clear
    of them."""
    out = {part: [_as_said(item) for item in getattr(writing, field)] for field, part in QA_PARTS}
    out["Never write"] += [_as_said(item) for item in writing.legacy_patterns]
    out[CHECKED_AUTOMATICALLY] = [{"name": c.name, "rule": c.rule} for c in BUILT_IN]
    out[ALWAYS_CHECKED_NAME] = [{"name": c.name, "rule": c.rule} for c in integrity.ALWAYS_CHECKED]
    out[STYLE_ON_NAME] = [{"name": r.name, "rule": style_rules.told(r)} for r in style_rules.on(writing)]
    return out


def _by_part(writing: Writing, global_rules: list[Rule], parts, by_role: dict[str, list[Rule]],
             role: tuple[str, str] | None) -> dict:
    """The rules for a document (the resume, the cover letter): Global, then each part under its name, then the rules
    for the matched target role when it has any."""
    out = {GLOBAL_NAME: [r.says for r in global_rules],
           **{part: [r.says for r in getattr(writing, field)] for field, part in parts}}
    if role and by_role.get(role[0]):
        out[role_rules_name(role[1])] = [r.says for r in by_role[role[0]]]
    return out


def _as_said(item) -> object:
    """A rule as its words; a banned phrase or a drift pattern as its fields, without the id."""
    return item.says if isinstance(item, Rule) else item.model_dump(mode="json", exclude={"id"})


MODELS = {"resume": Resume, "about": About, "writing": Writing}


class Documents(BaseModel):
    resume: Resume = Field(default_factory=Resume)
    about: About = Field(default_factory=About)
    writing: Writing = Field(default_factory=Writing)
