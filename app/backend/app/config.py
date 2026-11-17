"""Settings: the paths to the content this app ships and the database. Derived from another project's
`app/config.py` (app/PROVENANCE.md), trimmed: only the paths this app needs are here. The job-search app's own settings
are `app.settings`."""

from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

# The app folder in a checkout (app/backend/app/config.py -> app/): playbooks/, instructions/ and templates/ are in it.
_CONTENT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    #: Tests only (they point it at a throwaway database); the app itself uses JOBSEARCH_DATABASE_URL
    #: (`app.settings`). Unset on purpose: with neither set the app refuses to start (`db.database_url`).
    database_url: str = ""
    playbooks_path: Path = _CONTENT_ROOT / "playbooks"
    # What each step tells an agent, and the shape of its response (app.harness.instructions):
    # instructions/<feature>.<step>.instructions.md and templates/<feature>.<step>.template.md.
    instructions_path: Path = _CONTENT_ROOT / "instructions"
    templates_path: Path = _CONTENT_ROOT / "templates"
    # The harness's words and kinds of work (app.harness). This app ships none yet; the folders are optional.
    vocab_path: Path = _CONTENT_ROOT / "vocab"
    kinds_path: Path = _CONTENT_ROOT / "kinds"
    process_path: Path = _CONTENT_ROOT / "playbooks" / "project.pdca.process.yaml"


def public_url_hosts(url: str) -> list[str]:
    """The Host header values an address like "https://resumebot.example:8443/" is reached
    under: the bare host (a reverse proxy on 80/443 sends no port) and "host:*" (any port)."""
    host = urlparse(url if "//" in url else f"//{url}").hostname if url.strip() else None
    if not host:
        return []
    host = f"[{host}]" if ":" in host else host
    return [host, f"{host}:*"]


settings = Settings()
