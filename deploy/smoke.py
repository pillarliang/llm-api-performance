"""One real 512-token request to verify the deployed workbench end to end."""
import json
from pathlib import Path
import time
import urllib.request

root = Path(__file__).resolve().parents[1]
origin = "http://127.0.0.1:8001"


def call(path, data=None):
    request = urllib.request.Request(origin + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


config = call("/api/config")["config"]
config.update(name="deployment-smoke", version="v1", repetitions=1, warmup=0, concurrencies=[1],
              input_tokens=[512], timeout_seconds=60, parameters={"max_tokens": 256},
              tasks=[{"id": "deployment-smoke", "template_file": "prompts/smoke.txt", "corpus_file": "prompts/corpus.txt"}])
model = next(model for model in config["models"] if model["id"] == "qwen3.8-flash")
model["profiles"] = [next(profile for profile in model["profiles"] if profile["name"] == "off")]
model["enabled"] = True
config["models"] = [model]
plan = call("/api/plan", {"config": config, "mode": "incremental"})
assert plan["planned_cases"] == 1
started = call("/api/runs", {"config": config, "mode": "incremental"})
for _ in range(90):
    state = call("/api/status")
    if state["status"] not in {"starting", "running"}:
        break
    time.sleep(1)
else:
    call("/api/stop", {})
    raise SystemExit("Live smoke exceeded 90 seconds; stopped and preserved progress")
result = call("/api/artifact/" + started["path"] + "/results.json")
print(json.dumps({"path": started["path"], "status": result["status"], "totals": result["totals"],
                  "metrics": result["summary"][0]["metrics"], "errors": result["summary"][0]["errors"]}, ensure_ascii=False))
assert result["totals"]["successful_cases"] == 1, "Live model request failed; inspect preserved result"
assert call("/api/plan", {"config": config, "mode": "incremental"})["pending_calls"] == 0
request = urllib.request.Request(origin + "/api/artifact/" + started["path"] + "/report.html")
with urllib.request.urlopen(request, timeout=10) as response:
    report = response.read().decode()
    assert "__RESULT_DATA__" not in report and "renderReport" in report
print("Live smoke: success; incremental plan skips the successful request; offline report downloadable.")
