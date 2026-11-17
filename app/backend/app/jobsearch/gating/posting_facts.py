"""The cheap facts of a posting, read in code: the place, the
way of working, the pay it posts, its travel line and the percent it states, and the employer it names (or hides).

They are the facts the owner's code gates decide on, and they sit in a posting's structured data (the schema.org
JobPosting a page embeds, `adapters._common.page_facts`) and in its header and condition lines ("Location: Austin, TX",
"Work type: Hybrid", "Pay range: $90,000 - $115,000", "up to 25% travel"). Reading them needs no model. Each fact is
kept with the line of the posting it rests on (`Found.quote`), word for word, so the owner can see what a gate stopped
a role on, and so a fact a model reads can be held to the same rule: a quote that is not in the posting is no fact
(`quote_in`).

Only what the posting states is taken. A way of working comes from a label line ("Work type: Hybrid"), a header line
that is only that word, or a sentence that says it of this job ("This is an on-site role"); "remote work options" in a
list of benefits says nothing of this job, so it is not taken. Pay comes from a line that has a pay word and a dollar
figure; when a posting gives several ranges (one for each place), the first is the pay and the most generous of them all
is what a gate may stop a role on (`generous`), so a role is never stopped on a range that is not its own.

What code cannot settle goes to one short Haiku read of just the posting's header and condition lines (`lines_for`),
which answers with the same kind of fact and a quote (`gate_facts.py`). Whether the job leads business development is
asked of a read the same way, on the title and the sentences that speak of selling (`bd_lines`); code answers `no`
itself where the posting has none. This module reads no database.
"""

import functools
import re
from dataclasses import dataclass, field

from app.jobsearch.gating import stated_place
from app.jobsearch.gating.stated_place import PLACE_CHARS, stated
from app.jobsearch.measures import dials, employer_name
from app.jobsearch.measures.pay_place import HYBRID_WORDS, REMOTE_WORDS, names_a_town, parse_comp_text
from app.jobsearch.postings import stems, whole
from app.jobsearch.postings.record import (  # noqa: F401  (said here too)
    CODE,
    HAIKU,
    LISTING,
    SONNET,
    STRUCTURED,
    _norm,
    quote_in,
)

#: The ways of working a fact can be.
MODES = ("remote", "hybrid", "onsite")
#: The longest quote that is kept of a posting's line.
QUOTE_CHARS = 300
#: The first lines of a posting that are its header, and how much of a posting's header and condition lines a Haiku
#: read is given: a few hundred words, never the posting.
HEADER_LINES = 25
LINE_CHARS = 160
LINES_CHARS = 3_500
#: The most of a posting's sentences about selling a Haiku read is given (`bd_lines`), and the longest of them: a few
#: hundred words, where the header read's lines are 3,500 characters.
BD_CHARS = 2_500
BD_LINE_CHARS = 400
#: What the Main work read (`main_work_lines`) is given: the posting's first characters, then at most this many
#: sentences that hold a word of the owner's list, each held to `MW_LINE_CHARS`, the whole to `MW_CHARS`.
MW_OPENING_CHARS = 800
MW_SENTENCES = 5
MW_LINE_CHARS = 400
MW_CHARS = 2_500
#: The smallest and largest year's pay a line is taken for (an hourly figure is a year of 2,080 hours first).
PAY_FLOOR, PAY_CEILING = 20_000, 2_000_000


@dataclass(frozen=True)
class Found:
    """One fact: its value, the line of the posting it rests on, and who read it (`CODE`, `STRUCTURED`, `LISTING`,
    `HAIKU`)."""

    value: str
    quote: str
    by: str = CODE

    def as_dict(self) -> dict:
        return {"value": self.value, "quote": self.quote, "by": self.by}


@dataclass
class Extracted:
    """What a posting's text states of the cheap facts. `pay` is the first range stated, `bands` every range (low, high,
    line), `generous` the most generous band of them."""

    place: Found | None = None
    mode: Found | None = None
    pay: Found | None = None
    bands: list[tuple[int, int, str]] = field(default_factory=list)
    travel: Found | None = None
    #: The most percent of the time the posting says the job travels (a number as text), with the sentence: the fact the
    #: Travel dial reads (docs/PRD.md 46.4). `travel` stays the whole line, as the structured record keeps it.
    travel_percent: Found | None = None
    employer: Found | None = None
    #: Whether the posting names its employer, `yes` or `no`, stored only when its text says something the listing's
    #: name does not (a staffing firm's "our client is seeking", a label that names or hides the employer): the fact
    #: the Employer named dial reads (docs/PRD.md 46.4). Without one, the name speaks for itself (`employer_name`).
    employer_named: Found | None = None
    #: The most years of experience the posting asks (a number as text), the degree it requires and the one it
    #: prefers (a level word), and whether it asks a security clearance (`required`, `preferred` or `none`): the facts
    #: a dial reads (docs/PRD.md 46.4).
    experience: Found | None = None
    degree_required: Found | None = None
    degree_preferred: Found | None = None
    clearance: Found | None = None
    #: Whether the job leads business development, `yes` or `no`, with the sentence it rests on: the fact the Leads
    #: business development dial reads (docs/PRD.md 46.4). Code gives only `no` (a posting with no sentence about
    #: selling, `leads_bd_from_text`, which stays stored so nothing is asked again but is no fact a gate reads: it is a
    #: word list's miss, not a read); a posting that has some is left to a Haiku read, whose `yes` stands on a quote.
    leads_bd: Found | None = None
    #: The owner's word the posting says is the role's main work, or `none`, with the quote it rests on: the fact the
    #: Main work dial reads. Code gives only `none` (`main_work_from_text`: no word of the owner's list is in
    #: the posting); a word is a Haiku read's, on a quote of the posting.
    main_work: Found | None = None

    def facts(self) -> dict:
        return {k: v.as_dict() for k, v in (("location", self.place), ("work_mode", self.mode), ("pay", self.pay),
                                            ("travel", self.travel), ("travel_percent", self.travel_percent),
                                            ("employer", self.employer), ("employer_named", self.employer_named),
                                            ("experience_years", self.experience),
                                            ("degree_required", self.degree_required),
                                            ("degree_preferred", self.degree_preferred),
                                            ("clearance", self.clearance),
                                            ("leads_bd", self.leads_bd),
                                            ("main_work", self.main_work)) if v is not None}


