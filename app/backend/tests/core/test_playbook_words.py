"""The playbooks and the scout's instruction use today's words (docs/PRD.md 31.1; the title pass and the readers were
retired): a kept role is stored in Landing Zone, the screen is Notifications, and the scout has no reader
hand-out. The readers (the mail reader, the browser reader, a job system with no reader, a job board's guest-feed
reader) are allowed."""

import re
from pathlib import Path

from app.config import settings

RETIRED = re.compile(
    r"\b(?:in|as|past|to|from|before|waiting in) Found\b|\bFound (?:role|roles|stage)\b"
    r"|drafting bar|packets mission|Ready to Apply|\bInbox\b|Read the packet"
    r"|reader's answers|reader hand-out|small readers|\ba reader\b")

SCOUT_INSTRUCTION = Path(settings.instructions_path) / "jobsearch.scout.instructions.md"


def test_no_playbook_or_scout_instruction_text_uses_a_retired_word():
    files = sorted(Path(settings.playbooks_path).rglob("*.bpmn")) + [SCOUT_INSTRUCTION]
    assert len(files) > 10
    hits = []
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits += [f"{path.name}:{n}: {m.group(0)}" for m in RETIRED.finditer(line)]
    assert hits == []
