"""What one reply to an agent may hold.

Every agent here is a Claude model on the owner's subscription, with a window of 200,000 tokens. So the owner's own
text, their master resume, writing rules and profile, goes to the agent whole: nothing is halved, dropped or cut
to fit. A reply is refused, loudly and with the reason, only when it passes `REPLY_CEILING_CHARS`
(`jobsearch/work/door.py`).

A list of items that can wait for the next call (the role check, a site task and
its lessons) is a different thing: it is sent in batches of up to `AGENT_PAYLOAD_BUDGET_CHARS` characters, as many items
as fit, the rest on the next call. That is a batch size, kept because one reply judging many items judges them worse; it
cuts no text, and it is not applied to the writer's brief.
"""

import json
from collections.abc import Callable
from typing import Any

#: What Claude Code (2.1.197, in the scout image) shows a session of one reply from an MCP tool that does not carry the
#: to a file and only a short preview is shown, and none of the app's sessions has a tool to read a file: a session
#: never sees a reply that long.
ALWAYS_SHOWN_CHARS = 50_000

#: The most characters any one reply to an agent may hold, whatever is in it. A Claude session's window is 200,000
#: tokens; 200,000 characters is about 50,000 tokens, a quarter of it, and a long set of writing rules and a long
#: master resume go well inside. Past it a reply is refused with the reason
#: (`door.ReplyTooLarge`), never cut: a reply that big is a bug, not something to send half of. Defined once, here; the
#: tests assert against it, and the scout's `MAX_MCP_OUTPUT_TOKENS` (scout/scout.mjs) is sized from it.
REPLY_CEILING_CHARS = 200_000

#: Claude Code's own largest per-tool result size, in characters. A tool declares it in its definition so that a reply
#: up to it is shown whole, not saved to a file (`ALWAYS_SHOWN_CHARS` without it). It is Claude Code's maximum, so the
#: app's `REPLY_CEILING_CHARS` is the limit that binds, with room for what the wire adds to the characters counted.
RESULT_SIZE_META = {"anthropic/maxResultSizeChars": 500_000}

#: The most characters (serialized JSON) one batch of items for an agent may hold: the role check, a site task and the
#: lessons it points to. 30,000 characters is ~7,500 tokens, so a batch is judged with care. It is a batch size, and it
#: never cuts the owner's own text, which goes whole (see above). It is not a limit on one reply either: a site step
#: (the plan to write, an item, Act) is handed out whatever its size, up to the ceiling (`sites/agent_handout.py`).
#: Defined once, here; the tests assert against this constant.
AGENT_PAYLOAD_BUDGET_CHARS = 30_000

#: What a task reply's own wrapper (`ok`, task id/key/title/group/action,
#: plan_version, mode, a long title) is allowed to take before the task's
#: `data`/`context` is sized against the budget.
TASK_ENVELOPE_RESERVE_CHARS = 1_200


def payload_chars(value: Any) -> int:
    """The serialized size of a payload, the way an MCP/REST reply carries it."""
    return len(json.dumps(value, default=str, ensure_ascii=False))


def fit_rows(rows: list[Any], budget: int, render: Callable[[Any], dict]) -> tuple[list[dict], list[Any]]:
    """Render rows in order while their serialized size stays within
    `budget` characters; returns (rendered, rows left over). Always renders
    at least one row when there is any, so a single large row never turns
    into "nothing"."""
    out: list[dict] = []
    used = 0
    for index, row in enumerate(rows):
        rendered = render(row)
        size = payload_chars(rendered) + 1
        if out and used + size > budget:
            return out, rows[index:]
        out.append(rendered)
        used += size
    return out, []
