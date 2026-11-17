"""The master resume as a JSON Resume file (docs/PRD.md 8.4; the owner, 2026-10-30): the open interchange format other
resume tools import (https://jsonresume.org, schema v1.0.0), so the master can be taken to a different tool.

`json_resume` is one pure function from the two documents the master is made of, the resume and the application info
(`documents.py`), to the document. It reads what it is given and changes nothing: an export is an output, never a way
back in, and the database stays the source of truth.

Every text is copied as the master has it: nothing is reworded, trimmed or rearranged (`strip()` only decides whether a
value is empty, and an empty value, or a section with nothing in it, is left out, because the standard refuses an empty
email and a date that is not one). The fields the standard checks the form of are the exception: an email (one
address), a link (a web address as RFC 3986 writes it) and a date (as far as it can be read for certain) are written
only when they read as what they are, and then without the spaces round them. Anything else in them is left out, never
fixed up or guessed at, because a tool that imports the file (Reactive Resume) refuses the whole of it for one such
value. The master keeps its own order. What the standard has no true home for stays out rather than being forced into
a field that means something else:

- from the resume: Selected Achievements (JSON Resume has no top-level field for them: `awards` is the master's
  awards, `highlights` belong to a job), the alternate summaries, the philosophy, the parked notes, every note on a
  line, role or degree, a role's optional flag, tools and evidence, a bullet's group, and every id (the standard has no
  id field, so nothing re-imported could be linked back);
- from the application info: the metro location, work authorization, sponsorship, current company and notes, and any
  email or link that does not read as one.

The vocabulary of a field it does fill: `description` of a job is the master's half line on what the employer is, and
`summary` the scope line; a degree is `studyType`, whole (splitting it from an area would be a guess); an award is a
`title`; a project, which the master keeps as one sentence, is a `name` (the one field tools keep a project by:
Reactive Resume drops a project that has none, so written as a `description` the whole section was lost there); the
place is the whole text as the `city` (two capital letters after a comma are a state in one country and a country in
another, so no region or country code is guessed). An entry with none of the fields a tool keeps it by (a degree with
no school, a competency with no area, a role with no employer or title) is written as it is and that tool skips it:
the standard allows it, and no name is made up. `meta.version` is not used (the standard says it is a semantic
version, and a bare master number would misuse it). `meta.lastModified` is when the newest thing in the file was saved,
the version of the resume or the contact details beside it, whichever is later: an earlier version does not claim to be
new, and one version is one file whenever it is exported. The versions the file was made from are in `meta.resumebot`,
which the standard allows for a tool's own configuration.
"""

import re
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from app.jobsearch.profile.documents import About, Resume, Role

#: The schema the file says it follows: the tag of the version the owner named (the repository the standard is kept in).
SCHEMA_URL = "https://raw.githubusercontent.com/jsonresume/resume-schema/v1.0.0/schema.json"

#: What the owner is told when the master has nothing of its own to put in a file.
EMPTY = "Your master resume is empty, so there is nothing to export yet."


def nothing_to_export(resume: Resume) -> bool:
    """Whether the master resume has no summary and no role that would be written: nothing a resume holds. Achievements
    alone do not count, since they stay out of the file, nor does a role with nothing in it (no employer, title, place,
    lines or bullets: `_work` leaves it out). The page hides its Export link for the same master (`hasExport`)."""
    return not resume.summary.strip() and not _work(resume)


# --- dates ------------------------------------------------------------------------------------------------------------

#: Digits are 0 to 9 and nothing else (`\d` takes a digit of any script, and a year of them is a date the standard
#: refuses), and a month's name is ASCII letters (`(?ai:...)` ignores case for ASCII only: a long s is no "s").
_YEAR_PATTERN = r"(?:19|20)[0-9]{2}"
_YEAR = re.compile(rf"(?<![0-9])({_YEAR_PATTERN})(?![0-9])")
_ISO_FULL = re.compile(rf"(?<![0-9])({_YEAR_PATTERN})-([0-9]{{2}})-([0-9]{{2}})(?![0-9])")
#: "03/2019", "3-2019" and "03.2019", not the tail of "03/04/2019" (day and month in either order: only the year is
#: sure), and not the number of a quarter or a half ("Q1-2019").
_MONTH_YEAR = re.compile(rf"(?<![0-9/.\-A-Za-z])([0-9]{{1,2}})\s*[/.\-]\s*{_YEAR_PATTERN}(?![0-9])")
#: "2019-03" and "2019/03", not the head of a longer date. `_after_year` looks at what follows.
_YEAR_MONTH = re.compile(rf"(?<![0-9]){_YEAR_PATTERN}\s*[/.\-]\s*([0-9]{{1,2}})(?![0-9]|\s*[/.\-]\s*[0-9])")
#: A word straight after a number: "2019 - 2 years" counts years, and the 2 is no month.
_WORD_NEXT = re.compile(r"\s*[^\W\d_]")
_MONTH_WORD = (r"((?ai:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
               r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?))")
