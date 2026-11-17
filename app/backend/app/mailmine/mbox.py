"""Read an `.mbox` export (the file a mail export gives) as a stream: one message at a time, never the whole file in
memory, and only the first `MAX_MESSAGE_BYTES` of each message (the rest is attachments)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO

from app.mailmine.message import MAX_MESSAGE_BYTES

_FROM = b"From "
#: A line is read in pieces of at most this many bytes, so one endless line (an attachment with no breaks) is cut too.
CHUNK = 65536


def iter_messages(fileobj: BinaryIO, limit: int = MAX_MESSAGE_BYTES) -> Iterator[bytes]:
    """The bytes of each message in the file (without its `From ` separator line), cut at `limit` bytes. A message
    starts at a line beginning `From ` that follows a blank line (or the start of the file); a line of a body that
    begins that way is escaped as `>From ` by the exporter."""
    current: list[bytes] = []
    size = 0
    previous_blank = True
    started = False
    while line := fileobj.readline(CHUNK):
        if previous_blank and line.startswith(_FROM):
            if started and current:
                yield b"".join(current)
            current, size, started = [], 0, True
            previous_blank = False
            continue
        previous_blank = line in (b"\n", b"\r\n")
        if not started:
            # Text before the first separator is not a message, unless the file has no separators at all.
            started = True
        if size < limit:
            current.append(line)
            size += len(line)
    if started and current:
        yield b"".join(current)
