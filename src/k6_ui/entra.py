"""Signing in with Entra ID, as a confidential client (the flow itself is in oidc.py), and the
app's own token for Azure Storage.

The credential is a workload identity token (AZURE_FEDERATED_TOKEN_FILE, which Entra trusts
through a federated credential on the app registration) or a client secret. People sign in to the
app registration the cluster's API server trusts, so their ID token names them the way Kubernetes
does: the user and groups claims, with the API server's prefixes, are what access reviews ask about.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Config
from .oidc import OIDC, SignIn, SignInError

_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def federated_credential(token_file: str) -> dict[str, str]:
    try:
        # The kubelet rotates the projected token in place.
        with open(token_file) as f:
            assertion = f.read().strip()
    except OSError as err:
        raise SignInError(f"cannot read the workload identity token: {err.__class__.__name__}", status=500) from err
    return {"client_assertion_type": _ASSERTION_TYPE, "client_assertion": assertion}


def app_credential(cfg: Config) -> dict[str, str]:
    """The app's credential on Entra ID's token endpoint, read fresh each time."""
    if cfg.federated_token_file is None:
        return {"client_secret": cfg.client_secret or ""}
    return federated_credential(str(cfg.federated_token_file))


class Entra(OIDC):
    name = "Microsoft"

    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        base = f"{cfg.authority}/{cfg.tenant_id}"
        super().__init__(
            http,
            client_id=cfg.client_id,
            redirect_uri=cfg.redirect_uri,
            scope="openid profile",
            endpoints={"authorize": f"{base}/oauth2/v2.0/authorize", "token": f"{base}/oauth2/v2.0/token", "jwks": f"{base}/discovery/v2.0/keys"},
            issuers=(f"{base}/v2.0",),
        )
        self._cfg = cfg

    def _credential(self) -> dict[str, str]:
        return app_credential(self._cfg)

    def _check_claims(self, claims: dict[str, Any]) -> None:
        cfg = self._cfg
        if str(claims.get("tid", "")).lower() != cfg.tenant_id or not claims.get("oid"):
            raise SignInError("that account belongs to another tenant", status=401)
        if not isinstance(claims.get(cfg.username_claim), str) or not claims[cfg.username_claim]:
            raise SignInError(f"the ID token has no {cfg.username_claim}, which is how Kubernetes names you", status=401)
        # Over 200 groups, Entra leaves them out and points at Graph instead.
        if cfg.groups_claim in (claims.get("_claim_names") or {}):
            raise SignInError(f"your {cfg.groups_claim} do not fit in the ID token, so Kubernetes would not know them either", status=401)

    async def _sign_in(self, body: dict[str, Any], claims: dict[str, Any]) -> SignIn:
        cfg = self._cfg
        found = claims.get(cfg.groups_claim) or []
        return SignIn(
            oid=str(claims["oid"]).lower(),
            upn=str(claims.get("preferred_username") or claims.get("upn") or claims["oid"])[:120],
            name=str(claims.get("name") or "")[:120],
            # As the API server takes them: the claim as it is, with its prefix.
            user=f"{cfg.username_prefix}{claims[cfg.username_claim]}",
            groups=[f"{cfg.groups_prefix}{g}" for g in (found if isinstance(found, list) else [found]) if isinstance(g, str)][:200],
        )