#: A month by name, as a whole word ("Marketing" and "Junior" are not months).
_NAMED = re.compile(rf"(?<![A-Za-z]){_MONTH_WORD}(?![A-Za-z])", re.IGNORECASE)
#: A month by name directly before the year: "Jan 2019", "Sept. 2019", "December, 2019".
_NAMED_YEAR = re.compile(rf"(?<![A-Za-z]){_MONTH_WORD}(?![A-Za-z])\.?,?\s*{_YEAR_PATTERN}(?![0-9])", re.IGNORECASE)
_MONTH_NUMBER = {name: n for n, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
#: An end that says the job goes on (the standard's way to say so is to leave `endDate` out).
_OPEN_END = re.compile(r"\b(present|current|now|today|ongoing|to date)\b", re.IGNORECASE)


def _number(word: str) -> int:
    return _MONTH_NUMBER[word[:3].lower()]


def _after_year(text: str) -> list[int]:
    """The numbers written after a year ("2019-03", "2018-19"), each one only when no word follows it."""
    return [int(m[1]) for m in _YEAR_MONTH.finditer(text) if not _WORD_NEXT.match(text, m.end())]


def _months(text: str) -> set[int]:
    """The one month a text names, as a set that holds it: a number from 1 to 12 beside the year, or a month's name
    directly before it. A text that names two months ("Jan - Mar 2019"), or a month away from the year, is not sure of
    one, so the set is empty."""
    numeric = {int(m[1]) for m in _MONTH_YEAR.finditer(text)} | set(_after_year(text))
    numeric = {n for n in numeric if 1 <= n <= 12}
    beside = {_number(m[1]) for m in _NAMED_YEAR.finditer(text)}
    found = numeric | {_number(m[1]) for m in _NAMED.finditer(text)}
    return found if len(found) == 1 and found <= numeric | beside else set()


def iso_date(text: str, *, end: bool = False) -> str | None:
    """A date as the standard writes one (`YYYY`, `YYYY-MM` or `YYYY-MM-DD`), read from how a resume writes it, or None
    when it cannot be read for certain (the file then leaves the date out, and nothing is guessed). A blank is None, and
    so is an end that says it goes on ("Present", "current", "to date"); a text with no year, or with two ("2019 -
    2022" in one field), is None. With one year: a real full date is kept; a month from 1 to 12 beside it ("2019-03",
    "03/2019", "Jan 2019", "Sept. 2019") gives the month; anything else ("2019", "Spring 2019", "Q1-2019", "2019 - 2
    years", "03/04/2019") gives the year, because the year is certain and the rest is not. No first or last month is
    borrowed for a bare year. An end that is a school year or a range ("2018-19") is None: its first year is not the
    end."""
    if not (text or "").strip():
        return None
    if end and _OPEN_END.search(text):
        return None
    years = set(_YEAR.findall(text))
    if len(years) != 1:
        return None
    if end and any(not 1 <= int(m[1]) <= 12 for m in _YEAR_MONTH.finditer(text)):
        return None
    (year,) = years
    if full := _ISO_FULL.search(text):
        month, day = int(full[2]), int(full[3])
        try:
            date(int(year), month, day)
        except ValueError:
            pass
        else:
            return f"{year}-{month:02d}-{day:02d}"
    if months := _months(text):
        return f"{year}-{months.pop():02d}"
    return year


# --- the people and places in the header ------------------------------------------------------------------------------

_SCHEME = re.compile(r"https?://", re.IGNORECASE)
#: A web address written without its scheme: a host of dotted labels ending in letters, then maybe a port and a path.
_BARE_ADDRESS = re.compile(r"[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}(?::\d+)?(?:[/?#]\S*)?")
#: A web address as RFC 3986 writes it, which is what `format: uri` asks for: an http or https URL with a host of ASCII
#: characters and no user info, a port that is digits, and a path, query and fragment of the characters the RFC allows
#: (anything else, a space, a bar, a brace, a letter of another script, is written as %XX or is not a URI).
_URI_CHAR = r"(?:[A-Za-z0-9\-._~!$&'()*+,;=]|%[0-9A-Fa-f]{2})"
_URI = re.compile(
    rf"https?://{_URI_CHAR}+(?::[0-9]*)?(?:/(?:{_URI_CHAR}|[:@])*)*(?:\?(?:{_URI_CHAR}|[:@/?])*)?"
    rf"(?:#(?:{_URI_CHAR}|[:@/?])*)?", re.IGNORECASE)
#: One email address, written as the importers of the file read one (Reactive Resume's is the strictest): an ASCII part
#: before the "@" with no dot at its ends or doubled, then dotted labels of the domain and a top-level domain of
#: letters. A name with brackets, a list, a note and an address with an accented letter are none of them that.
_EMAIL = re.compile(
    r"[A-Za-z0-9_'+\-]+(?:\.[A-Za-z0-9_'+\-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}")


def _filled(value: str) -> bool:
    return bool(value.strip())


def _location(about: About) -> dict:
    """The address as written, and the place as the whole text as the city. "City, ST" is not taken apart: two capital
    letters after a comma are a state in one country and a country in another ("DE", "CA", "UK"), so no region and no
    country code is guessed, and no postal code either."""
    out = {}
    if _filled(about.address):
        out["address"] = about.address
    if _filled(about.location):
        out["city"] = about.location
    return out


def _email(value: str) -> str | None:
    """The email as the file writes it (without the spaces round it), or None when it is not one address."""
    value = value.strip()
    return value if _EMAIL.fullmatch(value) else None


def _profile(link: str) -> dict | None:
    """One link as a profile (`network` is the host without "www."; no brand is named and no username guessed), or None
    when it does not read as a web address (`_URI`): one with an http or https scheme is used as written, and one
    written "host/path" gets "https://" in front (in the file only; the owner's text is not changed)."""
    link = link.strip()
    if _SCHEME.match(link):
        url = link
    elif _BARE_ADDRESS.fullmatch(link):
        url = f"https://{link}"
    else:
        return None
    if not _URI.fullmatch(url):
        return None
    try:
        parts = urlsplit(url)
        host, _port = parts.hostname, parts.port  # the port raises ValueError past 65535
    except ValueError:
        return None
    if not host:
        return None
    return {"network": host.removeprefix("www."), "url": url}


def _basics(resume: Resume, about: About) -> dict:
    out: dict = {}
    facts = (("name", about.name), ("email", _email(about.email) or ""), ("phone", about.phone),
             ("summary", resume.summary))
    for key, value in facts:
        if _filled(value):
            out[key] = value
    if location := _location(about):
        out["location"] = location
    if profiles := [p for link in about.links if (p := _profile(link))]:
        out["profiles"] = profiles
    return out


# --- the body ---------------------------------------------------------------------------------------------------------


def _entry(**fields) -> dict:
    """The fields that have something in them, in the order given; a list keeps only its filled items."""
    out = {}
    for key, value in fields.items():
        if isinstance(value, list):
            if value := [v for v in value if _filled(v)]:
                out[key] = value
        elif _filled(value):
            out[key] = value
    return out


def _dates(role: Role) -> tuple[str, str]:
    """The start and end of a role as the file writes them, "" for one it leaves out. The standard says a job goes on by
    leaving the end out, and importers print "Present" for it. So an end that is written but cannot be read ("Dec '21",
    "TBD") takes the start with it: neither is written, rather than a start with no end saying the job is still held."""
    start, end = iso_date(role.start) or "", iso_date(role.end, end=True) or ""
    if not end and _filled(role.end) and not _OPEN_END.search(role.end):
        return "", ""
    return start, end


def _work(resume: Resume) -> list[dict]:
    """One entry for each role that has something to say besides its dates (a role of only dates is no job)."""
    jobs = []
    for r in resume.roles:
        start, end = _dates(r)
        job = _entry(
            name=r.employer, position=r.title, location=r.location, description=r.descriptor, summary=r.intro,
            startDate=start, endDate=end, highlights=[b.text for b in r.bullets],
        )
        if not job.keys() <= {"startDate", "endDate"}:
            jobs.append(job)
    return jobs


def json_resume(resume: Resume, about: About, *, master_version: int, about_version: int,
                modified_at: datetime) -> dict:
    """The master as a JSON Resume v1.0.0 document: `resume` is the version being exported and `about` the contact
    details in force now (a number from last year helps no one), each with its version number, which the file records
    in `meta.resumebot`. `modified_at` is the time written as `meta.lastModified` (UTC): when the newest of the two was
    saved, which the caller reads from the store. Sections come in the standard's order, and one with nothing in it is
    not written."""
    at = modified_at if modified_at.tzinfo else modified_at.replace(tzinfo=UTC)
    sections = {
        "basics": _basics(resume, about),
        "work": _work(resume),
        "education": [e for x in resume.education if (e := _entry(institution=x.school, studyType=x.degree))],
        "awards": [a for x in resume.awards if (a := _entry(title=x.text))],
        "skills": [s for c in resume.competencies if (s := _entry(name=c.area, keywords=c.items))],
        "projects": [p for x in resume.projects if (p := _entry(name=x.text))],
    }
    return {
        "$schema": SCHEMA_URL,
        **{name: body for name, body in sections.items() if body},
        "meta": {
            "lastModified": at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "resumebot": {"masterVersion": master_version, "aboutVersion": about_version},
        },
    }
