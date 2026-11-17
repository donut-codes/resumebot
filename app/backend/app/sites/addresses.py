"""What makes an address carry a token (docs/PRD.md 30.3).

A job system names the employer by an id its pages show to every visitor (a client key of 32 hex characters, a client id
that is a UUID). Such an id in the path of an address is not a secret, though `find_secret` takes it for one. So an
address is judged by its user name and password and by its query, with public ids taken out; free text keeps the
whole-string `find_secret`. A leaf module: it imports only `app.agents.security`.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.agents.security import SecretRejected, find_secret

#: A job system names the employer by an id its pages show to everyone (a client key of 32 hex characters, a
#: client id that is a UUID): not the secret `find_secret` takes such a string for.
PUBLIC_ID = re.compile(r"\b(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\b")

#: The keys of a recipe whose value is an address.
ADDRESS_KEYS = frozenset({"url", "url_prefix", "start_url", "recommended_url", "url_template"})


def query_secret(query: str) -> str | None:
    """Why `query` carries a token or key, or None. Public ids are taken out first."""
    return find_secret(PUBLIC_ID.sub("id", query))


def address_secret(address: str) -> str | None:
    """Why `address` carries a password, token or key, or None: a user name or password, or a token in the query. The
    path is not held: a public id in it is no token."""
    try:
        parsed = urlsplit(address)
        user = parsed.username or parsed.password
    except ValueError:
        return None  # not an address; `public_url` refuses it
    if user:
        return "a user name or password"
    return query_secret(parsed.query)


def reject_recipe_secrets(recipe) -> None:
    """Raise `SecretRejected("recipe", reason)` when a string in `recipe` carries a token or key. A value under an
    address key is judged as an address; every other string keeps the whole-string check."""
    if reason := _recipe_secret(recipe, None):
        raise SecretRejected("recipe", reason)


def _recipe_secret(node, key: str | None) -> str | None:
    if isinstance(node, dict):
        return next((r for k, v in node.items() if (r := _recipe_secret(v, str(k)))), None)
    if isinstance(node, (list, tuple)):
        return next((r for v in node if (r := _recipe_secret(v, key))), None)
    if isinstance(node, str):
        return address_secret(node) if key in ADDRESS_KEYS else find_secret(node)
    return None if node is None or isinstance(node, (bool, int, float)) else find_secret(str(node))
