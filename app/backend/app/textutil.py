"""Small text helpers every module used to copy: whitespace folded, one line held to a length, the first
letter's case. Pure functions, no imports from the rest of the app (so anything may use them)."""

from __future__ import annotations

from typing import Any


def squash(text: Any) -> str:
    """`text` with every run of whitespace (newlines included) folded to one space, trimmed at both ends.
    None and other empty values give "", anything else is read as its string."""
    return " ".join(str(text or "").split())


def cut_at_word(text: str, limit: int) -> str:
    """`text` held to `limit` characters, cut between words with an ellipsis, never in the middle of one.
    Only a run with no break in most of its length (a long path, a code, a line of one endless word) is cut
    mid-word, since backing off to the last space would leave next to nothing."""
    if len(text) <= limit:
        return text
    room = max(limit - 1, 0)
    head = text[:room]
    if text[room : room + 1].isspace():
        return head.rstrip(" ,;:-–—") + "…"  # the cut falls exactly on a word's end
    at = head.rfind(" ")
    if at >= room * 0.4:
        head = head[:at]
    return head.rstrip(" ,;:-–—") + "…"


def one_line(text: Any, limit: int) -> str:
    """`text` on one line, at most `limit` characters (an ellipsis marks a cut, always between words)."""
    return cut_at_word(squash(text), limit)


def ellipsize(text: str, limit: int) -> str:
    """`text` held to `limit` characters by a plain cut (not at a word) with an ellipsis; short text is kept."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clip(text: str | None, limit: int) -> str | None:
    """`text` trimmed and cut at `limit` characters, with no ellipsis and its line breaks kept; None when
    nothing is left."""
    text = (text or "").strip()
    return text[:limit] if text else None


def upper_first(text: str) -> str:
    """`text` with a capital first letter; the rest is left as written."""
    return text[:1].upper() + text[1:]


def lower_first(text: str, *, keep_acronym: bool = False) -> str:
    """`text` with a lowercase first letter, so it reads after other words; the rest is left as written.
    With `keep_acronym`, a start like "DNS ..." (a second capital) is kept as it is."""
    if keep_acronym and len(text) > 1 and text[1].isupper():
        return text
    return text[:1].lower() + text[1:]
