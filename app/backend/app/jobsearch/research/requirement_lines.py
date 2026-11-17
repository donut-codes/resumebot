"""The lines of a posting's requirement sections, cut by code (docs/PRD.md 26.12): one line for each non-blank line of
the posting's text under a requirement heading, verbatim and in order, numbered from 1 across the whole posting. The
analyst grades every one (`analysis.py`); it picks, merges and rewords none. Pure: the same text gives the same lines.

A part is `required` (Requirements, Qualifications, Education, Experience, Skills, Must-haves...) or `preferred`
(Preferred, Nice to have, Bonus...). A heading is never a line. Each line is kept in the shape of the structured
record's items (`{n, text, quote}`, with `part`), the quote being the line itself. A line that says of itself that it
is optional ("(preferred)", "MBA strongly preferred", "a plus", "nice to have") is a preferred line wherever it stands.
An item is read as an item: a bulleted label ("Expand Your Skills: ...") is never a heading, and the closing
equal-opportunity text and the sections that describe the job rather than ask for anything (what success looks like,
the career framework) end a part. The bullets a posting lists as its duties are no requirement line: `duty_bullets`
finds them, so that an analysis that grades none can be refused. Nothing here names a person, an employer or a term of
anyone's search."""

import re
from dataclasses import dataclass

REQUIRED, PREFERRED = "required", "preferred"
#: The class of a line that can't be measured from a resume: in no count, earned or possible (docs/PRD.md 26.12).
NOT_MEASURABLE = "N"
PARTS = (REQUIRED, PREFERRED)

_BULLET = re.compile(r"^[-•*·▪‣◦–]\s*")
#: An apostrophe, a curly one, or the mark a bad decoding leaves in its place.
_AP = r"[^\w\s]?"
_PREFERRED = re.compile(
    r"preferred|nice[- ]to[- ]haves?|bonus|a plus|desired|set you apart|stand out|ways to stand|ideally|great if", re.I)
#: The words of `_REQUIRED` that also name benefits, notices and the labels of items ("Expand your skills", "Minimum
#: age", "Travel required"): a phrase with only these in it is a heading when it is a known heading or stands alone.
_WEAK_REQUIRED = r"skills|basic|minimum|required"
_STRONG_REQUIRED = (
    rf"qualif|requirement|must[- ]haves?|need to see|what you(?: will|{_AP}ll|{_AP}d| would)? need|"
    rf"what you(?: will|{_AP}ll) have|what you{_AP}ve done|what we need|"
    rf"what (?:we|you){_AP}?(?:re| are) looking for|your expertise|describes? you|who you are|"
    rf"who we{_AP}?(?:re| are) looking for|look for|"
    rf"you(?:{_AP}ll| will)? bring|about you|ideal candidate|your background|what you have|"
    r"you have")
_REQUIRED = re.compile(rf"{_STRONG_REQUIRED}|{_WEAK_REQUIRED}", re.I)
_STRONG = re.compile(_STRONG_REQUIRED, re.I)
#: Sub-headings of a requirement part: they keep the part they sit in, or are required when they sit in none.
_SUB = re.compile(r"^(?:education|experience|certifications?|credentials?|licen[sc]es?)\b", re.I)
#: Headings of any other part, tested before the requirement words ("Physical Job Requirements" ends a part).
_OTHER_FIRST = re.compile(
    r"^(?:about\b(?!\s+you\b)|physical\b|pay\b|compensation\b|salary\b|base salary\b|"
    r"benefits?\b|perks\b|why\b|how to apply\b|"
    r"equal opportunity\b|privacy\b|applicant privacy\b|compliance\b|working time\b|location\b|our\b|zone \d|"
    rf"general information\b|what we offer\b|what you(?:{_AP}ll| will) (?:gain|get|receive)\b|"
    r"career (?:framework|level|band)\b|core values\b|values framework\b|"
    r"what (?:does )?success looks? like\b|success in (?:this|the) role\b|measures of success\b)", re.I)
