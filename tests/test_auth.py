import asyncio
import base64
import json
import time
from dataclasses import replace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from k6_ui.entra import Entra
from k6_ui.oidc import SignInError
from k6_ui.session import Sealer


def test_encrypted_sessions_reject_forgery_wrong_purpose_and_expiry():
    sealer = Sealer(b"a"*32)
    token = sealer.seal("session", {"user": "qa@example.com"}, 60)
    assert sealer.open("session", token)["user"] == "qa@example.com"
    assert sealer.open("login", token) is None
    assert sealer.open("session", token[:-4] + "AAAA") is None
    assert sealer.open("session", sealer.seal("session", {}, -1)) is None
    assert sealer.open("session", "bad") is None


def test_oidc_verifies_signature_issuer_audience_and_nonce(setup):
    app, _, _, _ = setup
    cfg = app.state.console.cfg
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())); jwk["kid"] = "key-1"
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"keys": [jwk]})))
    idp = Entra(cfg, http)
    claims = {"iss": f"{cfg.authority}/{cfg.tenant_id}/v2.0", "aud": cfg.client_id, "exp": int(time.time()) + 300,
              "iat": int(time.time()), "nonce": "nonce-1", "tid": cfg.tenant_id, "oid": "33333333-3333-3333-3333-333333333333", "preferred_username": "QA@example.com", "groups": ["qa"]}
    token = lambda changes: jwt.encode({**claims, **changes}, private, algorithm="RS256", headers={"kid": "key-1"})
    verified = asyncio.run(idp.verify_id_token(token({}), nonce="nonce-1"))
    signed = asyncio.run(idp._sign_in({}, verified))
    assert signed.user == "oidc:QA@example.com" and signed.groups == ["oidc:qa"]
    for changes in [{"aud": "other"}, {"iss": "https://other.example.com"}, {"nonce": "wrong"}, {"tid": "other"}, {"exp": 1}, {"preferred_username": ""}]:
        with pytest.raises(SignInError): asyncio.run(idp.verify_id_token(token(changes), nonce="nonce-1"))
    asyncio.run(http.aclose())


def test_callback_requires_its_original_flow_and_logout_clears_session(setup):
    app, client, _, _ = setup
    assert "auth_error" in client.get("/auth/callback?code=code&state=forged", follow_redirects=False).headers["location"]
    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", headers={"Origin": "http://localhost", "X-CSRF-Token": "test-csrf"}).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_callback_redeems_valid_flow_and_keeps_identity_mapping(setup):
    from urllib.parse import parse_qs, urlsplit
    from k6_ui.oidc import SignIn
    app, client, _, _ = setup
    client.cookies.clear()
    response = client.get("/auth/login", follow_redirects=False)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]

    async def redeem(**kwargs):
        assert kwargs["nonce"] == query["nonce"][0]
        assert len(kwargs["verifier"]) >= 43
        return SignIn("id", "qa@example.com", "QA Tester", "oidc:qa@example.com", ["oidc:qa"])

    app.state.idp.redeem_code = redeem
    response = client.get("/auth/callback", params={"code": "valid", "state": query["state"][0]}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/api/me").json()["name"] == "QA Tester"
