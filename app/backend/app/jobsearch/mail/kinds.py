"""What a message is, settled in code first (docs/PRD.md 14.4 and 14.6): the kinds of job mail, the rule that settles
the one mail it can (a bare notice that an application was received: every sentence of it a received sentence or a
known harmless one), and a calendar invite read for its date, time, place, link and people. Every other mail goes on to
the small model (`mail/haiku.py`), which quotes a sentence of the mail as the one it decided from.

Pure code: no database, no mailbox, no model. A message's text is data, never instructions: nothing here follows what a
message says, it only looks for the words a job system or an employer uses.

The kinds: `confirmation` (an application was received), `rejection` (they said no), `interview` (an invitation or a
scheduling mail), `offer`, `outreach` (a recruiter about a new role), `other` (mail about no application) and
`ambiguous` (the model could not tell; the owner decides). The rules give only `confirmation`."""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.mailmine.message import MAX_SCRUB_CHARS, MAX_SUBJECT_CHARS, Seen, scrub_quote
from app.sites import registry
from app.textutil import squash

CONFIRMATION, REJECTION, INTERVIEW, OFFER, OUTREACH, OTHER, AMBIGUOUS = (
    "confirmation", "rejection", "interview", "offer", "outreach", "other", "ambiguous")
#: What the small model may answer (it never answers `ambiguous` for the owner: it says it could not tell).
MODEL_KINDS = (CONFIRMATION, REJECTION, INTERVIEW, OFFER, OUTREACH, OTHER, AMBIGUOUS)
LINE_CHARS = 200
#: How much of a message is kept, and so all that the rules and the small model ever see of it: the first
#: `OPENING_CHARS` characters of its text and the first `SUBJECT_CHARS` of its subject (`sync._row`). A text or a
#: subject that reaches its cut may say its no past it, so `classify` never confirms one (docs/PRD.md 14.4).
OPENING_CHARS = 1_500
SUBJECT_CHARS = MAX_SUBJECT_CHARS

#: The sender domains of the job boards, and of every job board and job system that sends mail from its own domain (the
#: sender's registrable domain; `mail.reader.SYSTEM_SENDERS` is those that send an application's confirmation from it).
#: The sets are written out in the runbooks (`mail.kind`, `mail.senders`, `mail.confirms`), never taken from their
#: hosts. A board sends mail about the owner's own application and also alerts and digests that name many employers'
#: roles, so its mail names a role only when its subject speaks of an application (`may_name_a_role`).
JOB_BOARDS, JOB_SYSTEMS, _ = registry.mail_sets()
#: A subject that speaks of an application ("Your application to ...", "Thank you for applying"). An alert or a digest
#: says "apply" or "jobs", not that.
_OWN_APPLICATION = re.compile(r"\bappl(?:ied|ying|ication)\b", re.I)
#: Words in a subject that make a message worth reading the start of.
_JOB_SUBJECT = re.compile(
    r"\bappl(?:y|ied|ying|ication)|interview|candidate|candidacy|\boffer\b|opportunit|recruit|\bposition\b|\brole\b|"
    r"hiring|\bresume\b|next steps|your (?:job|career)|thank you for your interest|status of your", re.I)

#: What a bare received notice says: an application arrived ("Thank you for applying", "We have received your
#: application", "Your resume was received"). It names the application, the CV or the resume: "We have received your
#: order" is no job mail, and "you applied to ..." is no received notice (a digest says it too).
_RECEIVED = re.compile(
    r"\bthanks?(?: you)? for (?:your )?(?:applying|application)\b|"
    r"\b(?:application|cv|resume) (?:was |has been |is )?(?:received|submitted|sent|complete)\b|"
    r"\breceived your (?:application|cv|resume)\b", re.I)

#: Wording that makes a received notice more than bare, found anywhere in the subject or the text. Each family stands
#: for the way a no, a yes or a next step opens: with any of it the rules cannot tell what the mail says, so it is left
#: to the reader (PRD 14.4). A word may well be harmless in one mail: the cost is a model read, and a wrong
#: confirmation is worse. The patterns are plain words, each tried once at a word's start, so they take time in
#: proportion to the text (mail from strangers).
_NOT_BARE = (
    # negation
    re.compile(
        r"\b(?:no|not|nor|never|none|nothing|nobody|neither|cannot|without|unable|unsuccessful|longer)\b|n['\u2019]t\b",
        re.I),
    # outcome
    re.compile(
        r"\b(?:select\w*|success\w*|reject\w*|decid\w*|decision|chos(?:e|en)|choose|hire[ds]?|hiring|pursu\w+|"
        r"advanc\w*|shortlist\w*|consider\w*|interest\w*|outcome|result\w*|feedback|mov(?:e|es|ed|ing)|proceed\w*|"
        r"continu\w*|progress\w*|go(?:ing)? (?:ahead|forward|further|with|in)|other|another|else|different|better|"
        r"stronger|competitive|future|luck|wish\w*|next|stage|round|phase|steps?)\b", re.I),
    # closure
    re.compile(
        r"\b(?:clos(?:e|es|ed|ing|ure)|withdr(?:aw|awn|ew|awal)|cancel\w*|expir\w+|ends?|ended|ending|fill(?:ed)?|"
        r"discontinu\w+|suspend\w+|paused|hold|rescind\w*|remov\w+|archiv\w+)\b", re.I),
    # interview
    re.compile(
        r"\b(?:interview\w*|schedul\w+|screen\w*|meet\w*|calls?|chat\w*|speak\w*|talk\w*|invit\w+|conversation|"
        r"calendar|phone|video|zoom|teams|assessment|test)\b", re.I),
    # offer
    re.compile(
        r"\b(?:offer\w*|salary|compensation|onboard\w*|welcome|congratulat\w*|pleased|delighted|thrilled|excited)\b",
        re.I),
    # availability
    re.compile(r"\b(?:availab\w+|slots?|convenient|calendly)\b", re.I),
    # weak
    re.compile(
        r"\b(?:unfortunate\w*|sadly|regret\w*|sorry|apolog\w+|declin\w+|afraid|however|alas|disappoint\w*)\b|"
        r"\bat this (?:time|point)\b|\bon this occasion\b", re.I),
    # update: "update on your application", "application status", the words a status mail opens with
    re.compile(
        r"\bupdate (?:on|about|regarding|concerning|to|for|with) (?:your|the|this) (?:application|candidacy)\b|"
        r"\b(?:application|candidacy) (?:update|status)\b|\bstatus of your (?:application|candidacy)\b", re.I),
)