#: The rest: they end a part when nothing above made them a requirement heading.
_OTHER = re.compile(
    r"^(?:the role\b|role\b|job (?:summary|title|description|details)\b|summary\b|overview\b|"
    rf"(?:key |main |core |supervisory )?responsibilit|duties\b|what you{_AP}ll (?:be )?do|what you will (?:be )?do|"
    rf"what you{_AP}ll be doing|in this role\b|the impact\b|who we are\b|the team\b|seniority level\b|"
    r"employment type\b|industries\b|job function\b|impact\b)", re.I)
_MODS = r"(?:(?:minimum|basic|required|preferred|key|core|technical|desired|additional)\s+){0,2}"
#: A heading alone on its line, with no colon: only these exact phrases, so "Experience with SQL" is a line.
_BARE_HEADING = re.compile(
    rf"^{_MODS}(?:qualifications?|requirements?|skills|education|experience|must[- ]haves?|nice[- ]to[- ]haves?|"
    rf"bonus(?: points)?|what you{_AP}ll bring|who you are|required|preferred|minimum|basic|what we need to see|"
    rf"what you(?:(?: will|{_AP}ll|{_AP}d| would) need(?: to [\w ]{{1,25}})?| need)|what you(?: will|{_AP}ll) have|"
    rf"what you{_AP}ve done|here{_AP}s what you need|what we need|what we look for|"
    rf"what (?:we|you){_AP}?(?:re| are) looking for|who we{_AP}?(?:re| are) looking for|what you have|"
    rf"what you(?: will|{_AP}ll)? bring(?: to [\w ]{{1,25}})?|this (?:might|may|could) describe you|"
    r"your expertise|what will set you apart|"
    r"ways to stand out(?: from the crowd)?|job requirements|ideal candidate|about you|your background)"
    r"(?:\s+(?:and|&)\s+\w+)?$"
    r"|^(?:about (?:the (?:company|role|job|team)|us|you|this role|\w+(?: \w+)?)|(?:the )?role|"
    r"job (?:summary|title|description)|summary|overview|(?:key |main |core )?responsibilities|duties|"
    r"supervisory responsibilit(?:y|ies)|"
    rf"what you{_AP}ll (?:be )?do(?:ing)?|what you will (?:be )?do(?:ing)?|what you(?:{_AP}ll| will) be doing|"
    rf"in this role,? you will|the impact you(?: will|{_AP}ll) have|benefits|perks|what we offer(?: you)?|"
    r"compensation(?: and benefits)?|pay|salary|base salary range|pay (?:range )?transparency[\w ,]*|"
    r"zone \d pay range|physical (?:job )?(?:demands|requirements)|compliance|general information|"
    r"applicant privacy notice|working time|location|job details|join us on|download our app|"
    rf"why (?:join us|you{_AP}ll love it|[\w ]{{1,25}}\??)|how to apply|our (?:culture|values|mission)[\w ]*|"
    r"equal opportunity(?: employer)?|who we are|the team|seniority level|employment type|industries|job function|"
    r"career (?:framework|level|band)|(?:core )?values(?: framework)?|"
    rf"what you(?: will|{_AP}ll) (?:gain|get|receive)(?: [\w ]{{1,25}})?)$",
    re.I)
#: A line of the boilerplate that closes a posting (equal opportunity, the salary notice): ends a part, is no line.
_BOILERPLATE = re.compile(
    r"equal opportunit|equal employment|\beoe\b|affirmative action|does not discriminate|base salary range|"
    r"minimum gross base salary|salary range for|pay range for|reasonable accommodation|protected veteran|"
    r"without regard to (?:race|colou?r|sex|gender|religion)|regardless of (?:race|colou?r|gender|sex|religion)|"
    r"(?:tobacco|smoke|nicotine)[- ]free|drug[- ]free (?:workplace|environment|campus)", re.I)
#: A heading of what success looks like in the role, alone on its line: a picture of the first year, nothing asked.
_SUCCESS = re.compile(
    r"^(?:what (?:does )?success looks? like(?: (?:in|for) (?:this|the) (?:role|position|job))?|"
    r"success in (?:this|the) role|measures of success|how we measure success)\s*[?:]?$", re.I)
