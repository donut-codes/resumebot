"""The style rules that ship with the app (docs/PRD.md 33.6): five plain rules about how a packet reads, each on until
the owner switches it off (`Writing.style_off`). The packet check holds the ones that are on against the words the
writer prints, and the writer is told them with the rest of the QA settings (`documents.rules_for`).

- **Shipped and read-only.** Each rule is an id, a name and a sentence in plain words (what the settings page shows) and
  a finder: the words of one text that break it. They are the same for every user.
- **Switches.** `Writing.style_off` lists the ids switched off. A writing document saved before there were switches has
  none, so every rule is on; an id this release does not know is kept and ignored.
- **A phrase the owner already bans.** A starter phrase the owner's Never write holds too is left to the owner's rule,
  which shows their reason: the packet never gets two lines for one phrase.
- **Text copied whole from a master line** is not read (`find_matches`' `exempt`): the master is the source and is never
  edited to satisfy a rule. The owner's Never write and the integrity checks still read it.
"""

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.jobsearch.profile import never_write

if TYPE_CHECKING:
    from app.jobsearch.profile.documents import Writing

#: The packet check these rules make up.
PACKET_CHECK = "Your style rules"

#: A finder: the words of one text that break its rule, given the starter phrases the owner's Never write already holds.
Finder = Callable[[str, Collection[str]], list[str]]


@dataclass(frozen=True)
class StyleRule:
    id: str
    #: In plain words, as the owner reads it on the settings page and in a packet's checks.
    name: str
    #: The rule in a sentence: what the writer is told and what the page shows under the name.
    rule: str
    finder: Finder
    #: The plain phrases of a rule that is a list of phrases, so the ones the owner already bans can be left to them.
    phrases: tuple[str, ...] = ()


# --- no em dash ------------------------------------------------------------------------------------------------------

#: Every dash there is (U+2010 to U+2015: the hyphen kinds, the figure dash, the en dash, the em dash and the bar),
#: for a class of characters that are not one. The plain hyphen is added, escaped.
_DASHES = "".join(chr(code) for code in range(0x2010, 0x2016)) + r"\-"
_EN_DASH, _EM_DASH = chr(0x2013), chr(0x2014)
_EM = re.compile(_EM_DASH)
#: A spaced hyphen or en dash between words. Horizontal space only: a hyphen that starts a line is a list marker.
_SPACED_DASH = re.compile(rf"(?<=[^\s{_DASHES}])[ \t]+[\-{_EN_DASH}][ \t]+(?=[^\s{_DASHES}])")
#: Two hyphens or more between words, spaced or not.
_DOUBLE_HYPHEN = re.compile(r"(?<=[^\s\-])[ \t]*-{2,}[ \t]*(?=[^\s\-])")
_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?"
           r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
#: What a date ends in (before a dash) and starts with (after it): a year, a month, a quarter, or "present".
_DATE_BEFORE = re.compile(rf"(?:\b(?:19|20)\d\d|\b{_MONTHS}\.?|\bQ[1-4]|\b(?:present|current|now|today))\s*$", re.I)
_DATE_AFTER = re.compile(rf"(?:(?:19|20)\d\d|{_MONTHS}|Q[1-4]|present|current|now|today|ongoing)\b", re.I)


def _dashes(text: str, held: Collection[str]) -> list[str]:
    """An em dash anywhere; a spaced en dash or hyphen, or two hyphens, between words. A dash between two dates
    ("2019 - 2022", "Jan 2019 - Mar 2021") and a hyphen inside a word ("long-term") are left alone."""
    found = [m.group(0) for m in _EM.finditer(text)]
    for m in _SPACED_DASH.finditer(text):
        if _DATE_BEFORE.search(text[:m.start()]) and _DATE_AFTER.match(text, m.end()):
            continue
        found.append(m.group(0))
    return found + [m.group(0) for m in _DOUBLE_HYPHEN.finditer(text)]


