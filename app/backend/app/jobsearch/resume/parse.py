"""Import your resume (Me, the master resume): the owner chooses their resume file or pastes its text, and a small
Claude session (Haiku, the `parser` kind, the resume mode of the job `jobs/parse.py` keeps) divides it into the fields
of the master resume as the Me page holds them, for the owner to look over before saving anything.

This file is what the resume mode adds to the posting parser: the fields a session reports, how many entries each
holds, the check of a report, and the words the session is handed. The job's states, timeouts, readiness and tools are
`jobs/parse.py`'s, the same for both kinds; reading a file's text is `resume/text.py`.

What a session reports is the resume's words, never rewritten: `summary`, `achievements`, `competencies`, `roles`
(each with its employer, title, place, dates, scope line, what the employer is, and bullets), `education`,
`philosophy`, `awards`, `projects`, and the owner's `contact` details, which fill the facts forms ask for (the about
document), not a part of the resume itself. A role's evidence is not a part of a resume, so a session never fills
it.

No text has a most: a line, a summary or a bullet is as long as the resume has it, and a report that shortened one
would be the resume rewritten (the character-limits sweep). What is counted is how many entries a field has, which no
resume comes near; and the resume's own size is bounded only by what an agent can be sent with the answer shape
(`parse_routes.py`, `owner_text.py`).
"""

from typing import Any

#: The fewest characters a resume's text can have.
MIN_CHARS = 50

#: What a session reports, in the order the Me page holds them, and how many entries each holds.
TEXT_FIELDS = ("summary", "philosophy")
LINE_FIELDS = ("achievements", "awards", "projects")
FIELDS = (
    "summary", "achievements", "competencies", "roles", "education", "philosophy", "awards", "projects", "contact")
MAX_LINES = {"achievements": 40, "awards": 50, "projects": 40}
MAX_COMPETENCIES, MAX_TERMS = 30, 60
MAX_ROLES, MAX_BULLETS = 40, 40
MAX_EDUCATION = 15
MAX_LINKS = 10

ROLE_SHORT = ("employer", "title", "location", "start", "end", "descriptor")
ROLE_FIELDS = (*ROLE_SHORT, "intro", "bullets")
EDUCATION_FIELDS = ("degree", "school")
COMPETENCY_FIELDS = ("area", "items")
CONTACT_SHORT = ("name", "location", "address", "phone", "email")
CONTACT_FIELDS = (*CONTACT_SHORT, "links")

NOTHING_FOUND = "The agent found nothing it could use in that text. Check that it is your resume."

#: What the session is handed with the text, in the words of its instructions.
HOW = (
    "This is a resume, not a job posting: divide it into the master resume's fields, each from the resume's own "
    "words, copied verbatim and never reworded or polished, however plainly written (a line of several distinct "
    "facts may be split into one line per fact, in the same words): summary, achievements (a list of lines), "
    "competencies (a list of {area, items}), roles (a list of {employer, title, location, start, end, intro, "
    "descriptor, bullets}), education (a list of {degree, school}), philosophy, awards and projects (lists of "
    "lines) and contact {name, location, address, phone, email, links}. Leave a field empty ('' or []) when the "
    "resume does not have it; never invent or add. Send them with jobs.parse_report: {id, fields}. Then call "
    "jobs.parse_next again."
)


class Problems:
    """What is wrong with a report, collected so every problem is named at once."""

    def __init__(self) -> None:
        self.found: list[str] = []

    def add(self, problem: str) -> None:
        self.found.append(problem)

    def text(self, where: str, value: Any) -> str:
        """`value` as trimmed text, however long: None is empty; anything but text is a problem."""
        if value is None:
            return ""
        if not isinstance(value, str):
            self.add(f"{where} must be text, not {type(value).__name__}")
            return ""
        return value.strip()

    def lines(self, where: str, value: Any, most: int, each: str = "line") -> list[str]:
        """A list of text lines: trimmed, empty ones left out, at most `most` of them."""
        if value is None:
            return []
        if not isinstance(value, list):
            self.add(f"{where} must be a list of text, one {each} each")
            return []
        kept = [self.text(f"{where}[{i}]", item) for i, item in enumerate(value)]
        kept = [k for k in kept if k]
        if len(kept) > most:
            self.add(f"{where} has {len(kept)}; at most {most}")
        return kept

    def objects(self, where: str, value: Any, most: int) -> list[tuple[int, dict]]:
        """A list of objects, each with its place in the list; anything else is a problem."""
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            self.add(f"{where} must be a list of objects")
            return []
        if len(value) > most:
            self.add(f"{where} has {len(value)}; at most {most}")
        return list(enumerate(value))

    def known(self, where: str, raw: dict, allowed: tuple[str, ...]) -> None:
        unknown = [str(k) for k in raw if k not in allowed]
        if unknown:
            self.add(f"{where}: {', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not "
                     f"{'a field' if len(unknown) == 1 else 'fields'}; the fields are {', '.join(allowed)}")