#: An item that says of itself that it is optional: "(preferred)", "MBA strongly preferred", "Python is a plus",
#: "nice to have". Only the item's own mark: "preferred" before a noun ("the client's preferred tools") is not one.
_BRACKETED_MARK = r"\(\s*(?:strongly |highly )?(?:preferred|a plus|nice[- ]to[- ]have)\s*\)"
_ITEM_PREFERRED = re.compile(
    rf"{_BRACKETED_MARK}"
    r"|\b(?:is|are|be|being)\s+(?:(?:strongly|highly|also|very|definitely|generally)\s+)?preferred\b"
    r"|\bpreferred,?\s+(?:but\s+)?(?:is\s+)?not\b"
    r"|\bpreferred[\s.,:!]*$"
    r"|\ba (?:big |huge |definite )?plus\b"
    r"|\bnice[- ]to[- ]haves?\b", re.I)
#: What makes such a line a mixed one that stays required: it also asks for something ("degree required; master's
#: preferred", "minimum five years, ten preferred"), or it has a second clause that may hold the required half.
_ITEM_ASKS = re.compile(
    r"(?<!not )(?<!not strictly )(?<!n't )(?<!n’t )\brequired\b|\brequires?\b|\bmust\b|\bminimum\b|;", re.I)
#: A second sentence is one too ("Bachelor's degree in business. Master's degree preferred."), and so is a mark inside a
#: bracket that holds more than the mark ("Python (C++ is a plus)").
_SENTENCE_BREAK = re.compile(r"[.!?]\s+(?=[A-Z0-9(\"'“‘])")
_BRACKET_WITH_WORDS = re.compile(rf"(?!{_BRACKETED_MARK})\([^()]*\)", re.I)
_HAS_BRACKETED_MARK = re.compile(_BRACKETED_MARK, re.I)
_TITLE_WORD = r"(?:[A-Z][^\s]*|and|or|of|the|for|&)"
#: A line of a few Title Case words that ends in a requirement word ("Required Job Qualifications"), or in a bracket
#: that says so ("Must-Have Pantry Staples (Minimum Qualifications)"), with no bullet: a heading.
_TITLED = re.compile(
    rf"^{_TITLE_WORD}(?:\s+{_TITLE_WORD}){{0,5}}\s+(?i:qualifications?|requirements?|skills)$"
    r"|^[^()]{1,50}\((?i:minimum|preferred|required|basic|desired)[^()]{0,30}\)$")
#: A few Title Case words and then the mark alone ("Certifications (Preferred)", "Domain Knowledge (Preferred)"): that
#: heads a list, where "Asset management experience (preferred)" is an item of one.
_TITLED_MARK = re.compile(rf"^(?:\d+[.)]\s*)?{_TITLE_WORD}(?:\s+{_TITLE_WORD}){{0,5}}\s*(?i:{_BRACKETED_MARK})$")
#: A heading that names what the candidate can do by a capability word: "Expected Capabilities", "Core Competencies",
#: "Technical Proficiencies", "Expected Capabilities & Tooling Fluency", or "Competencies" alone. The word is never
#: taken inside a longer phrase, and only a modifier that asks something of the candidate may stand before it, so "Our
#: capabilities", "Platform Capabilities" and "Proficiency in Python" are not headings. Tested with nothing after the
#: colon (`_kind` `alone`): "Technical Proficiency: Comfort with VoIP" is an item and keeps its label.
_CAPABILITY_WORD = r"(?:capabilit(?:y|ies)|competenc(?:y|ies)|proficienc(?:y|ies)|fluency)"
_CAPABILITY = re.compile(
    r"^(?:competencies|(?:(?:expected|required|preferred|desired|key|core|technical)\s+)"
    rf"{{1,2}}{_CAPABILITY_WORD})(?:\s+(?:and|&)\s+(?:[\w-]+\s+){{0,2}}{_CAPABILITY_WORD})?$", re.I)