# --- no markup -------------------------------------------------------------------------------------------------------

#: Markup the Word file would print as typed. An italic opener follows a space, punctuation or the start and is followed
#: by a non-space; its closer follows a non-space and is followed by a non-word character: `snake_case_name`, "5*3",
#: "a * b" and a lone asterisk are not markup.
_MARKUP = (
    re.compile(r"\*\*(?=\S)[^\n]*?(?<=\S)\*\*"),
    re.compile(r"__(?=\S)[^\n]*?(?<=\S)__"),
    re.compile(r"(?<![\w*])\*(?![\s*])[^\n*]*?(?<![\s*])\*(?![\w*])"),
    re.compile(r"(?<![\w_])_(?![\s_])[^\n_]*?(?<![\s_])_(?![\w_])"),
    re.compile(r"`[^`]*`"),
    re.compile(r"</?(?:b|i|strong|em|u)(?![\w-])[^>]*>", re.IGNORECASE),
)


def _markup(text: str, held: Collection[str]) -> list[str]:
    return [m.group(0) for pattern in _MARKUP for m in pattern.finditer(text)]


# --- no year beside a degree -----------------------------------------------------------------------------------------

#: A degree, by its word (any case) or its abbreviation (as written: "MA 2019" is a state, so a two-letter abbreviation
#: counts only when "in" follows). "Master" is a degree as "master's", "masters", "master of" or "master degree".
_DEGREE_WORDS = re.compile(
    r"\b(?:bachelor(?:['’]?s)?|master(?:['’]s|s)|master\s+(?:degree|of)|associate(?:['’]s)?\s+degree|doctorate"
    r"|ph\.?d)\b\.?", re.IGNORECASE)
_DEGREE_ABBREVIATIONS = re.compile(r"\b(?:MBA|JD)\b|\b(?:J\.D\.|[BM]\.[SA]\.)|\b(?:BS|BA|MS|MA)\s+in\b")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_CLASS_OF = re.compile(r"\bclass\s+of\b", re.IGNORECASE)
#: The end of a sentence: a full stop, ! or ? before space or the end, unless it follows a lone letter (the periods of
#: "B.S." and "Ph.D." do not end one).
_SENTENCE_END = re.compile(r"(?<!(?<![A-Za-z])[A-Za-z])[.!?]+(?=\s|$)")
_WORD = re.compile(r"\w+")
#: A year counts as beside a degree within this many words of it.
WITHIN_WORDS = 6


def _year_by_degree(text: str, held: Collection[str]) -> list[str]:
    """A year or "class of" within six words of a degree, in one sentence: the span from one to the other."""
    degrees = [m.span() for pattern in (_DEGREE_WORDS, _DEGREE_ABBREVIATIONS) for m in pattern.finditer(text)]
    marks = [m.span() for pattern in (_YEAR, _CLASS_OF) for m in pattern.finditer(text)]
    if not degrees or not marks:
        return []
    ends = [m.start() for m in _SENTENCE_END.finditer(text)]
    found: list[str] = []
    for d_start, d_end in sorted(set(degrees)):
        near: list[tuple[int, int, int]] = []  # (words away, start, end) of each year marker in range
        for m_start, m_end in marks:
            first, second = ((d_end, m_start) if d_end <= m_start else (m_end, d_start))
            if first > second or any(first <= end < second for end in ends):
                continue
            away = len(_WORD.findall(text[first:second])) + 1
            if away <= WITHIN_WORDS:
                near.append((away, m_start, m_end))
        if near:
            _, m_start, m_end = min(near)
            snippet = text[min(d_start, m_start):max(d_end, m_end)]
            if snippet not in found:
                found.append(snippet)
    return found


# --- phrases ---------------------------------------------------------------------------------------------------------