# --- the allow-list (docs/PRD.md 14.4) --------------------------------------------------------------------------------
# A bare notice is only as safe as its word lists are complete, and a no, an offer or an interview request has more
# wordings than any list holds ("The search for this role has concluded", "You got the job!", "Are you free Thursday?").
# So the lists above only refuse; what confirms is the allow-list below: every sentence of the subject and of the text
# must be a received sentence or a known harmless one, whole, and each is a fixed sentence with no free word in it. A
# free slot for a name was tried and refused: any capitalized words fit it, "Position Eliminated" and "Search Concluded"
# among them, and wrongly confirm mails. So a mail that names a
# company, a job or anything else, in the subject or the text, is left to the reader, and so is a subject that is only
# the employer's name beside a received phrase. Patterns are whole-sentence matches over fixed words, so they take
# time in proportion to the text (mail from strangers).

_NOUN = r"(?:application|cv|resume)"

#: The received sentences (clauses), fixed, none naming an employer or a job. Each holds received wording `_RECEIVED`
#: finds, except "thank you for submitting your application", which the whole clause is the line for.
_RECEIVED_CLAUSE = re.compile(
    rf"(?:we(?: have|['\u2019]ve)?|(?:the|our) team(?: has)?) received your {_NOUN}|"
    rf"(?:your )?{_NOUN} (?:(?:was|has been|have been|is) )?(?:received|submitted|sent|complete)|"
    rf"thanks?(?: you)? for (?:your )?(?:applying|application|submitting your {_NOUN})", re.I)

#: What a person fit would be ("there is a match", "your skills are a good fit", "your qualifications match").
_FIT = (r"(?:there is (?:a|any)(?: good| strong)? (?:match|fit)|your (?:skills|qualifications|background|experience) "
        r"(?:(?:are|is) (?:a|an)(?: good| strong)? (?:match|fit)|match(?: the (?:role|position|job))?))")
_CONTACT = (r"(?:(?:we|our team|the team|a recruiter|someone|a member of our team) )?(?:will|shall) "
            r"(?:be in touch|get in touch|contact you|reach out|get back to you)(?: (?:soon|shortly))?")
#: A fit as a condition, alone or after a contact promise ("a recruiter will contact you if there is a match").
_COND = rf"(?:(?:if|should) {_FIT})"
#: The team a sign-off is from, with no employer's name ("The Recruiting Team").
_TEAM = (r"(?:the )?(?:(?:recruiting|recruitment|talent acquisition|talent|careers|people|hr|human resources) "
         r"(?:team|department)|recruiting|recruitment|talent acquisition|human resources|hr|team)")

#: The harmless sentences (clauses) a received notice carries, fixed: a greeting with no name, an automated reply, what
#: happens to the application ("our team will review it") and a way to check it, a closing, a copyright line with no
#: name. None says what the employer decided, asks the owner for anything or names anyone.
_HARMLESS = re.compile("|".join((
    r"(?:hi|hello|hey|dear|greetings)(?: (?:there|applicant|candidate))?",
    r"(?:this is )?an automated (?:message|reply|email|response|notification)",
    r"we got it",
    r"(?:(?:we|our team|the team|a member of our team) )?(?:will|shall) (?:carefully )?(?:review|read) "
    r"(?:it|your application|your resume|your cv|all applications|every application|each application)(?: carefully)?",
    r"(?:we|our team|the team) reviews? (?:every|each|all) (?:application|submission)s?",
    r"you (?:can|may) (?:check|track|view|see) (?:its|the|your application['\u2019]?s?) status (?:in|on|at|through|"
    r"using) (?:your|the|our)(?: (?:candidate|applicant|career|careers|job))? (?:account|portal|dashboard|profile)",
    r"thanks?(?: again)?|(?:best|kind|warm) regards|regards|sincerely|cheers",
    _TEAM,
    r"(?:\u00a9|\(c\)|copyright)(?: \d{4})?",
    r"all rights reserved",
    _COND,
    rf"{_CONTACT}(?: {_COND})?")), re.I)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
