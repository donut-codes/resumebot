"""What one site's runbook says, as data (the generic-sites design, Part 1.1). A runbook is one JSON file about one
site: the hosts it lives on, how its addresses read, how it is paced and stopped, and which mail it sends. Code names
the problem, never the site: the shipped envelope below is every field shipped code understands.

Whether a runbook may ship is not a field. It is the verdict `site_terms.json` holds for the runbook's hosts, which a
person read from the site's public terms page (`registry.Registry.verdict`).

Five fields are for the restricted folder alone (`RESTRICTED_ONLY`): the loader refuses them anywhere else, by the
file's real path (`loader.py`). Shipped code never types a key of `extras`; the hook that reads it does.

Pure data and checks: nothing here reads a file, the network or the database."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.sites import address_rules, feed_schema

SCHEMA_VERSION = 1

#: The seconds between two requests to a host that no runbook slows (`polite.Fetcher`'s own default).
DEFAULT_PACE_SECONDS = 1.0
#: The statuses of an answer that says "not now", not "no", for a host no runbook names its own (`polite.NOT_NOW`).
DEFAULT_SLOW_STATUSES = (408, 425, 429)
#: The least seconds between two requests to a host read though its robots.txt asks automated readers not to.
ROBOTS_MIN_PACE_SECONDS = 30.0

#: The fields a runbook may carry only from the restricted folder.
RESTRICTED_ONLY = ("robots", "reader", "read_hosts", "signed_in", "extras")

#: What a `read` may be (the kinds the saved skills already have).
READ_KINDS = ("request", "html", "embedded", "list_page", "page")

_KEY = re.compile(r"^[a-z0-9][a-z0-9_]*$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


class Mail(BaseModel):
    """Whose mail this site sends: a job board's (`board`: it names a role only when its subject speaks of an
    application), a job system's (`system`) or none. `senders` are the sender domains, written out: they are never
    taken from `hosts`. `confirms`: the site sends an application's confirmation from its own domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["board", "system", "none"] = "none"
    senders: tuple[str, ...] = ()
    confirms: bool = False

    @field_validator("senders")
    @classmethod
    def _lower(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for sender in value:
            if not _HOST.match(sender):
                raise ValueError(f"a sender must be a lower-case domain, not {sender!r}")
        return value


class Flags(BaseModel):
    """What a job system's address says of how it is read: today's `System` attributes, with their defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    drawn: bool = False
    places: bool = False
    needs: tuple[str, ...] = ("slug",)
    customer_host: bool = False
    own_parts: tuple[str, ...] | None = None
    why_not: str | None = None
    probe_names: bool | None = None
    posting_only: bool = False


class Probe(BaseModel):
    """Guessing a board by an employer's name. `proves_board`: an answer for the name proves the board exists (False
    where the system answers for an account that holds no posting).

    The data form of the guess (`feed_read.probe`): `url` (with `{slug}`) is asked, and the answer must be a JSON
    object; with `present`, the dotted path in it must hold a list with something in it (the board has postings);
    `name`, the dotted path of the employer's name when the feed gives one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proves_board: bool = True
    url: str | None = None
    present: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _data(self) -> "Probe":
        if self.url is None:
            if self.present is not None or self.name is not None:
                raise ValueError("present and name go with the probe's url")
            return self
        if not self.url.startswith("https://") or "{slug}" not in self.url:
            raise ValueError("a probe's url is an https address that holds {slug}")
        if self.url.replace("{slug}", "").count("{") or self.url.replace("{slug}", "").count("}"):
            raise ValueError("a probe's url holds {slug} and no other place")
        for what, path in (("present", self.present), ("name", self.name)):
            if path is not None:
                feed_schema.path_ok(path, f"probe.{what}")
        return self


