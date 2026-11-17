"""The known site terms list (docs/PRD.md 55.10): sites whose published terms of service were read, shipped as
`site_terms.json`. Where to look shows one warning line on a site whose terms forbid automated tools, with a link to
its terms page. The list only informs: nothing here blocks, switches off or changes anything, and it stores nothing.

Each entry is `{name, hosts, terms_url, date_read, quote, verdict}`: `verdict` is `allows`, `silent` or `forbids`;
`quote` is the evidence, the terms' own words, and is never shown. The page matches an address against `hosts` itself
(a host and its subdomains), so `forbidding()` hands it only what it needs. Separate from `ats.NOT_OFFERED`, which
hides a job system and stops its reading; this list does neither."""

import json
from functools import cache
from importlib import resources
from urllib.parse import urlsplit

VERDICTS = ("allows", "silent", "forbids")


def _check(entry: dict) -> None:
    name = entry.get("name") or "?"
    if entry.get("verdict") not in VERDICTS:
        raise ValueError(f"site terms: {name}: the verdict must be one of {VERDICTS}")
    if urlsplit(str(entry.get("terms_url"))).scheme != "https":
        raise ValueError(f"site terms: {name}: the terms address must be https")
    if not entry.get("hosts"):
        raise ValueError(f"site terms: {name}: no hosts")
    if entry["verdict"] != "silent" and not str(entry.get("quote") or "").strip():
        raise ValueError(f"site terms: {name}: a {entry['verdict']} entry needs its quote")


@cache
def _load() -> tuple[dict, ...]:
    text = resources.files("app.sites").joinpath("site_terms.json").read_text(encoding="utf-8")
    entries = json.loads(text)
    for entry in entries:
        _check(entry)
    return tuple(entries)


def forbidding() -> list[dict]:
    """The sites whose terms forbid automated tools, as `[{hosts, terms_url}]`."""
    return [{"hosts": list(e["hosts"]), "terms_url": e["terms_url"]} for e in _load() if e["verdict"] == "forbids"]
