"""The owner's action door's actions (docs/PRD.md path 50): one registry (`registry.py`), the read actions (`reads.py`),
the write actions (`writes.py`), the actions that set a new install up (`onboarding.py`), the master-resume help
(`resume_help.py`) and what is common to them (`runner.py`: jobs, dry runs, Activity). The door itself, the four MCP
tools Claude Code reaches them through, is `owner_actions/mcp.py`.

(The package is `owner_actions` and not `actions`: `app.jobsearch.actions` is the process engine's actions.)"""

from app.jobsearch.owner_actions import onboarding, reads, resume_help, writes  # noqa: F401  (registers the actions)
from app.jobsearch.owner_actions.registry import REGISTRY, Action

__all__ = ["REGISTRY", "Action"]
