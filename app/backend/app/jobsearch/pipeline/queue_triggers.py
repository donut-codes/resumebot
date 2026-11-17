"""The triggers that keep the stored order current (docs/PRD.md 24.19; `stored_order.py`), as SQLite triggers.

A lane of the board is read in the database by the columns of the role (`queue_points`, `queue_day`, `queue_moment`,
`queue_until`), which `stored_order.settle` works out; what tells `settle` which roles to look at again is a line in
`js_queue_dirty` (`QueueDirty`), added by a trigger in the transaction of every write of an input: a trigger is the one
thing that sees every write, whichever code made it. They were migration 0151's ten Postgres triggers, written in
plpgsql; SQLite has no functions to call, so each trigger holds its statements, and one that fired on `INSERT OR
DELETE` is two. `create_schema` makes them after the tables (`make`); a file made once has them for good, and the
migrations that made them on Postgres never run on SQLite.

What is an input: a role made, or changed in one of the columns the number and the age read (`posted`, `found`,
`board_url`, `fit`, `fit_breakdown`, `target_role`, `analysis_state`, the `deck_read` mark of the analysis,
`created_at`, `origin`); a rank written, changed (`fit`, `profile_version`, `created_at`, `role_id`) or deleted; a
posting of a role written, changed in the columns that date it (`opportunity_id`, `posted`, `first_seen`, `ats`, `url`,
`apply_url`) or deleted; a version of the profile's four documents saved (`PROFILE_KINDS`); the owner's list of job
sites changed (a site added, deleted, or changed in `url`, `kind` or `list_name`). A change that moves a rank or a
posting from one role to another names both roles. A posting no role holds names no role, so it adds no line. A
rewrite that leaves every one of those columns as it was adds none (`IS NOT` is SQLite's `IS DISTINCT FROM`: it takes
a missing value as a value). A line is only ever inserted, so a writer never waits on another. The lines only say
where to look: the app works the number out and deletes them.

One difference from Postgres: a JSON column is compared as the text it is stored as (`fit_breakdown`), where Postgres
compared `jsonb`, which has no key order. A breakdown written again with the same keys in another order adds a line it
did not there: the role is looked at once more and written only if its number differs.
"""

from sqlalchemy.engine import Connection

#: The profile's documents the number reads (`profile.base.KINDS`: the scoring span and the queue's numbers); a test
#: holds this to that.
PROFILE_KINDS = ("criteria", "gates", "scoring", "settings")

_ROLE = "INSERT INTO js_queue_dirty (owner, role_id) VALUES ({owner}, {role});"
_ROLE_IF = "INSERT INTO js_queue_dirty (owner, role_id) SELECT {owner}, {role} WHERE {when};"
_OWNER = "INSERT INTO js_queue_dirty (owner, role_id) VALUES ({owner}, NULL);"


def _trigger(name: str, event: str, table: str, body: str, when: str | None = None) -> tuple[str, str]:
    condition = f"\nWHEN ({when})" if when else ""
    return name, f"CREATE TRIGGER {name} {event} ON {table} FOR EACH ROW{condition}\nBEGIN\n    {body}\nEND"


def _changed(old_new: tuple[str, ...]) -> str:
    return " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in old_new)


#: The columns of a role the number and the age read, in the order the migration named them.
_ROLE_COLUMNS = ("posted", "found", "board_url", "fit", "fit_breakdown", "target_role", "analysis_state", "analysis",
                 "created_at", "origin")
_ROLE_CHANGED = " OR ".join((
    *(f"OLD.{c} IS NOT NEW.{c}" for c in ("posted", "found", "board_url", "fit", "fit_breakdown", "target_role",
                                          "analysis_state")),
    "json_extract(OLD.analysis, '$.deck_read') IS NOT json_extract(NEW.analysis, '$.deck_read')",
    "OLD.created_at IS NOT NEW.created_at", "OLD.origin IS NOT NEW.origin"))