#: A clause ends at a comma, a semicolon, a colon, a pipe, a spaced dash or "and": each side must be a fixed sentence.
_CLAUSE_BREAK = re.compile(r"\s*[,;:|]\s*|\s+[-\u2013\u2014]\s+|\s*\band\b\s*", re.I)
#: A sentence longer than this is no received sentence (the notices say it in a few words).
_SENTENCE_CHARS = 300


def _fold(text: str) -> str:
    """Text as the rules read it: whitespace folded, accents removed (compatibility decomposition, combining marks
    dropped), and invisible format characters (zero-width spaces and joiners, soft hyphens) dropped, so a word split by
    one or dressed in a full-width letter is the word the lists look for."""
    decomposed = unicodedata.normalize("NFKD", squash(text))
    return squash("".join(c for c in decomposed if not unicodedata.combining(c) and unicodedata.category(c) != "Cf"))


def _outside_basic_latin(text: str) -> bool:
    """Whether a folded text holds a letter that is not basic Latin (a Cyrillic "e" in "Rejected" reads as the English
    word and is none of the lists' words)."""
    return any(c.isalpha() and not c.isascii() for c in text)


def _bare_sentence(sentence: str) -> tuple[bool, str | None]:
    """Whether a sentence is a received sentence or a harmless one, clause by clause, and its first received clause."""
    received = None
    for clause in (c.strip() for c in _CLAUSE_BREAK.split(sentence)):
        if not clause:
            continue
        if _RECEIVED_CLAUSE.fullmatch(clause):
            received = received or clause
        elif not _HARMLESS.fullmatch(clause):
            return False, None
    return True, received


def _bare(text: str) -> tuple[bool, str | None]:
    """Whether every sentence of `text` is a received or a harmless one (nothing else is in it), and the first
    received clause of it."""
    received = None
    for sentence in _SENTENCE_END.split(text):
        sentence = sentence.strip(" .!?")
        if not sentence:
            continue
        ok, found = _bare_sentence(sentence) if len(sentence) <= _SENTENCE_CHARS else (False, None)
        if not ok:
            return False, None
        received = received or found
    return True, received


@dataclass(frozen=True)
class Verdict:
    """A message's kind and the one line that decided it."""

    kind: str
    line: str
    by: str = "rules"


def _sent_from(seen: Seen, domains: tuple[str, ...]) -> bool:
    return any(seen.sender_key == d or seen.sender_key.endswith("." + d) for d in domains)


def from_a_system(seen: Seen) -> bool:
    """Whether the sender is a job system or a job board (its domain is one of `JOB_SYSTEMS`)."""
    return _sent_from(seen, JOB_SYSTEMS)


def may_name_a_role(seen: Seen) -> bool:
    """Whether the sender's say-so names the employer (docs/PRD.md 14.5): a job system's mail does. A job board's does
    only when its subject speaks of an application, since the same board sends alerts and digests that name employers
    the owner has roles at. Anyone else's does not (`match.py` asks the owner instead)."""
    return from_a_system(seen) and (
        not _sent_from(seen, JOB_BOARDS) or _OWN_APPLICATION.search(seen.subject) is not None)


def worth_reading(seen: Seen, employer_keys: list[str] = ()) -> bool:  # pyright: ignore[reportArgumentType]
    """From the header alone: whether the start of the message is worth fetching. A job system's mail, a subject with a
    job word, or a sender whose address names an employer of a role the owner has. Never anything else."""
    if from_a_system(seen) or _JOB_SUBJECT.search(seen.subject):
        return True
    sender = re.sub(r"[^a-z0-9]", "", seen.sender_key.split("@")[-1].split(".")[0])
    return any(len(k.replace(" ", "")) >= 4 and k.replace(" ", "") in sender for k in employer_keys)


def _said(clause: str | None, kind: str, where: str) -> Verdict | None:
    """The verdict that a clause of the subject or the text says `kind`: its received words (the whole clause when
    they are not one run, "Your application for Planner has been received") are the line."""
    if not clause:
        return None
    found = _RECEIVED.search(clause)
    words = scrub_quote(found.group(0) if found else clause, 90)
    return Verdict(kind, f'The {where} says "{words}".'[:LINE_CHARS])


