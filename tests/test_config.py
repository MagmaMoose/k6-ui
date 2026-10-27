from pathlib import Path

import pytest
from pydantic import ValidationError

from k6_ui.config import Catalog, Environment, Profile, Test as Scenario, load_config, seconds


def test_duration_parser():
    assert seconds("1h2m3s") == 3723
    assert seconds("90s") == 90
    for value in ["", "1.5m", "--quiet", "1m2h", "2s 3s"]:
        with pytest.raises(ValueError): seconds(value)


def test_catalog_rejects_script_escape_and_secrets():
    for script in ["../escape.js", "/etc/passwd"]:
        with pytest.raises(ValidationError): Scenario(id="test", label="Test", script=script)
    with pytest.raises(ValidationError): Scenario(id="test", label="Test", script="test.js", env={"CLIENT_SECRET": "private"})
    with pytest.raises(ValidationError): Scenario(id="test", label="Test", script="test.js", env={"VUS": "1"})
    with pytest.raises(ValidationError): Catalog(tests=[Scenario(id="test", label="Test", script="test.js")], environments=[Environment(name="dev", label="Development", tests=["missing"])])


def test_startup_refuses_bad_identity_origin_and_branding(tmp_path):
    catalog = Path(__file__).parents[1] / "examples/catalog.yaml"
    env = {"AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111", "AZURE_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
           "AZURE_CLIENT_SECRET": "test-only", "K6_UI_SESSION_KEY": "s"*32, "K6_UI_PUBLIC_ORIGIN": "https://loadtest.example.com", "K6_UI_CATALOG": str(catalog)}
    assert load_config(env).catalog.environments[0].name == "dev"
    for key, value in [("AZURE_CLIENT_ID", "invalid"), ("K6_UI_PUBLIC_ORIGIN", "http://remote.example.com"), ("K6_UI_ACCENT", "red;url(x)"), ("K6_UI_SESSION_KEY", "short"), ("K6_UI_RETENTION_HOURS", "0")]:
        with pytest.raises(ValueError): load_config({**env, key: value})


def test_ready_requires_defined_scripts(setup):
    app, client, _, _ = setup
    assert client.get("/readyz").status_code == 503
    (app.state.console.cfg.scripts_dir / "smoke.js").write_text("export default function() {}")
    assert client.get("/readyz").status_code == 200
    assert "style-src 'self'" in client.get("/theme.css").headers["Content-Security-Policy"]
