"""Single-process control plane for the benchmark, served on port 8001."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import yaml

from benchmark import BenchmarkRun, plan, read_records, validate_config
from catalog import CatalogError, ModelCatalog
from report import build_report

ROOT = Path(__file__).resolve().parent
RESULTS = Path(os.environ.get("RESULTS_ROOT", ROOT / "results")).resolve()
CONFIG = Path(os.environ.get("CONFIG_PATH", ROOT / "config.local.yaml"))
DEFAULT = ROOT / "config.focus.example.yaml"
active_task = None
active_state = None
run_guard = asyncio.Lock()
model_catalog = ModelCatalog(CONFIG.parent / "model-catalog.json")


@asynccontextmanager
async def lifespan(app):
    yield
    if active_task and not active_task.done():
        active_task.cancel()
        try:
            await active_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Model Studio · LLM 性能评测", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/healthz")
async def health():
    return {"status": "ok", "service": "llm-performance"}


@app.get("/api/models")
async def available_models(refresh: bool = False):
    base_url = os.environ.get("DASHSCOPE_BASE_URL")
    if not base_url:
        base_url = next((model["base_url"] for model in current_config()["models"]
                         if model["api_key_env"] == "DASHSCOPE_API_KEY"), None)
    if not base_url:
        raise HTTPException(503, "尚未配置 DASHSCOPE_BASE_URL")
    try:
        return await model_catalog.get(base_url, os.environ.get("DASHSCOPE_API_KEY"), refresh=refresh)
    except CatalogError as exc:
        raise HTTPException(502, str(exc)) from exc


def current_config():
    return checked_config((CONFIG if CONFIG.exists() else DEFAULT).read_text(encoding="utf-8"))


def checked_config(content):
    config = validate_config(yaml.safe_load(content))
    # Browser configuration can only read bundled task assets.
    for task in config["tasks"]:
        for field in ("template_file", "corpus_file"):
            path = (ROOT / task[field]).resolve()
            if not path.is_relative_to(ROOT / "prompts"):
                raise ValueError("Web 配置的任务文件必须位于 prompts/ 目录")
    return validate_config(config, ROOT)


async def payload(request):
    body = await request.body()
    if len(body) > 262144:
        raise HTTPException(413, "配置不得超过 256 KiB")
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, TypeError):
        raise HTTPException(400, "请求必须为 JSON 对象")


def parse_config(data):
    try:
        content = yaml.safe_dump(data["config"], allow_unicode=True) if isinstance(data.get("config"), dict) else data.get("yaml")
        if not isinstance(content, str):
            raise ValueError("缺少 YAML 配置")
        return checked_config(content)
    except (ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/config")
async def get_config():
    config = current_config()
    return {"config": config, "yaml": yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            "credentials": [{"name": name, "configured": bool(os.environ.get(name))} for name in sorted({m["api_key_env"] for m in config["models"]})]}


@app.post("/api/config/validate")
async def validate_editor(request: Request):
    config = parse_config(await payload(request))
    return {"config": config, "yaml": yaml.safe_dump(config, allow_unicode=True, sort_keys=False)}


@app.put("/api/config")
async def save_config(request: Request):
    config = parse_config(await payload(request))
    async with run_guard:
        if active_task and not active_task.done():
            raise HTTPException(409, "评测运行中，结束或停止后再保存配置")
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG.with_suffix(".yaml.tmp")
        temporary.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temporary.replace(CONFIG)
    return {"saved": True}


@app.post("/api/plan")
async def get_plan(request: Request):
    data = await payload(request)
    config = parse_config(data)
    try:
        description, prepared, _ = await asyncio.to_thread(plan, config, ROOT, RESULTS, data.get("mode", "incremental"))
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return dict(description, groups_detail=[g for _, _, g in prepared])


async def execute(run):
    global active_state
    active_state["status"] = "running"
    try:
        await run.run()
        active_state["status"] = "finished"
    except asyncio.CancelledError:
        active_state["status"] = "cancelled"
    except Exception:
        # Do not return SDK exception strings that may contain HTTP credentials.
        active_state.update(status="error", error="执行失败，请检查结果日志、端点参数及服务配置")
    finally:
        source = run.output / "results.json"
        if source.exists():
            try:
                build_report(source)
            except (OSError, ValueError):
                active_state.update(status="error", error="评测结果已保存，但 HTML 报告生成失败，可通过 report.py 重建")


@app.post("/api/runs")
async def start_run(request: Request):
    global active_task, active_state
    data = await payload(request)
    config = parse_config(data)
    async with run_guard:
        if active_task and not active_task.done():
            raise HTTPException(409, "已有评测正在运行")
        try:
            description, prepared, output = await asyncio.to_thread(plan, config, ROOT, RESULTS, data.get("mode", "incremental"))
            records = read_records(output / "requests.jsonl")
            from benchmark import plan_jobs
            jobs = plan_jobs(prepared, records, config["repetitions"])
            missing = {m["api_key_env"] for m, _, g in prepared if jobs[g["group_id"]] and not os.environ.get(m["api_key_env"])}
            if missing:
                raise ValueError("缺少环境变量：" + ", ".join(sorted(missing)))
            if (output / ".run.lock").exists():
                raise ValueError("该 run 已锁定，请检查是否正在通过命令行执行")
            run = BenchmarkRun(config, ROOT, output, prepared)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        active_state = dict(status="starting", path=str(output.relative_to(RESULTS)), session_id=run.session_id, plan=description)
        active_task = asyncio.create_task(execute(run))
    return active_state


@app.post("/api/stop")
async def stop():
    if active_task and not active_task.done():
        active_task.cancel()
        try:
            await active_task
        except asyncio.CancelledError:
            if active_state:
                active_state["status"] = "cancelled"
    return {"stopped": True}


@app.get("/api/status")
async def status():
    state = dict(active_state) if active_state else {"status": "idle"}
    if active_state:
        directory = RESULTS / active_state["path"]
        # The event loop writes complete lines synchronously; status runs between writes.
        records = read_records(directory / "requests.jsonl")
        state["session_attempts"] = sum(r["session_id"] == state["session_id"] and r["phase"] == "measured" for r in records)
        state["session_successes"] = sum(r["session_id"] == state["session_id"] and r["phase"] == "measured" and r["success"] for r in records)
        events = directory / "events.jsonl"
        state["events"] = read_records(events)[-30:]
    return state


@app.get("/api/runs")
async def list_runs():
    items = []
    for path in sorted(RESULTS.glob("*/*/runs/*/results.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items.append(dict(path=str(path.parent.relative_to(RESULTS)), name=data["settings"]["name"],
                              version=data["settings"]["version"], run_id=path.parent.name,
                              started_at=data["started_at"], status=data["status"], totals=data["totals"], data_source=data["data_source"]))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(items, key=lambda item: item["started_at"], reverse=True)


@app.get("/api/artifact/{artifact_path:path}")
async def artifact(artifact_path: str):
    path = (RESULTS / artifact_path).resolve()
    if not path.is_relative_to(RESULTS) or path.name not in {"results.json", "report.html", "requests.jsonl", "events.jsonl"} or not path.is_file():
        raise HTTPException(404, "结果文件不存在")
    return FileResponse(path, filename=path.name)
