"""Access-token verification for the two supported identity providers.

* **Privy** (default): ES256 tokens signed with the app's keys published at
  ``https://auth.privy.io/api/v1/apps/{app_id}/jwks.json``; ``iss`` must be ``privy.io`` and ``aud`` the
  Privy app id. The subject is a Privy DID (``did:privy:...``), mapped to a stable UUID (UUID v5) that keys
  all of the user's rows.
* **Supabase**: asymmetric JWKS keys (ES256/RS256) or the legacy HS256 secret; ``iss`` is
  ``{SUPABASE_URL}/auth/v1`` and ``aud`` ``authenticated``.

Signature, ``exp``, ``iss`` and ``aud`` are verified in every case; ``alg: none`` is never accepted.
"""

import uuid
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.core.config import Settings
from app.core.errors import UnauthorizedError

ASYMMETRIC_ALGS = ["ES256", "RS256", "EdDSA"]
PRIVY_NAMESPACE = uuid.UUID("6f1d4a52-3c7e-5b8a-9f20-1c4e7d9b2a61")


@dataclass(frozen=True)
class AuthenticatedUser:
    id: uuid.UUID  # internal key for all user-owned rows
    subject: str  # provider subject (Privy DID or Supabase user id)
    email: str | None
    provider: str


def privy_user_uuid(did: str) -> uuid.UUID:
    return uuid.uuid5(PRIVY_NAMESPACE, did)


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> PyJWKClient:
    # Keys are cached in-process; an unknown ``kid`` triggers a refetch (key rotation).
    return PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)


def _decode(token: str, key, alg: str, audience: str, issuer: str) -> dict:
    return jwt.decode(
        token,
        key,
        algorithms=[alg],
        audience=audience,
        issuer=issuer,
        options={"require": ["exp", "iat", "sub", "aud", "iss"]},
    )


def verify_token(token: str, settings: Settings, jwks_client: PyJWKClient | None = None) -> AuthenticatedUser:
    if not settings.auth_configured:
        raise UnauthorizedError("Authentication is not configured on this server.")
    try:
        alg = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Malformed token.") from exc

    try:
        if settings.auth_provider == "privy":
            if alg != "ES256":
                raise UnauthorizedError("Unsupported token signature.")
            client = jwks_client or _jwks_client(settings.jwks_url)
            claims = _decode(
                token,
                client.get_signing_key_from_jwt(token).key,
                alg,
                settings.privy_app_id,
                settings.jwt_issuer,
            )
            did = str(claims["sub"])
            if not did.startswith("did:privy:"):
                raise UnauthorizedError("Invalid subject.")
            return AuthenticatedUser(id=privy_user_uuid(did), subject=did, email=None, provider="privy")

        if alg == "HS256":
            if not settings.supabase_jwt_secret:
                raise UnauthorizedError("Unsupported token signature.")
            key = settings.supabase_jwt_secret
        elif alg in ASYMMETRIC_ALGS:
            key = (jwks_client or _jwks_client(settings.jwks_url)).get_signing_key_from_jwt(token).key
        else:
            raise UnauthorizedError("Unsupported token signature.")
        claims = _decode(token, key, alg, settings.supabase_jwt_audience, settings.jwt_issuer)
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Token expired.") from exc
    except jwt.PyJWKClientError as exc:
        raise UnauthorizedError("Signing key not found.") from exc
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Invalid token.") from exc

    if claims.get("role") != "authenticated":
        raise UnauthorizedError("Token is not a user session.")
    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except ValueError as exc:
        raise UnauthorizedError("Invalid subject.") from exc
    return AuthenticatedUser(id=user_id, subject=str(user_id), email=claims.get("email"), provider="supabase")
