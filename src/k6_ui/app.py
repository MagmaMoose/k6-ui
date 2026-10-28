import asyncio
import hmac
import re
import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .archive import ArchiveError
from .config import Branding, Config, Profile, SCENARIO
from .entra import Entra
from .kube import Kube, KubeError
from .oidc import SignInError
from .service import Console, ConsoleError
from .session import Cookies, Sealer

SOURCE = "https://github.com/MagmaMoose/k6-ui"
CSP = "; ".join(["default-src 'none'", "script-src 'self'", "style-src 'self'", "img-src 'self' data:", "font-src 'self'", "connect-src 'self'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'"])

def theme_css(brand: Branding) -> str:
    """The operator's colours over styles.css, whose `:root` loses to `html:root` in any order."""

    def rules(accent: str, text: str, soft: int) -> str:
        found = [f"--accent:{accent}", f"--accent-soft:color-mix(in srgb,{accent} {soft}%,var(--surface))"] if accent else []
        return ";".join(found + ([f"--accent-text:{text}"] if text else []))

    light = rules(brand.accent, brand.accent_text, 10)
    dark = rules(brand.accent_dark or brand.accent, brand.accent_text_dark or brand.accent_text, 14)
    return (f"html:root{{{light}}}\n" if light else "") + (f"@media (prefers-color-scheme: dark){{html:root{{{dark}}}}}\n" if dark else "")

def release_notes(version: str) -> str | None:
    """Where a version's changes are written up: its GitHub release, or the pull request it was built from."""
    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        return f"{SOURCE}/releases/tag/{version}"
    if found := re.fullmatch(r"pr-([0-9]+)", version):
        return f"{SOURCE}/pull/{found.group(1)}"
    return None

def favicon_svg(brand: Branding) -> str:
    tile, glyph = brand.accent or "#0f766e", brand.accent_text or "#ffffff"
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="{tile}"/><path d="M7 21l6-12v12l6-12v12l6-12" fill="none" stroke="{glyph}" stroke-width="2.5" stroke-linejoin="round"/></svg>'


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test: str = Field(pattern=SCENARIO, max_length=300)
    profile: Profile
    target: str = Field(default="", max_length=2048)


def create_app(cfg: Config, transport=None, kube=None, bundle=None):
    http = httpx.AsyncClient(transport=transport, follow_redirects=False)
    idp = Entra(cfg, http)
    kube = kube or Kube.in_cluster()
    console = Console(cfg, kube, **({"bundle": bundle} if bundle else {}))
    cookies = Cookies(Sealer(cfg.session_key), secure=cfg.secure_cookies)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(console.maintain())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await kube.aclose()
        await http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cookies, app.state.console, app.state.idp = cookies, console, idp

    @app.middleware("http")
    async def headers(request, call_next):
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 8192:
            return JSONResponse({"message": "Request is too large."}, 413)
        response = await call_next(request)
        response.headers.update({"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Cache-Control": "no-store" if request.url.path.startswith(("/api/", "/auth/")) else "no-cache"})
        if cfg.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"message": "The run settings are invalid.", "fields": [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]}, 422)

    @app.exception_handler(ConsoleError)
    async def console_error(request, exc):
        return JSONResponse({"message": str(exc)}, exc.status)

    @app.exception_handler(KubeError)
    async def kube_error(request, exc):
        return JSONResponse({"message": "Kubernetes is unavailable or refused the operation."}, 502)

    @app.exception_handler(ArchiveError)
    async def archive_error(request, exc):
        return JSONResponse({"message": str(exc)}, 422)

    @app.exception_handler(TimeoutError)
    async def archive_timeout(request, exc):
        return JSONResponse({"message": "Script bundling exceeded its 60-second deadline."}, 504)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"message": exc.detail}, exc.status_code)

    def session(request: Request):
        data = cookies.read_session(request)
        if data is None:
            raise HTTPException(401, "Sign in to continue.")
        return data

    Session = Annotated[dict, Depends(session)]

    def writable(request: Request, data: Session):
        if not hmac.compare_digest(request.headers.get("origin", "").encode(), cfg.origin.encode()) or not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), str(data.get("csrf", "")).encode()):
            raise HTTPException(403, "Request did not originate from this session.")
        return data

    Writable = Annotated[dict, Depends(writable)]

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        if not console.ready():
            return JSONResponse({"status": "scripts unavailable"}, 503)
        return {"status": "ok"}

    @app.get("/theme.css")
    async def theme():
        return Response(theme_css(cfg.branding), media_type="text/css")

    @app.get("/favicon.svg")
    async def favicon():
        return Response(favicon_svg(cfg.branding), media_type="image/svg+xml")

    @app.get("/auth/login")
    async def login():
        flow = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32), "verifier": secrets.token_urlsafe(64)}
        response = RedirectResponse(idp.authorize_url(**flow), 303)
        cookies.write_login(response, flow, 600)
        return response

    def back(error=""):
        response = RedirectResponse("/" + ("?" + urlencode({"auth_error": error}) if error else ""), 303)
        cookies.clear(response, cookies.login)
        return response

    @app.get("/auth/callback")
    async def callback(request: Request):
        flow = cookies.read_login(request)
        if not flow or not hmac.compare_digest(request.query_params.get("state", "").encode(), str(flow["state"]).encode()):
            return back("This sign-in expired. Sign in again.")
        if request.query_params.get("error"):
            return back("Sign-in was refused.")
        try:
            signed = await idp.redeem_code(code=request.query_params.get("code", ""), verifier=flow["verifier"], nonce=flow["nonce"])
        except SignInError:
            return back("Sign-in failed. Check the identity provider configuration.")
        response = back()
        cookies.write_session(request, response, {"user": signed.user, "groups": signed.groups, "name": signed.name,
                                                 "upn": signed.upn, "csrf": secrets.token_urlsafe(32)}, cfg.session_seconds)
        return response

    @app.post("/auth/logout")
    async def logout(request: Request, actor: Writable):
        response = JSONResponse({"signed_out": True})
        cookies.clear_session(request, response)
        return response

    @app.get("/api/me")
    async def me(actor: Session):
        return {"name": actor.get("name") or actor.get("upn"), "csrf": actor["csrf"], "version": cfg.version,
                "release_notes": release_notes(cfg.version), "environments": await console.catalog(actor)}

    @app.get("/api/environments/{environment}/runs")
    async def history(environment: str, actor: Session):
        return {"runs": await console.history(actor, environment)}

    @app.post("/api/environments/{environment}/runs", status_code=201)
    async def start(environment: str, body: Start, actor: Writable):
        return await console.start(actor, environment, body.test, body.profile, body.target)

    @app.get("/api/environments/{environment}/runs/{name}")
    async def detail(environment: str, name: str, actor: Session):
        return await console.detail(actor, environment, name)

    @app.post("/api/environments/{environment}/runs/{name}/stop")
    async def stop(environment: str, name: str, actor: Writable):
        return await console.stop(actor, environment, name)

    if cfg.static_dir and cfg.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="frontend")
    return app
