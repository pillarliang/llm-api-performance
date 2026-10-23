"""Single-process control plane for the benchmark, served on port 8001."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
import yaml

from benchmark import BenchmarkRun, atomic_json, now, plan, read_records, validate_config
from catalog import CatalogError, ModelCatalog
from connections import bind_providers, default_connection, normalize_base, resolve_connection
from report import build_report
from storage import get_store, StorageError, ARTIFACTS, encoded, valid_key, write_bytes

ROOT = Path(__file__).resolve().parent
RESULTS = Path(os.environ.get("RESULTS_ROOT", ROOT / "results")).resolve()
CONFIG = Path(os.environ.get("CONFIG_PATH", ROOT / "config.local.yaml"))
DEFAULT = ROOT / "config.focus.example.yaml"
active_task = None
active_state = None
run_guard = asyncio.Lock()
archive_guard = asyncio.Lock()
run_registry = {}
model_catalog = ModelCatalog(CONFIG.parent / "model-catalog.json")


@asynccontextmanager
async def lifespan(app):
    store = get_store(RESULTS)
    staged_jobs = set((RESULTS / ".jobs").glob("*.json"))
    if store.remote:
        # A failed final upload is retried before reading history from OSS.
        await asyncio.to_thread(store.retry_pending)
        for key in await asyncio.to_thread(store.keys, ".jobs/"):
            if key.endswith(".json"):
                local = store.path(key)
                if not local.exists():
                    write_bytes(local, await asyncio.to_thread(store.read, key))
    # Only public job metadata is persisted; API credentials remain in memory.
    for path in sorted((RESULTS / ".jobs").glob("*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            directory = (RESULTS / state["path"]).resolve()
            if not directory.is_relative_to(RESULTS):
                continue
            if state["status"] in {"starting", "running"}:
                if store.remote:
                    for name in ARTIFACTS:
                        if not (directory / name).exists():
                            content = await asyncio.to_thread(store.read, state["path"] + "/" + name)
                            if content is not None:
                                write_bytes(directory / name, content)
                state.update(status="interrupted", error="服务重启，任务已中断；可使用增量模式补跑。")
                records = [r for r in read_records(directory / "requests.jsonl")
                           if r["session_id"] == state["session_id"] and r["phase"] == "measured"]
                completed = {r["case_id"]: r for r in records if r.get("case_complete")}
                state["progress"] = dict(completed=len(completed), succeeded=sum(r["success"] for r in completed.values()),
                                         failed=sum(not r["success"] for r in completed.values()), active=0, retrying=0,
                                         attempts=len(records), retries=len(records)-len({r["case_id"] for r in records}))
                atomic_json(path, state)
                # This service owns these jobs; a crashed previous worker may leave a lock.
                (directory / ".run.lock").unlink(missing_ok=True)
            if store.remote and (state["status"] == "interrupted" or path in staged_jobs):
                # Keep any newer local logs left by a crash and archive them.
                if not directory.exists():
                    directory.mkdir(parents=True)
                for name in ARTIFACTS:
                    if not (directory / name).exists():
                        content = await asyncio.to_thread(store.read, state["path"] + "/" + name)
                        if content is not None:
                            write_bytes(directory / name, content)
                state["has_results"] = (directory / "results.json").exists()
                await asyncio.to_thread(store.archive, directory, state)
            elif store.remote:
                state["has_results"] = await asyncio.to_thread(store.read, state["path"] + "/results.json") is not None
                path.unlink(missing_ok=True)
            else:
                state["has_results"] = (directory / "results.json").exists()
            run_registry[state["job_id"]] = dict(state=state, task=None, run=None)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    retry_task = asyncio.create_task(retry_archives()) if store.remote else None
    yield
    tasks = [entry["task"] for entry in run_registry.values() if entry["task"] and not entry["task"].done()]
    for entry in run_registry.values():
        if entry["task"] and not entry["task"].done() and entry["state"]["status"] in {"starting", "running"}:
            entry["task"].cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if retry_task:
        retry_task.cancel()
        await asyncio.gather(retry_task, return_exceptions=True)


app = FastAPI(title="Model Studio · LLM 性能评测", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.exception_handler(StorageError)
async def storage_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


async def retry_archives():
    while True:
        await asyncio.sleep(15)
        try:
            async with archive_guard:
                recovered = await asyncio.to_thread(get_store(RESULTS).retry_pending)
                for state in recovered:
                    entry = run_registry.get(state["job_id"])
                    if entry:
                        entry["state"].clear()
                        entry["state"].update(state)
        except StorageError:
            pass  # The durable outbox remains available for the next retry.


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/healthz")
async def health():
    return {"status": "ok", "service": "llm-performance", "results_backend": os.environ.get("RESULTS_BACKEND", "local")}


def default_base():
    return yaml.safe_load(DEFAULT.read_text(encoding="utf-8"))["models"][0]["base_url"]


@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def connection_models(connection, refresh=False):
    try:
        base, _, key = resolve_connection(connection, default_base())
        # Custom credentials and model lists remain request-local, never in shared disk caches.
        service = ModelCatalog(None) if connection.get("mode") == "custom" else model_catalog
        return await service.get(base, key, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except CatalogError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/models")
async def available_models(refresh: bool = False):
    return await connection_models({"mode": "default"}, refresh)


@app.post("/api/models")
async def custom_models(request: Request):
    data = await payload(request)
    return await connection_models(data.get("connection", {}), bool(data.get("refresh")))


def bind_connection(config, data, require_key=False):
    try:
        if 'providers' in config:
            return bind_providers(config, data.get('connections', []), default_base(), require_key=require_key)
        connection = data.get("connection", {"mode": "default"})
        base, name, key = resolve_connection(connection, default_base(), require_key=require_key)
        for model in config["models"]:
            # Legacy callers may omit connection, but may not redirect a server-owned key.
            if "connection" not in data and normalize_base(model["base_url"]) != base:
                raise ValueError("自定义端点需要提供独立的连接配置和 API Key")
            model.pop("base_url_env", None)
            model.update(base_url=base, api_key_env=name)
        return {name: key}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def current_config():
    return checked_config((CONFIG if CONFIG.exists() else DEFAULT).read_text(encoding="utf-8"))


def checked_config(content):
    config = validate_config(yaml.safe_load(content), allow_empty_models=True)
    # Browser configuration can only read bundled task assets.
    for task in config["tasks"]:
        for field in ("template_file", "corpus_file"):
            path = (ROOT / task[field]).resolve()
            if not path.is_relative_to(ROOT / "prompts"):
                raise ValueError("Web 配置的任务文件必须位于 prompts/ 目录")
    return validate_config(config, ROOT, allow_empty_models=True)


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


def parse_config(data, require_key=False):
    try:
        content = yaml.safe_dump(data["config"], allow_unicode=True) if isinstance(data.get("config"), dict) else data.get("yaml")
        if not isinstance(content, str):
            raise ValueError("缺少 YAML 配置")
        config = yaml.safe_load(content)
        if not isinstance(config, dict):
            raise ValueError('配置必须是对象')
        credentials = bind_connection(config, data, require_key=require_key)
        return checked_config(yaml.safe_dump(config, allow_unicode=True)), credentials
    except (ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/config")
async def get_config():
    config = current_config()
    base, _, key = default_connection(default_base())
    return {"connection": {"mode": "default", "base_url": base, "configured": bool(key)},
            "config": config, "yaml": yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            "credentials": [{"name": name, "configured": bool(os.environ.get(name))} for name in sorted({m["api_key_env"] for m in config["models"]})]}


@app.post("/api/config/validate")
async def validate_editor(request: Request):
    config, _ = parse_config(await payload(request))
    return {"config": config, "yaml": yaml.safe_dump(config, allow_unicode=True, sort_keys=False)}


@app.put("/api/config")
async def save_config(request: Request):
    data = await payload(request)
    config, _ = parse_config(data)
    async with run_guard:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG.with_suffix(".yaml.tmp")
        temporary.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temporary.replace(CONFIG)
    return {"saved": True}


@app.post("/api/plan")
async def get_plan(request: Request):
    data = await payload(request)
    config, _ = parse_config(data, require_key=True)
    try:
        description, prepared, _ = await asyncio.to_thread(plan, config, ROOT, RESULTS, data.get("mode", "incremental"))
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return dict(description, groups_detail=[g for _, _, g in prepared])


def job_snapshot(entry):
    state = dict(entry["state"])
    run = entry["run"]
    if run is not None and hasattr(run, "progress"):
        state["progress"] = dict(run.progress)
    progress = state.get("progress", {})
    state["session_attempts"] = progress.get("attempts", 0)
    state["session_successes"] = progress.get("succeeded", 0)
    return state


def persist_job(entry):
    (RESULTS / ".jobs").mkdir(parents=True, exist_ok=True)
    atomic_json(RESULTS / ".jobs" / (entry["state"]["job_id"] + ".json"), job_snapshot(entry))


async def archive_job(entry, output):
    store = get_store(RESULTS)
    if not store.remote:
        return
    async with archive_guard:
        snapshot = job_snapshot(entry)
        snapshot.pop("storage_pending", None)
        entry["state"].update(storage_pending=True, has_results=False)
        try:
            await asyncio.to_thread(store.archive, output, snapshot)
            entry["state"].clear()
            entry["state"].update(snapshot)
        except StorageError as exc:
            entry["state"].update(storage_pending=True, has_results=False, error=str(exc))
            persist_job(entry)


async def execute(run, entry):
    state = entry["state"]
    state["status"] = "running"
    try:
        await run.run()
        state["status"] = "finished"
    except asyncio.CancelledError:
        state["status"] = "cancelled"
    except Exception:
        state.update(status="error", error="执行失败，请检查结果、端点参数及服务配置")
    finally:
        if run.credentials is not None:
            run.credentials.clear()
        source = run.output / "results.json"
        state["has_results"] = source.exists()
        if source.exists():
            try:
                build_report(source)
            except (OSError, ValueError):
                state.update(status="error", error="评测结果已保存，但 HTML 报告生成失败，可通过 report.py 重建")
        state["finished_at"] = now()
        state.update(job_snapshot(entry))
        persist_job(entry)
        await archive_job(entry, run.output)
        entry["run"] = None


@app.post("/api/runs")
async def start_run(request: Request):
    global active_task, active_state
    data = await payload(request)
    config, credentials = parse_config(data, require_key=True)
    async with run_guard:
        try:
            description, prepared, output = await asyncio.to_thread(plan, config, ROOT, RESULTS, data.get("mode", "full"))
            records = read_records(output / "requests.jsonl")
            from benchmark import plan_jobs
            jobs = plan_jobs(prepared, records, config["repetitions"])
            missing = {m["api_key_env"] for m, _, g in prepared if jobs[g["group_id"]] and not credentials.get(m["api_key_env"])}
            if missing:
                raise ValueError("缺少环境变量：" + ", ".join(sorted(missing)))
            reserved = any(e["state"]["path"] == str(output.relative_to(RESULTS)) and e["state"]["status"] in {"starting", "running"} for e in run_registry.values())
            if reserved or (output / ".run.lock").exists():
                raise HTTPException(409, "该运行正在执行，不能同时增量补跑；请选择「新建运行」开启独立任务。")
            run = BenchmarkRun(config, ROOT, output, prepared, credentials=credentials)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        active_state = dict(job_id=run.session_id, status="starting", name=config["name"], version=config["version"],
                            started_at=now(), path=str(output.relative_to(RESULTS)), session_id=run.session_id, plan=description,
                            targets=[dict(provider=m.get("provider_name", m["id"]), model=m["model"]) for m in config["models"] if m.get("enabled", True)],
                            input_tokens=config["input_tokens"], concurrencies=config["concurrencies"])
        entry = dict(state=active_state, task=None, run=run)
        persist_job(entry)
        store = get_store(RESULTS)
        if store.remote:
            try:
                await asyncio.to_thread(store.put, ".jobs/" + run.session_id + ".json", encoded(job_snapshot(entry)))
            except StorageError:
                credentials.clear()
                run.credentials.clear()
                (RESULTS / ".jobs" / (run.session_id + ".json")).unlink(missing_ok=True)
                raise
        run_registry[run.session_id] = entry
        active_task = entry["task"] = asyncio.create_task(execute(run, entry))
    return job_snapshot(entry)


async def stop_entry(entry):
    task = entry["task"]
    if task and not task.done() and entry["state"]["status"] in {"starting", "running"}:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if entry["state"]["status"] in {"starting", "running"}:
            entry["state"]["status"] = "cancelled"
            if entry["run"] and entry["run"].credentials is not None:
                entry["run"].credentials.clear()
            persist_job(entry)
            if entry["run"]:
                await archive_job(entry, entry["run"].output)
            entry["run"] = None
    return {"stopped": True}


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    if job_id not in run_registry:
        raise HTTPException(404, "任务不存在")
    return await stop_entry(run_registry[job_id])


@app.post("/api/stop")
async def stop():
    if active_state and active_state.get("job_id") in run_registry:
        return await stop_entry(run_registry[active_state["job_id"]])
    return {"stopped": True}


@app.get("/api/jobs")
async def list_jobs():
    return [job_snapshot(entry) for entry in reversed(list(run_registry.values()))]


@app.get("/api/status")
async def status():
    if active_state and active_state.get("job_id") in run_registry:
        return job_snapshot(run_registry[active_state["job_id"]])
    return dict(active_state) if active_state else {"status": "idle"}


@app.get("/api/runs")
async def list_runs():
    return await asyncio.to_thread(read_runs)


def read_runs():
    store = get_store(RESULTS)
    items = []
    for key in store.keys():
        path = Path(key)
        if len(path.parts) != 5 or path.parts[2] != "runs" or path.name != "results.json":
            continue
        try:
            data = json.loads(store.read(key))
            items.append(dict(path=str(path.parent), name=data["settings"]["name"],
                              version=data["settings"]["version"], run_id=path.parent.name,
                              started_at=data["started_at"], status=data["status"], totals=data["totals"], data_source=data["data_source"]))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(items, key=lambda item: item["started_at"], reverse=True)


@app.get("/api/artifact/{artifact_path:path}")
async def artifact(artifact_path: str):
    try:
        valid_key(artifact_path)
    except ValueError:
        raise HTTPException(404, "结果文件不存在")
    if Path(artifact_path).name not in ARTIFACTS:
        raise HTTPException(404, "结果文件不存在")
    store = get_store(RESULTS)
    if store.remote:
        data = await asyncio.to_thread(store.read, artifact_path)
        if data is None:
            raise HTTPException(404, "结果文件不存在")
        media_type = {"results.json": "application/json", "report.html": "text/html"}.get(Path(artifact_path).name, "application/x-ndjson")
        return Response(data, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{Path(artifact_path).name}"'})
    path = (RESULTS / artifact_path).resolve()
    if not path.is_relative_to(RESULTS) or path.name not in {"results.json", "report.html", "requests.jsonl", "events.jsonl"} or not path.is_file():
        raise HTTPException(404, "结果文件不存在")
    return FileResponse(path, filename=path.name)