def _clip(line: str) -> str:
    return " ".join(line.split())[:QUOTE_CHARS]


@functools.lru_cache(maxsize=4)
def _lines(text: str | None) -> tuple[str, ...]:
    """The lines of a text that have a character, their white space made one space. Kept for the last few texts, as a
    tuple nobody changes: one posting is read for a dozen facts, each of which asks for its lines and sentences."""
    return tuple(" ".join(line.split()) for line in (text or "").splitlines() if line.strip())


# --- the way of working --------------------------------------------------------------------------------------------

_MODE_WORDS = (("hybrid", HYBRID_WORDS),
               ("remote", REMOTE_WORDS),
               ("onsite", re.compile(r"\b(?:on[\s-]?site|in[\s-]?(?:the[\s-]?)?office|in[\s-]?person)\b", re.I)))
_LABEL_MODE = re.compile(
    r"^\W*(?:(?:work(?:place)?|job|location|position|employment|office)\s+)?(?:type|mode|model|arrangement|policy|"
    r"setting|status|style|flexibility)\s*[:\-–—]\s*(?P<v>.{1,80})$", re.I)
_REMOTE_LABEL = re.compile(r"^\W*(?:remote|remote work|telecommute|work from home)\s*[:?]\s*(?P<v>.{1,40})$", re.I)
_JUST_MODE = re.compile(r"^\W*(?:100\s*%\s*|fully\s+|completely\s+)?(remote|hybrid|on[\s-]?site|in[\s-]?office)\W*$",
                        re.I)
_SAYS_MODE = re.compile(
    r"\bthis\s+(?:is\s+(?:a|an)\s+|will\s+be\s+(?:a|an)\s+)(?:(?:fully|100%|completely|entirely)\s+)?"
    r"(?P<a>remote|hybrid|on[\s-]?site|in[\s-]?office|in[\s-]?person)\s+(?:role|position|job|opportunity)\b"
    r"|\bthis\s+(?:role|position|job)\s+is\s+(?:(?:a|an)\s+)?(?:(?:fully|100%|completely|entirely)\s+)?"
    r"(?P<b>remote|hybrid|on[\s-]?site|in[\s-]?office)\b(?!\s+(?:or|and|/))"
    r"|\b(?:role|position|job)\s+(?:is|will\s+be)\s+(?:based\s+)?(?P<c>on[\s-]?site|in\s+the\s+office)\b", re.I)


def _mode_of(words: str) -> str | None:
    """The one way of working these words name, or None when they name none or more than one ("Remote or hybrid")."""
    named = {mode for mode, pattern in _MODE_WORDS if pattern.search(words)}
    return named.pop() if len(named) == 1 else None


def _says_mode(sentence: str) -> str | None:
    found = _SAYS_MODE.search(sentence)
    return _mode_of(next(g for g in found.groups() if g)) if found else None


_REMOTE_YES = re.compile(r"\W*(?:yes|true|100\s*%|fully)\b", re.I)
_SENT_OR_NL = re.compile(r"(?<=[.!?])\s+|\n")
_SENT_END = re.compile(r"(?<=[.!?])\s+")


def mode_from_text(text: str | None) -> Found | None:
    """The way of working the posting states of this job (remote, hybrid or onsite), or None. A label line ("Work type:
    Hybrid", "Remote: Yes"), a header line that is only the word, and a sentence that says it of this job; two readings
    that disagree are no fact."""
    lines = _lines(text)
    found: list[tuple[str, str]] = []
    for at, line in enumerate(lines):
        label = _LABEL_MODE.match(line)
        if label and (mode := _mode_of(label.group("v"))):
            found.append((mode, line))
            continue
        flag = _REMOTE_LABEL.match(line)
        if flag and _REMOTE_YES.match(flag.group("v")):
            found.append(("remote", line))
            continue
        if at < HEADER_LINES and (just := _JUST_MODE.match(line)):
            found.append((_mode_of(just.group(1)) or "", line))
    for sentence in _SENT_OR_NL.split(text or ""):
        if len(sentence) <= 400 and (mode := _says_mode(sentence)):
            found.append((mode, _clip(sentence)))
    found = [(m, q) for m, q in found if m]
    if not found or len({m for m, _ in found}) != 1:
        return None
    return Found(found[0][0], _clip(found[0][1]))


