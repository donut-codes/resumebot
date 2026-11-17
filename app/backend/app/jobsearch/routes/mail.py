"""The mail login over HTTP (`mail/login.py`), the screen under Agent Settings:

- `GET /mail-login` says whether a login is saved and as whom, with the providers' help. The password is never in it.
- `PUT /mail-login` saves a login (write-only: the password goes in and does not come out).
- `POST /mail-login/test` logs in to the saved login read-only and lists the folders; the answer is `ok` and words, 200
  either way (a refused login is an answer, not a fault).
- `DELETE /mail-login` forgets it."""

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel, Field, SecretStr

from app import db
from app.db import ReadSession, Session
from app.errors import NotFound
from app.jobsearch.mail import login as mail_login
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.missions.kinds import read_mail
from app.mailmine import imap
from app.settings import settings

router = APIRouter(prefix="/api/v1/mail-login")


class LoginBody(BaseModel):
    username: str = Field(max_length=320)
    #: A secret type, so no log line or error shows it.
    password: SecretStr
    #: Only for a provider the app doesn't know ("Another provider"); a known one's address is used.
    host: str | None = Field(default=None, max_length=300)
    port: int | None = None


@router.get("")
async def login_view(session: ReadSession) -> dict:
    return await mail_login.view(session, settings.owner)


@router.put("")
async def login_save(body: LoginBody, session: Session, reader: ReadSession) -> dict:
    await mail_login.save(
        session, settings.owner, username=body.username, password=body.password.get_secret_value(), host=body.host,
        port=body.port)
    await read_mail.mailbox_connected(session, settings.owner)
    await session.commit()
    return await mail_login.view(reader, settings.owner)  # read on a reader, after the commit


@router.post("/test")
async def login_test(session: ReadSession) -> dict:
    who = await mail_login.saved(session, settings.owner)
    if who is None:
        raise NotFound("No mail login is saved yet.")
    await db.release(session)  # the mailbox may take seconds to answer: no transaction is held meanwhile
    try:
        async with asyncio.timeout(go_mail.READ_SECONDS):
            names = await asyncio.to_thread(mail_login.test, who)
    except imap.MailReadError as exc:
        return {"ok": False, "message": str(exc), "folders": []}
    except TimeoutError:
        return {"ok": False, "message": "The mailbox took too long to answer.", "folders": []}
    except Exception:  # the answer is words, never the server's text
        return {"ok": False, "message": "The mailbox couldn't be reached.", "folders": []}
    return {"ok": True, "message": f"Signed in as {who.username}. It reads {', '.join(names)}.", "folders": names}


@router.delete("")
async def login_remove(session: Session, reader: ReadSession) -> dict:
    await mail_login.remove(session, settings.owner)
    await read_mail.mailbox_removed(session, settings.owner)
    await session.commit()
    return await mail_login.view(reader, settings.owner)  # read on a reader, after the commit
