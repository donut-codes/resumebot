"""The room an agent leaves for the owner's own text.

There is no character limit on what the owner types (the owner, 2026-10-14), so no box stops typing and no route cuts or
refuses text for its length. One thing can still refuse a text: an agent cannot be sent more than
`REPLY_CEILING_CHARS` (`planengine/budget.py`) at once. A text the owner writes for an agent to read whole, that would
not fit with the owner-written text sent beside it in the same reply, is refused when it is saved or sent, in words,
with its size, before anything is saved. It is never cut.

`ROOM_CHARS` is what a reply leaves for the owner-written texts in it; the rest of the ceiling (`RESERVE_CHARS`) is for
what else a reply holds. A text is checked when it is saved or sent, at the size now saved of the text that rides with
it (`beside`). The door (`jobsearch/work/door.py`) still measures the real reply and refuses one over the ceiling with
its
size, so a miss in this reserve refuses; it never cuts.

Only a multi-line box whose text an agent reads whole is checked here (a pasted resume, a file of instructions, what
the owner did in Find Gaps, an answer to a site's question, a packet's brief). A one-line box (a reason, a name, a
title) has no check. Services flush; nothing here writes.
"""

from app.errors import TooLarge
from app.harness import wording
from app.planengine.budget import AGENT_PAYLOAD_BUDGET_CHARS, REPLY_CEILING_CHARS

#: What a reply keeps for everything that is not owner-written text: the most one batch of items adds to a reply
#: (`AGENT_PAYLOAD_BUDGET_CHARS`, which is also what one posting at its kept size adds), and 10,000 for the task's
#: envelope, the role's facts, the analyst's findings, the owner's note and what escaping adds to prose. The agent's
#: instructions and its answer shape are not in the reserve: they are measured, as the text `beside` another.
RESERVE_CHARS = AGENT_PAYLOAD_BUDGET_CHARS + 10_000

#: What one reply leaves for the owner-written texts in it.
ROOM_CHARS = REPLY_CEILING_CHARS - RESERVE_CHARS

#: The default `result` of a refusal: what became of the text.
NOT_SENT = "it was not sent. Nothing was cut and nothing was saved."


class TextTooLarge(TooLarge):
    """An owner text, with what rides beside it, that an agent could not be sent whole. Answered 413 with the sentence
    as `detail`; nothing is saved."""


def too_large_words(
    what: str,
    size: int,
    *,
    beside: tuple[str, int] | None = None,
    room: int = ROOM_CHARS,
    result: str = NOT_SENT,
    fix: str = "Shorten it.",
) -> str:
    """The sentence for a text that does not fit. `what` is the text in the owner's words ("Your resume text"); `size`
    its characters; `beside` the name and size of the text sent with it ("the file sent with it", 20,000), which makes
    the sentence name both sizes and say "of the two"; `room` the most one reply holds for them; `result` what became of
    it, and `fix` what to do."""
    values = {"what": what, "size": f"{size:,}", "room": f"{room:,}", "result": result, "fix": fix}
    if beside is None:
        return wording.say("owner_text", "too_large_alone", **values)
    name, other = beside
    return wording.say("owner_text", "too_large_beside", other_name=name, other_size=f"{other:,}", **values)


def refuse_over_room(
    text: str,
    what: str,
    *,
    beside: tuple[str, int] | None = None,
    room: int = ROOM_CHARS,
    result: str = NOT_SENT,
    fix: str = "Shorten it.",
) -> None:
    """Raise `TextTooLarge` when `text`, with the `beside` text that rides in the same reply, passes `room`. It checks
    and never changes: the caller saves or sends the text as it is, or nothing."""
    size = len(text)
    if size + (beside[1] if beside else 0) > room:
        raise TextTooLarge(too_large_words(what, size, beside=beside, room=room, result=result, fix=fix))
