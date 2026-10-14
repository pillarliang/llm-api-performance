"""Verify a deployed service without emitting credentials or triggering models."""
import json
from pathlib import Path
import urllib.request

root = Path(__file__).resolve().parents[1]
for origin in ("http://127.0.0.1:8001", "http://47.237.15.194:8001"):
    with urllib.request.urlopen(origin + "/healthz", timeout=15) as response:
        data = json.load(response)
        assert data["status"] == "ok"
        print(origin + "/healthz", response.status, data["service"])
request = urllib.request.Request("http://127.0.0.1:8001/api/config")
with urllib.request.urlopen(request, timeout=10) as response:
    data = json.load(response)
    assert all(item["configured"] for item in data["credentials"])
    print("No-login configuration: OK; model credentials: configured")

for origin in ("http://127.0.0.1:8001", "http://47.237.15.194:8001"):
    with urllib.request.urlopen(origin + "/api/models", timeout=50) as response:
        data = json.load(response)
        assert data["count"] == len(data["models"])
        ids = {item["id"] for item in data["models"]}
        assert {"glm-5.3", "glm-5.2", "qwen3.8-max", "qwen3.8-flash"} <= ids
        assert not data["stale"], data.get("error")
        print(origin + "/api/models", response.status, data["count"], "models; focus models available")