# --- the pay -------------------------------------------------------------------------------------------------------

_PAY_WORD = re.compile(r"salary|compensation|\bpay\b|wage|\bbase\b|\brange\b|\brate\b|annual|per\s+(?:year|annum|hour)"
                       r"|/\s*(?:yr|year|hr|hour)|\bote\b|\busd\b|\$\s?\d[\d,]*\s?[kK]?\s*(?:-|–|—|to)\s*\$?",
                       re.I)
_NOT_PAY = re.compile(r"revenue|raised|funding|valuation|billion|million|\barr\b|\bipo\b|market cap|assets under|"
                      r"in sales|quota|\bbudget\b", re.I)
_FIGURE = re.compile(r"\$\s?\d")


def _band_of(line: str) -> tuple[int, int] | None:
    low, high = parse_comp_text(line)
    if not low or not high or low < PAY_FLOOR // 4 or high > PAY_CEILING or high < PAY_FLOOR:
        return None
    return low, high


def pay_from_text(text: str | None) -> tuple[Found | None, list[tuple[int, int, str]]]:
    """The pay a posting states: a line with a dollar figure and a pay word (the line before it may carry the word:
    "Pay range:" over "$90,000 - $115,000"), read as a year's pay (`gates.parse_comp_text`). Returns the first range as
    a fact (its value is the range as written, `$90,000-$115,000`) and every range stated, each with its line."""
    lines = _sentences(text)
    bands: list[tuple[int, int, str]] = []
    for at, line in enumerate(lines):
        if not _FIGURE.search(line) or _NOT_PAY.search(line):
            continue
        before = lines[at - 1] if at and len(lines[at - 1]) <= 80 else ""
        if not (_PAY_WORD.search(line) or _PAY_WORD.search(before)):
            continue
        if (band := _band_of(line)) is not None and all((band[0], band[1]) != (b[0], b[1]) for b in bands):
            bands.append((band[0], band[1], _clip(f"{before} {line}" if before and not _PAY_WORD.search(line)
                                                  else line)))
    if not bands:
        return None, []
    low, high, quote = bands[0]
    return Found(f"${low:,}-${high:,}" if low != high else f"${low:,}", quote), bands


def generous(bands: list[tuple[int, int, str]]) -> tuple[int, int] | None:
    """The most generous band the ranges give: the highest bottom and the highest top. A gate stops a role on pay only
    when even this fails, so a range that is for another place is never what stops it."""
    return (max(b[0] for b in bands), max(b[1] for b in bands)) if bands else None


_DIGIT = re.compile(r"\d")
_PAY_TALK = re.compile(r"salary|compensation|pay range|base pay|hourly|per hour|per year|annual", re.I)


def has_pay_words(text: str | None) -> bool:
    """Whether the text has a line that talks of pay with a figure that is not read as one (code found no range): a
    Haiku read may find it."""
    return any(_DIGIT.search(line) and _PAY_TALK.search(line)
               and not _NOT_PAY.search(line) for line in _sentences(text))


# --- travel and the employer ---------------------------------------------------------------------------------------

_TRAVEL = re.compile(r"\btravel(?:l?ing)?\b", re.I)
_TRAVEL_SAYS = re.compile(r"\d+\s*%|percent|up to|occasional|frequent|extensive|minimal|limited|required|willing|"
                          r"ability to|overnight|\bnights?\b|days?\b", re.I)
_EMPLOYER_LABEL = re.compile(
    r"^\W*(?:company|employer|organi[sz]ation|hiring company|client)(?:\s+name)?\s*[:\-–—]\s*(?P<v>.{2,80})$",
    re.I)


def travel_from_text(text: str | None) -> Found | None:
    """The line of the posting that states how much the job travels, or None. The app never judges it: a travel limit is
    a sentence the owner wrote, answered from this line (`gate_facts`)."""
    for line in _sentences(text):
        if _TRAVEL.search(line) and _TRAVEL_SAYS.search(line) and len(line) >= 12:
            return Found(_clip(line), _clip(line))
    return None


# One figure, or the two ends of a range ("10-25%", "10% to 15%"), as a percent.
_PERCENT = re.compile(
    r"(?<![\d.])(?P<lo>\d{1,3}(?:\.\d+)?)\s*(?:%\s*)?(?:-|–|—|to)\s*(?P<hi>\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)"
    r"|(?<![\d.])(?P<n>\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)", re.I)
_TRAVEL_WORD = re.compile(r"\btravel(?:l?(?:ing|ed)|s)?\b", re.I)
#: A percent that is of something else: the cost of travel, paid travel, a discount, a remote role, a match.
_NOT_TIME = re.compile(
    r"^\s*(?:of\s+(?:the\s+|your\s+|all\s+)?(?:travel\s+)?(?:expenses?|costs?|salary|base|pay|premium|fares?)\b|paid\b|"
    r"covered\b|reimburs\w*|remote(?:ly)?\b|from\s+home|at\s+home|virtual(?:ly)?\b|hybrid|on[\s-]?site|"
    r"in[\s-]?(?:the[\s-]?)?office|employer\b|discount|off\b|match\b|funded\b|coverage\b|bonus\b|"
    r"of\s+(?:the\s+)?(?:u\.s\.\s+)?(?:roads|world|globe|population)\b)", re.I)
