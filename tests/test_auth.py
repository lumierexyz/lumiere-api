import uuid

from tests.conftest import OTHER_KEY, make_token
from tests.fixtures import monthly_statement_csv


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_missing_token_rejected(client):
    r = client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_valid_token_provisions_profile(client, auth, user_id):
    r = client.get("/api/v1/me", headers=auth)
    assert r.status_code == 200
    assert r.json()["id"] == str(user_id)


def test_expired_token_rejected(client, user_id):
    r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {make_token(user_id, exp_offset=-10)}"})
    assert r.status_code == 401 and r.json()["error"]["message"] == "Token expired."


def test_wrong_audience_issuer_role_signature_rejected(client, user_id):
    for token in (
        make_token(user_id, aud="other"),
        make_token(user_id, iss="https://evil.example/auth/v1"),
        make_token(user_id, role="service_role"),
        make_token(user_id, key=OTHER_KEY),
        make_token(user_id, kid="unknown"),
    ):
        assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_alg_none_rejected(client, user_id):
    import jwt

    token = jwt.encode({"sub": str(user_id), "aud": "authenticated"}, key=None, algorithm="none")
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_users_cannot_read_or_modify_each_others_data(client, auth, other_auth):
    files = {"file": ("s.csv", monthly_statement_csv(3), "text/csv")}
    assert client.post("/api/v1/transactions/import", headers=auth, files=files).status_code == 201
    mine = client.get("/api/v1/transactions", headers=auth).json()
    assert mine["total"] > 0
    theirs = client.get("/api/v1/transactions", headers=other_auth).json()
    assert theirs["total"] == 0
    tx_id = mine["items"][0]["id"]
    r = client.patch(f"/api/v1/transactions/{tx_id}", headers=other_auth, json={"category": "housing"})
    assert r.status_code == 404
    assert client.get("/api/v1/expenses/summary", headers=other_auth).json()["has_data"] is False


def test_body_user_id_is_ignored(client, auth, user_id):
    r = client.patch("/api/v1/me", headers=auth, json={"display_name": "A", "id": str(uuid.uuid4())})
    assert r.status_code == 200 and r.json()["id"] == str(user_id)


def test_delete_financial_data_and_account(client, auth):
    client.post(
        "/api/v1/transactions/import",
        headers=auth,
        files={"file": ("s.csv", monthly_statement_csv(2), "text/csv")},
    )
    r = client.delete("/api/v1/me/data", headers=auth)
    assert r.status_code == 200
    assert client.get("/api/v1/transactions", headers=auth).json()["total"] == 0
    assert client.delete("/api/v1/me", headers=auth).status_code == 400  # confirmation required
    r = client.delete("/api/v1/me?confirm=true", headers=auth)
    assert r.status_code == 200 and r.json()["deleted_financial_data"] is True
