"""Tokens, and the never-list (docs/PRD.md §7 "Never, at any tier").

The app holds no secrets for an agent. No MCP tool has a password, key, or token field,
and every free-text field an agent can write through is run through
`find_secret` before it's stored. A passphrase has no shape to catch -- the
real defence is that no tool accepts one -- but a PEM block, a JWT, a known
API-key prefix, or a long high-entropy token is refused outright, so a
credential pasted into the wrong field (a note, a quote, a question answer)
doesn't end up sitting in the register in plain text.
"""

import hashlib
import math
import re
from typing import Any

from app.errors import DomainError


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- the never-list: structured secrets rejected in any free-text field ---

_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
# Common API-key / token prefixes from major providers and package hosts.
_KEY_PREFIXES = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9]{10,}"          # OpenAI/Anthropic-style secret keys
    r"|sk-ant-[A-Za-z0-9_-]{10,}"
    r"|ghp_[A-Za-z0-9]{20,}"        # GitHub personal access token
    r"|gho_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{12,}"           # AWS access key id
    r"|ASIA[0-9A-Z]{12,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"  # Slack tokens
    r"|AIza[0-9A-Za-z_-]{20,}"      # a cloud API key
    r"|npm_[A-Za-z0-9]{20,}"
    r")\b"
)
_HIGH_ENTROPY_TOKEN = re.compile(r"\b[A-Za-z0-9+/_=-]{40,}\b")
#: A hex key or digest (an MD5 or SHA, a 128-bit key): only sixteen symbols, so it can never reach the 4 bits a base64
#: token shows, and is judged on its own scale.
_HEX_KEY = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_WORDLIKE = re.compile(r"[A-Za-z][a-z]{0,23}|[A-Z]{2,24}|\d{1,8}")
#: The words inside one camelCase or PascalCase part ("hcmUI" is hcm and UI, "CandidateExperience" two words).
_CAMEL_PIECE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _is_wordlike(part: str) -> bool:
    """One part between separators is a word, an acronym, a number, or camelCase words run together. The pieces of a
    camelCase part must average three characters: a random mixed-case run splits into one- and two-character bits."""
    if _WORDLIKE.fullmatch(part):
        return True
    pieces = _CAMEL_PIECE.findall(part)
    if "".join(pieces) != part or not all(_WORDLIKE.fullmatch(x) for x in pieces):
        return False
    return len(part) >= 3 * len(pieces)


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_words(candidate: str) -> bool:
    """Whether a long run of characters is made of words, not a token: a path, a slug or an address
    ("en-US/External_Career_Site/job/Remote-USA/..."). Its many distinct letters score high on entropy, but split at
    its separators it is mostly short words, Capitalized words, acronyms and small numbers; a random token's pieces
    are not."""
    parts = [p for p in re.split(r"[/_=+-]+", candidate) if p]
    if not parts:
        return False
    return sum(len(p) for p in parts if _is_wordlike(p)) >= 0.7 * sum(len(p) for p in parts)


def _random_token(candidate: str) -> bool:
    return not _is_words(candidate) and _shannon_entropy(candidate) >= 4.0


def find_secret(text: str | None) -> str | None:
    """Returns a short reason a value looks like a structured secret, or
    None when it's clean. Checked against every agent-supplied free-text
    field before it's stored (never interpolated into a template first --
    the check runs on the raw value)."""
    if not text:
        return None
    if _PEM_BLOCK.search(text):
        return "looks like a PEM private key block"
    if _JWT.search(text):
        return "looks like a JWT"
    m = _KEY_PREFIXES.search(text)
    if m:
        return f"looks like an API key ({m.group(1)[:8]}...)"
    for candidate in _HEX_KEY.findall(text):
        # Hex tops out at 4.0 bits a character and a random run sits near 3.7, so it is judged on a lower bar: it
        # must still mix digits and letters and not repeat itself.
        if (any(c.isdigit() for c in candidate) and any(c.isalpha() for c in candidate)
                and _shannon_entropy(candidate) >= 3.0):
            return "looks like a long random token"
    for candidate in _HIGH_ENTROPY_TOKEN.findall(text):
        # Shannon entropy per character; random secrets sit well above ordinary prose or identifiers of the same
        # length (~4.0+ for a base64 alphabet), but a long path or slug of real words can too, so words are let by.
        if _random_token(candidate):
            return "looks like a long random token"
    return None


def redact_secrets(text: str) -> str:
    """`text` with anything `find_secret` would flag replaced by `[hidden]`: a private key block, a JWT, a key with
    a well-known prefix and a long random-looking token. For output a tool printed that a model is about to read
    (the Teacher's try of a draft tool), where refusing the whole thing would throw away the useful part."""
    if not text:
        return text
    out = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", "[hidden]", text)
    out = _JWT.sub("[hidden]", out)
    out = _KEY_PREFIXES.sub("[hidden]", out)
    return _HIGH_ENTROPY_TOKEN.sub(lambda m: "[hidden]" if _random_token(m.group(0)) else m.group(0), out)


#: A key (or a string value's name) that must never leave here, whatever a payload holds.
_SECRETISH = re.compile(
    r"secret|token|passw|passcode|api[_-]?key|authorization|credential|private[_-]?key|\bop://", re.I,
)
REMOVED = "[removed]"


def scrub(value: Any) -> Any:
    """`value` with anything secret-looking removed: a value under a key that names a secret, a token or a login, a
    vault reference wherever it appears, and a string that is itself a key, a token or a private key
    (`security.redact_secrets`). Payloads hold none by design; this is the second lock."""
    if isinstance(value, dict):
        return {k: (REMOVED if _SECRETISH.search(str(k)) else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return redact_secrets(re.sub(r"op://\S+", REMOVED, value))
    return value


class TooLong(DomainError):
    """An agent's text is longer than its field holds; the message says which field and by how much."""


class SecretRejected(DomainError):
    """Raised by a tool when an agent-supplied field fails `find_secret`.
    The MCP layer turns this into a refused tool result asking the agent to
    redact and resend, never a stored value."""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason} -- redact and resend")


def reject_secrets(**fields: str | None) -> None:
    """Call with every free-text field a tool is about to store. Raises
    `SecretRejected` on the first one that looks like a credential."""
    for name, value in fields.items():
        reason = find_secret(value)
        if reason:
            raise SecretRejected(name, reason)


def cap_length(text: str, field: str, limit: int = 2000) -> str:
    """Agent-supplied text is stored quoted and length-capped, never truncated silently past
    what the caller
    already agreed to -- callers pass the field's own limit (evidence.quote
    is 500 chars)."""
    if len(text) > limit:
        raise TooLong(f"{field} is too long ({len(text)} > {limit} chars)")
    return text