#: A sentence that says there is no travel ("No travel required", "no requirement to travel"): a figure in it is of
#: something else ("100% remote").
_NO_TRAVEL = re.compile(
    r"\b(?:no|without)\s+(?:(?:\w+\s+){0,2}?)(?:requirement\s+(?:for|to|of)\s+)?travel|"
    r"\bnot\s+(?:required|needed|expected|necessary)\s+to\s+travel|"
    r"\btravel(?:l?ing)?\s+(?:is\s+|are\s+)?not\s+(?:required|needed|expected|necessary)\b", re.I)
#: A short line that is only the word, over a line that is only a figure ("Travel required" over "25%").
_TRAVEL_HEADING = re.compile(
    r"^\W*travel(?:\s+(?:required|requirements?|percentage|expectations?|expected))?\W*$", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
#: How far a figure may stand from the word "travel" in a sentence, and what may not lie between.
NEAR_CHARS = 60


_STOPS = re.compile(r"[;!?]")


def _near(figure: re.Match, word: re.Match, sentence: str) -> bool:
    gap = (sentence[word.end():figure.start()] if word.end() <= figure.start()
           else sentence[figure.end():word.start()] if figure.end() <= word.start() else "")
    return len(gap) <= NEAR_CHARS and not _STOPS.search(gap)


def travel_percent_from_text(text: str | None) -> Found | None:
    """The most percent of the time a posting says the job travels, or None when it states no figure: a sentence with
    the word "travel" and a percent beside it ("up to 25% travel", "Travel: 10-25%", "travel 50% of the time"; a range
    is its top), or a figure alone under a "Travel" heading. A percent that is of something else (paid travel, a
    discount, "100% remote") is not read. Of several figures the largest stands, with the sentence that gave it: a limit
    on travel is held to the most a posting asks. The value is the number as text; no figure is no fact."""
    lines = _lines(text)
    best: tuple[float, str] | None = None
    for at, line in enumerate(lines):
        for sentence in _SENTENCE.split(line):
            words = [] if _NO_TRAVEL.search(sentence) else list(_TRAVEL_WORD.finditer(sentence))
            heading = lines[at - 1] if at and not words and len(sentence) <= 40 and _TRAVEL_HEADING.match(
                lines[at - 1]) else ""
            if not words and not heading:
                continue
            for figure in _PERCENT.finditer(sentence):
                n = float(figure.group("hi") or figure.group("n"))
                if n > 100 or _NOT_TIME.match(sentence[figure.end():]):
                    continue
                if words and not any(_near(figure, w, sentence) for w in words):
                    continue
                if best is None or n > best[0]:
                    best = (n, f"{heading} {sentence}" if heading else sentence)
    return Found(f"{best[0]:g}", _clip(best[1])) if best else None


def employer_from_text(text: str | None) -> Found | None:
    """The employer a header line names ("Company: Acme"), or None."""
    for line in _lines(text)[:40]:
        found = _EMPLOYER_LABEL.match(line)
        if found and (name := found.group("v").strip(" .")):
            return Found(name, _clip(line))
    return None


# --- an employer the posting does not name (docs/PRD.md 46.4, the Employer named dial) ------------------------------

#: "Our client", "my client", "one of our biggest client", and "Staffco has a client": the anchors of a staffing firm's
#: own sentence about the employer it hires for.
_OUR_CLIENT = re.compile(r"\b(?:our|my|one\s+of\s+our)\s+(?:[\w&-]+\s+){0,2}?client\b", re.I)
_HAS_CLIENT = re.compile(r"\bhas\s+(?:an?\s+)?(?:[\w-]+\s+){0,3}?client\b", re.I)
#: That the client is the one hiring.
_CLIENT_HIRES = re.compile(
    r"\b(?:is|are)\s+(?:currently\s+|now\s+|actively\s+|urgently\s+|immediately\s+)?(?:seeking|looking|hiring|"
    r"searching|recruiting|in\s+need|expanding|adding|staffing)\b|"
    r"\bhas\s+(?:an?\s+)?(?:immediate\s+|urgent\s+)?(?:opening|need|opportunity|vacancy)\b", re.I)
#: What may follow "client" when the sentence is about the client and not about a "client service team" or a name
#: ("Our client Wingtip Toys is hiring"), and what may follow "client," when it is a description, not a name.
_AFTER_CLIENT = frozenset({"is", "are", "has", "have", "in", "located", "based", "that", "who", "which"})
_AFTER_COMMA = frozenset({"a", "an", "the", "one", "who", "which", "that", "located", "based", "in", "is", "has", "and",
                          "our", "this"})


def _client_unnamed(sentence: str, anchor: re.Match) -> bool:
    """Whether the sentence, from `anchor` (the word "client" and what leads to it), is about a client that is hiring
    and names none: the client is the subject ("Our client is seeking", "Our client's team is hiring", "Our client, a
    leading provider, is looking"), and no name follows it ("Our client, Wingtip Toys, ..." names it)."""
    rest = sentence[anchor.end():]
    if not _CLIENT_HIRES.search(rest):
        return False
    head = rest.lstrip()
    if rest.startswith(("'s", "’s")) or head.startswith("("):
        return True
    if head.startswith(","):
        word = re.match(r",\s*([A-Za-z][\w&.'-]*)", head)
        return not (word and word.group(1)[0].isupper() and word.group(1).lower() not in _AFTER_COMMA)
    first = re.match(r"[A-Za-z]+", head)
    return bool(first) and first.group(0).lower() in _AFTER_CLIENT


def unnamed_client_from_text(text: str | None) -> Found | None:
    """The sentence in which a client that is hiring is left unnamed ("Our client is seeking a Data Manager", "Staffco
    has a client in Oakfield that is seeking ..."), or None: a staffing firm's posting that names no employer. A
    sentence that names the client, speaks of clients in general ("our clients are seeking value") or of a "client
    service team" says no such thing."""
    for sentence in _sentences(text):
        for pattern in (_OUR_CLIENT, _HAS_CLIENT):
            if any(_client_unnamed(sentence, anchor) for anchor in pattern.finditer(sentence)):
                return Found("no", _clip(sentence))
    return None


def _same_name(a: str, b: str) -> bool:
    """Whether two names are one employer's, loosely: the same letters, or one begins with the other."""
    x, y = (re.sub(r"[^a-z0-9]", "", n.lower()) for n in (a, b))
    return bool(x and y) and (x.startswith(y) or y.startswith(x))


def employer_named_from(company: str | None, text: str | None, employer: Found | None) -> Found | None:
    """What the posting's own words say of whether it names its employer, when that is more than the listing's name says
    (`employer_name.hidden` reads the name on its own): `no` when its label line or its page's data holds a placeholder
    ("Company: Confidential"), or when it speaks of an unnamed client that is hiring; `yes` when the name the listing
    gave is hidden and the text names the employer. A label that names another employer than the listing's is the
    client's, so it is never read as hidden. None when the text adds nothing."""
    if employer is not None:
        if employer_name.hidden(employer.value):
            return Found("no", employer.quote, employer.by)
        if employer_name.hidden(company):
            return Found("yes", employer.quote, employer.by)
        if company and not _same_name(employer.value, company):
            return None  # the label names the client
    found = unnamed_client_from_text(text)
    return Found("no", found.quote, found.by) if found else None


@functools.lru_cache(maxsize=4)
def _sentences(text: str | None) -> tuple[str, ...]:
    """The lines of a text, a long line cut into its sentences (so a keyword is quoted with its sentence, not a
    paragraph)."""
    out: list[str] = []
    for line in _lines(text):
        if len(line) > 300:
            out.extend(s.strip() for s in _SENT_END.split(line) if s.strip())
        else:
            out.append(line)
    return tuple(out)


# --- the years asked, the degree and a clearance (docs/PRD.md 46.4) ------------------------------------------------

_YEARS_EXP = re.compile(
    r"(?<![\w$.,/-])(?P<n>\d{1,2})\s*\+?\s*(?:(?:-|to)\s*\d{1,2}\s*\+?\s*|or\s+more\s+)?years?\b", re.I)
_YEARS_MIN = re.compile(r"\b(?:minimum|min\.?|at\s+least)\s+(?:of\s+)?(?P<n>\d{1,2})\s*\+?\s*years?\b", re.I)
_EXPERIENCE = re.compile(r"\bexperience\b|\bexperienced\b|\bin\s+(?:a|an)\s+(?:similar|related|relevant)\s+role\b",
                         re.I)
_NOT_YEARS = re.compile(r"\bin\s+business\b|\bsince\b|\bfounded\b|\bin\s+a\s+row\b|\byears?\s+(?:old|ago)\b", re.I)
#: The most years a line is taken for.
MOST_YEARS = 40


def experience_from_text(text: str | None) -> Found | None:
    """The most years of experience a posting asks, from its sentences: "N+ years ... experience", "N-M years" (the
    lower figure is what is asked), "minimum N years", "at least N years". A sentence about the employer's own age ("20
    years in business") asks nothing. The value is the largest figure found and the quote the sentence that gave it."""
    best: tuple[int, str] | None = None
    for sentence in _sentences(text):
        low = stems.folded(sentence)
        if low is not None and "year" not in low:  # `_YEARS_MIN` and `_YEARS_EXP` both need the word
            continue
        if _NOT_YEARS.search(sentence):
            continue
        figures = [int(m.group("n")) for m in _YEARS_MIN.finditer(sentence)]
        figures += [int(m.group("n")) for m in _YEARS_EXP.finditer(sentence) if _EXPERIENCE.search(sentence)]
        figures = [n for n in figures if 0 < n <= MOST_YEARS]
        if figures and (best is None or max(figures) > best[0]):
            best = (max(figures), sentence)
    return Found(str(best[0]), _clip(best[1])) if best else None


#: The degree levels, lowest first, as a dial's pick list has them. A bare "degree" names no level.
DEGREE_LEVELS = ("degree", "associate", "bachelor", "master", "doctorate")
_LEVEL_WORDS = (
    ("doctorate", re.compile(r"\bph\.?\s?d\b|\bdoctora(?:te|l)\b|\bjuris doctor\b", re.I)),
    ("master", re.compile(r"\bmaster(?:'s|’s)|\bmasters\s+degree\b|\bmaster\s+(?:of|degree)\b", re.I)),
    ("master", re.compile(r"\bMBA\b")),
    ("bachelor", re.compile(r"\bbachelor(?:'s|’s|s)?\b", re.I)),
    ("associate", re.compile(r"\bassociate(?:'s|’s)\b|\bassociate\s+degree\b", re.I)),
)
#: "BS", "BA" and "MS" are a degree only in a sentence that says "degree" or "equivalent" (they are also states).
_BS_CODE = re.compile(r"\bB\.?S\.?(?:c\.?)?(?=[\s,/)]|$)|\bB\.?A\.?(?=[\s,/)]|$)")
_MS_CODE = re.compile(r"\bM\.?S\.?(?:c\.?)?(?=[\s,/)]|$)")
_DEGREE_WORD = re.compile(r"\bdegrees?\b|\bequivalent\b", re.I)
_STRONG = re.compile(r"\brequire[sd]?\b|\brequirements?\b|\bminimum\b|\bmust\b|\bnecessary\b|\bmandatory\b|\bneeded\b|"
                     r"\bequivalent\b|\bat\s+least\b|\bexpected\b", re.I)
_SOFT = re.compile(r"\bprefer(?:red|ably)?\b|\bdesired?\b|\ba\s+plus\b|\bbonus\b|\bnice\s+to\s+have\b|\bideally\b|"
                   r"\badvantage\b|\bhelpful\b", re.I)


@dataclass
class Degrees:
    """The degree a posting requires and the one it prefers (each a `Found` whose value is a level word), or None."""

    required: Found | None = None
    preferred: Found | None = None


_DEGREE_PHRASE = re.compile(
    r"\b(?:college|university|four-year|4-year)\s+degree\b|\ba\s+degree\b|\bdegree\s+in\b", re.I)
_CLAUSE = re.compile(r";|,\s+(?:and|but)\s+|\s+but\s+")


def _levels_in(clause: str, degree_said: bool) -> list[str]:
    found = {level for level, pattern in _LEVEL_WORDS if pattern.search(clause)}
    if degree_said and _BS_CODE.search(clause):
        found.add("bachelor")
    if degree_said and _MS_CODE.search(clause):
        found.add("master")
    if not found and _DEGREE_PHRASE.search(clause):
        found.add("degree")
    return sorted(found, key=DEGREE_LEVELS.index)


#: The words every match of `_LEVEL_WORDS` and `_DEGREE_PHRASE` holds, one for each of their alternatives
#: (`postings/stems.py`): a new alternative needs its word here. The codes (`_BS_CODE`, `_MS_CODE` and the "MBA" of
#: `_LEVEL_WORDS`) are matched in capitals, so they are asked of the sentence as it is written.
_DEGREE_STEMS = ("ph", "doctora", "juris", "master", "bachelor", "associate", "degree")
_DEGREE_CODES = ("BS", "B.S", "BA", "B.A", "MS", "M.S", "MBA")


def degree_from_text(text: str | None) -> Degrees:
    """The degree a posting requires and the one it prefers, by level, from its sentences. A clause with a level word
    (bachelor, associate, master, MBA, PhD; "BS" and "MS" only in a sentence that says "degree" or "equivalent") and a
    word of requirement (required, minimum, must, "or equivalent") is a required degree; with a word of preference
    (preferred, a plus, desired) a preferred one; with neither it says nothing. A sentence is read a clause at a time,
    so "Master's degree required; PhD preferred" gives both. Of the levels one clause names ("bachelor's or master's")
    the lowest is what is asked; of the clauses the highest stands."""
    best: dict[str, tuple[int, str]] = {}
    for sentence in _sentences(text):
        low = stems.folded(sentence)
        if low is not None and not any(word in low for word in _DEGREE_STEMS) and not any(
                code in sentence for code in _DEGREE_CODES):
            continue
        degree_said = bool(_DEGREE_WORD.search(sentence))
        for clause in _CLAUSE.split(sentence):
            levels = _levels_in(clause, degree_said)
            if not levels:
                continue
            strength = "preferred" if _SOFT.search(clause) else "required" if _STRONG.search(clause) else None
            if strength is None:
                continue
            rank = DEGREE_LEVELS.index(levels[0])
            if strength not in best or rank > best[strength][0]:
                best[strength] = (rank, _clip(sentence))
    found = {s: Found(DEGREE_LEVELS[best[s][0]], best[s][1]) for s in best}
    return Degrees(required=found.get("required"), preferred=found.get("preferred"))


_CLEARANCE = re.compile(r"\bts\s*/\s*sci\b|\btop\s+secret\b|\b(?:secret|security|active|interim)\s+clearance|"
                        r"\bpublic\s+trust\b|\bpolygraph\b|\bclearance\s+(?:is\s+)?(?:required|needed)\b", re.I)
_NO_CLEARANCE = re.compile(r"\b(?:no|not|without|doesn't|does\s+not|don't|do\s+not)\b[^.]{0,40}\bclearance\b|"
                           r"\bclearance\b[^.]{0,30}\b(?:not\s+(?:required|needed|necessary)|isn't\s+required)\b", re.I)


#: The words every match of `_CLEARANCE` and `_NO_CLEARANCE` holds, one for each of their alternatives
#: (`postings/stems.py`): a new alternative needs its word here.
_CLEARANCE_STEMS = ("clearance", "secret", "sci", "trust", "polygraph")


def clearance_from_text(text: str | None) -> Found | None:
    """Whether a posting asks a security clearance: `required` (TS/SCI, a secret or security clearance, public trust, a
    polygraph), `preferred` (with a word of preference) or `none` ("no clearance required"), with its sentence. None
    when it says nothing of one. Of several sentences, required outranks preferred and preferred outranks none."""
    found: dict[str, str] = {}
    for sentence in _sentences(text):
        low = stems.folded(sentence)
        if low is not None and not any(word in low for word in _CLEARANCE_STEMS):
            continue
        if _NO_CLEARANCE.search(sentence):
            found.setdefault("none", sentence)
        elif _CLEARANCE.search(sentence):
            found.setdefault("preferred" if _SOFT.search(sentence) else "required", sentence)
    value = next((v for v in ("required", "preferred", "none") if v in found), None)
    return Found(value, _clip(found[value])) if value else None


# --- whether the job leads business development (docs/PRD.md 46.4) -------------------------------------------------

#: The words that make a sentence of a posting a candidate for "leads business development": selling, business
#: development, new business, a sales pipeline, a quota, bookings, a revenue target, new clients or accounts, closing
#: deals. A "pipeline" is one only beside a word of selling ("a data pipeline" is not). A candidate is only that: the
#: sentence is read by a model before it says anything of the job.
_BD = re.compile(
    r"\bsales\b|\bselling\b|business[\s-]+development|\bbiz[\s-]?dev\b|\bnew[\s-]+business\b|\bquotas?\b|\bbookings?\b|"
    r"\b(?:sales|business|revenue|deal|client|customer|prospect|opportunity|account)\s+pipelines?\b|"
    r"\brevenue\s+(?:targets?|goals?)\b|\bnew[\s-]+(?:clients?|customers?|accounts?|logos?)\b|"
    r"\bclos(?:e|es|ed|ing)\s+(?:\w+\s+){0,2}?(?:deals?|sales?|business|contracts?)\b", re.I)


#: The words every match of `_BD` holds, one for each of its alternatives (`postings/stems.py`): a new alternative
#: needs its word here.
_BD_STEMS = ("sales", "selling", "business", "biz", "quota", "booking", "pipeline", "revenue", "new", "clos")


def _maybe_bd(line: str) -> bool:
    low = stems.folded(line)
    return low is None or any(word in low for word in _BD_STEMS)


def bd_candidates(text: str | None, title: str | None = None) -> list[str]:
    """The sentences of a posting that speak of selling, in the posting's order and word for word (each held to
    `BD_LINE_CHARS`), and the title first when it does. Nothing when neither does: the posting has nothing to read."""
    found = [line[:BD_LINE_CHARS] for line in _sentences(text) if _maybe_bd(line) and _BD.search(line)]
    named = " ".join((title or "").split())
    if named and _BD.search(named):
        found.insert(0, named[:BD_LINE_CHARS])
    return list(dict.fromkeys(found))


def bd_lines(text: str | None, title: str | None = None, limit: int = BD_CHARS) -> str:
    """The lines a Haiku read of "does the job lead business development" is given: the title and every sentence of the
    posting that speaks of selling (`bd_candidates`), held to `limit` characters; "" when nothing does (code answers
    `no`, `leads_bd_from_text`). Never the posting."""
    candidates = bd_candidates(text, title)
    if not candidates:
        return ""
    head = " ".join((title or "").split())[:BD_LINE_CHARS]
    keep = [head, *(c for c in candidates if c != head)] if head else candidates
    out: list[str] = []
    used = 0
    for line in keep:
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def leads_bd_from_text(text: str | None, title: str | None = None) -> Found | None:
    """`no`, with no line to rest on, for a posting that has text and no sentence (and no title) that speaks of selling:
    there is nothing for a reader to find in it, and an error here runs toward a pass. None for a posting that has some
    (a reader decides) and for no text at all."""
    if not (text or "").strip() or bd_candidates(text, title):
        return None
    return Found("no", "", CODE)


# --- the role's main work (the owner's Main work dial) -------------------------------------------------------------


def _listed(words) -> str:
    """The owner's Main work words, a list of them or the line's comma-separated value, as one value `dials.title_hits`
    takes."""
    return ",".join(words) if isinstance(words, (list, tuple)) else ",".join(dials.values_of(words))


def main_work_candidates(text: str | None, title: str | None, words) -> list[str]:
    """The sentences of a posting that hold a word of the owner's list (whole words, `dials.title_hits`), in the
    posting's order and word for word, each held to `MW_LINE_CHARS`; none when the list is empty."""
    listed = _listed(words)
    if not listed:
        return []
    return list(dict.fromkeys(line[:MW_LINE_CHARS] for line in _sentences(text) if dials.title_hits(line, listed)))


def main_work_lines(text: str | None, title: str | None, words) -> str:
    """The lines a Haiku read of "what is this role's main work" is given: the title, the posting's first
    `MW_OPENING_CHARS` characters, then up to `MW_SENTENCES` sentences that hold a word of the owner's list, held to
    `MW_CHARS` in all. Never the posting."""
    head = " ".join((title or "").split())[:MW_LINE_CHARS]
    opening = " ".join((text or "").split())[:MW_OPENING_CHARS]
    parts = [part for part in (head, opening) if part]
    parts += [c for c in main_work_candidates(text, title, words) if c not in parts][:MW_SENTENCES]
    out: list[str] = []
    used = 0
    for part in parts:
        if used + len(part) + 1 > MW_CHARS:
            break
        out.append(part)
        used += len(part) + 1
    return "\n".join(out)


def main_work_from_text(text: str | None, title: str | None, words) -> Found | None:
    """`none`, with no quote, for a posting that has a whole text and holds no word of the owner's list anywhere (the
    title included): there is nothing for a reader to find. Never a word: only a reader's quote makes one. None for no
    list, a thin text (a snippet says nothing of the job) and a posting that holds a word."""
    listed = _listed(words)
    if not listed or not (text or "").strip() or whole.thin(text):
        return None
    if dials.title_hits(f"{title or ''}\n{text}", listed):
        return None
    return Found("none", "", CODE)


# --- everything ----------------------------------------------------------------------------------------------------


def _place_quote(text: str | None) -> str:
    """The posting's own words for the place `stated_place.stated` read: the first label line with its value
    ("Location: Austin, TX"), or a "Locations" heading with the items under it ("Locations - Austin, TX"), as the text
    has them."""
    lines = _lines(text)
    for at, line in enumerate(lines):
        label = stated_place._LABEL.match(line)
        if not label:
            continue
        if label.group("place"):
            return line
        items = []
        for item in lines[at + 1:]:
            if not item.startswith("- "):
                break
            items.append(item)
        if items:
            return " ".join([line, *items])
    return ""


def structured(text: str | None) -> dict:
    """The facts of a schema.org JobPosting the text embeds (`adapters._common.page_facts`), as {location, work_mode,
    pay, employer}: only what the page states. Nothing when the text embeds none."""
    from app.jobsearch.adapters._common import page_facts

    if not text or "ld+json" not in text.lower():
        return {}
    return page_facts(text)


def extract(text: str | None, company: str | None = None, title: str | None = None) -> Extracted:
    """What a posting's text states of the cheap facts, each with the line it rests on. The structured data a page
    embeds comes first, then the posting's own lines. `company` is the name the listing gives the employer: with it,
    the text is also read for whether the posting names its employer (`employer_named_from`). `title` is the role's:
    a title that speaks of selling leaves the posting to a reader (`leads_bd_from_text`)."""
    out = Extracted()
    page = structured(text)
    if page.get("location") and names_a_town(page["location"]):
        out.place = Found(page["location"][:PLACE_CHARS], "schema.org JobPosting: jobLocation", STRUCTURED)
    if page.get("work_mode") in MODES:
        out.mode = Found(page["work_mode"], "schema.org JobPosting: jobLocationType", STRUCTURED)
    if page.get("comp"):
        low, high = parse_comp_text(page["comp"])
        if low and high and PAY_FLOOR <= high <= PAY_CEILING:
            out.pay = Found(page["comp"], "schema.org JobPosting: baseSalary", STRUCTURED)
            out.bands = [(low, high, "schema.org JobPosting: baseSalary")]
    if page.get("company"):
        out.employer = Found(page["company"], "schema.org JobPosting: hiringOrganization", STRUCTURED)
    if out.place is None and (place := stated(text)):
        out.place = Found(place, _clip(_place_quote(text) or place))
    if out.mode is None:
        out.mode = mode_from_text(text)
    if out.pay is None:
        out.pay, out.bands = pay_from_text(text)
    out.travel = travel_from_text(text)
    out.travel_percent = travel_percent_from_text(text)
    if out.employer is None:
        out.employer = employer_from_text(text)
    if company is not None:
        out.employer_named = employer_named_from(company, text, out.employer)
    out.experience = experience_from_text(text)
    degrees = degree_from_text(text)
    out.degree_required, out.degree_preferred = degrees.required, degrees.preferred
    out.clearance = clearance_from_text(text)
    out.leads_bd = leads_bd_from_text(text, title)
    return out


# --- what a Haiku read is given ------------------------------------------------------------------------------------

#: The words that make a line of a posting a condition of the job: where, how, pay, travel, clearance, hours.
_CONDITION = re.compile(
    r"\bremote\b|hybrid|on[\s-]?site|in[\s-]?(?:the[\s-]?)?(?:office|person)|work(?:ing)?\s+(?:from|at)\s+home|"
    r"telecommut|relocat|\btravel|clearance|sponsor|\bvisa\b|work authori[sz]ation|\bschedule\b|\bshifts?\b|weekend|"
    r"salary|compensation|pay range|base pay|hourly|per year|per hour|\blocations?\b|\bcity\b|\boffice\b", re.I)


def lines_for(text: str | None, limit: int = LINES_CHARS) -> str:
    """The lines a Haiku read is given of a posting: its header (the first short lines) and every other line or sentence
    that states a condition of the job, in the posting's order and word for word, held to `limit` characters. Never the
    posting."""
    lines = _lines(text)
    keep: list[str] = [line for line in lines[:HEADER_LINES] if len(line) <= LINE_CHARS]
    for sentence in _sentences(text):
        if sentence not in keep and len(sentence) <= 400 and _CONDITION.search(sentence):
            keep.append(sentence)
    out: list[str] = []
    used = 0
    for line in keep:
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)
