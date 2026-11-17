"""The job-search app's settings: environment only, none of them secret values (a secret is a file path)."""

import logging
import os
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The app's name. Shown on the page and in /health.
APP_NAME = "resumebot"
VERSION = "2.5.8"

#: The folder that is this install's own, outside the code (a local install: `app/scripts/local/stack.py` makes it and
#: starts the app with this variable set). It holds the settings file `.env` (the owner, the time zone, the paths of the
#: secret files, and the lanes' own settings: app/scout/lanes-local.mjs reads the same file), `secrets/`, the logs and
#: the lanes' folders. Named by this environment variable and nothing else. Unset (a test): there is no
#: such folder, and the settings come from the environment and a `.env` in the working folder.
DATA_DIR_VARIABLE = "RESUMEBOT_DATA_DIR"


def data_dir() -> Path | None:
    value = os.environ.get(DATA_DIR_VARIABLE, "").strip()
    return Path(value).expanduser() if value else None


def _env_files() -> tuple[str, ...]:
    folder = data_dir()
    return (".env", str(folder / ".env")) if folder else (".env",)


#: The file in the data folder the launcher writes the browser bridge's port to (app/scout/lanes-local.mjs).
BROWSER_PORT_FILE = "browser-port"


def _read(file: Path) -> str:
    try:
        return file.read_text(encoding="utf-8")
    except OSError:
        return ""


def _secret_file(name: str) -> str:
    folder = data_dir()
    return str(folder / "secrets" / name) if folder else f"/secrets/{name}"