def classify(seen: Seen, invite: Invite | None = None) -> Verdict | None:
    """The kind of a message by rules alone, or None when the rules do not settle it (the small model reads it next, and
    the Inbox gets it when the model cannot tell). The rules decide two things only (docs/PRD.md 14.4):
    1. Mail that is not about a job application gives nothing: a newsletter, a security code, an account verification,
       a shopping receipt has no application received wording; a credit card, a loan, an apartment, a scholarship, a
       volunteer place, an insurance policy, a gym or a college application has it but names the card, the loan, the
       place or the club, and a bare notice names nothing, so it gives nothing too. The rules look at no sender: a
       nameless bare notice ("Thank you for applying. We have received your application.") from a sender that is no
       job system and no employer still confirms. The guard is downstream (`match.py`, docs/PRD.md 14.5): a nameless
       mail matches a role only through the sender, whose name or address must hold the employer's name, and a
       stranger's mail moves nothing (the owner is asked, or no role matches).
    2. A bare received notice is a confirmation: received or thank-you-for-applying wording (`_RECEIVED`) in the
       subject or the text; nowhere in either any negation, outcome, closure, interview, offer, availability or weak
       word (`_NOT_BARE`); no letter outside basic Latin once accents are removed (a Cyrillic look-alike would hide a
       word from every list); and every sentence of the subject and of the text a fixed received sentence or a fixed
       harmless one, whole (`_bare`: the lists only refuse, the allow-list confirms, so a no worded in a way no list
       holds is left to the reader, and a mail that names a company, a job or anything else is left to the reader
       too); and no calendar invite. The rules read only what is kept of a mail (`OPENING_CHARS` of its text,
       `SUBJECT_CHARS` of its subject), and a no may follow the cut: a text or a subject that reaches its cut is
       never confirmed.
    Every other job-application mail is None. The rules make no rejection, no offer and no interview, whatever the
    mail says: "Thank you for applying ... we will be moving forward with other candidates", "decided not to extend an
    offer", a cancelled interview and "applicants who are not selected will be notified" were each read wrongly by
    rule lists, so the reader decides from the start of the mail. `invite` is the mail's calendar invite, which the
    reader is handed; a mail that carries one is never bare."""
    if invite is not None or seen.calendar:
        return None
    if len(seen.text) >= OPENING_CHARS or len(seen.subject) >= SUBJECT_CHARS:
        return None  # kept only up to its cut: the rest may hold the no
    subject, text = _fold(seen.subject), _fold(seen.text)
    if _outside_basic_latin(subject) or _outside_basic_latin(text):
        return None
    if any(pattern.search(subject) or pattern.search(text) for pattern in _NOT_BARE):
        return None
    (subject_bare, in_subject), (text_bare, in_text) = _bare(subject), _bare(text)
    if not (subject_bare and text_bare):
        return None
    return _said(in_subject, CONFIRMATION, "subject") or _said(in_text, CONFIRMATION, "message")


