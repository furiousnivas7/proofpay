"""Demo login: two fixed accounts, "client" and "freelancer", using HTTP Basic auth.

Good enough for a sandbox demo. Not for real users.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from proofpay.config import Settings
from proofpay.models import User

DEMO_USERS = {"client": "client", "freelancer": "freelancer"}  # username -> role

basic = HTTPBasic()


def seed_demo_users(session: Session) -> None:
    for username, role in DEMO_USERS.items():
        if session.scalar(select(User).where(User.username == username)) is None:
            session.add(User(username=username, role=role))
    session.commit()


def _password_for(username: str, settings: Settings) -> str | None:
    return {
        "client": settings.demo_client_password,
        "freelancer": settings.demo_freelancer_password,
    }.get(username)


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def current_user(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(basic),
    session: Session = Depends(get_session),
) -> User:
    settings: Settings = request.app.state.settings
    expected = _password_for(credentials.username, settings)
    ok = expected is not None and secrets.compare_digest(credentials.password.encode(), expected.encode())
    user = session.scalar(select(User).where(User.username == credentials.username)) if ok else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return user
