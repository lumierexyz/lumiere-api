"""FastAPI dependencies: the only way a route learns who the caller is."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.jwt import AuthenticatedUser, verify_token
from app.core.config import Settings, get_settings
from app.core.errors import UnauthorizedError
from app.core.rate_limit import limiter
from app.database.session import get_db
from app.users.models import Profile

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> AuthenticatedUser:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Missing bearer token.")
    user = verify_token(token.strip(), settings, getattr(request.app.state, "jwks_client", None))
    limiter.check(f"user:{user.id}", settings.rate_limit_per_minute)
    request.state.user_id = str(user.id)
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


def get_profile(user: CurrentUser, db: DbSession) -> Profile:
    """Return the caller's profile, creating it on first authenticated request.

    Sign-up happens in the frontend through Supabase Auth; the first API call provisions the
    matching profile row from verified claims only.
    """
    profile = db.get(Profile, user.id)
    if profile is None:
        profile = Profile(id=user.id, email=user.email)
        db.add(profile)
        db.commit()
    return profile


CurrentProfile = Annotated[Profile, Depends(get_profile)]
