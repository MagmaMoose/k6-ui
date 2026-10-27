import asyncio

import httpx

from k6_ui.kube import Kube, KubeError


def test_kubernetes_token_is_read_after_rotation_and_logs_are_bounded(tmp_path):
    token = tmp_path / "token"; token.write_text("first")
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/log"):
            return httpx.Response(200, text="summary")
        return httpx.Response(200, json={"items": []})
    kube = Kube(server="https://kube.example.com", token_file=str(token), transport=httpx.MockTransport(handle))
    asyncio.run(kube.list("/api/v1/namespaces/loadtest/pods"))
    token.write_text("rotated")
    assert asyncio.run(kube.text("/api/v1/namespaces/loadtest/pods/test/log", {"limitBytes": "8192"})) == "summary"
    assert calls[0].headers["authorization"] == "Bearer first"
    assert calls[1].headers["authorization"] == "Bearer rotated"
    assert calls[1].url.params["limitBytes"] == "8192"
    asyncio.run(kube.aclose())