_RANK_COLUMNS = ("fit", "profile_version", "created_at", "role_id")
_POSTING_COLUMNS = ("opportunity_id", "posted", "first_seen", "ats", "url", "apply_url")
_SITE_COLUMNS = ("url", "kind", "list_name")

#: (name, the statement that makes it). Twelve: the migration's ten, with the two that fired on `INSERT OR DELETE`
#: split in two.
TRIGGERS: tuple[tuple[str, str], ...] = (
    _trigger("js_opportunities_queue_insert", "AFTER INSERT", "js_opportunities",
             _ROLE.format(owner="NEW.owner", role="NEW.id")),
    _trigger("js_opportunities_queue_update", f"AFTER UPDATE OF {', '.join(_ROLE_COLUMNS)}", "js_opportunities",
             _ROLE.format(owner="NEW.owner", role="NEW.id"), _ROLE_CHANGED),
    _trigger("js_role_ranks_queue_write", "AFTER INSERT", "js_role_ranks",
             _ROLE.format(owner="NEW.owner", role="NEW.role_id")),
    _trigger("js_role_ranks_queue_delete", "AFTER DELETE", "js_role_ranks",
             _ROLE.format(owner="OLD.owner", role="OLD.role_id")),
    _trigger("js_role_ranks_queue_update", f"AFTER UPDATE OF {', '.join(_RANK_COLUMNS)}", "js_role_ranks",
             _ROLE.format(owner="NEW.owner", role="NEW.role_id") + "\n    "
             + _ROLE_IF.format(owner="OLD.owner", role="OLD.role_id", when="OLD.role_id IS NOT NEW.role_id"),
             _changed(_RANK_COLUMNS)),
    # a posting no role holds (the many a search finds) names no role, so the trigger is not even called for it
    _trigger("js_postings_queue_insert", "AFTER INSERT", "js_postings",
             _ROLE.format(owner="NEW.owner", role="NEW.opportunity_id"), "NEW.opportunity_id IS NOT NULL"),
    _trigger("js_postings_queue_delete", "AFTER DELETE", "js_postings",
             _ROLE.format(owner="OLD.owner", role="OLD.opportunity_id"), "OLD.opportunity_id IS NOT NULL"),
    _trigger("js_postings_queue_update", f"AFTER UPDATE OF {', '.join(_POSTING_COLUMNS)}", "js_postings",
             _ROLE_IF.format(owner="NEW.owner", role="NEW.opportunity_id", when="NEW.opportunity_id IS NOT NULL")
             + "\n    " + _ROLE_IF.format(owner="OLD.owner", role="OLD.opportunity_id",
                                           when="OLD.opportunity_id IS NOT NULL "
                                                "AND OLD.opportunity_id IS NOT NEW.opportunity_id"),
             _changed(_POSTING_COLUMNS)),
    _trigger("profile_versions_queue_insert", "AFTER INSERT", "profile_versions", _OWNER.format(owner="NEW.owner"),
             "NEW.kind IN (" + ", ".join(f"'{kind}'" for kind in PROFILE_KINDS) + ")"),
    _trigger("js_sites_queue_write", "AFTER INSERT", "js_sites", _OWNER.format(owner="NEW.owner")),
    _trigger("js_sites_queue_delete", "AFTER DELETE", "js_sites", _OWNER.format(owner="OLD.owner")),
    _trigger("js_sites_queue_update", f"AFTER UPDATE OF {', '.join(_SITE_COLUMNS)}", "js_sites",
             _OWNER.format(owner="NEW.owner"), _changed(_SITE_COLUMNS)),
)


def make(connection: Connection) -> None:
    """Make every trigger (each dropped first, so the same call twice leaves the same triggers). The tables they name
    are made already (`db.create_schema` calls this after `create_all`)."""
    for name, statement in TRIGGERS:
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
        connection.exec_driver_sql(statement)