class JobSearchSettings(BaseSettings):
    # The settings file is read as UTF-8, with or without the byte order mark that Windows tools (Notepad, PowerShell's
    # UTF-8) put at its start, never as the machine's own code page, which would garble a name with an accent in it.
    model_config = SettingsConfigDict(
        env_prefix="JOBSEARCH_", env_file=_env_files(), env_file_encoding="utf-8-sig", extra="ignore")

    #: Its own database. Empty falls back to DATABASE_URL (a test, never the install: the install sets this).
    database_url: str = ""
    #: The one person whose roles these are. Every role is unique per owner.
    owner: str = "owner"
    #: The scout's bearer token, as a file (chmod 600). Unset or unreadable: the MCP door refuses every call. Unset
    #: with a data folder: its `secrets/agent-token`.
    agent_token_file: str = Field(default_factory=lambda: _secret_file("agent-token"))
    #: The token of the door the owner's browser reads job sites through (docs/PRD.md 30.8), as a file (chmod
    #: 600). Its own, not the scout's: it opens two tools and nothing else. Unset or unreadable: the door refuses.
    browser_token_file: str = Field(default_factory=lambda: _secret_file("browser-token"))
    #: The token of the owner's own action door (docs/PRD.md path 50.2), as a file (chmod 600): a third token, so no
    #: lane's agent token and no browser token ever opens it. Made by `stack.py setup`. Unset or unreadable: the door
    #: answers 401 and nothing runs.
    owner_token_file: str = Field(default_factory=lambda: _secret_file("owner-token"))
    #: The key every request to the browser bridge must carry (docs/PRD.md path 52; the owner, 2026-11-14), as a file
    #: (chmod 600): made once by the launcher (app/scout/lanes-local.mjs) in the data folder's `secrets/bridge-key`.
    #: The app's own code sends it with every call to the bridge. Unset or unreadable: it sends none, and the bridge
    #: refuses.
    bridge_key_file: str = Field(default_factory=lambda: _secret_file("bridge-key"))
    #: The port of the browser bridge on this machine (the launcher's, 127.0.0.1 only: app/scout/own-chrome.mjs), which
    #: the app's own code reaches the app's browser through (`browser/code.py`). The install's SCOUT_BROWSER_PORT.
    #: Read as text, so a blank or odd value never stops the app: `bridge_port` falls back to 9333.
    browser_port: str = Field(
        default="9333", validation_alias=AliasChoices("JOBSEARCH_BROWSER_PORT", "SCOUT_BROWSER_PORT"))
    #: The key the mail login's app password is sealed with (`jobsearch/mail/seal.py`), as a file (chmod 600) in the
    #: install's secrets, never in the database. Missing: it is made the first time a login is saved.
    mail_key_file: str = Field(default_factory=lambda: _secret_file("mail-key"))
    #: Where the scout is woken (the scout's /wake address) and the shared secret file. Either unset: no wake;
    #: the scout's own check-in timer finds the open search.
    wake_url: str = ""
    wake_secret_file: str = Field(default_factory=lambda: _secret_file("wake-secret"))
    #: How often a search starts by itself, in hours (0: only when asked). Read once, as the first schedule of the
    #: Scout the boards mission; after that the mission's own schedule is the one (docs/PRD.md 18.4).
    interval_hours: float = 24.0
    #: A search the scout hasn't finished in this many minutes is closed as failed so the next can start.
    stuck_minutes: int = 120
    #: The folder a finished packet's resume and letter (PDF and Word) are saved to, one folder per role. The data
    #: folder's `.env` names it as RESUMEBOT_PACKET_DIR (not JOBSEARCH_*). Empty: nothing is saved.
    packet_dir: str = Field(default="", validation_alias="RESUMEBOT_PACKET_DIR")
    #: Extra Host headers the MCP door accepts (comma-separated), e.g. "home.lan:*".
    mcp_allowed_hosts: str = ""
    #: Extra names the whole app answers to (comma-separated host names, "*.example.org" allowed, ports ignored),
    #: besides localhost, IP addresses, `.local` and `.lan` names, the MCP door's hosts and the public address
    #: (http_guard.py). The install's ALLOWED_HOSTS.
    allowed_hosts: str = ""
    public_url: str = ""
    #: What the scout is told to search by when "Search now" gives no focus.
    default_focus: str = "all of the profile's target functions"
    #: Where searches are scheduled and dates are read (an IANA name such as "Europe/Paris"); the install sets it.
    #: Blank counts as unset. Unset means UTC. A name that is not a zone stops the app at start (`check_timezone`).
    timezone: str = "UTC"
    run_schedule: bool = True
    #: An agent that has not called the app for this many hours shows as offline on the Agents page. The scout checks
    #: in at least twice a day on its own timer, so a day and a bit is "it is not running".
    connected_hours: float = 26.0
    #: How many roles the catch-up after a tracker import keeps with the analyst at once (docs/PRD.md 2.9). Each is
    #: researched by one session, and the sessions are the pool's: set it to the number of them. One by default.
    #: The install's ANALYSTS.
    analysts: int = Field(default=1, ge=1, le=8)

    #: The domains the browser's sessions may work on (comma-separated, subdomains included); empty: all of them. An
    #: item for any other domain waits, held, until this is widened (`browser_pace.in_scope`). Code-first reads (the
    #: app's own page fetches) are not limited by it. The install's BROWSER_DOMAINS.
    browser_domains: str = ""

    def browser_scope(self) -> tuple[str, ...]:
        return tuple(d for d in (x.strip().lower().lstrip(".") for x in self.browser_domains.split(",")) if d)

    @model_validator(mode="before")
    @classmethod
    def _blank_timezone_is_unset(cls, data: object) -> object:
        """A blank JOBSEARCH_TIMEZONE line is the same as none: dropped, so the default applies and `timezone_is_set`
        is false."""
        if isinstance(data, dict) and not str(data.get("timezone") or "").strip():
            data = {k: v for k, v in data.items() if k != "timezone"}
        elif isinstance(data, dict):
            data = {**data, "timezone": str(data["timezone"]).strip()}
        return data

    @property
    def timezone_is_set(self) -> bool:
        return "timezone" in self.model_fields_set

    def check_timezone(self) -> None:
        """Run once at app start. An unknown zone name stops the app with a message naming the setting, instead of
        failing at the first use. An unset zone is allowed (UTC) and logged once."""
        if not self.timezone_is_set:
            logging.getLogger(__name__).warning(
                "JOBSEARCH_TIMEZONE is not set: times, days and the night's schedule are in UTC until it is set to "
                "your zone, e.g. Europe/Paris.")
            return
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise RuntimeError(
                f"JOBSEARCH_TIMEZONE is {self.timezone!r}, which is not a time zone name. Set it to an IANA name "
                "such as Europe/Paris, or remove the line to use UTC.") from None

    def token_path(self) -> Path:
        return Path(self.agent_token_file)

    def browser_token_path(self) -> Path:
        return Path(self.browser_token_file)

    def owner_token_path(self) -> Path:
        return Path(self.owner_token_file)

    def bridge_key(self) -> str:
        """The key the browser bridge asks of every request, or "" when there is no key file yet."""
        return _read(Path(self.bridge_key_file)).strip()

    def bridge_port(self) -> int:
        """The browser bridge's port: the one the launcher wrote to the data folder's `browser-port` as it started the
        bridge (it may have taken a free port), else `browser_port`, else 9333."""
        folder = data_dir()
        for port in ((_read(folder / BROWSER_PORT_FILE) if folder else ""), self.browser_port):
            port = port.strip()
            if port.isdigit() and 0 < int(port) < 65536:
                return int(port)
        return 9333


settings = JobSearchSettings()
