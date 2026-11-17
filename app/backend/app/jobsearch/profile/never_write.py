"""What the packet check looks for in a draft (docs/PRD.md 33.5, 33.6): the owner's "Never write" phrases, the checks
that ship with the app, and the patterns carried over from before phrases were plain words.

- **The owner's phrases are plain words.** `phrase_pattern` builds the matcher from the words alone: case ignored,
  whole words only, straight and curly apostrophes and quotes alike, a hyphen or a space alike, and the simple variants
  of each word (plural -s/-es/-ies, -ed, -ing). Every character of a phrase is escaped, so a phrase is never read as a
  pattern.
- **The built-in checks** (`BUILT_IN`) are kinds of text rather than phrases, the same for every user and read-only.
  Each has a plain-words name and the QA rule it backs.
- **Legacy patterns** (`Writing.legacy_patterns`) are the owner's older entries that were regular expressions. They are
  enforced exactly as before until the owner places them.
"""

import re
from dataclasses import dataclass

#: Hyphens, for a character class (the plain hyphen last, escaped, so it is never read as a range).
_HYPHENS = "‐‑\\-"
_SEPARATOR = rf"[\s{_HYPHENS}]+"
_APOSTROPHES = "'’‘ʼ"
_QUOTES = '"“”„'
_VOWELS = "aeiou"
#: A word is varied only when it is plain letters; anything else (a number, a symbol) is matched as written.
_LETTERS = re.compile(r"^[^\W\d_]+$")


def _stems(word: str) -> set[str]:
    """The bases a word may have come from, the word itself first: `led` -> {led}; `leveraging` -> {leveraging,
    leverag, leverage}; `policies` -> {policies, policie, polic, policy}. A base is at least three letters."""
    w = word.lower()
    out = {w}

    def add(base: str) -> None:
        if len(base) >= 3:
            out.add(base)

    if w.endswith("ing") and len(w) > 4:
        base = w[:-3]
        add(base)
        if len(base) >= 3:
            add(base + "e")
        if len(base) >= 2 and base[-1] == base[-2] and base[-1] not in _VOWELS:
            add(base[:-1])
    if w.endswith("ed") and len(w) > 3:
        base = w[:-2]
        add(base)
        if len(base) >= 3:
            add(base + "e")
        if len(base) >= 2 and base[-1] == base[-2] and base[-1] not in _VOWELS:
            add(base[:-1])
        if w.endswith("ied"):
            add(w[:-3] + "y")
    if w.endswith("ies") and len(w) > 4:
        add(w[:-3] + "y")
    if w.endswith("es") and len(w) > 3:
        add(w[:-2])
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        add(w[:-1])
    return out


def _forms(word: str) -> set[str]:
    """Every form of a word the matcher accepts: the word, and each base with -s, -es, -ed, -ing (and -ies, -ied after
    a y, a doubled last letter after a short stressed syllable)."""
    forms = {word.lower()}
    for base in _stems(word):
        forms.add(base)
        forms |= {base + "s", base + "es", base + "ed", base + "ing"}
        if base.endswith("e"):
            forms |= {base + "d", base[:-1] + "ing"}
        if base.endswith("y") and len(base) > 2:
            forms |= {base[:-1] + "ies", base[:-1] + "ied"}
        if len(base) >= 3 and base[-1] not in _VOWELS + "wxy" and base[-2] in _VOWELS and base[-3] not in _VOWELS:
            forms |= {base + base[-1] + "ed", base + base[-1] + "ing"}
    return forms


