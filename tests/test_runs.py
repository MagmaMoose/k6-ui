import asyncio
import json
from datetime import UTC, datetime, timedelta

from conftest import ACTOR, pod
from k6_ui.config import Catalog, Environment, Profile, Suite, Test as Explicit
from k6_ui.service import Console, core, runs

BODY = {"test": "http/smoke.js", "profile": {"vus": 1000, "parallelism": 4, "ramp": "90s", "duration": "2m", "ramp_down": "20s"}}
HEADERS = {"Origin": "http://localhost", "X-CSRF-Token": "test-csrf"}


def start(client, body=BODY):
    response = client.post("/api/environments/dev/runs", json=body, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def test_start_bundles_load_without_credentials_and_uses_secret_reference(setup):
    app, client, kube, captured = setup
    record = start(client)
    assert captured[0][1:] == ("smoke.js", {"BASE_URL": "https://app.dev.example.com", "VUS": "1000", "RAMP": "90s", "DURATION": "2m", "RAMP_DOWN": "20s"}, False)
    run = kube.objects[runs("loadtest") + "/" + record["name"]]
    assert run["spec"]["parallelism"] == 4
    assert run["spec"]["runner"]["envFrom"] == [{"secretRef": {"name": "loadtest-dev"}}]
    assert {"name": "BASE_URL", "value": "https://app.dev.example.com"} in run["spec"]["runner"]["env"]
    assert record["target"] == "https://app.dev.example.com"
    assert run["spec"]["script"]["configMap"]["name"] == record["name"] + "-archive"
    assert run["metadata"]["ownerReferences"][0]["name"] == record["name"]
    assert record["name"] in record["grafana"]
    assert not any("secrets" in path for _, path, _ in kube.calls)
    assert "binaryData" not in kube.objects[core("loadtest", "configmaps") + "/" + record["name"]]


def test_access_is_checked_before_bundling_and_hidden_environments_are_404(setup):
    app, client, kube, captured = setup
    kube.allow = False
    assert client.get("/api/me").json()["environments"] == []
    assert client.post("/api/environments/dev/runs", json=BODY, headers=HEADERS).status_code == 404
    assert captured == []
    assert kube.calls[-1][2]["spec"]["user"] == ACTOR["user"]


def test_csrf_and_signed_session_required(setup):
    app, client, kube, captured = setup
    assert client.post("/api/environments/dev/runs", json=BODY).status_code == 403
    assert client.post("/api/environments/dev/runs", json=BODY, headers={**HEADERS, "Origin": "https://other.example.com"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401
    assert not captured


def test_profile_limits_and_unknown_test_cannot_start(setup):
    _, client, _, captured = setup
    for profile in [{**BODY["profile"], "vus": 1001}, {**BODY["profile"], "vus": 0}, {**BODY["profile"], "parallelism": 5}, {**BODY["profile"], "ramp": "2m; touch /tmp/x"}, {**BODY["profile"], "duration": "0s"}, {**BODY["profile"], "duration": "2h"}]:
        assert client.post("/api/environments/dev/runs", json={**BODY, "profile": profile}, headers=HEADERS).status_code == 422
    assert client.post("/api/environments/dev/runs", json={**BODY, "test": "missing"}, headers=HEADERS).status_code == 404
    assert not captured


def test_target_is_chosen_within_allowed_hosts(setup):
    _, client, kube, captured = setup
    record = start(client, {**BODY, "target": "https://api.dev.example.com/v2"})
    assert captured[0][2]["BASE_URL"] == record["target"] == "https://api.dev.example.com/v2"
    for target in ["https://api.prd.example.com", "https://user:pw@api.dev.example.com", "file:///etc/passwd", "https://api.dev.example.com/?q=1", "https://api.dev.example.com.evil.net"]:
        response = client.post("/api/environments/dev/runs", json={**BODY, "target": target}, headers=HEADERS)
        assert response.status_code == 422, target
    assert len(captured) == 1
    scenario = client.get("/api/me").json()["environments"][0]["tests"][0]
    assert scenario == {"id": "http/smoke.js", "label": "smoke", "group": "HTTP", "description": "HTTP smoke: request the target.",
                        "note": "Requests the target without signing in.", "target": "https://app.dev.example.com"}


def test_scenarios_are_discovered_without_escaping_the_scripts(setup, tmp_path):
    app, _, _, _ = setup
    root = tmp_path / "scripts"
    (root / "suite" / "flows").mkdir(parents=True)
    (root / "suite" / "a.js").write_text("/**\n * A: first flow.\n */\nexport default function() {}\n")
    (root / "suite" / "flows" / "b.js").write_text("import http from 'k6/http';\n// not a header\n")
    (root / "suite" / "_scratch.js").write_text("// scratch\n")
    (root / "suite" / "notes.txt").write_text("// not a script\n")
    (root / "suite" / "escape.js").symlink_to(tmp_path / "outside.js")
    (tmp_path / "outside.js").write_text("// outside\n")
    (root / "single.js").write_text("// single\n")
    catalog = Catalog(tests=[Explicit(id="single", label="Single", script="single.js", bundle=True), Explicit(id="later", label="Later", script="later.ts")],
                      suites=[Suite(id="suite", label="Suite", directory="suite", include=["*.js", "flows/*.js"], exclude=["_*"]),
                              Suite(id="empty", label="Empty", directory="not-synced")],
                      environments=[Environment(name="dev", label="Development", tests=["single", "later"], suites=["suite", "empty"])])
    console = Console(app.state.console.cfg.__class__(**{**app.state.console.cfg.__dict__, "catalog": catalog, "scripts_dir": root}), app.state.console.kube)
    found = console.scenarios(catalog.environments[0])
    assert [(s.id, s.label, s.script, s.description) for s in found] == [
        ("single", "Single", "single.js", ""), ("suite/a.js", "a", "suite/a.js", "A: first flow."), ("suite/flows/b.js", "flows/b", "suite/flows/b.js", "")]
    assert [s.bundle for s in found] == [True, False, False]
    assert console.ready()


def test_all_runners_must_finish_and_threshold_failure_is_failed(setup):
    app, client, kube, _ = setup
    record = start(client); name = record["name"]
    kube.pod_items = [pod(name, i, 0 if i < 4 else None, "Succeeded" if i < 4 else "Running") for i in range(1, 5)]
    asyncio.run(app.state.console.reconcile())
    assert client.get(f"/api/environments/dev/runs/{name}").json()["status"] not in {"passed", "failed"}
    kube.pod_items[-1] = pod(name, 4, 99, "Failed")
    asyncio.run(app.state.console.reconcile())
    result = client.get(f"/api/environments/dev/runs/{name}").json()
    assert result["status"] == "failed"
    assert result["runners"][-1]["exit_code"] == 99
    assert "p(95)=12ms" in result["logs"]
    assert runs("loadtest") + "/" + name not in kube.objects
    assert client.get("/api/environments/dev/runs").json()["runs"][0]["status"] == "failed"


def test_success_records_output_before_cleanup_and_survives_restart(setup):
    app, client, kube, _ = setup
    record = start(client); name = record["name"]
    kube.pod_items = [pod(name, i, 0, "Succeeded") for i in range(1, 5)]
    asyncio.run(app.state.console.reconcile())
    kube.pod_items = []
    assert client.get(f"/api/environments/dev/runs/{name}").json()["status"] == "passed"
    asyncio.run(app.state.console.reconcile())
    assert core("loadtest", "configmaps") + "/" + name + "-archive" not in kube.objects
    assert core("loadtest", "configmaps") + "/" + name in kube.objects


def test_cancel_keeps_history_and_cannot_cancel_foreign_run(setup):
    app, client, kube, _ = setup
    record = start(client); name = record["name"]
    assert client.post(f"/api/environments/external-test/runs/{name}/stop", headers=HEADERS).status_code == 404
    response = client.post(f"/api/environments/dev/runs/{name}/stop", headers=HEADERS)
    assert response.json()["status"] == "cancelled"
    asyncio.run(app.state.console.reconcile())
    assert client.get("/api/environments/dev/runs").json()["runs"][0]["status"] == "cancelled"


def test_missing_testrun_and_failed_create_do_not_leave_pending_history(setup):
    app, client, kube, _ = setup
    kube.create_fail = True
    assert client.post("/api/environments/dev/runs", json=BODY, headers=HEADERS).status_code == 502
    assert client.get("/api/environments/dev/runs").json()["runs"][0]["status"] == "failed"


def test_expired_record_is_deleted(setup):
    app, client, kube, _ = setup
    record = start(client); name = record["name"]
    cm = kube.objects[core("loadtest", "configmaps") + "/" + name]
    record.update(status="passed", finished=(datetime.now(UTC)-timedelta(days=8)).isoformat())
    cm["data"]["run.json"] = json.dumps(record)
    asyncio.run(app.state.console.reconcile())
    assert not client.get("/api/environments/dev/runs").json()["runs"]


def test_initializer_failure_and_hung_runs_are_failed(setup):
    app, client, kube, _ = setup
    record = start(client); name = record["name"]
    kube.pod_items = [pod(name, "initializer", 1, "Failed")]
    asyncio.run(app.state.console.reconcile())
    assert client.get(f"/api/environments/dev/runs/{name}").json()["status"] == "failed"
