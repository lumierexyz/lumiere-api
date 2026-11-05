"""Supabase access-token verification.

Supabase projects sign access tokens either with asymmetric keys published at
``/auth/v1/.well-known/jwks.json`` (ES256/RS256, current default) or, for legacy projects, with a
shared HS256 secret. We verify signature, ``exp``, ``iss`` and ``aud`` in every case and never accept
``alg: none``. The identity used everywhere downstream is the verified ``sub`` claim.
"""

import uuid
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.core.config import Settings
from app.core.errors import UnauthorizedError

ASYMMETRIC_ALGS = ["ES256", "RS256", "EdDSA"]


@dataclass(frozen=True)
class AuthenticatedUser:
    id: uuid.UUID
    email: str | None
    role: str


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> PyJWKClient:
    # Keys are cached in-process; unknown ``kid`` triggers a refetch (key rotation).
    return PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)


def verify_token(token: str, settings: Settings, jwks_client: PyJWKClient | None = None) -> AuthenticatedUser:
    if not settings.jwt_issuer:
        raise UnauthorizedError("Authentication is not configured on this server.")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Malformed token.") from exc

    alg = header.get("alg")
    options = {"require": ["exp", "iat", "sub", "aud", "iss"]}
    try:
        if alg == "HS256":
            if not settings.supabase_jwt_secret:
                raise UnauthorizedError("Unsupported token signature.")
            claims = jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience=settings.supabase_jwt_audience,
                issuer=settings.jwt_issuer,
                options=options,
            )
        elif alg in ASYMMETRIC_ALGS:
            client = jwks_client or _jwks_client(settings.jwks_url)
            key = client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=settings.supabase_jwt_audience,
                issuer=settings.jwt_issuer,
                options=options,
            )
        else:
            raise UnauthorizedError("Unsupported token signature.")
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Token expired.") from exc
    except jwt.PyJWKClientError as exc:
        raise UnauthorizedError("Signing key not found.") from exc
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Invalid token.") from exc

    if claims.get("role") not in ("authenticated",):
        raise UnauthorizedError("Token is not a user session.")
    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except ValueError as exc:
        raise UnauthorizedError("Invalid subject.") from exc
    return AuthenticatedUser(id=user_id, email=claims.get("email"), role=claims["role"])
