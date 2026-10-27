import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from k6_ui.app import create_app
from k6_ui.config import Catalog, Config
from k6_ui.kube import KubeError

ACTOR = {"user": "oidc:qa@example.com", "groups": ["oidc:qa"], "name": "QA Tester", "csrf": "test-csrf"}


class FakeKube:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.allow = True
        self.create_fail = False
        self.pod_items = []
        self.log_text = "checks passed\nhttp_req_duration: p(95)=12ms"

    async def create(self, path, body):
        self.calls.append(("POST", path, copy.deepcopy(body)))
        if "subjectaccessreviews" in path:
            return {"status": {"allowed": self.allow}}
        if self.create_fail and "/testruns" in path:
            raise KubeError(403, "denied")
        result = copy.deepcopy(body)
        result["metadata"].update(uid="uid-" + result["metadata"]["name"], resourceVersion="1")
        self.objects[path + "/" + result["metadata"]["name"]] = result
        return copy.deepcopy(result)

    async def get(self, path):
        return copy.deepcopy(self.objects.get(path))

    async def list(self, path, selector=""):
        if path.endswith("/pods"):
            return copy.deepcopy(self.pod_items)
        found = [v for p, v in self.objects.items() if p.rsplit("/", 1)[0] == path]
        return copy.deepcopy([v for v in found if all(v["metadata"].get("labels", {}).get(k) == val for k, val in (s.split("=", 1) for s in selector.split(",") if s))])

    async def patch(self, path, body):
        obj = self.objects[path]
        if body.get("metadata", {}).get("resourceVersion") != obj["metadata"]["resourceVersion"]:
            raise KubeError(409, "conflict")
        obj.setdefault("data", {}).update(body.get("data", {}))
        obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        return copy.deepcopy(obj)

    async def delete(self, path):
        self.calls.append(("DELETE", path, None))
        self.objects.pop(path, None)

    async def text(self, path, params):
        return self.log_text

    async def aclose(self):
        pass


@pytest.fixture
def setup(tmp_path):
    catalog = Catalog.read(str(Path(__file__).parents[1] / "examples/catalog.yaml"))
    cfg = Config(tenant_id="11111111-1111-1111-1111-111111111111", client_id="22222222-2222-2222-2222-222222222222",
                 origin="http://localhost", session_key=b"s" * 32, catalog=catalog, scripts_dir=tmp_path, client_secret="test-only", static_dir=None)
    captured = []

    async def bundle(root, script, variables):
        captured.append((root, script, variables))
        return b"test archive"

    kube = FakeKube()
    app = create_app(cfg, kube=kube, bundle=bundle)
    client = TestClient(app, base_url=cfg.origin)
    response = Response()
    app.state.cookies.write_session(Request({"type": "http", "headers": []}), response, ACTOR, 3600)
    for cookie in response.headers.getlist("set-cookie"):
        name, value = cookie.split(";", 1)[0].split("=", 1)
        client.cookies.set(name, value, domain="localhost.local", path="/")
    return app, client, kube, captured


def pod(name, runner, exit_code=None, phase="Running"):
    state = {"running": {}} if exit_code is None else {"terminated": {"exitCode": exit_code}}
    return {"metadata": {"name": f"{name}-{runner}-pod", "labels": {"app": "k6", "job-name": f"{name}-{runner}"}},
            "status": {"phase": phase, "containerStatuses": [{"state": state}]}}