#: Curly quotes, long dashes and the ellipsis, as plain characters (code points: the source stays ASCII).
_QUOTES = str.maketrans({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-", 0x2026: "..."})
#: The sentence a reader's answer may be held to: the part of it that is compared. A line is a sentence; the longer
#: part of an answer is cut from the kept line anyway.
_COMPARED_CHARS = 250
#: The least a reader's sentence may be: four words and 20 characters. "e", "we" or "[email]" is in almost any mail and
#: shows nothing of what the reader decided from.
_QUOTE_MIN_WORDS, _QUOTE_MIN_CHARS = 4, 20


def _flat(text: str) -> str:
    """A text as it is compared: scrubbed the way a kept line is (addresses, links, numbers and codes replaced), quotes
    and dashes made plain, lower case, runs of whitespace folded."""
    return squash(scrub_quote(text, 10 * MAX_SCRUB_CHARS).translate(_QUOTES)).lower()


def _windows(text: str) -> list[str]:
    """`text` scrubbed in overlapping windows (the scrubber reads at most `MAX_SCRUB_CHARS` characters), so any run of
    up to half a window lies whole in one of them."""
    flat = squash(text)
    step = MAX_SCRUB_CHARS // 2
    return [_flat(flat[at:at + MAX_SCRUB_CHARS]) for at in range(0, max(len(flat), 1), step)]


def quoted_from(said: str, *sources: str) -> bool:
    """Whether the sentence a reader quotes is in the message: it, scrubbed and squashed, is found in one of the sources
    (the subject, the start of the text, the invite's summary), scrubbed and squashed the same way, ignoring case,
    quote marks and the ellipsis or full stop it may end with (docs/PRD.md 14.4). A paraphrase or an invented sentence
    is not found, and neither is a fragment: it is at least four words and 20 characters. That proves only that a real
    run of the mail's words was quoted, not that the reader decided from it."""
    wanted = _flat(said)[:_COMPARED_CHARS].strip(" .\"'")
    if len(wanted) < _QUOTE_MIN_CHARS or len(wanted.split()) < _QUOTE_MIN_WORDS:
        return False
    return any(wanted in window for source in sources for window in _windows(source))


# --- a calendar invite -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Invite:
    """What a calendar invite says of one interview. `start` is a moment (with its zone); a date-only invite has `day`
    and no `start`, and so has one whose clock time is in a zone nothing could name (the day as written is all that is
    known then)."""

    summary: str = ""
    start: datetime | None = None
    end: datetime | None = None
    day: date | None = None
    place: str = ""
    link: str = ""
    people: tuple[str, ...] = ()
    cancelled: bool = False


_LINK = re.compile(r"https://[^\s<>\"')\\]+")
_NAME = re.compile(r"[A-Za-z-]+")


def _split(line: str) -> tuple[str, str, str] | None:
    """A content line as (name, parameters, value). The parameters end at the first colon outside double quotes and
    brackets: a Windows zone is written `TZID=(UTC-08:00) Pacific Time (US & Canada)`, a colon inside its name."""
    named = _NAME.match(line)
    if named is None or named.end() >= len(line) or line[named.end()] not in ":;":
        return None
    for brackets in (True, False):  # a bracket that never closes: split at the first plain colon instead
        quoted, depth = False, 0
        for at in range(named.end(), len(line)):
            char = line[at]
            if char == '"':
                quoted = not quoted
            elif quoted:
                continue
            elif brackets and char == "(":
                depth += 1
            elif brackets and char == ")" and depth:
                depth -= 1
            elif char == ":" and depth == 0:
                return named.group().upper(), line[named.end():at].lstrip(";"), line[at + 1:]
    return None


def _unfold(text: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value).strip()


def _params(raw: str) -> dict[str, str]:
    out = {}
    for part in re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', raw):  # a semicolon inside quotes is not a separator
        key, _, value = part.partition("=")
        if key:
            out[key.upper()] = value.strip('"')
    return out


# The names a TZID can carry. An IANA name is read as it is; a path that ends in one (libical's
# `/freeassociation.sourceforge.net/Tzfile/America/New_York`) by the part that is; a Windows name (Outlook's
# `Pacific Standard Time`, or the long form `(UTC-08:00) Pacific Time (US & Canada)`) by this table (CLDR's, one zone
# for each name, the common ones); any other by the offsets of the invite's own VTIMEZONE; and one nothing can place by
# no clock time at all. (`US/Central` and `US/East-Indiana` are IANA's own names for two zones.)
_WINDOWS_ZONES = {
    "dateline standard time": "Etc/GMT+12", "utc-11": "Etc/GMT+11", "aleutian standard time": "America/Adak",
    "hawaiian standard time": "Pacific/Honolulu", "marquesas standard time": "Pacific/Marquesas",
    "alaskan standard time": "America/Anchorage", "utc-09": "Etc/GMT+9", "utc-08": "Etc/GMT+8",
    "pacific standard time (mexico)": "America/Tijuana", "pacific standard time": "America/Los_Angeles",
    "us mountain standard time": "America/Phoenix", "mountain standard time (mexico)": "America/Mazatlan",
    "mountain standard time": "America/Denver", "yukon standard time": "America/Whitehorse",
    "central america standard time": "America/Guatemala", "central standard time": "US/Central",
    "easter island standard time": "Pacific/Easter", "central standard time (mexico)": "America/Mexico_City",
    "canada central standard time": "America/Regina", "sa pacific standard time": "America/Bogota",
    "eastern standard time (mexico)": "America/Cancun", "eastern standard time": "America/New_York",
    "haiti standard time": "America/Port-au-Prince", "cuba standard time": "America/Havana",
    "us eastern standard time": "US/East-Indiana",
    "turks and caicos standard time": "America/Grand_Turk", "paraguay standard time": "America/Asuncion",
    "atlantic standard time": "America/Halifax", "venezuela standard time": "America/Caracas",
    "central brazilian standard time": "America/Cuiaba", "sa western standard time": "America/La_Paz",
    "pacific sa standard time": "America/Santiago", "newfoundland standard time": "America/St_Johns",
    "tocantins standard time": "America/Araguaina", "e. south america standard time": "America/Sao_Paulo",
    "sa eastern standard time": "America/Cayenne", "argentina standard time": "America/Argentina/Buenos_Aires",
    "greenland standard time": "America/Godthab", "montevideo standard time": "America/Montevideo",
    "magallanes standard time": "America/Punta_Arenas", "saint pierre standard time": "America/Miquelon",
    "bahia standard time": "America/Bahia", "utc-02": "Etc/GMT+2", "azores standard time": "Atlantic/Azores",
    "cape verde standard time": "Atlantic/Cape_Verde", "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik", "sao tome standard time": "Africa/Sao_Tome",
    "morocco standard time": "Africa/Casablanca", "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest", "romance standard time": "Europe/Paris",
    "central european standard time": "Europe/Warsaw", "w. central africa standard time": "Africa/Lagos",
    "gtb standard time": "Europe/Bucharest", "middle east standard time": "Asia/Beirut",
    "egypt standard time": "Africa/Cairo", "e. europe standard time": "Europe/Chisinau",
    "syria standard time": "Asia/Damascus", "west bank standard time": "Asia/Hebron",
    "south africa standard time": "Africa/Johannesburg", "fle standard time": "Europe/Kiev",
    "israel standard time": "Asia/Jerusalem", "jordan standard time": "Asia/Amman",
    "south sudan standard time": "Africa/Juba", "kaliningrad standard time": "Europe/Kaliningrad",
    "sudan standard time": "Africa/Khartoum", "libya standard time": "Africa/Tripoli",
    "namibia standard time": "Africa/Windhoek", "arabic standard time": "Asia/Baghdad",
    "turkey standard time": "Europe/Istanbul", "arab standard time": "Asia/Riyadh",
    "belarus standard time": "Europe/Minsk", "russian standard time": "Europe/Moscow",
    "e. africa standard time": "Africa/Nairobi", "iran standard time": "Asia/Tehran",
    "arabian standard time": "Asia/Dubai", "astrakhan standard time": "Europe/Astrakhan",
    "azerbaijan standard time": "Asia/Baku", "russia time zone 3": "Europe/Samara",
    "mauritius standard time": "Indian/Mauritius", "saratov standard time": "Europe/Saratov",
    "georgian standard time": "Asia/Tbilisi", "volgograd standard time": "Europe/Volgograd",
    "caucasus standard time": "Asia/Yerevan", "afghanistan standard time": "Asia/Kabul",
    "west asia standard time": "Asia/Tashkent", "ekaterinburg standard time": "Asia/Yekaterinburg",
    "pakistan standard time": "Asia/Karachi", "qyzylorda standard time": "Asia/Qyzylorda",
    "india standard time": "Asia/Kolkata", "sri lanka standard time": "Asia/Colombo",
    "nepal standard time": "Asia/Kathmandu", "central asia standard time": "Asia/Almaty",
    "bangladesh standard time": "Asia/Dhaka", "omsk standard time": "Asia/Omsk",
    "myanmar standard time": "Asia/Yangon", "se asia standard time": "Asia/Bangkok",
    "altai standard time": "Asia/Barnaul", "w. mongolia standard time": "Asia/Hovd",
    "north asia standard time": "Asia/Krasnoyarsk", "n. central asia standard time": "Asia/Novosibirsk",
    "tomsk standard time": "Asia/Tomsk", "china standard time": "Asia/Shanghai",
    "north asia east standard time": "Asia/Irkutsk", "singapore standard time": "Asia/Singapore",
    "w. australia standard time": "Australia/Perth", "taipei standard time": "Asia/Taipei",
    "ulaanbaatar standard time": "Asia/Ulaanbaatar", "aus central w. standard time": "Australia/Eucla",
    "transbaikal standard time": "Asia/Chita", "tokyo standard time": "Asia/Tokyo",
    "north korea standard time": "Asia/Pyongyang", "korea standard time": "Asia/Seoul",
    "yakutsk standard time": "Asia/Yakutsk", "cen. australia standard time": "Australia/Adelaide",
    "aus central standard time": "Australia/Darwin", "e. australia standard time": "Australia/Brisbane",
    "aus eastern standard time": "Australia/Sydney", "west pacific standard time": "Pacific/Port_Moresby",
    "tasmania standard time": "Australia/Hobart", "vladivostok standard time": "Asia/Vladivostok",
    "lord howe standard time": "Australia/Lord_Howe", "bougainville standard time": "Pacific/Bougainville",
    "russia time zone 10": "Asia/Srednekolymsk", "magadan standard time": "Asia/Magadan",
    "norfolk standard time": "Pacific/Norfolk", "sakhalin standard time": "Asia/Sakhalin",
    "central pacific standard time": "Pacific/Guadalcanal", "russia time zone 11": "Asia/Kamchatka",
    "new zealand standard time": "Pacific/Auckland", "utc+12": "Etc/GMT-12", "fiji standard time": "Pacific/Fiji",
    "kamchatka standard time": "Asia/Kamchatka", "chatham islands standard time": "Pacific/Chatham",
    "utc+13": "Etc/GMT-13", "tonga standard time": "Pacific/Tongatapu", "samoa standard time": "Pacific/Apia",
    "line islands standard time": "Pacific/Kiritimati",
}
#: The long form's words after its bracket, for the zones that are most often met under it.
_WINDOWS_LABELS = {
    "pacific time (us & canada)": "pacific standard time", "mountain time (us & canada)": "mountain standard time",
    "central time (us & canada)": "central standard time", "eastern time (us & canada)": "eastern standard time",
    "atlantic time (canada)": "atlantic standard time", "alaska": "alaskan standard time",
    "arizona": "us mountain standard time", "indiana (east)": "us eastern standard time",
    "hawaii": "hawaiian standard time", "dublin, edinburgh, lisbon, london": "gmt standard time",
    "amsterdam, berlin, bern, rome, stockholm, vienna": "w. europe standard time",
    "brussels, copenhagen, madrid, paris": "romance standard time",
    "chennai, kolkata, mumbai, new delhi": "india standard time",
    "beijing, chongqing, hong kong, urumqi": "china standard time", "osaka, sapporo, tokyo": "tokyo standard time",
    "canberra, melbourne, sydney": "aus eastern standard time", "auckland, wellington": "new zealand standard time",
}
_LONG_FORM = re.compile(r"\((?:UTC|GMT)[+-]\d\d:\d\d\)\s*(.+)", re.I)


def _zone_named(tzid: str) -> ZoneInfo | None:
    """The zone a TZID names (see above), or None when it names none that is known."""
    name = " ".join(tzid.replace('"', "").split())
    path = [part for part in name.split("/") if part]
    tries = [name, *("/".join(path[i:]) for i in range(1, len(path)))]
    long_form = _LONG_FORM.fullmatch(name)
    windows = _WINDOWS_LABELS.get(long_form.group(1).lower(), "") if long_form else name.lower()
    if windows in _WINDOWS_ZONES:
        tries.append(_WINDOWS_ZONES[windows])
    for key in tries:
        try:
            return ZoneInfo(key)
        except (KeyError, ValueError, OSError):
            continue
    return None


@dataclass(frozen=True)
class _Onset:
    """One part of a VTIMEZONE (STANDARD or DAYLIGHT): the local time it began, the rule that repeats it
    (`FREQ=YEARLY;BYMONTH=11;BYDAY=1SU`; none: it began once), the offset from UTC from then on, and whether it also
    began on dates an `RDATE` lists (which are not read, so such a part cannot be placed in time)."""

    began: datetime
    rule: str
    offset: timedelta
    listed: bool = False


def _utc_offset(text: str) -> timedelta:
    """`-0800` or `+053000` as an offset. Raises ValueError for one that is not written so, or is a day or more from UTC
    (real zones lie within 14 hours; `timezone()` itself refuses 24)."""
    sign, digits = (-1 if text.strip().startswith("-") else 1), text.strip().lstrip("+-")
    if len(digits) not in (4, 6) or not digits.isdigit():
        raise ValueError(text)
    offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]), seconds=int(digits[4:] or 0))
    if abs(offset) >= timedelta(hours=24):
        raise ValueError(text)
    return offset


