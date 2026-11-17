"""The front door of every HTTP call to the app: which Host it may be addressed by, and which page may
change something.

Two checks, both before any route runs, on the page itself (`web.py` serves it from behind this guard), the API, the
export, the page's calls and both MCP doors alike:

- **Host.** A name an attacker owns can be pointed at this machine after the page loaded (DNS rebinding), and the page
  could then read the owner's documents as if it were the owner's own. So a call is answered only when its Host is
  this machine's own kind of name: localhost, an IP address, a `.local` or `.lan` name, or a name the install lists
  (`JOBSEARCH_ALLOWED_HOSTS`, the MCP door's `JOBSEARCH_MCP_ALLOWED_HOSTS` and the public address). The port is not
  part of the check: a browser sends it (`localhost:8430`), a proxy may drop it.
- **Origin.** A page on another site can make the owner's browser send a POST that carries no body (start Claude work,
  read all, run a mission). A browser says where such a call came from; a change (anything but GET, HEAD and OPTIONS)
  whose Origin is another host than the one it was sent to, or which the browser marks cross-site, is refused. Calls
  from the scout, curl and the owner's computer carry no Origin and are judged by their token as before.

Hosts are read on every call from the settings, so a test or an install can change them without a restart.

One more job, because this is the outermost layer the app owns: a client that hangs up while the app is still reading
its body (`ClientDisconnect`, raised by Starlette, by a route that reads the body and by the MCP library alike) is the
end of that call, not an error. Left to travel up, it reached the server's error log as a traceback for a call nobody
was waiting on.
"""

import ipaddress
import json
import logging
import time
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from starlette.requests import ClientDisconnect

from app.config import public_url_hosts
from app.settings import settings

log = logging.getLogger(__name__)

#: Host names this machine's own kind of network answers to: nobody else can point these at another address.
BUILT_IN = ("localhost", "*.localhost", "*.local", "*.lan")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
NOT_THIS_ADDRESS = (
    "This address is not one this resumebot answers to. Open it by its IP address, localhost or a name listed in the "
    "install's ALLOWED_HOSTS."
)
NOT_FROM_HERE = "A change has to come from this resumebot's own page, not from another site."


#: The rule behind each refusal status, as the log names it.
RULES = {421: "host not allowed", 403: "origin not allowed"}
#: A rule is logged at most once in this many seconds, so a client that keeps knocking fills no log.
LOG_EVERY = 60.0
#: When each rule was last logged; `clock` is replaced by tests.
_logged: dict[str, float] = {}
clock = time.monotonic


def _authority(value: str) -> str:
    """The authority of a Host or Origin value for a log line: host and port only (no scheme, user info, path or query),
    printable and short."""
    value = value.strip()
    if "://" in value:
        value = urlsplit(value).netloc
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rpartition("@")[2]
    return "".join(c for c in value if c.isprintable())[:100]


def _log_refusal(status: int, headers: dict[str, str]) -> None:
    rule = RULES.get(status, f"refused {status}")
    now = clock()
    last = _logged.get(rule)
    if last is not None and 0 <= now - last < LOG_EVERY:
        return
    _logged[rule] = now
    host = _authority(headers.get("host", "")) or "(none)"
    if status == 421:
        log.warning("refused 421, %s: Host %s", rule, host)
    else:
        log.warning("refused %s, %s: Origin %s sent to Host %s", status, rule,
                    _authority(headers.get("origin", "")) or "(none)", host)


def host_name(value: str) -> str:
    """The name in a Host (or an Origin's authority), without port, brackets or a trailing dot, lower-cased."""
    value = value.strip().lower()
    if value.startswith("["):  # [::1]:8420
        end = value.find("]")
        return value[1:end] if end > 0 else value[1:]
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def _port(value: str) -> str | None:
    value = value.strip()
    if value.startswith("["):
        value = value[value.find("]") + 1 :] if "]" in value else ""
    elif value.count(":") != 1:
        return None
    _, _, port = value.rpartition(":")
    return port if port.isdigit() else None


def allowed_patterns() -> list[str]:
    """What this install names besides the built-in kinds, as host-name patterns (ports dropped: "jobs.example.org:*" is
    "jobs.example.org")."""
    listed = [*settings.allowed_hosts.split(","), *settings.mcp_allowed_hosts.split(",")]
    listed += public_url_hosts(settings.public_url)
    return [host_name(item) for item in listed if item.strip()]


def host_allowed(name: str) -> bool:
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return any(fnmatchcase(name, pattern) for pattern in (*BUILT_IN, *allowed_patterns()))


def judge(method: str, headers: dict[str, str]) -> tuple[int, str] | None:
    """None when the call may go on, else the refusal as (status, words)."""
    host = headers.get("host", "")
    if not host_allowed(host_name(host)):
        return 421, NOT_THIS_ADDRESS
    if method.upper() in SAFE_METHODS:
        return None
    origin = headers.get("origin")
    if origin is not None:
        authority = urlsplit(origin).netloc if origin != "null" else ""
        same = bool(authority) and host_name(authority) == host_name(host)
        origin_port, host_port = _port(authority), _port(host)
        if same and origin_port and host_port and origin_port != host_port:
            same = False
        if not same:
            return 403, NOT_FROM_HERE
    elif headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
        return 403, NOT_FROM_HERE
    return None


class HttpGuard:
    """ASGI middleware: see the module docstring."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        refusal = judge(scope.get("method", "GET"), headers)
        if refusal is None:
            try:
                await self.app(scope, receive, send)
            except ClientDisconnect:
                # Nobody is listening, so nothing is answered; a response started already stays as it was.
                log.debug("the client hung up mid-request: %s %s", scope.get("method", "GET"), scope.get("path", ""))
        elif scope["type"] == "websocket":
            _log_refusal(refusal[0], headers)
            await send({"type": "websocket.close", "code": 1008})
        else:
            status, words = refusal
            _log_refusal(status, headers)
            body = json.dumps({"detail": words}).encode()
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
