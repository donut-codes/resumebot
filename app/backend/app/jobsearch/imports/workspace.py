"""Reading an earlier workspace folder into the owner's documents (docs/PRD.md 2.3): the master resume, the
form answers and the writing rules, from its own files. Deterministic: it reads that folder's layout
(headings, bullets, italic notes, numbered rules) and keeps anything it can't place word for word in a document's notes,
so nothing is lost and nothing is guessed.

    Master/<anything>MasterResume.md   (the newest that isn't a backup)   -> resume (and the contact line -> about)
    Profile/form-answers.md                                               -> about
    Profile/writing-style.md, content-rules.md, resume-variants.md,
    Profile/packet-review.md  (each optional; a missing one is reported)  -> writing

A previous setup's match profile, preferences and watchlist are prose a person or an agent turns into the profile; they
are not read here.
"""

import re
from pathlib import Path

from app.jobsearch.profile.documents import About, Resume, Writing
from app.sites import registry

_FRONT = re.compile(r"\A---\n.*?\n---\n", re.S)
_ITALIC = re.compile(r"^\*(?!\*)(.+?)\*(?!\*)\s*$")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_DATES = re.compile(r"\s*\|\s*(?P<dates>[^|]+)$")
_DASH = re.compile(r"\s+[–—-]\s+")


