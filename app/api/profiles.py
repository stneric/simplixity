"""Profiles: pick one, make one, give it a password, leave it.

The session is a browser-session cookie, no `max-age`, so closing the browser
is what brings the picker back. "Choose your profile when you open the page"
is the behaviour asked for, and a cookie that outlived the browser would skip
it for whoever sits down next.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import SESSION_COOKIE, USER_PATTERN, DbDep, UserDep
from app.store import profiles

router = APIRouter()


class Credentials(BaseModel):
    name: str = Field(pattern=USER_PATTERN)
    # Empty is "no password": on create it makes an open profile, on login it
    # is what an open profile is signed into with.
    password: str = Field(default="", max_length=200)


class PasswordChange(BaseModel):
    current: str = Field(default="", max_length=200)
    # Empty removes the password.
    password: str = Field(default="", max_length=200)


async def _sign_in(request: Request, response: Response, db: DbDep, name: str) -> dict:
    profile = await profiles.get(db, name)
    response.set_cookie(
        SESSION_COOKIE,
        await profiles.session_token(db, profile),
        httponly=True,
        samesite="lax",
        # Over TLS the cookie is never sent in the clear again, not even to the
        # plain-HTTP port. Only then: a local install on http://localhost would
        # otherwise sign in and immediately forget it.
        secure=request.url.scheme == "https",
        path="/",
    )
    return {"current": name, "protected": profile["password_hash"] is not None}


@router.get("/api/profiles")
async def list_profiles(request: Request, db: DbDep) -> dict:
    """The picker's contents, and who this browser already is, if anyone.

    Open to everyone by design: the names are what a picker shows, and a
    `current` of null is how the page knows to show it.
    """
    token = request.cookies.get(SESSION_COOKIE, "")
    return {
        "current": await profiles.verify_session(db, token) if token else None,
        "profiles": await profiles.list_profiles(db),
    }


@router.post("/api/profiles")
async def create_profile(
    body: Credentials, request: Request, response: Response, db: DbDep
) -> dict:
    """Make a profile and sign straight into it."""
    if not await profiles.create(db, body.name, body.password):
        raise HTTPException(status_code=409, detail="That profile already exists")
    return await _sign_in(request, response, db, body.name)


@router.post("/api/profiles/login")
async def login(body: Credentials, request: Request, response: Response, db: DbDep) -> dict:
    profile = await profiles.get(db, body.name)
    if profile is None:
        raise HTTPException(status_code=404, detail="Unknown profile")
    if not profiles.check_password(body.password, profile["password_hash"]):
        raise HTTPException(status_code=401, detail="Wrong password")
    return await _sign_in(request, response, db, body.name)


@router.post("/api/profiles/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.put("/api/profiles/password")
async def change_password(
    body: PasswordChange, request: Request, response: Response, db: DbDep, user: UserDep
) -> dict:
    """Set, change or remove this profile's password.

    The current one is asked for even though the caller holds a session: a
    session is a browser left open, and that is exactly the situation a
    password on a shared machine is meant to survive.
    """
    profile = await profiles.get(db, user)
    if not profiles.check_password(body.current, profile["password_hash"]):
        raise HTTPException(status_code=401, detail="Wrong password")
    await profiles.set_password(db, user, body.password)
    # The old session signed the old hash; this browser gets a fresh one.
    return await _sign_in(request, response, db, user)
