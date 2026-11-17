"""The word tests that spare the cleaner's patterns.

The cleaner and the readers it feeds ask a dozen ignore-case patterns of every sentence of a posting, and almost no
sentence matches one. Every match of such a pattern holds one of a few plain words (its stems: "year" for the years a
posting asks, "clearance" or "secret" or "trust" for a clearance), so a reader asks the sentence, lowered, whether it
holds one of them (`stem in folded`) before it asks the pattern, which costs a fraction as much. The word test only ever
says "this pattern cannot match here". It never says a pattern does, so the pattern stays the rule: a sentence that
passes is still asked it, and what a reader gives does not change.

That holds where `str.lower` and the engine's ignore-case matching agree on every letter a stem is written in. A few
characters are folded onto an ASCII letter by the engine and not left as that letter by `str.lower` (`UNFOLDED`); a
text with one is never word-tested: `folded` gives None and the reader asks the pattern.
`tests/jobsearch/test_cleaner_word_tests.py` checks that over every character, and holds each reader to the answer its
patterns alone give. A pattern that gains an alternative needs its stem beside it, and an example there.

A leaf: it imports nothing, so the cleaner, the readers and `whole` can all use it."""

#: U+0130 (a dotted capital I, which lowers to two characters), U+0131 (a dotless i) and U+017F (a long s): what an
#: ignore-case search takes for "i" and "s" and `str.lower` does not leave as one. The Kelvin sign (U+212A) is not
#: here: it lowers to "k", as the engine takes it.
UNFOLDED = "".join(chr(code) for code in (0x130, 0x131, 0x17F))


def folded(text: str) -> str | None:
    """The text lowered, for asking whether it holds a stem (`stem in folded(text)`), or None when it holds a character
    of `UNFOLDED` and only its pattern can say whether it matches."""
    if text.isascii() or not any(char in text for char in UNFOLDED):
        return text.lower()
    return None
