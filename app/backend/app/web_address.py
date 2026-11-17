"""Web addresses that are shown as links. A job system, the scout or the analyst may hand the app any text as
an address, and a link built from it must never run a script. Written for this app."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

#: A web address written without its scheme, as a person writes it: a host name with at least one dot and a letters-only
#: last part ("example.com", "www.example.com/careers", "example.com:8443/x"), then nothing but a port, a path, a query
#: or a fragment. No scheme, no `@` (an e-mail address), no space: those are not bare web addresses.
_BARE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]*[A-Za-z0-9](?::\d{1,5})?(?:[/?#]\S*)?")


def http_url(value: Any) -> str | None:
    """`value` as a web address, or None. Only `http` and `https` addresses with a host are kept, so a link built from
    it can never be `javascript:`, `data:`, `file:` or a bare path. A bare domain ("example.com") becomes
    `https://example.com`: that is how a person writes a website. Surrounding space is trimmed; an address with a
    control character inside it is not one."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or any(ord(c) < 32 or ord(c) == 127 for c in text):
        return None
    if _BARE.fullmatch(text):
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        return None
    return text if parts.scheme.lower() in ("http", "https") and parts.hostname else None
