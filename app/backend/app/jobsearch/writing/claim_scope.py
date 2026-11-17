"""Two free code checks on the writer's drafted lines, both plain word comparison with no model behind them.

**Scope inflation** (`inflation`): a drafted line claims more than the master lines it cites. It reads three things the
cited lines (and their notes) do not hold: a number (a numeral or a number word), a breadth word ("whole", "every",
"company-wide") and a result or a stronger part in the work ("cut" where the line only aimed to, "led" where it only
supported). Plain rewording is not flagged: a result verb is compared by family (`RESULT_FAMILIES`: "cut", "reduced"
and "lowered" are one family), a leading verb likewise (`LEAD`: "led", "directed", "headed"). It is a heuristic over
words, so a legitimate rewording it has no entry for is flagged too: the packet check that uses it is a warning the
reviewer reads, never a block.

**Repeated descriptor** (`repeats`): the resume prints a role's descriptor from the master under the title, so a scope
line or bullet that only says the same again ("Three-month consulting engagement." under "Consulting engagement") prints
it twice.

Generic English only: no name, employer, place or figure of anyone's.
"""

import re

INFLATION_CHECK = "No line claims more than the master lines it cites"
REPEAT_CHECK = "No line repeats the role's descriptor"

NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "hundred": "100", "thousand": "1000", "million": "1000000", "dozen": "12",
    "half": "half", "double": "double", "twice": "double", "triple": "triple",
}
#: Words that widen who or how much a line covers. "only" and "always" are left out: they narrow, or are idiom.
BREADTH = frozenset({
    "all", "every", "whole", "entire", "everyone", "everybody", "everything", "firmwide", "companywide",
    "organizationwide", "orgwide", "enterprisewide", "statewide", "nationwide", "worldwide", "global", "globally",
})
#: Each family is the stems of verbs that mean one result; a line may use any word of the family the source uses.
RESULT_FAMILIES: dict[str, tuple[str, ...]] = {
    "reduce": ("cut", "reduc", "lower", "shorten", "decreas", "trim", "slash", "shrank", "shrunk", "curb"),
    "increase": ("increas", "grew", "grow", "rais", "boost", "improv", "expand", "lift", "enhanc"),
    "end": ("ended", "elimin", "stopp", "remov", "retir", "discontinu"),
    "save": ("saved", "saving", "recover", "reclaim"),
    "prevent": ("prevent", "avert", "avoid"),
    "speed": ("accelerat", "speed", "faster", "quicken", "expedit"),
    "multiply": ("doubl", "tripl", "halv", "quadrupl"),
}
#: Verbs for running the work, against verbs for taking part in it.
LEAD = ("led", "lead", "direct", "head", "ran", "run", "manag", "oversaw", "oversee", "owned", "spearhead", "chair")
SUPPORT = ("support", "assist", "help", "contribut", "particip", "join", "aid")

_WORD = re.compile(r"[a-z]+(?:-[a-z]+)*|\d[\d,]*(?:\.\d+)?%?")
_STOP = frozenset("a an the of and or to for in on at by with from as is was were be been this that it its".split())


def _parts(text: str) -> list[str]:
    """The words of `text`, hyphenated ones split ("three-month" is "three", "month")."""
    return [p for w in _WORD.findall(text.lower()) for p in w.split("-")]


def _is(word: str, stem: str) -> bool:
    """Is `word` the verb or noun of `stem` (a stem of five letters or more matches as a prefix: "reduc", "reduced")."""
    return (word == stem or (len(stem) >= 5 and word.startswith(stem))
            or word in (stem + "s", stem + "ed", stem + "ing"))


def _numbers(text: str) -> set[str]:
    out: set[str] = set()
    for w in _parts(text):
        if w[0].isdigit():
            out.add(w.replace(",", "").rstrip("%"))
        elif w in NUMBER_WORDS:
            out.add(NUMBER_WORDS[w])
    return out


def _breadth(text: str) -> set[str]:
    """Breadth words, a hyphenated one ("company-wide") counted as written."""
    joined = {w.replace("-", "") for w in _WORD.findall(text.lower())}
    return joined & BREADTH


#: Words that make a clause an aim, not a result ("aimed to cut", "the goal was to reduce", "planned a pilot").
AIM = ("aim", "goal", "plan", "intend", "propos", "pilot", "hope", "seek", "sought", "tried", "attempt", "target",
       "objective", "prototyp", "explor")


def _delivered(source: str) -> str:
    """`source` without its aim clauses (split at punctuation): a result named only as an aim was not delivered."""
    return " ".join(c for c in re.split(r"[;,.:]", source) if not _has(c, AIM))


def _families(text: str) -> set[str]:
    words = _parts(text)
    return {name for name, stems in RESULT_FAMILIES.items() if any(_is(w, s) for w in words for s in stems)}


def _has(text: str, stems: tuple[str, ...]) -> bool:
    return any(_is(w, s) for w in _parts(text) for s in stems)


def inflation(text: str, source: str) -> list[str]:
    """What `text` claims that `source` (the cited lines and their notes, joined) does not: a number, a breadth word,
    a result, a stronger part in the work. Empty when there is no source to compare to."""
    if not source.strip():
        return []
    problems = [f"the number {n} isn't in the cited lines" for n in sorted(_numbers(text) - _numbers(source))]
    problems += [f"the word {w!r} claims a wider scope than the cited lines"
                 for w in sorted(_breadth(text) - _breadth(source))]
    problems += [f"a result ({f}) the cited lines don't state"
                 for f in sorted(_families(text) - _families(_delivered(source)))]
    if _has(text, LEAD) and not _has(source, LEAD) and _has(source, SUPPORT):
        problems.append("leads the work where the cited lines only support it")
    return problems


def _content(text: str) -> set[str]:
    out = set()
    for w in _parts(text):
        if w in _STOP:
            continue
        w = NUMBER_WORDS.get(w, w)
        out.add(re.sub(r"(ing|ed|es|s)$", "", w) if len(w) > 4 else w)
    return out


def repeats(descriptor: str, line: str) -> bool:
    """Does `line` say no more than `descriptor` already prints: every word of the descriptor is in it, and it adds at
    most two words of its own ("Consulting engagement" and "Three-month consulting engagement.")."""
    want, got = _content(descriptor), _content(line)
    return bool(want) and want <= got and len(got) <= len(want) + 2
