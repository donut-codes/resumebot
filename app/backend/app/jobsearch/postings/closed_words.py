"""The words a page uses when the posting it was meant to show is closed or missing: one list, for every check that
reads a page for that (Go's checks in code and in the app's Chrome, `go_checks.closed_words`; the code-first browser
worker's removed listing, `browser_code.page_state`).

It is a quick extra signal beside Go's main check, which compares the page with the posting held
(`posting_match.py`). Add a phrase here, never a second list somewhere else. Each entry is a regular expression, matched
without regard to case against the page's visible text with curly quotes made straight and spaces squeezed.

`NOT_AVAILABLE` is the one form that is matched a line at a time (`lines`), because it means closed only as a whole
sentence about the posting: "This opportunity is currently not available." (the notice a job system's careers page draws
for a closed posting, at the posting's own address). The posting's noun is the subject, at the start of
a sentence, and the sentence ends there (a full stop, or the end of the text), so "Sponsorship for this position is not
available." and "This position is not available to candidates outside the country." are not it. A line break in the
page's text is no sentence end by itself (markup wraps a sentence, and a <br> breaks one): a line starts the sentence
only with a capital letter, and a line's end ends it only when the next line starts with one, so the same two sentences
broken across lines are not it either.

A phrase is the page saying its posting is closed only as the statement of a sentence, never as the condition of one
(`CONDITION`: open roles must not be ended by their own postings' "Applications are accepted until the
position is filled"). One guard stands over every phrase of the list, so no phrase is rewritten and a new one is
covered the day it is added: a hit is dropped when a condition word stands before it in its own clause ("until the
position is filled", "or until the job is no longer posted", "if the role is filled before the closing date", "the
level at which the position is filled", "to ensure the role is filled"). The clause starts at the sentence's start (a
full stop, or a line end followed by a capital, as above) or at a comma, semicolon, colon, bracket or dash, so the
condition word of another sentence or clause excuses nothing: "Apply until Friday. This position has been filled." and
"If you came here to apply, this job has been filled." are closed. A comma inside a date, or before an "or" that
carries the condition on ("until 3 March, 2031, or the position is filled"), starts no clause. Nor does a colon between
digits ("until 5:00 PM") or a full stop that ends no sentence: one with a small letter, a digit, a lone capital and
its own full stop, or a time zone after it ("until Jan. 5", "until 5 p.m. E.T. or", "until 5 p.m. ET or").

A phrase joined to what stands before it by "or" ("..., or the position is filled") shares that condition wherever in
the sentence it stands, so its clause is the whole sentence (`SENTENCE_START`): "until 3 March (11:59 PM) or the
position is filled" and "until the closing date, Friday 3 March, or the position is filled" close nothing."""

import re

CLOSED_PHRASES: tuple[str, ...] = (
    # The posting no longer takes applications.
    r"(?:no longer|not currently) (?:accepting|taking) (?:any )?(?:new )?(?:job )?applications",
    r"applications? (?:are |is )?(?:now )?closed",
    r"(?:position|job|role|vacancy|opportunity) has been filled",
    r"no longer being accepted",
    r"(?:position|job|role|vacancy) you (?:are|were) (?:trying to )?appl(?:y|ying) (?:to|for) (?:has been|is) "
    r"(?:filled|closed|no longer)",
    # The posting is closed, expired or removed.
    r"(?:job|position|posting|role|vacancy|opening|requisition|opportunity) (?:is no longer (?:available|open|active|"
    r"posted)|"
    r"has (?:been )?(?:closed|filled|expired|removed|taken down))",
    r"(?:this|the) (?:job|position|posting|role|opening|opportunity) (?:is|has been) (?:no longer available|removed|"
    r"closed|filled|expired)",
    r"(?:job|posting|listing) (?:posting )?(?:has )?expired",
    r"(?:job|posting|position|opportunity) (?:is|has) closed",
    r"\bno longer available\b",
    r"(?:job|position|posting|page|opportunity) (?:you (?:are|were) looking for )?no longer exists",
    # The page, or the job, is not there.
    r"(?:page|job|position|posting|opportunity) (?:that )?you(?: are|'re| were)? (?:looking for|trying to (?:reach|"
    r"view|find)|requested) (?:does ?n't|does not|did ?n't|could ?n't|can ?not|cannot|could not|is no longer|was not|"
    r"was ?n't|is not|may no longer)",
    r"\bpage not found\b",
    r"\b(?:job|position|posting|requisition|opportunity) (?:was )?not found\b",
    r"we (?:could ?n't|could not|can ?not|cannot|ca ?n't) find (?:the |this |that )?(?:job|position|posting|page|"
    r"opportunity)",
)

