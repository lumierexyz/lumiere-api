"""Test harness: isolated SQLite DB, real RS256 JWT verification against a test key pair, and
mocked external providers (no network access in tests)."""

import os
import time
import uuid

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": "sqlite://",
        "AUTH_PROVIDER": "supabase",
        "PRIVY_APP_ID": "",
        "PRIVY_APP_SECRET": "",
        "SUPABASE_URL": "https://test-project.supabase.co",
        "SUPABASE_JWT_SECRET": "",
        "FRED_API_KEY": "test-fred-key",
        "ANTHROPIC_API_KEY": "",
        "RATE_LIMIT_PER_MINUTE": "10000",
        "HTTP_MAX_RETRIES": "2",
    }
)

import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.database import registry  # noqa: E402
from app.database.session import create_engine, set_engine  # noqa: E402

ISSUER = "https://test-project.supabase.co/auth/v1"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _SigningKey:
    def __init__(self, key):
        self.key = key


class FakeJWKSClient:
    """Stands in for PyJWKClient: resolves our test ``kid`` to the test public key."""

    def get_signing_key_from_jwt(self, token: str):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != "test-kid":
            raise jwt.PyJWKClientError("Unable to find a signing key that matches")
        return _SigningKey(PRIVATE_KEY.public_key())


def make_token(
    user_id: uuid.UUID,
    *,
    key=None,
    exp_offset: int = 3600,
    aud: str = "authenticated",
    iss: str = ISSUER,
    role: str = "authenticated",
    kid: str = "test-kid",
    email: str = "u@test.dev",
) -> str:
    now = int(time.time())
    claims = {
        "sub": str(user_id),
        "aud": aud,
        "iss": iss,
        "role": role,
        "email": email,
        "iat": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(claims, key or PRIVATE_KEY, algorithm="RS256", headers={"kid": kid})


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    from sqlalchemy import event

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    registry.Base.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    from sqlalchemy.orm import Session

    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def app(engine):
    get_settings.cache_clear()
    limiter.reset()
    from app.main import create_app

    application = create_app()
    application.state.jwks_client = FakeJWKSClient()
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def auth(user_id):
    return {"Authorization": f"Bearer {make_token(user_id)}"}


@pytest.fixture()
def other_auth():
    return {"Authorization": f"Bearer {make_token(uuid.uuid4())}"}