def _zones(lines: list[str]) -> dict[str, list[_Onset]]:
    """The invite's own VTIMEZONE blocks: the parts of each zone, by the zone's name (lower case)."""
    zones: dict[str, list[_Onset]] = {}
    name, inside = "", False
    part: dict[str, str] | None = None
    for line in lines:
        word = line.upper()
        if word.startswith("BEGIN:VTIMEZONE"):
            name, part, inside = "", None, True
        elif word.startswith("END:VTIMEZONE"):
            inside = False
        elif not inside:
            continue
        elif word.startswith(("BEGIN:STANDARD", "BEGIN:DAYLIGHT")):
            part = {}
        elif word.startswith(("END:STANDARD", "END:DAYLIGHT")):
            try:
                if name and part is not None:
                    zones.setdefault(name, []).append(_Onset(
                        datetime.strptime(part["DTSTART"], "%Y%m%dT%H%M%S"), part.get("RRULE", ""),
                        _utc_offset(part["TZOFFSETTO"]), "RDATE" in part))
            except (KeyError, ValueError):
                pass  # a part that cannot be read leaves its zone short of it
            part = None
        elif (split := _split(line)) is not None:
            if part is not None:
                part.setdefault(split[0], split[2].strip())
            elif split[0] == "TZID":
                name = split[2].strip().lower()
    return zones


