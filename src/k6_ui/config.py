import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
DURATION = r"^(?:[0-9]+h)?(?:[0-9]+m)?(?:[0-9]+s)?$"
VARIABLE = r"^([A-Z][A-Z0-9_]{0,63})?$"
SCENARIO = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(/[A-Za-z0-9_.-]+){0,8}$"
LOAD = {"VUS", "RAMP", "DURATION", "RAMP_DOWN"}


def seconds(value: str) -> int:
    if not value or not re.fullmatch(DURATION, value):
        raise ValueError("Use a duration such as 90s, 2m or 1h30m.")
    return sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"([0-9]+)([hms])", value))


def relative(path: str) -> str:
    if Path(path).is_absolute() or ".." in PurePosixPath(path).parts:
        raise ValueError("Script paths must stay inside the scripts directory.")
    return path


def check_env(env: dict[str, str], target: str):
    for key, value in env.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key) or re.search(r"SECRET|PASSWORD|TOKEN|CREDENTIAL|API_KEY", key):
            raise ValueError("Test env accepts non-secret configuration only. Use a Kubernetes Secret for credentials.")
        if key in LOAD or len(value) > 2048:
            raise ValueError("Test env cannot override the load profile.")
    if target in LOAD or re.search(r"SECRET|PASSWORD|TOKEN|CREDENTIAL|API_KEY", target):
        raise ValueError("A target variable cannot be a load or credential setting.")


def target_url(value: str, allowed: list[str]) -> str:
    """The URL a run may load: http(s), no credentials, and a host the environment allows."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or len(value) > 2048:
        raise ValueError("Enter the target as an http(s) URL without credentials, query or fragment.")
    if not any(fnmatch(parts.hostname, pattern) for pattern in allowed):
        raise ValueError(f"{parts.hostname} is not an allowed target in this environment.")
    return value


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vus: int = Field(default=20, ge=1, le=10000, strict=True)
    parallelism: int = Field(default=1, ge=1, le=16, strict=True)
    ramp: str = "30s"
    duration: str = "2m"
    ramp_down: str = "20s"

    @model_validator(mode="after")
    def valid(self):
        if self.parallelism > self.vus:
            raise ValueError("Runner count cannot exceed virtual users.")
        if seconds(self.duration) < 1:
            raise ValueError("Duration must be positive.")
        if seconds(self.ramp) + seconds(self.duration) + seconds(self.ramp_down) > 3600:
            raise ValueError("Total duration cannot exceed one hour.")
        return self

    def variables(self) -> dict[str, str]:
        return {"VUS": str(self.vus), "RAMP": self.ramp, "DURATION": self.duration, "RAMP_DOWN": self.ramp_down}


class Test(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=NAME)
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    script: str = Field(min_length=1, max_length=300)
    env: dict[str, str] = Field(default_factory=dict)
    target: str = Field(default="", pattern=VARIABLE)
    bundle: bool = False

    @model_validator(mode="after")
    def valid(self):
        relative(self.script)
        check_env(self.env, self.target)
        return self


class Suite(BaseModel):
    """Every script matching `include` under `directory` is a scenario, so new scripts need no catalog change."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=NAME)
    label: str = Field(min_length=1, max_length=120)
    directory: str = Field(default=".", min_length=1, max_length=300)
    include: list[str] = Field(default_factory=lambda: ["*.js"], min_length=1, max_length=20)
    exclude: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=500)
    env: dict[str, str] = Field(default_factory=dict)
    target: str = Field(default="", pattern=VARIABLE)
    bundle: bool = False

    @model_validator(mode="after")
    def valid(self):
        for path in [self.directory, *self.include, *self.exclude]:
            relative(path)
        check_env(self.env, self.target)
        return self


class Environment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    label: str = Field(min_length=1, max_length=120)
    namespace: str = Field(default="loadtest", pattern=NAME)
    secret: str = Field(default="", max_length=253)
    tests: list[str] = Field(default_factory=list)
    suites: list[str] = Field(default_factory=list)
    targets: dict[str, str] = Field(default_factory=dict)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=50)
    groups: list[str] = Field(default_factory=list)
    defaults: Profile = Field(default_factory=Profile)
    max_vus: int = Field(default=1000, ge=1, le=10000)
    max_parallelism: int = Field(default=4, ge=1, le=16)
    grafana_url: str = ""

    @model_validator(mode="after")
    def valid(self):
        if self.secret and not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", self.secret):
            raise ValueError("Secret must be a Kubernetes name.")
        if not self.tests and not self.suites:
            raise ValueError("An environment needs at least one test or suite.")
        if any(not re.fullmatch(r"[a-z0-9*?.-]{1,253}", h) for h in self.allowed_hosts):
            raise ValueError("Allowed hosts are lowercase host names; * and ? match any characters.")
        for name, url in self.targets.items():
            if name not in self.tests + self.suites:
                raise ValueError(f"Target {name} names no test or suite of this environment.")
            target_url(url, self.allowed_hosts)
        if self.grafana_url:
            parts = urlsplit(self.grafana_url)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.query or parts.fragment:
                raise ValueError("Grafana URL must use HTTPS without credentials, query or fragment.")
        self.check_profile(self.defaults)
        return self

    def check_profile(self, profile: Profile):
        if profile.vus > self.max_vus or profile.parallelism > self.max_parallelism:
            raise ValueError("Load exceeds this environment's configured limit.")


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tests: list[Test] = Field(default_factory=list)
    suites: list[Suite] = Field(default_factory=list)
    environments: list[Environment] = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self):
        ids = [t.id for t in self.tests] + [s.id for s in self.suites]
        names = [e.name for e in self.environments]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ValueError("Test and suite IDs and environment names must be unique.")
        if any(set(e.tests) - {t.id for t in self.tests} or set(e.suites) - {s.id for s in self.suites} for e in self.environments):
            raise ValueError("Every environment test and suite must exist in the catalog.")
        return self

    @classmethod
    def read(cls, path: str):
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