#: The posting itself "is (currently) not available" or "is unavailable", as a whole sentence (the module's note): an
#: optional "Sorry," before it, the words kept as `said`, and nothing after them but the sentence's end. The capital
#: letters that let a line start or end the sentence are read as written (`(?-i:...)`), whatever the rest ignores.
NOT_AVAILABLE = (
    r"(?:^(?=(?-i:[A-Z]))|(?<=[.!?] ))(?:(?:we(?:'re| are) )?sorry[,.!]? (?:but )?|unfortunately,? )?"
    r"(?P<said>(?:this|the) (?:job|position|posting|role|vacancy|opening|requisition|opportunity) "
    r"(?:is (?:currently |presently )?(?:not available|unavailable)|is not (?:currently|presently) available|"
    r"(?:currently|presently) (?:not available|unavailable)))"
    r" ?(?:[.!]|\Z|\n(?=(?-i:[A-Z])))"
)

#: The words that make what follows them a condition, not a statement (the module's note). "while" and "should" are left
#: out on purpose: "While this position has been filled, ..." and "You should know this job has expired." are closed.
CONDITION = (
    r"(?<![\w'])(?:until|till|'til|once|when|whenever|if|unless|before|after|in case|in the event|as soon as|whether|"
    r"(?:at|in|by|on) which|ensur(?:e|es|ed|ing)|(?:make|makes|making|made) sure|so that)\b"
)
#: Where a sentence starts: after a sentence's end (a full stop only when it ends one: not before a small letter, a
#: digit, a lone capital with its own full stop, or a time zone's letters), a line end that ends a sentence (the only
#: line ends `_sentences` keeps), or a semicolon.
SENTENCE_START = r"\. (?![a-z\d]|[A-Z]\.|[A-Z]{1,2}T\b)|[!?] |\n|;"
#: Where a clause starts: where a sentence does, or at a comma (not a date's, nor one before "or"), a colon (not a
#: time's), a bracket or a dash.
CLAUSE_START = SENTENCE_START + r"|(?<!\d):(?!\d)|[()]|, (?!\d|or )| [-–—] "
#: The end of what stands before a phrase that an "or" joins to the words before it: "or", then "the", if any.
JOINED = r"(?<![\w'])or (?:(?:the|this|that) )?$"
_LOOK_BACK = 300  # characters before a hit that are searched for its clause's start: longer than any clause

_PATTERN = re.compile("|".join(f"(?:{p})" for p in CLOSED_PHRASES), re.I)
_SENTENCE = re.compile(NOT_AVAILABLE, re.I | re.M)
_CONDITION = re.compile(CONDITION, re.I)
_CLAUSE_START = re.compile(CLAUSE_START)
_SENTENCE_START = re.compile(SENTENCE_START)
_JOINED = re.compile(JOINED, re.I)
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‛": "'", "′": "'"})


def plain(text: str | None) -> str:
    """The text as the phrases are matched against: curly quotes straight, runs of white space one space."""
    return re.sub(r"\s+", " ", (text or "").translate(_QUOTES))


def lines(text: str | None) -> str:
    """The text as `NOT_AVAILABLE` is matched against: as `plain`, with each line kept a line of its own."""
    return "\n".join(kept for line in (text or "").splitlines() if (kept := plain(line).strip()))


def _sentences(text: str | None) -> str:
    """The text as `lines`, with only the line ends that end a sentence kept (the next line starts with a capital)."""
    return re.sub(r"\n(?![A-Z])", " ", lines(text))


def _conditional(sentences: str, at: int) -> bool:
    """Whether the phrase that starts at `at` is the condition of its clause, not the page's statement (`CONDITION`)."""
    before = sentences[max(0, at - _LOOK_BACK): at]
    start = _SENTENCE_START if _JOINED.search(before) else _CLAUSE_START  # an "or" carries the sentence's condition on
    clause = start.split(before)[-1]
    return _CONDITION.search(clause) is not None


def found(text: str | None) -> str | None:
    """The first closed or missing phrase `text` states (not one inside a condition), as the page wrote it, or None."""
    sentences = _sentences(text)
    for hit in _PATTERN.finditer(sentences.replace("\n", " ")):  # as `plain`: a phrase may run over a line end
        if not _conditional(sentences, hit.start()):
            return hit.group(0)
    said = _SENTENCE.search(lines(text))
    return said.group("said") if said else None
