"""The turn-wait log: how long each writer waited for the file's one turn, to measure the write-wait target (p99 under
250 ms). Off unless `RESUMEBOT_TURN_LOG=1`; then each turn that ends appends one line to
`<data folder>/logs/turn-waits-<UTC date>.log`: `<UTC time> wait=<s> hold=<s>`, with `busy` in place of the hold
when the turn was refused, and ` at=<module:function:line>` (the code that took the turn) when it waited or held
over 1 s. A failing write is dropped. `python -m app.turn_log <file>...` summarises logs."""

import datetime
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path

from app import db
from app.settings import data_dir

VARIABLE = "RESUMEBOT_TURN_LOG"
#: A turn that waited or held longer than this (seconds) also names the place that took it: ` at=module:function:line`.
SLOW = 1.0


class TurnLog:
    def __init__(self, folder: Path, clock: Callable[[], datetime.datetime] | None = None) -> None:
        self.folder = folder
        self.folder_ready = False
        self.clock = clock or (lambda: datetime.datetime.now(datetime.UTC))

    def record(self, waited: float, held: float | None) -> None:
        try:
            now = self.clock()
            tail = "busy" if held is None else f"hold={held:.4f}"
            place = db.ending_place if (waited > SLOW or (held or 0.0) > SLOW) else None
            at = f" at={place}" if place else ""
            line = f"{now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z wait={waited:.4f} {tail}{at}\n"
            if not self.folder_ready:
                self.folder.mkdir(parents=True, exist_ok=True)
                self.folder_ready = True
            with open(self.folder / f"turn-waits-{now.date().isoformat()}.log", "a", encoding="utf-8") as out:
                out.write(line)
        except Exception:
            return


def install(folder: Path, clock: Callable[[], datetime.datetime] | None = None) -> Callable[[], None]:
    """Start logging to `folder`; the returned function stops it."""
    log = TurnLog(folder, clock)
    db.turn_ends.append(log.record)

    def stop() -> None:
        if log.record in db.turn_ends:
            db.turn_ends.remove(log.record)

    return stop


def install_from_environment() -> None:
    if os.environ.get(VARIABLE, "").strip() != "1":
        return
    folder = data_dir()
    if folder is not None:
        install(folder / "logs")


def summarise(lines: list[str]) -> dict[str, float | int]:
    waits: list[float] = []
    holds: list[float] = []
    busy = 0
    for line in lines:
        parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
        try:
            waits.append(float(parts["wait"]))
        except (KeyError, ValueError):
            continue
        if "busy" in line.split():
            busy += 1
        elif "hold" in parts:
            try:
                holds.append(float(parts["hold"]))
            except ValueError:
                pass
    waits.sort()

    def pick(q: float) -> float:
        return waits[min(len(waits) - 1, math.ceil(q * len(waits)) - 1)] if waits else 0.0

    out: dict[str, float | int] = {"count": len(waits), "p50": pick(0.5), "p99": pick(0.99),
                                   "max": waits[-1] if waits else 0.0,
                                   "over_1s": sum(1 for w in waits if w > 1.0), "busy": busy}
    if holds:
        out["max_hold"] = max(holds)
    return out


def main(files: list[str]) -> int:
    lines: list[str] = []
    for name in files:
        lines.extend(Path(name).read_text(encoding="utf-8").splitlines())
    result = summarise(lines)
    print(f"count {result['count']}")
    for key in ("p50", "p99", "max"):
        print(f"{key} wait {result[key]:.4f} s")
    print(f"waits over 1 s {result['over_1s']}")
    print(f"busy {result['busy']}")
    if "max_hold" in result:
        print(f"max hold {result['max_hold']:.4f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