_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _began_by(onset: _Onset, stamp: datetime) -> datetime | None:
    """The latest local time at or before `stamp` that `onset` began, or None when it had not yet. Reads the yearly
    rules zones use (`BYMONTH` with `BYDAY=2SU` or `BYDAY=-1SU`, to an `UNTIL` or none); raises ValueError for any
    other, and for a part that also lists dates with `RDATE`."""
    if onset.began > stamp:
        return None
    if onset.listed:
        raise ValueError("RDATE")
    if not onset.rule:
        return onset.began
    rule = {key: value for key, _, value in (part.partition("=") for part in onset.rule.upper().split(";"))}
    ordinal = re.fullmatch(r"([+-]?\d)(SU|MO|TU|WE|TH|FR|SA)", rule.get("BYDAY", ""))
    if rule.get("FREQ") != "YEARLY" or ordinal is None or not rule.get("BYMONTH", "").isdigit():
        raise ValueError(onset.rule)
    nth, weekday, month = int(ordinal.group(1)), _WEEKDAYS.index(ordinal.group(2)), int(rule["BYMONTH"])
    until = datetime.strptime(rule["UNTIL"][:8], "%Y%m%d").date() if "UNTIL" in rule else None
    found = None
    for year in (stamp.year, stamp.year - 1):
        if nth > 0:
            first = date(year, month, 1)
            day = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))
        else:
            last = date(year, month, calendar.monthrange(year, month)[1])
            day = last - timedelta(days=(last.weekday() - weekday) % 7 + 7 * (-nth - 1))
        began = datetime.combine(day, onset.began.time())
        if day.month == month and onset.began <= began <= stamp and (until is None or day <= until):
            found = max(found, began) if found else began
    return found


def _offset_in(onsets: list[_Onset], stamp: datetime) -> timedelta | None:
    """The offset from UTC a VTIMEZONE gives the local time `stamp`: that of the part that began last. None when it
    has no part that had begun by then, or one whose rule cannot be read or runs past the calendar."""
    latest: tuple[datetime, timedelta] | None = None
    try:
        for onset in onsets:
            began = _began_by(onset, stamp)
            if began is not None and (latest is None or began > latest[0]):
                latest = (began, onset.offset)
    except (ValueError, OverflowError):
        return None
    return latest[1] if latest else None