def _competencies(p: Problems, value: Any) -> list[dict]:
    out = []
    for i, raw in p.objects("competencies", value, MAX_COMPETENCIES):
        where = f"competencies[{i}]"
        p.known(where, raw, COMPETENCY_FIELDS)
        area = p.text(f"{where}.area", raw.get("area"))
        items = p.lines(f"{where}.items", raw.get("items"), MAX_TERMS, "term")
        if area or items:
            out.append({"area": area, "items": items})
    return out


def _roles(p: Problems, value: Any) -> list[dict]:
    out = []
    for i, raw in p.objects("roles", value, MAX_ROLES):
        where = f"roles[{i}]"
        p.known(where, raw, ROLE_FIELDS)
        role = {key: p.text(f"{where}.{key}", raw.get(key)) for key in ROLE_SHORT}
        role["intro"] = p.text(f"{where}.intro", raw.get("intro"))
        role["bullets"] = p.lines(f"{where}.bullets", raw.get("bullets"), MAX_BULLETS, "bullet")
        if any(role.values()):
            out.append(role)
    return out


def _education(p: Problems, value: Any) -> list[dict]:
    out = []
    for i, raw in p.objects("education", value, MAX_EDUCATION):
        where = f"education[{i}]"
        p.known(where, raw, EDUCATION_FIELDS)
        entry = {key: p.text(f"{where}.{key}", raw.get(key)) for key in EDUCATION_FIELDS}
        if any(entry.values()):
            out.append(entry)
    return out


def _contact(p: Problems, value: Any) -> dict:
    out: dict = {key: "" for key in CONTACT_SHORT}
    out["links"] = []
    if value is None:
        return out
    if not isinstance(value, dict):
        p.add("contact must be an object: {" + ", ".join(CONTACT_FIELDS) + "}")
        return out
    p.known("contact", value, CONTACT_FIELDS)
    for key in CONTACT_SHORT:
        out[key] = p.text(f"contact.{key}", value.get(key))
    out["links"] = p.lines("contact.links", value.get("links"), MAX_LINKS, "link")
    return out


def check(raw: Any) -> tuple[dict, list[str]]:
    """The master resume's fields as a session reported them, tidied (text trimmed, lines and entries with nothing in
    them left out, every field there, empty when the resume does not have it), and every problem with the report
    named at once, so the session can fix it and send it again."""
    if not isinstance(raw, dict):
        return {}, ["fields must be an object: {" + ", ".join(FIELDS) + "}"]
    p = Problems()
    p.known("fields", raw, FIELDS)
    out: dict = {}
    for key in TEXT_FIELDS:
        out[key] = p.text(key, raw.get(key))
    for key in LINE_FIELDS:
        out[key] = p.lines(key, raw.get(key), MAX_LINES[key])
    out["competencies"] = _competencies(p, raw.get("competencies"))
    out["roles"] = _roles(p, raw.get("roles"))
    out["education"] = _education(p, raw.get("education"))
    out["contact"] = _contact(p, raw.get("contact"))
    return {key: out[key] for key in FIELDS}, p.found


def is_empty(fields: dict) -> bool:
    """Whether a checked report holds nothing: the text was not a resume."""
    contact = fields["contact"]
    return not (any(fields[k] for k in FIELDS if k != "contact") or any(contact.values()))