def _literal(text: str) -> str:
    """`text` as a pattern that matches exactly itself, an apostrophe or a quote any of its kinds."""
    out = []
    for ch in text:
        if ch in _APOSTROPHES:
            out.append(f"[{_APOSTROPHES}]")
        elif ch in _QUOTES:
            out.append(f"[{_QUOTES}]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


#: `_literal`, for the checks beside this one that match words as they are written (a certification's name), with an
#: apostrophe or a quote any of its kinds.
literal = _literal


def _word(token: str) -> str:
    if len(token) < 3 or not _LETTERS.match(token):
        return _literal(token)
    forms = sorted(_forms(token), key=lambda f: (-len(f), f))
    return "(?:" + "|".join(_literal(f) for f in forms) + ")"


def phrase_pattern(phrase: str) -> re.Pattern | None:
    """The matcher for one plain phrase, or None when it holds no words. Apostrophes inside a word stay in it
    (`don't`); a hyphen or a run of spaces between words matches either."""
    tokens = [t for t in re.split(_SEPARATOR, phrase.strip()) if t]
    if not tokens:
        return None
    body = _SEPARATOR.join(_word(t) for t in tokens)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


# --- the checks that ship with the app --------------------------------------------------------------------------

#: The packet check the built-in checks join.
PACKET_CHECK = "Nothing you ban appears"
_ROUND_NUMBERS = "ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty"
_SMALL_NUMBERS = "two|three|four|five|six|seven|eight|nine"
#: A word that is not the start of a phrase ("one of the tools"): the words between a number and what it counts.
_FILLER = r"(?!(?:of|the|and|or|for|to|with|in|on|at|a|an)\b)[\w'’]+"


@dataclass(frozen=True)
class ShippedCheck:
    id: str
    #: In plain words, as the owner reads it.
    name: str
    #: The QA rule it enforces, in a sentence.
    rule: str
    pattern: re.Pattern

    @property
    def backs(self) -> str:
        return PACKET_CHECK


def _rx(text: str) -> re.Pattern:
    return re.compile(text, re.IGNORECASE)


BUILT_IN: tuple[ShippedCheck, ...] = (
    ShippedCheck(
        "years-of-experience", "A count of years of experience",
        "Show what was done, not how long: a count of years invites an age screen and says nothing about the work.",
        _rx(rf"(?<!\w)(?:\d+\+?|(?:{_ROUND_NUMBERS})\+?)[\s{_HYPHENS}]*(?:years?|yrs?)(?!\w)")),
    ShippedCheck(
        "headcount-flex", "A headcount used as a flex",
        "Describe scope by what was led or delivered, not by a headcount: \"12-person team\" and \"50+ people\" "
        "cannot be checked against the master.",
        _rx(rf"(?<!\w)\d[\d,]*\+?[\s{_HYPHENS}]*persons?(?!\w)"
            r"|(?<!\w)\d[\d,]*\+\s*(?:people|practitioners|employees|staff|consultants)(?!\w)")),
    ShippedCheck(
        "outcome-absolutes", "An absolute about outcomes",
        "Claim only what the master proves: \"zero incidents\", \"every finding\" and \"all defects\" cannot be shown.",
        _rx(r"(?<!\w)(?:zero|no)\s+(?:[\w'’]+\s+){0,2}?"
            r"(?:incidents?|findings?|defects?|attrition|escalations?)(?!\w)"
            r"|(?<!\w)(?:every|all)\s+(?:[\w'’]+\s+)?"
            r"(?:incidents?|findings?|defects?|attrition|escalations?)(?!\w)"
            r"|(?<!\w)retained\s+(?:every|all)(?!\w)|(?<!\w)retained\s+100\s*%")),
    ShippedCheck(
        "counts-of-things-built", "Counts of things built",
        "Describe work at the level of its milestones, not as a count of what was built: \"twelve new assistants\" and "
        "\"a 9-skill framework\" are counts, not outcomes.",
        _rx(rf"(?<!\w)(?:\d+\+?|(?:{_SMALL_NUMBERS}|{_ROUND_NUMBERS})\+?)[\s{_HYPHENS}]+(?:{_FILLER}[\s{_HYPHENS}]+){{0,2}}?"
            r"(?:agents?|assistants?|skills?|tools?|competenc(?:y|ies))(?!\w)")),
)


def built_in_checks() -> list[dict]:
    """The checks that ship with the app, for the page: each one's name and the QA rule it backs."""
    return [{"id": c.id, "name": c.name, "rule": c.rule, "backs": c.backs} for c in BUILT_IN]


@dataclass(frozen=True)
class Match:
    """One place a draft breaks a rule: where it is, the words that matched, and the rule it broke as the owner reads it
    (the owner's phrase with its why, a legacy pattern, or a built-in check by name). A rule that could not be checked
    has no place: `where` and `snippet` are empty and `rule` says what went wrong."""

    where: str
    snippet: str
    rule: str

    def says(self) -> str:
        """The line a check prints: `where: 'words' (rule)`, or the sentence alone for a rule that was not checked."""
        return f"{self.where}: {self.snippet!r} ({self.rule})" if self.where else self.rule


def _locate(pattern: re.Pattern, text: str, where: str, rule: str) -> Match | None:
    m = pattern.search(text)
    return Match(where, m.group(0), rule) if m else None


def find_matches(writing, texts: list[tuple[str, str]], *, built_in: bool = True) -> list[Match]:
    """Every place a draft breaks the owner's phrases, the legacy patterns or (unless `built_in` is false) a built-in
    check: where, what matched and which rule it was (the owner's phrase with its why, a legacy pattern, or the
    built-in check by name). `texts` is each piece of printed text with where it is.

    `built_in=False` leaves out the four built-in text kinds and keeps the owner's phrases and the legacy patterns. It
    is the one switch for text that is not a document a screener reads (a spoken answer says "years", and the checks
    on a coach's draft are not the resume's)."""
    found: list[Match] = []
    for rule in writing.banned:
        pattern = phrase_pattern(rule.phrase)
        for where, text in (texts if pattern else ()):
            hit = _locate(pattern, text, where, rule.why or "never written")
            if hit:
                found.append(hit)
    for rule in writing.legacy_patterns:
        try:
            pattern = re.compile(rule.pattern if rule.regex else re.escape(rule.pattern), re.IGNORECASE)
        except re.error as exc:  # a rule that cannot be checked is a failed check, never a pass
            found.append(Match("", "", f"the rule {rule.pattern!r} isn't a valid pattern ({exc}), "
                                       f"so nothing was checked against it"))
            continue
        for where, text in texts:
            hit = _locate(pattern, text, where, rule.why or "never written")
            if hit:
                found.append(hit)
    for check in (BUILT_IN if built_in else ()):
        for where, text in texts:
            hit = _locate(check.pattern, text, where, f"checked automatically: {check.name}")
            if hit:
                found.append(hit)
    return found


def find_hits(writing, texts: list[tuple[str, str]], *, built_in: bool = True) -> list[str]:
    """`find_matches`, as the lines a check prints: each names where, what matched and which rule it was. The lines do
    not change with `built_in` left on."""
    return [m.says() for m in find_matches(writing, texts, built_in=built_in)]
