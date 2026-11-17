"""The mail providers the owner can connect (`providers.yaml`): each one's inbox address and the steps for an app
password. Data, not code: nothing here names a provider."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

FILE = Path(__file__).with_name("providers.yaml")


@dataclass(frozen=True)
class Provider:
    name: str
    match: tuple[str, ...]
    host: str
    port: int
    strip_spaces: bool
    steps: tuple[str, ...]

    def view(self) -> dict:
        return {"name": self.name, "match": list(self.match), "host": self.host, "port": self.port,
                "steps": list(self.steps)}


def _split(host: str) -> tuple[str, int]:
    name, _, port = host.partition(":")
    return name, int(port) if port.isdigit() else 993


def load() -> list[Provider]:
    """The providers in the file's order; the last, with no `match`, is the one for any other."""
    raw = yaml.safe_load(FILE.read_text(encoding="utf-8")) or {}
    out: list[Provider] = []
    for entry in raw.get("providers", []):
        host, port = _split(str(entry.get("host") or ""))
        out.append(Provider(
            name=str(entry["name"]), match=tuple(str(m).lower() for m in entry.get("match") or ()), host=host,
            port=port, strip_spaces=bool(entry.get("strip_spaces")),
            steps=tuple(str(s) for s in entry.get("steps") or ())))
    return out


def for_address(address: str) -> Provider | None:
    """The provider a mail address belongs to (by its domain), or None for any other."""
    domain = address.strip().lower().rpartition("@")[2]
    if not domain:
        return None
    return next((p for p in load() if domain in p.match), None)