CONCEDING = (
    "although I lack", "although I don't have", "although I do not have", "despite not having", "despite lacking",
    "while I don't have", "while I do not have", "I may not have", "even though I have not", "even though I don't have",
)
WEAK = (
    "responsible for", "helped with", "worked on", "various", "multiple", "successfully", "spearheaded", "leveraged",
    "synergy",
)


def _phrases(phrases: tuple[str, ...]) -> Finder:
    """A finder for plain phrases, matched as the owner's Never write phrases are (`never_write.phrase_pattern`): case,
    plural and -ed/-ing endings are caught. A phrase in `held` is the owner's to report."""
    compiled = [(p, never_write.phrase_pattern(p)) for p in phrases]

    def find(text: str, held: Collection[str]) -> list[str]:
        return [m.group(0) for phrase, pattern in compiled if pattern is not None and phrase not in held
                for m in pattern.finditer(text)]

    return find


STYLE_RULES: tuple[StyleRule, ...] = (
    StyleRule("no-em-dash", "No em dash",
              "Use a comma, a period or a new sentence. A dash between two dates is fine.", _dashes),
    StyleRule("no-inline-markup", "No markup",
              "Bold, italics and other markup print as typed in the Word file. Write plain text; the app sets its own "
              "labels.", _markup),
    StyleRule("no-year-by-degree", "No year beside a degree",
              "A year next to a degree tells a reader your age. Name the degree and the school.", _year_by_degree),
    StyleRule("no-pre-conceding", "No phrase that concedes a gap first",
              "Say nothing about a gap, or say one confident sentence. Never open with what you lack.",
              _phrases(CONCEDING), CONCEDING),
    StyleRule("no-weak-words", "No weak or inflated words",
              "Words like 'responsible for', 'various' and 'leveraged' say little and sound padded.",
              _phrases(WEAK), WEAK),
)


def told(rule: StyleRule) -> str:
    """The rule as the writer is handed it: its sentence and, for a rule that is a list of phrases, every phrase the
    check reads. The sentence on the settings page gives examples; the writer needs the whole list, or it learns which
    words the check holds only from a refusal."""
    return f"{rule.rule} Every phrase the check reads: {', '.join(rule.phrases)}." if rule.phrases else rule.rule


def shipped() -> list[dict]:
    """The rules that ship with the app, for the settings page: each one's id, name and sentence."""
    return [{"id": r.id, "name": r.name, "rule": r.rule} for r in STYLE_RULES]


def on(writing: "Writing") -> list[StyleRule]:
    """The rules that are on for this owner, in the app's order: all but the ones in `style_off`. An id the app does
    not know switches nothing off."""
    off = set(writing.style_off)
    return [r for r in STYLE_RULES if r.id not in off]


def _owner_patterns(writing: "Writing") -> list[re.Pattern]:
    """The matchers of what the owner's Never write holds: their phrases, then the older entries that are still
    patterns (one that cannot be compiled holds nothing: the packet check reports it)."""
    out = [p for rule in writing.banned if (p := never_write.phrase_pattern(rule.phrase)) is not None]
    for rule in writing.legacy_patterns:
        try:
            out.append(re.compile(rule.pattern if rule.regex else re.escape(rule.pattern), re.IGNORECASE))
        except re.error:
            continue
    return out


def find_matches(writing: "Writing", texts: list[tuple[str, str]],
                 exempt: Collection[str] = ()) -> list[never_write.Match]:
    """Every place the printed text breaks a style rule that is on: where, the words that matched, and the rule's name.
    `texts` is each piece of printed text with where it is. `exempt` names the places whose text is a master line
    copied whole, which no style rule reads (the owner's Never write and the built-in checks still do)."""
    owned = _owner_patterns(writing)
    found: list[never_write.Match] = []
    for rule in on(writing):
        held = frozenset(p for p in rule.phrases if any(o.fullmatch(p) for o in owned))
        for where, text in texts:
            if where not in exempt:
                found += [never_write.Match(where, snippet, rule.name) for snippet in rule.finder(text, held)]
    return found
