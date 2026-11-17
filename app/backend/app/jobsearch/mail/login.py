"""The owner's mail login, kept in the app (the owner, 2026-10-16; `routes/mail.py` is its screen's door).

The login is a sealed, write-only secret, and an install needs only this one: the inbox address and port, the mail
address and its app password, sealed (`mail/seal.py`) in `js_mail_logins`. The
password is written and never read back: nothing here returns it but `find`, which hands it to the mail reader and
nowhere else."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Unprocessable
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.mail import seal as mail_seal
from app.jobsearch.models import MailLogin
from app.jobsearch.work import queue
from app.mailmine import imap, providers


async def _row(session: AsyncSession, owner: str) -> MailLogin | None:
    return await session.scalar(select(MailLogin).where(MailLogin.owner == owner))


async def view(session: AsyncSession, owner: str) -> dict:
    """What the screen is shown: whether a login is saved and as whom, and the providers' help. Never the password."""
    row = await _row(session, owner)
    provider = providers.for_address(row.username) if row else None
    return {
        "connected": row is not None,
        "username": row.username if row else None,
        "host": row.host if row else None,
        "port": row.port if row else None,
        "provider": provider.name if provider else None,
        "providers": [p.view() for p in providers.load()],
    }


async def save(session: AsyncSession, owner: str, *, username: str, password: str, host: str | None,
               port: int | None) -> None:
    """Keep the login (replacing a saved one). The inbox address comes from the mail address's provider unless one is
    given. 422 in words when it can't be kept. The caller commits."""
    username = username.strip()
    if "@" not in username or " " in username:
        raise Unprocessable("Type your whole mail address, like you@example.com.")
    provider = providers.for_address(username)
    password = "".join(password.split()) if provider is None or provider.strip_spaces else password.strip()
    if not password:
        raise Unprocessable("Paste the app password.")
    host = (host or "").strip() or (provider.host if provider else "")
    if not host:
        raise Unprocessable("Say which mail server this address uses (its IMAP address).")
    port = port or (provider.port if provider and provider.host == host else 993)
    if not 0 < port < 65536:
        raise Unprocessable("That isn't a port number.")
    try:
        sealed = mail_seal.seal(password, owner=owner)
    except mail_seal.SealUnavailable as exc:
        raise Unprocessable(str(exc)) from None
    row = await _row(session, owner)
    if row is None:
        session.add(MailLogin(owner=owner, host=host, port=port, username=username, password_sealed=sealed))
    else:
        row.host, row.port, row.username, row.password_sealed = host, port, username, sealed
    await session.flush()


async def remove(session: AsyncSession, owner: str) -> None:
    """Forget the saved login. The caller commits."""
    with queue.exempt("forgetting the login creates no work, and no loop reads the saved login"):
        await session.execute(delete(MailLogin).where(MailLogin.owner == owner))


async def saved(session: AsyncSession, owner: str) -> go_mail.Login | None:
    """The saved login opened for one read, or None when there is none (or its key is gone: the screen says so)."""
    row = await _row(session, owner)
    if row is None:
        return None
    try:
        password = mail_seal.unseal(row.password_sealed, owner=owner)
    except mail_seal.SealUnavailable:
        return None
    return go_mail.Login(host=row.host, port=row.port, username=row.username, password=password)


async def find(session: AsyncSession, owner: str) -> go_mail.Login | None:
    """The login Go's checks read the mail with: the saved one (None when none is saved)."""
    return await saved(session, owner)


def test(who: go_mail.Login) -> list[str]:
    """Open the mailbox read-only and list its folders (the ones a read would search). Blocking. Raises
    `imap.MailReadError` in plain words."""
    with go_mail.open_inbox(who) as box:
        return imap.folders(box.conn)