def _moment(value: str, params: dict[str, str], zone: ZoneInfo, zones: dict[str, list[_Onset]] | None = None,
            ) -> tuple[datetime | None, date | None]:
    """A DTSTART or DTEND value as (a moment, a day): `20261105T140000Z` is UTC, a value with a TZID is in that zone
    (`_zone_named`, else the invite's own VTIMEZONE), a bare one is the owner's own, and a date alone is a day with no
    time. A time in a zone that nothing can name is the day as written and no time: the owner's zone is no guess to
    put on someone else's clock."""
    value = value.strip()
    try:
        if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
            return None, datetime.strptime(value[:8], "%Y%m%d").date()
        stamp = datetime.strptime(value.rstrip("Zz"), "%Y%m%dT%H%M%S")
    except ValueError:
        return None, None
    if value.endswith(("Z", "z")):
        return stamp.replace(tzinfo=UTC), None
    if "TZID" not in params:
        return stamp.replace(tzinfo=zone), None
    where = _zone_named(params["TZID"])
    if where is not None:
        return stamp.replace(tzinfo=where), None
    offset = _offset_in((zones or {}).get(params["TZID"].strip().lower(), []), stamp)
    if offset is not None:
        return stamp.replace(tzinfo=timezone(offset)), None
    return None, stamp.date()


def _person(params: dict[str, str], value: str) -> str:
    name = _unescape(params.get("CN", ""))
    if name:
        return name[:80]
    address = value.removeprefix("mailto:").removeprefix("MAILTO:").strip()
    return address.split("@")[0].replace(".", " ").title()[:80] if "@" in address else ""


def parse_invite(text: str, zone: ZoneInfo | None = None, *, exclude: frozenset[str] = frozenset()) -> Invite | None:
    """The first event of a calendar invite (`.ics` text), or None when it holds none. `exclude` is addresses left
    out of the people (the owner's own). Read in code only: nothing in it is followed or opened."""
    zone = zone or ZoneInfo("UTC")
    lines = _unfold(text or "")
    zones = _zones(lines) if "VTIMEZONE" in (text or "").upper() else {}
    event: dict[str, tuple[dict[str, str], str]] = {}
    people: list[str] = []
    method, inside, seen_event = "", False, False
    for line in lines:
        if line.upper().startswith("BEGIN:VEVENT"):
            if seen_event:
                break
            inside = seen_event = True
            continue
        if line.upper().startswith("END:VEVENT"):
            inside = False
            continue
        found = _split(line)
        if found is None:
            continue
        name, raw, value = found
        if name == "METHOD" and not inside:
            method = value.strip().upper()
        if not inside:
            continue
        params = _params(raw)
        if name in ("ATTENDEE", "ORGANIZER"):
            address = value.lower().removeprefix("mailto:").strip()
            who = _person(params, value)
            if who and address not in exclude and who not in people:
                people.append(who)
        else:
            event.setdefault(name, (params, value))
    if not seen_event:
        return None
    start, day = _moment(event["DTSTART"][1], event["DTSTART"][0], zone, zones) if "DTSTART" in event else (None, None)
    end = _moment(event["DTEND"][1], event["DTEND"][0], zone, zones)[0] if "DTEND" in event else None
    place = _unescape(event["LOCATION"][1]) if "LOCATION" in event else ""
    body = " ".join(_unescape(event[n][1]) for n in ("URL", "LOCATION", "DESCRIPTION") if n in event)
    link = _LINK.search(body)
    where = "" if _LINK.fullmatch(place) else place
    status = event.get("STATUS", ({}, ""))[1].strip().upper()
    return Invite(
        summary=_unescape(event["SUMMARY"][1])[:200] if "SUMMARY" in event else "", start=start, end=end, day=day,
        place=where[:200], link=link.group(0)[:500] if link else "", people=tuple(people[:10]),
        cancelled=method == "CANCEL" or status == "CANCELLED")


def invite_view(invite: Invite) -> dict:
    """The invite as a small JSON value (what a mail row keeps until it is acted on)."""
    return {"cancelled": invite.cancelled, "summary": invite.summary,
            "start": invite.start.isoformat() if invite.start else None,
            "end": invite.end.isoformat() if invite.end else None,
            "day": invite.day.isoformat() if invite.day else None,
            "place": invite.place, "link": invite.link, "people": list(invite.people)}


def invite_of(view: dict | None) -> Invite | None:
    """An invite from `invite_view`'s value (None when there was none)."""
    if not view or not (view.get("start") or view.get("day") or view.get("summary")):
        return None

    def moment(name: str) -> datetime | None:
        try:
            return datetime.fromisoformat(view[name]) if view.get(name) else None
        except (TypeError, ValueError):
            return None

    try:
        day = date.fromisoformat(view["day"]) if view.get("day") else None
    except (TypeError, ValueError):
        day = None
    return Invite(summary=str(view.get("summary") or ""), start=moment("start"), end=moment("end"), day=day,
                  place=str(view.get("place") or ""), link=str(view.get("link") or ""),
                  people=tuple(str(p) for p in view.get("people") or ()), cancelled=bool(view.get("cancelled")))


def when_words(when: datetime | None, day: date | None, zone: ZoneInfo | None = None) -> str:
    """When an interview is, in the owner's words: "Thu 5 Nov, 2:00 PM", or the day alone."""
    if when is not None:
        local = when.astimezone(zone) if zone else when
        clock = f"{local.hour % 12 or 12}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"
        return f"{local:%a} {local.day} {local:%b}, {clock}"
    if day is not None:
        return f"{day:%a} {day.day} {day:%b}"
    return ""
