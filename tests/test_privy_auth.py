"""Privy mode: ES256 access tokens from the app's JWKS, DID subjects, server-side email lookup."""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.auth import privy_admin
from app.auth.jwt import privy_user_uuid
from app.core.config import get_settings
from app.core.rate_limit import limiter

APP_ID = "test-privy-app"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER = ec.generate_private_key(ec.SECP256R1())
DID = "did:privy:cm0abc123"


class _Key:
    def __init__(self, k):
        self.key = k


class PrivyJWKS:
    def get_signing_key_from_jwt(self, token):
        if jwt.get_unverified_header(token).get("kid") != "privy-kid":
            raise jwt.PyJWKClientError("no key")
        return _Key(KEY.public_key())


def privy_token(sub=DID, aud=APP_ID, iss="privy.io", key=KEY, exp=3600, alg="ES256"):
    now = int(time.time())
    claims = {"sid": "s1", "sub": sub, "aud": aud, "iss": iss, "iat": now, "exp": now + exp}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "privy-kid"})


@pytest.fixture()
def privy_client(engine, monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "privy")
    monkeypatch.setenv("PRIVY_APP_ID", APP_ID)
    monkeypatch.setenv("PRIVY_APP_SECRET", "test-secret")
    get_settings.cache_clear()
    limiter.reset()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization", "")[:6]))
        if request.method == "GET":
            return httpx.Response(
                200, json={"id": DID, "linked_accounts": [{"type": "email", "address": "ada@example.com"}]}
            )
        return httpx.Response(200, json={})

    privy_admin.set_transport(httpx.MockTransport(handler))
    from app.main import create_app

    app = create_app()
    app.state.jwks_client = PrivyJWKS()
    yield TestClient(app), calls
    privy_admin.set_transport(None)
    get_settings.cache_clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_privy_token_creates_profile_with_verified_email(privy_client):
    client, calls = privy_client
    r = client.get("/api/v1/me", headers=auth(privy_token()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(privy_user_uuid(DID))
    assert body["email"] == "ada@example.com"
    assert calls[0][:2] == ("GET", f"/api/v1/users/{DID}") and calls[0][2] == "Basic "


def test_same_did_same_account_different_did_isolated(privy_client):
    client, _ = privy_client
    up = client.post(
        "/api/v1/transactions/import",
        headers=auth(privy_token()),
        files={"file": ("a.csv", b"Date,Description,Amount\n2026-01-02,RENT,-1000\n", "text/csv")},
    )
    assert up.status_code == 201
    assert client.get("/api/v1/transactions", headers=auth(privy_token())).json()["total"] == 1
    other = privy_token(sub="did:privy:someoneelse")
    assert client.get("/api/v1/transactions", headers=auth(other)).json()["total"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"aud": "another-app"},
        {"iss": "evil.io"},
        {"key": OTHER},
        {"exp": -5},
        {"sub": "not-a-did"},
    ],
)
def test_privy_rejections(privy_client, kwargs):
    client, _ = privy_client
    assert client.get("/api/v1/me", headers=auth(privy_token(**kwargs))).status_code == 401


def test_privy_rejects_supabase_style_tokens(privy_client):
    client, _ = privy_client
    hs = jwt.encode(
        {"sub": DID, "aud": APP_ID, "iss": "privy.io", "iat": 1, "exp": int(time.time()) + 60},
        "x" * 32,
        algorithm="HS256",
    )
    assert client.get("/api/v1/me", headers=auth(hs)).status_code == 401


def test_account_deletion_removes_privy_user(privy_client):
    client, calls = privy_client
    client.get("/api/v1/me", headers=auth(privy_token()))
    r = client.delete("/api/v1/me?confirm=true", headers=auth(privy_token()))
    assert r.status_code == 200 and r.json()["deleted_auth_account"] is True
    assert ("DELETE", f"/api/v1/users/{DID}") in [c[:2] for c in calls]


def test_ready_reports_privy_configured(privy_client):
    client, _ = privy_client
    checks = client.get("/ready").json()["checks"]
    assert checks["auth_configured"] is True


def test_secret_never_in_responses(privy_client):
    client, _ = privy_client
    blob = (
        json.dumps(client.get("/openapi.json").json())
        + client.get("/api/v1/me", headers=auth(privy_token())).text
    )
    assert "test-secret" not in blob