def _slug(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "item"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def _clean(text: str) -> str:
    return _MD_LINK.sub(r"\1", text).strip()


def _sections(markdown: str, level: int = 2) -> list[tuple[str, list[str]]]:
    """The document split at headings of `level` (`## `): [(title, lines)], with what comes before under ""."""
    mark = "#" * level + " "
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in _FRONT.sub("", markdown or "", count=1).splitlines():
        if line.startswith(mark):
            out.append((line[len(mark):].strip(), []))
        else:
            out[-1][1].append(line)
    return out


def _text(lines: list[str]) -> str:
    """The prose of some lines: separators and blank edges dropped, paragraphs kept."""
    kept = [ln for ln in lines if ln.strip() not in ("---",)]
    return "\n".join(kept).strip()


def _split_dates(dates: str) -> tuple[str, str]:
    parts = _DASH.split(dates.strip(), maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (dates.strip(), "")


# --- the master resume ------------------------------------------------------------------------------------------


def _bullets(lines: list[str], taken: set[str], group: str = "") -> list[dict]:
    """`- text` lines, each with the italic sub-lines under it as its note."""
    out: list[dict] = []
    for line in lines:
        stripped = line.strip()
        if line.startswith("- "):
            text = line[2:].strip()
            notes = re.findall(r"\s*\*\(([^)]*)\)\*", text)  # trailing "*(confirmed ...)*"
            text = re.sub(r"\s*\*\([^)]*\)\*", "", text).strip()
            out.append({"id": _slug(text[:40], taken), "text": _clean(text), "group": group,
                        "note": " ".join(n.strip() for n in notes)})
        elif out and stripped.startswith("- ") and line.startswith((" ", "\t")):
            sub = stripped[2:].strip().strip("*").strip()
            out[-1]["note"] = " ".join(x for x in (out[-1]["note"], sub) if x)
        elif out and stripped and line.startswith((" ", "\t")):
            out[-1]["note"] = " ".join(x for x in (out[-1]["note"], stripped.strip("*").strip()) if x)
    return out


def _role_heading(employer_line: str) -> dict:
    """`Employer, City, ST | dates`, `Employer — City`, or just `Employer`."""
    line, dates = employer_line, ""
    m = _DATES.search(line)
    if m:
        dates, line = m.group("dates"), line[: m.start()]
    if " — " in line:
        employer, location = line.split(" — ", 1)
    else:
        parts = [p.strip() for p in line.split(",")]
        employer = parts[0]
        location = ", ".join(parts[1:])
    start, end = _split_dates(dates) if dates else ("", "")
    return {"employer": employer.strip(), "location": location.strip(), "start": start, "end": end}


def _title_line(line: str) -> tuple[str, str, str, bool] | None:
    """A title: `#### Title (2020 – 2022)`, `#### Title — *optional block*`, or `**Title** | 2021 – Present`.
    Returns (title, start, end, optional) or None."""
    optional = "optional" in line.lower()
    line = re.sub(r"\s+—\s+\*[^*]*\*\s*$", "", line).strip()
    if line.startswith("#### "):
        title = line[5:].strip()
        m = re.search(r"\s*\(([^)]*\d[^)]*)\)\s*$", title)
        start, end = _split_dates(m.group(1)) if m else ("", "")
        return (title[: m.start()].strip() if m else title), start, end, optional
    m = re.match(r"^\*\*([^*]+)\*\*\s*\|\s*(.+)$", line)
    if m:
        start, end = _split_dates(m.group(2))
        return m.group(1).strip(), start, end, optional
    return None


def _roles(lines: list[str], taken: set[str]) -> list[dict]:
    roles: list[dict] = []
    employer: dict | None = None
    group = ""
    for line in lines:
        stripped = line.strip()
        if line.startswith("### "):
            employer = _role_heading(line[4:].strip())
            group = ""
            continue
        title = _title_line(stripped) if employer else None
        if title:
            name, start, end, optional = title
            roles.append({
                **employer, "id": _slug(f"{employer['employer']} {name}"[:40], taken), "title": name,
                "start": start or employer["start"], "end": end or employer["end"], "optional": optional,
                "intro": "", "descriptor": "", "note": "", "bullets": [],
            })
            group = ""
            continue
        if not roles or employer is None:
            continue
        role = roles[-1]
        if not stripped or stripped == "---":
            continue
        if re.match(r"^\*\*[^*]+\*\*$", stripped):  # a bold group heading inside a role
            group = stripped.strip("*").strip()
        elif line.startswith("- "):
            role["bullets"] += _bullets([line], taken, group)
        elif line.startswith((" ", "\t")) and role["bullets"]:
            # An indented line under a bullet (often `  - *note*`) is that bullet's note.
            extra = stripped[2:] if stripped.startswith("- ") else stripped
            last = role["bullets"][-1]
            last["note"] = " ".join(x for x in (last["note"], extra.strip("*").strip()) if x)
        elif _ITALIC.match(stripped):
            note = _ITALIC.match(stripped).group(1).strip()
            role["note"] = " ".join(x for x in (role["note"], note) if x)
        elif not role["bullets"] and not role["intro"]:
            role["intro"] = _clean(stripped)
        else:
            role["note"] = " ".join(x for x in (role["note"], _clean(stripped)) if x)
    return roles


def _competencies(lines: list[str], taken: set[str]) -> list[dict]:
    out = []
    for line in lines:
        m = re.match(r"^\*\*([^*:]+):?\*\*:?\s*(.+)$", line.strip())
        if m:
            items = [i.strip() for i in re.split(r"\s+·\s+", m.group(2)) if i.strip()]
            out.append({"id": _slug(m.group(1), taken), "area": m.group(1).strip(), "items": items})
    return out


def _education(lines: list[str], taken: set[str]) -> list[dict]:
    out: list[dict] = []
    for line in lines:
        s = line.strip()
        m = re.match(r"^\*\*([^*]+)\*\*,?\s*(.*)$", s)
        if m:
            out.append({"id": _slug(m.group(1), taken), "degree": m.group(1).strip(), "school": m.group(2).strip(),
                        "note": ""})
        elif out and _ITALIC.match(s):
            out[-1]["note"] = " ".join(x for x in (out[-1]["note"], _ITALIC.match(s).group(1).strip()) if x)
    return out


def _lines(lines: list[str], taken: set[str]) -> list[dict]:
    return [{"id": b["id"], "text": b["text"], "note": b["note"]} for b in _bullets(lines, taken)]


def _evidence(lines: list[str], taken: set[str]) -> tuple[list[dict], list[str]]:
    """`- **Title:** text` (or `**Title |** ...`) items, each as a project line `Title: text` (evidence now lives in a
    role, and code can't tell which role an entry belongs to, so the owner places them); other prose is returned to be
    kept as a note."""
    out, loose = [], []
    for b in _bullets(lines, taken):
        m = re.match(r"^\*\*(.+?)\*\*:?\s*(.*)$", b["text"])
        if m:
            title, text = m.group(1).strip().rstrip(":"), m.group(2).strip()
            out.append({"id": b["id"], "text": f"{title}: {text}" if title and text else title or text, "note": ""})
        else:
            out.append({"id": b["id"], "text": b["text"], "note": ""})
    loose = [ln.strip() for ln in lines if ln.strip() and not ln.startswith(("- ", " ", "\t")) and ln.strip() != "---"]
    return out, loose


def parse_master(markdown: str) -> tuple[dict, dict]:
    """The master resume as a `resume` document, and the contact facts its header carries (for `about`)."""
    taken: set[str] = set()
    resume: dict = {"notes": []}
    contact: dict = {}
    evidence: list[dict] = []
    for title, lines in _sections(markdown):
        key = title.lower()
        if title == "":
            for line in lines:
                s = line.strip()
                if s.startswith(">"):
                    continue  # the owner decided a master's standing notes are not kept (there are no guardrails)
                if s.startswith("# "):
                    contact["name"] = s[2:].strip()
                elif s.startswith("**") and "|" in s:
                    bits = [_clean(b).strip("*").strip() for b in s.split("|")]
                    contact["location"] = bits[0]
                    for b in bits[1:]:
                        if "@" in b:
                            contact["email"] = b
                        elif re.search(r"\d{3}[-. ]\d{3}[-. ]\d{4}", b):
                            contact["phone"] = b
                        elif "." in b:
                            contact.setdefault("links", []).append(b)
            continue
        if "summary" in key:
            resume["summary"] = _text(lines)
        elif "achievement" in key:
            resume["achievements"] = _lines(lines, taken)
        elif "philosophy" in key:
            resume["philosophy"] = _text(lines)
        elif "competenc" in key or "skills" in key:
            resume["competencies"] = _competencies(lines, taken)
        elif "experience" in key:
            resume["roles"] = _roles(lines, taken)
        elif "education" in key:
            resume["education"] = _education(lines, taken)
        elif "award" in key or "recognition" in key:
            resume["awards"] = _lines(lines, taken)
        elif "project" in key:
            resume["projects"] = _lines(lines, taken)
        elif "evidence" in key:
            evidence, loose = _evidence(lines, taken)
            if loose:
                resume["notes"].append({"id": _slug("evidence-notes", taken), "text": "\n".join(loose),
                                        "source": title})
        elif "guardrail" in key or "tailoring" in key:
            continue  # the owner decided guardrails are dropped; the writing rules hold how to write
        elif _text(lines):
            resume["notes"].append({"id": _slug(title, taken), "text": _text(lines), "source": title})
    resume["projects"] = [*resume.get("projects", []), *evidence]
    return Resume.model_validate(resume).model_dump(mode="json"), contact


# --- application info -------------------------------------------------------------------------------------------

#: Form-answer keys that are facts with their own field, by words their key starts with.
_ABOUT_KEYS = [
    (("phone",), "phone"), (("email",), "email"), (("home address", "address"), "address"),
    (("metro",), "metro_location"),
    (("authorized", "work authorization"), "work_authorization"),
    (("will require visa", "require visa", "visa sponsorship", "sponsorship"), "sponsorship"),
    (("current", "most recent company"), "current_company"), (("legal name", "name"), "name"),
    (("city",), "location"),
]


def _about_keys() -> list[tuple[tuple[str, ...], str]]:
    """The form-answer keys, and, for the owner's links, the key of a board whose roles are followed to the employer's
    own site (a runbook's `follow_to_employer`): the form asks for the owner's profile on that board by its name."""
    names = tuple(sorted({w.lower() for r in registry.current().runbooks if r.kind == "board" and r.follow_to_employer
                          for w in (r.key, r.name)}))
    return [*_ABOUT_KEYS, (names, "links")] if names else _ABOUT_KEYS


def _bare(link: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", link.strip().lower()).rstrip("/")


def parse_form_answers(markdown: str, contact: dict | None = None) -> dict:
    about: dict = {"notes": [], "links": []}
    for key, value in (contact or {}).items():
        about[key] = value
    loose: list[str] = []
    for line in _FRONT.sub("", markdown or "", count=1).splitlines():
        m = re.match(r"^-\s+(?:\*\*)?([^:*]+?)(?:\*\*)?:\s*(.*)$", line.strip())
        if not m:
            if line.strip() and not line.startswith("#"):
                loose.append(line.strip())
            continue
        key, value = m.group(1).strip(), _clean(m.group(2))
        if not value:
            continue
        field = next((f for starts, f in _about_keys() if key.lower().startswith(starts)), None)
        if field == "links":
            if not any(_bare(value) == _bare(link) for link in about["links"]):
                about["links"].append(value)
        elif field and not about.get(field):
            about[field] = value
        # A question and its standing answer are not kept: the owner decided there are no standing answers.
    if loose:
        about["notes"].append({"id": "form-answers", "text": "\n".join(loose), "source": "form-answers.md"})
    return About.model_validate(about).model_dump(mode="json")


# --- the writing rules ------------------------------------------------------------------------------------------


def _numbered(lines: list[str], taken: set[str], prefix: str = "rule") -> list[dict]:
    """`1. text` / `4a. text` items, each with the lines under it, until the next item."""
    out: list[dict] = []
    for line in lines:
        m = re.match(r"^(\d+[a-z]?)\.\s+(.*)$", line.strip()) if not line.startswith((" ", "\t")) else None
        if m:
            out.append({"id": _slug(f"{prefix}-{m.group(1)}", taken), "says": _clean(m.group(2))})
        elif out and line.strip() and not line.strip().startswith("- `"):
            out[-1]["says"] = (out[-1]["says"] + "\n" + _clean(line.strip())).strip()
    return out


def parse_writing(style: str = "", rules: str = "", variants: str = "", review: str = "") -> dict:
    taken: set[str] = set()
    writing: dict = {"notes": []}
    # A previous setup's voice paragraph and content rules both go to Rules for your voice (docs/PRD.md 19.5): code
    # can't sort the content rules into the other sections.
    voice = _text(_FRONT.sub("", style or "", count=1).splitlines())
    writing["voice_rules"] = [{"id": _slug("voice", taken), "says": voice}] if voice else []

    body = _FRONT.sub("", rules or "", count=1)
    # A plain phrase is a Never write entry; a pattern (`re:`) goes unconverted to the legacy patterns, which the owner
    # places later.
    banned, legacy = [], []
    for m in re.finditer(r"^\s*-\s+`([^`]+)`\s*->\s*(.+)$", body, re.M):
        term, why = m.group(1), m.group(2).strip()
        is_re = term.startswith("re:")
        rid = _slug(f"banned {term[3:] if is_re else term}"[:40], taken)
        if is_re:
            legacy.append({"id": rid, "pattern": term[3:], "regex": True, "why": why})
        else:
            banned.append({"id": rid, "phrase": term, "why": why})
    writing["banned"] = banned
    writing["legacy_patterns"] = legacy
    rule_lines = [ln for ln in body.splitlines() if not re.match(r"^\s*-\s+`[^`]+`\s*->", ln)]
    intro = []
    for line in rule_lines:
        if re.match(r"^\d+[a-z]?\.\s", line):
            break
        intro.append(line)
    writing["voice_rules"] += _numbered(rule_lines, taken)
    if _text(intro):
        writing["notes"].append({"id": "content-rules-intro", "text": _text(intro), "source": "content-rules.md"})

    for title, lines in _sections(variants):
        if "handcraft" in title.lower() or "per posting" in title.lower():
            writing["resume_global"] = _numbered(lines, taken, "format")
            prose = [ln for ln in lines if ln.strip() and not re.match(r"^\s*(\d+[a-z]?\.|-|\s)", ln)]
            if prose:
                writing["notes"].append({"id": "resume-format-notes", "text": "\n".join(p.strip() for p in prose),
                                         "source": "resume-variants.md"})

    for title, lines in _sections(_FRONT.sub("", review or "", count=1).replace("\n# ", "\n## ", 1)):
        key = title.lower()
        if "review" in key and "order" in key:
            writing["review_steps"] = _numbered(lines, taken, "step")
        elif "drift" in key:
            rows = [ln for ln in lines if ln.startswith("|") and not set(ln.replace("|", "").strip()) <= {"-", " "}]
            drifts = []
            for row in rows[1:]:  # the first row is the header
                cells = [_clean(c).replace("**", "").strip() for c in row.strip().strip("|").split("|")]
                if len(cells) >= 3:
                    drifts.append({"id": _slug(cells[0], taken), "name": cells[0], "looks_like": cells[1],
                                   "fix": cells[2]})
            writing["drift_patterns"] = drifts
        elif "flag" in key:
            writing["review_flags"] = [{"id": b["id"], "says": b["text"]} for b in _bullets(lines, taken)]
    return Writing.model_validate(writing).model_dump(mode="json")


# --- the workspace ----------------------------------------------------------------------------------------------


def _read(path: Path | None) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path and path.is_file() else ""


def find_master(folder: Path) -> Path | None:
    """The workspace's master resume: the newest `*MasterResume*.md` under Master/ that isn't a backup."""
    master = folder / "Master"
    found = [p for p in master.glob("*MasterResume*.md") if "backup" not in p.name.lower()] if master.is_dir() else []
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def read_workspace(folder: Path) -> tuple[dict, list[str]]:
    """The documents a plugin workspace holds, and a line for each file saying what was read (or that it is missing).
    Only the documents whose files exist are returned."""
    report: list[str] = []
    out: dict = {}
    contact: dict = {}
    master = find_master(folder)
    if master:
        resume, contact = parse_master(_read(master))
        out["resume"] = resume
        bullets = sum(len(r["bullets"]) for r in resume["roles"])
        report.append(f"{master.name}: {len(resume['roles'])} roles, {bullets} bullets, "
                      f"{len(resume['notes'])} notes kept as written, {len(resume['projects'])} projects "
                      "(an evidence library's entries are listed as projects, for you to place in their roles)")
    else:
        report.append("no master resume (Master/*MasterResume*.md)")
    profile = folder / "Profile"
    answers = profile / "form-answers.md"
    if answers.is_file() or contact:
        out["about"] = parse_form_answers(_read(answers), contact)
        report.append("form-answers.md: the facts it holds (standing answers are not kept)" if answers.is_file()
                      else "no form-answers.md: contact facts from the master only")
    names = ("writing-style.md", "content-rules.md", "resume-variants.md", "packet-review.md")
    files = {n: profile / n for n in names}
    if any(p.is_file() for p in files.values()):
        w = parse_writing(*(_read(files[n]) for n in names))
        out["writing"] = w
        report.append(f"writing: {len(w['voice_rules'])} voice rules, {len(w['banned'])} banned phrases, "
                      f"{len(w['legacy_patterns'])} banned patterns, "
                      f"{len(w['resume_global'])} resume rules, {len(w['review_steps'])} review steps, "
                      f"{len(w['drift_patterns'])} drift patterns")
        report += [f"no {n}" for n, p in files.items() if not p.is_file()]
    return out, report