class Robots(BaseModel):
    """Read though robots.txt asks automated readers not to, for one host and the one path under it (restricted folder
    only; `loader.py` and the one door `Fetcher.read_exempt` hold the guards)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    path_prefix: str = Field(min_length=1)


class Runbook(BaseModel):
    """One site's runbook: the shipped envelope (Part 1.1), then the fields of the restricted folder."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_: Literal[1] = Field(SCHEMA_VERSION, alias="schema")
    #: The id that is stored (`Employer.ats`, a skill's `ats`): opaque, kept as it is.
    key: str
    #: The owner-facing label.
    name: str = Field(min_length=1)
    kind: Literal["board", "job_system", "host"]
    #: The registrable domains (or hosts) the site is on: a host or any host under one.
    hosts: tuple[str, ...] = Field(min_length=1)
    #: Job systems only: where `ats.parse` asks this system among the others (0, 1, 2 ...).
    order: int | None = Field(None, ge=0)
    #: Many companies' pages sit on this host: a website there does not show whose it is.
    shared_host: bool = False
    #: A new install starts with this job system switched off, and does not list it while it is.
    starts_off: bool = False
    mail: Mail = Mail()
    #: How the site's addresses read (the address design, Part 1.2): `own_careers_paths`, and the rules of
    #: `address_rules` (`one_posting`, `board`, `apply_page`, `board_url`).
    address: dict[str, Any] = Field(default_factory=dict)
    #: How the site is read (Part 1.3); absent: its addresses are read and nothing else.
    read: dict[str, Any] | None = None
    #: Seconds between two requests to the site's hosts; absent: the generic pace (never for a runbook with `robots`).
    pace_seconds: float | None = Field(None, gt=0)
    slow_statuses: tuple[int, ...] | None = None
    #: What a refusal at the site means: it stops the site for good, or until the owner's next off hours.
    stop: Literal["for_good", "until_next_off_hours"] = "for_good"
    flags: Flags = Flags()
    probe: Probe | None = None
    #: A board whose roles must be followed to the employer's own site; its copy stands in until then.
    follow_to_employer: bool = False
    #: The app's own code reads this site's postings (not a recipe, not the browser): the pace is held for it by name.
    code_reads_postings: bool = False
    # --- the restricted folder only -----------------------------------------------------------------------------
    robots: Robots | None = None
    #: The name of a hook module in the restricted folder's `code/`.
    reader: str | None = None
    #: Hosts, besides `hosts`, that the hook reads the site's data from (a feed apart from the site's pages).
    read_hosts: tuple[str, ...] = ()
    signed_in: bool = False
    #: Opaque to shipped code: the hook that reads this runbook knows its keys.
    extras: dict[str, Any] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _KEY.match(value):
            raise ValueError("the key is lower-case letters, digits and underscores")
        return value

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for host in value:
            if not _HOST.match(host) or "." not in host:
                raise ValueError(f"a host must be a lower-case dotted host with no scheme or path, not {host!r}")
        return value

    @field_validator("read_hosts")
    @classmethod
    def _read_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for host in value:
            if not _HOST.match(host) or "." not in host:
                raise ValueError(f"a read host must be a lower-case dotted host with no scheme or path, not {host!r}")
        return value

    @field_validator("reader")
    @classmethod
    def _reader(cls, value: str | None) -> str | None:
        if value is not None and not _KEY.match(value):
            raise ValueError("a reader is the bare name of a module in the restricted folder's code/")
        return value

    @field_validator("read")
    @classmethod
    def _read(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and value.get("kind") not in READ_KINDS:
            raise ValueError(f"a read has a kind, one of {READ_KINDS}")
        return value

    @field_validator("address")
    @classmethod
    def _address(cls, value: dict[str, Any]) -> dict[str, Any]:
        paths = value.get("own_careers_paths")
        ok = isinstance(paths, list) and paths and all(isinstance(p, str) and p for p in paths)
        if paths is not None and not ok:
            raise ValueError("own_careers_paths is a list of path segments")
        address_rules.check(value)
        return value

    @model_validator(mode="after")
    def _whole(self) -> "Runbook":
        if self.read is not None and self.read.get("kind") in ("request", "embedded"):
            try:
                feed_schema.check(self.read, self.hosts)
            except ValueError as exc:
                raise ValueError(f"read: {exc}") from None
        if self.kind == "job_system" and self.order is None:
            raise ValueError("a job system has an order")
        if self.kind != "job_system" and self.order is not None:
            raise ValueError("only a job system has an order")
        if self.read_hosts and not self.reader:
            raise ValueError("read_hosts go with a reader")
        if self.starts_off and self.kind != "job_system":
            raise ValueError("only a job system starts off")
        if self.flags.posting_only and self.kind != "job_system":
            raise ValueError("only a job system is posting_only")
        if self.robots is not None:
            host = self.robots.host
            if not any(host == h or host.endswith("." + h) for h in self.hosts):
                raise ValueError("robots.host is one of the runbook's hosts, or under one")
            if self.pace_seconds is None or self.pace_seconds < ROBOTS_MIN_PACE_SECONDS:
                raise ValueError(f"a runbook with robots is paced at {ROBOTS_MIN_PACE_SECONDS:g} seconds or more")
            if self.stop != "until_next_off_hours":
                raise ValueError("a runbook with robots stops until the next off hours")
        return self

    @property
    def has_restricted_fields(self) -> bool:
        return bool(self.robots or self.reader or self.read_hosts or self.signed_in or self.extras)