#: What closes a posting, which a part opened by a capability heading also ends at (a part opened by any other heading
#: is cut as it always was). A competencies list sits last in many postings, or ahead of parts the splitter has no
#: heading for, and what follows it is no requirement: pay and travel notices, ad tags, recruiter notices, the
#: location list, the success, duty and summary headings. Only forms the log shows after such a list. The ones that
#: stand at the start of a line are tested there, so "Time management" and "Pay attention to detail" stay lines.
_CAPABILITY_END = re.compile(
    r"compensation range|on-target earnings|recruitment scams?|unsolicited (?:employment )?offers|"
    r"^(?:#[a-z]|apply (?:now|here)\b|note to third[- ]party recruiters|(?:time|job) type\b|"
    r"posting (?:start|end) date\b|travel (?:required|percentage)\b|"
    r"success (?:measures|in (?:this|the) role)\b|what success looks like\b|measures of success\b|"
    r"anticipated weekly hours\b|essential (?:duties|functions)\b|"
    r"job role summary\b|other (?:duties|information)\b|additional locations\b|what we bring\b|"
    r"our approach to compensation\b|"
    r"(?:[\w ,]{1,30} )?pay ranges?\s*:?$)", re.I)
_PHRASE = re.compile(r"^([^:\n]{1,60}?)\s*:\s*")
#: A line wrapped in quotes, straight or curly, alone on its line ("I am the person Acme is looking for."): a posting
#: that titles its parts by statements.
_QUOTED = re.compile(r"^[\"“”‘’'«»]\s*(.+?)\s*[\"“”‘’'«»]$")
#: A short lead-in sentence that says what comes under it is who the employer wants: it ends in "looking for" ("The
#: person we're looking for", "What we're looking for in you"). Never one that opens with "if", "not" or "when".
_LOOKING_FOR = re.compile(
    r"\blooking for(?: (?:in|on|from) (?:you|a candidate|an ideal candidate|this role|the role|your resume|"
    r"your background)| you to bring)?$", re.I)
#: "...you must have", "...you'll also have", "It would be great if you had": what the candidate has.
_YOU_HAVE = re.compile(rf"\byou(?: must| should| will|{_AP}ll|{_AP}ve)?(?: also)? (?:have|bring|had)$", re.I)
#: A line that starts so is a sentence about something else ("If this is not what you're looking for").
_NOT_A_LEAD_IN = re.compile(r"(?:if|not|when|unless)\b", re.I)
#: "You are", "You have" and the like, alone or with "someone who": a lead-in to a list of what the candidate is or has.
#: Only the whole line, so "You are responsible for the team" is a line.
_YOU_ARE = re.compile(
    rf"^you(?:{_AP}re| are| have|{_AP}ve| will have|{_AP}ll have| will bring|{_AP}ll bring| bring)"
    r"(?: (?:someone|a person|the person|the one|a candidate|an individual)(?: (?:who|that))?)?$", re.I)
#: A markdown heading ("## What We Look For in a Candidate").
_MARKDOWN = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
#: A lead-in or quoted line is a heading only while it is about this short.
_LEAD_IN_WORDS = 12
#: The heading of the part that lists what the job does, alone on its line ("Responsibilities", "Key Responsibilities",
#: "Essential Duties and Responsibilities", "What you'll do"): the bullets under it are the posting's duties.
_DUTY_WORD = r"(?:responsibilit(?:y|ies)|duties)"
_DUTIES_HEADING = re.compile(
    r"^(?:(?:(?:your|the|key|main|core|primary|principal|essential|major|job|role|position|supervisory|management)\s+)"
    rf"{{0,2}}{_DUTY_WORD}(?:\s+(?:and|&)\s+{_DUTY_WORD})?|"
    rf"what you(?: will|{_AP}ll)(?: be)? do(?:ing)?|in this role,? you(?: will|{_AP}ll))$", re.I)
#: A duty bullet that is the catch-all every posting ends its duties with, which the analyst leaves out ("Other duties
#: as assigned", "Perform additional duties as required").
_CATCH_ALL_DUTY = re.compile(
    r"\bother (?:[\w-]+ )?duties\b|\bduties as (?:assigned|required|needed)\b|\badditional duties\b", re.I)


@dataclass(frozen=True)
class Line:
    n: int
    part: str
    text: str


@dataclass(frozen=True)
class DutyBullet:
    heading: str  # the duties heading the bullet stands under, as the posting writes it
    text: str


def _is_capability(heading: str) -> bool:
    return _CAPABILITY.match(heading.strip()) is not None