@dataclass(frozen=True)
class Branding:
    accent: str = ""
    accent_text: str = ""
    accent_dark: str = ""
    accent_text_dark: str = ""


@dataclass(frozen=True)
class Config:
    tenant_id: str
    client_id: str
    origin: str
    session_key: bytes
    catalog: Catalog
    scripts_dir: Path
    client_secret: str | None = None
    federated_token_file: Path | None = None
    authority: str = "https://login.microsoftonline.com"
    session_seconds: int = 28800
    username_claim: str = "preferred_username"
    username_prefix: str = "oidc:"
    groups_claim: str = "groups"
    groups_prefix: str = "oidc:"
    static_dir: Path | None = None
    port: int = 8080
    version: str = "dev"
    retention_hours: int = 168
    branding: Branding = field(default_factory=Branding)

    @property
    def redirect_uri(self):
        return f"{self.origin}/auth/callback"

    @property
    def secure_cookies(self):
        return self.origin.startswith("https://")


def secret(env, key):
    return env.get(key) or (Path(env[key + "_FILE"]).read_text().strip() if env.get(key + "_FILE") else "")


def origin(value):
    p = urlsplit(value)
    if not p.hostname or p.username or p.password or p.path not in ("", "/") or p.query or p.fragment:
        raise ValueError("Public origin must be scheme://host[:port].")
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Public origin requires HTTPS, except on loopback.")
    return value.rstrip("/")


def load_config(env=None):
    e = dict(os.environ if env is None else env)
    key = secret(e, "K6_UI_SESSION_KEY")
    if len(key) < 32:
        raise ValueError("K6_UI_SESSION_KEY needs at least 32 characters.")
    ids = [e.get(n, "") for n in ["AZURE_TENANT_ID", "AZURE_CLIENT_ID"]]
    if not all(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", i) for i in ids):
        raise ValueError("AZURE_TENANT_ID and AZURE_CLIENT_ID must be GUIDs.")
    token = e.get("AZURE_FEDERATED_TOKEN_FILE")
    client_secret = secret(e, "AZURE_CLIENT_SECRET")
    if not token and not client_secret:
        raise ValueError("Configure workload identity or AZURE_CLIENT_SECRET_FILE.")
    brand = {name: e.get("K6_UI_" + name.upper(), "") for name in Branding.__dataclass_fields__}
    if any(v and not re.fullmatch(r"#[0-9a-fA-F]{6}", v) for v in brand.values()):
        raise ValueError("Branding colours must be #rrggbb.")
    hours = int(e.get("K6_UI_RETENTION_HOURS", "168"))
    session_hours = int(e.get("K6_UI_SESSION_HOURS", "8"))
    if not 1 <= hours <= 720 or not 1 <= session_hours <= 24:
        raise ValueError("Retention must be 1–720 hours; sessions 1–24 hours.")
    authority = origin(e.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com"))
    if not authority.startswith("https://"):
        raise ValueError("Azure authority requires HTTPS.")
    return Config(tenant_id=ids[0].lower(), client_id=ids[1].lower(), origin=origin(e["K6_UI_PUBLIC_ORIGIN"]),
                  session_key=key.encode(), catalog=Catalog.read(e["K6_UI_CATALOG"]),
                  scripts_dir=Path(e.get("K6_UI_SCRIPTS_DIR", "/scripts")),
                  client_secret=client_secret or None, federated_token_file=Path(token) if token else None,
                  authority=authority, session_seconds=session_hours * 3600,
                  username_claim=e.get("K6_UI_USERNAME_CLAIM", "preferred_username"),
                  username_prefix=e.get("K6_UI_USERNAME_PREFIX", "oidc:"),
                  groups_claim=e.get("K6_UI_GROUPS_CLAIM", "groups"), groups_prefix=e.get("K6_UI_GROUPS_PREFIX", "oidc:"),
                  static_dir=Path(e.get("K6_UI_STATIC_DIR", "/app/static")), port=int(e.get("PORT", "8080")),
                  version=e.get("APP_VERSION", "dev"), retention_hours=hours, branding=Branding(**brand))
