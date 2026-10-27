import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from k6_ui.archive import ArchiveError, MAX_ARCHIVE_BYTES, request_archive
from k6_ui.archiver import create_archiver


def test_archiver_accepts_script_variables_but_no_filesystem_root(tmp_path):
    captured = []

    async def builder(root, script, variables):
        captured.append((root, script, variables))
        return b"archive"

    with TestClient(create_archiver(tmp_path, builder)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/archive", json={"script": "smoke.js", "variables": {"VUS": "1000"}}).content == b"archive"
        assert captured == [(tmp_path, "smoke.js", {"VUS": "1000"})]
        assert client.post("/archive", json={"script": "smoke.js", "variables": {}, "root": "/etc"}).status_code == 422
        assert client.post("/archive", json={"script": "smoke.js", "variables": {"invalid-key": "value"}}).status_code == 422


def test_archiver_errors_do_not_return_script_output(tmp_path):
    async def builder(*args):
        raise ArchiveError("script output")

    with TestClient(create_archiver(tmp_path, builder)) as client:
        response = client.post("/archive", json={"script": "smoke.js", "variables": {}})
        assert response.status_code == 422
        assert b"script output" not in response.content


def test_web_requests_archive_from_loopback(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "https://invalid.example.com")

    def respond(request):
        assert request.url == "http://127.0.0.1:8081/archive"
        assert b'"script":"smoke.js"' in request.content
        assert b'"VUS":"1000"' in request.content
        return httpx.Response(200, content=b"archive")

    assert asyncio.run(request_archive(Path("/scripts"), "smoke.js", {"VUS": "1000"}, httpx.MockTransport(respond))) == b"archive"


@pytest.mark.parametrize("status,content", [(422, b"script output"), (200, b""), (200, b"x" * (MAX_ARCHIVE_BYTES + 1))])
def test_web_refuses_bad_archive_responses(status, content):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=content))
    with pytest.raises(ArchiveError):
        asyncio.run(request_archive(Path("/scripts"), "smoke.js", {}, transport))