def _kind(phrase: str, current: str | None, capability: bool = False, *,
          alone: bool = True) -> tuple[bool, str | None, bool]:
    """`(a known heading, the part it opens, whether a capability heading opened it)`; `capability` says whether one
    opened the part `current`, which a sub-heading or a heading that is not known leaves as it is. A heading that is
    not known is not a heading here. `alone` is False for a phrase with its line after it ("Technical Proficiency:
    Comfort with VoIP systems"): the capability headings below are then the label of an item, not a heading, so the
    line keeps its label."""
    if _OTHER_FIRST.match(phrase) and not _PREFERRED.search(phrase):
        return True, None, False
    if _PREFERRED.search(phrase):
        return True, PREFERRED, _is_capability(phrase)
    if _REQUIRED.search(phrase) or (alone and _is_capability(phrase)):
        return True, REQUIRED, _is_capability(phrase)
    if _SUB.match(phrase):
        return True, current or REQUIRED, capability and current is not None
    if _OTHER.match(phrase):
        return True, None, False
    return False, current, capability


def _unquoted(text: str) -> str | None:
    """A whole line in quotes, without them and without its closing stop; None when the line is not one."""
    m = _QUOTED.match(text)
    return m.group(1).strip().rstrip(".:!").strip() if m else None


def _lead_in(text: str) -> str | None:
    """The part a short line opens when, less any closing stop, it is a lead-in to what the employer wants of the
    candidate (preferred when it says "ideally" or "great if", else required); None when it is not one."""
    text = text.strip().rstrip(".:!").strip()
    if not 0 < len(text.split()) <= _LEAD_IN_WORDS or _NOT_A_LEAD_IN.match(text):
        return None
    if (_OTHER_FIRST.match(text) or _OTHER.match(text)) and not _PREFERRED.search(text):  # "The impact you will have"
        return None
    if not (_LOOKING_FOR.search(text) or _YOU_ARE.match(text) or _YOU_HAVE.search(text)):
        return None
    return PREFERRED if _PREFERRED.search(text) else REQUIRED


def _is_bare_heading(text: str) -> bool:
    text = text.strip()
    return len(text.split()) <= 7 and (_BARE_HEADING.match(text) or _CAPABILITY.match(text)) is not None


def _is_item_label(phrase: str) -> bool:
    """Whether a phrase that matches `_REQUIRED` only by a weak word ("Expand Your Skills", "Strong analysis and
    process management skills", "Minimum age") and is no known heading is the label of an item when its line follows
    it. A heading that names a requirement part by a strong word ("Qualifications", "What you need"), an exact heading
    ("Technical Skills") and a phrase that opens another part are never labels."""
    phrase = re.sub(r"^\W+", "", phrase)  # a bullet mark the bullet list does not know ("●")
    return (_REQUIRED.search(phrase) is not None and _STRONG.search(phrase) is None
            and _PREFERRED.search(phrase) is None and _OTHER_FIRST.match(phrase) is None
            and not _is_bare_heading(phrase))


def _says_preferred(line: str) -> bool:
    """Whether an item calls itself optional and asks for nothing else: a preferred line, in whatever part it stands."""
    if _ITEM_ASKS.search(line) or _SENTENCE_BREAK.search(line):
        return False
    return _ITEM_PREFERRED.search(_BRACKET_WITH_WORDS.sub(" ", line)) is not None


def split(text: str | None) -> list[Line]:
    """The posting's requirement lines (`Line.n` counts from 1 in the order they stand)."""
    out: list[Line] = []
    kind: str | None = None
    capability = False  # a capability heading opened the part `kind`: it also ends at `_CAPABILITY_END`
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if (hashed := _MARKDOWN.match(line)) is not None:  # a markdown heading: never a line
            heading = hashed.group(1)
            if (opened := _lead_in(heading)) is not None:
                kind, capability = opened, False
            else:
                _, kind, capability = _kind(heading, kind, capability)
            continue
        bulleted = _BULLET.match(line) is not None
        line = _BULLET.sub("", line).strip()
        if _BOILERPLATE.search(line) or (capability and _CAPABILITY_END.search(line)):
            kind, capability = None, False
            continue
        if not bulleted and (quoted := _unquoted(line)) is not None and len(quoted.split()) <= _LEAD_IN_WORDS:
            # A quoted line alone is a title: of a requirement part when it says so, else it ends the part before it.
            if (opened := _lead_in(quoted)) is not None:
                kind, capability = opened, False
            elif _is_bare_heading(quoted) or _TITLED.match(quoted):
                _, kind, capability = _kind(quoted, kind, capability)
            else:
                kind, capability = None, False
            continue
        if not bulleted and (opened := _lead_in(line)) is not None:
            kind, capability = opened, False
            continue
        if (not bulleted and len(line.split()) <= 7 and _TITLED.match(line)
                and not (_HAS_BRACKETED_MARK.search(line) and not _TITLED_MARK.match(line))):
            _, kind, capability = _kind(line, kind, capability)
            continue
        if not bulleted and _SUCCESS.match(line):  # what success looks like: a picture of the role, nothing asked
            kind, capability = None, False
            continue
        if _is_bare_heading(line):
            _, kind, capability = _kind(line, kind, capability)
            continue
        while (m := _PHRASE.match(line)) and len(m.group(1).split()) <= 8:
            rest = line[m.end():].strip()
            known, opened, opened_by_capability = _kind(m.group(1), kind, capability, alone=not rest)
            if known and rest and (bulleted or kind == REQUIRED) and _is_item_label(m.group(1)):
                break  # a bulleted item, or any item in a required part, keeps its label ("Expand Your Skills: ...")
            if known:
                kind, capability, line = opened, opened_by_capability, rest
            elif not rest:  # an unknown heading with a colon and nothing after it: a sub-heading, the part goes on
                line = ""
            else:
                break
        if kind and sum(c.isalnum() for c in line) >= 2:
            out.append(Line(len(out) + 1, PREFERRED if _says_preferred(line) else kind, line))
    return out


def as_items(lines: list[Line]) -> list[dict]:
    """The lines in the shape of the structured record's items: `{n, text, quote}`, and the part."""
    return [{"n": x.n, "part": x.part, "text": x.text, "quote": x.text} for x in lines]


def _opens_another_part(text: str) -> bool:
    """Whether a plain line that is not a duties heading is the heading of any other part (requirements, preferred,
    the company, benefits, what success looks like...), by the same readings `split` uses. A heading `split` has no part
    for ("Leadership", "Delivery:") is a group of the duties and ends nothing, and so is a sentence."""
    if len(text.split()) > 8:
        return False
    bare = text.rstrip(":").strip()
    if _is_bare_heading(bare) or _TITLED.match(bare) or _SUCCESS.match(bare) or _lead_in(bare) is not None:
        return True
    m = _PHRASE.match(text)
    if m is None or len(m.group(1).split()) > 8 or _DUTIES_HEADING.match(m.group(1).strip()):
        return False  # "Responsibilities: lead the team" is a sentence of the duties' own part
    return _kind(m.group(1), None, alone=not text[m.end():])[0]


def duty_bullets(text: str | None) -> list[DutyBullet]:
    """The bullets the posting lists under a responsibilities heading ("Responsibilities", "Key Responsibilities",
    "Duties", "What you'll do"), in order, less the catch-all ("Other duties as assigned"): the duties the analyst
    grades, one item for each. The part ends at the next heading of another part and at the closing boilerplate; a
    heading the splitter has no part for is a group of the duties and does not end it. Only bulleted lines count:
    duties written as prose have no bullet to grade, so a posting of those has none. Pure, and cut by the same readings
    as `split`."""
    out: list[DutyBullet] = []
    heading: str | None = None
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        bulleted = _BULLET.match(line) is not None
        line = _BULLET.sub("", line).strip()
        if _BOILERPLATE.search(line):
            heading = None
        elif bulleted:
            if heading and sum(c.isalnum() for c in line) >= 2 and not _CATCH_ALL_DUTY.search(line):
                out.append(DutyBullet(heading, line))
        else:
            hashed = _MARKDOWN.match(line)
            plain = (hashed.group(1) if hashed else line).strip()
            if _DUTIES_HEADING.match(plain.rstrip(":").strip()):
                heading = plain.rstrip(":").strip()
            elif _opens_another_part(plain):
                heading = None
    return out
