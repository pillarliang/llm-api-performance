"""Streaming benchmark runner. Dry-run by default; use --execute to call APIs."""
import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import time

try:
    import openai
    import httpx
except ImportError:
    openai = None
    httpx = None
import yaml

from metrics import measure, summarize, totals
from tokens import TOKENIZER, count, make_prompt
from connections import normalize_providers
from storage import get_store, restore_latest, encoded, StorageError

CONFIG_PATH = Path(__file__).with_name("config.example.yaml")
UPDATE_MODE = "incremental"
EXECUTE = False
OUTPUT_ROOT = None
CHECKPOINT_SECONDS = 5
RESERVED = {"model", "messages", "stream", "stream_options", "n"}


def now():
    return datetime.now(timezone.utc).isoformat()


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def safe_name(value):
    if value is None or isinstance(value, bool):
        raise ValueError("name/version 必须是非空普通目录名")
    text = str(value)
    if not re.fullmatch(r"[\w.-]+", text) or text in {".", ".."}:
        raise ValueError("name/version 必须是非空普通目录名，不能包含路径分隔符")
    return text


def validate_config(config, config_dir=None, allow_empty_models=False):
    if not isinstance(config, dict):
        raise ValueError("配置必须是 YAML 对象")
    for key in ("name", "version", "repetitions", "warmup", "concurrencies", "input_tokens", "timeout_seconds", "cache_state", "tasks", "models"):
        if key not in config:
            raise ValueError(f"缺少配置字段：{key}")
    config["name"], config["version"] = safe_name(config["name"]), safe_name(config["version"])
    for key, minimum in (("repetitions", 1), ("warmup", 0)):
        if type(config[key]) is not int or config[key] < minimum:
            raise ValueError(f"{key} 必须是 >= {minimum} 的整数")
    for key in ("concurrencies", "input_tokens"):
        if not isinstance(config[key], list) or not config[key]:
            raise ValueError(f"{key} 必须是非空列表")
        for value in config[key]:
            if key == "input_tokens" and value is None:
                continue
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} 必须包含正整数")
    retries = config.get("max_retries", 0)
    if type(retries) is not int or not 0 <= retries <= 5:
        raise ValueError("max_retries 必须是 0 到 5 的整数")
    timeout = config["timeout_seconds"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds 必须是有限正数")
    def parameters(obj):
        params = obj.get("parameters", {})
        if not isinstance(params, dict) or RESERVED.intersection(params):
            raise ValueError("parameters 必须是对象，且不能覆盖 model/messages/stream/stream_options/n")
        json.dumps(params, allow_nan=False)
    parameters(config)
    for collection in ("tasks", "models"):
        if not isinstance(config[collection], list) or (not config[collection] and not (collection == 'models' and allow_empty_models)):
            raise ValueError(f"{collection} 必须是非空列表")
        if any(not isinstance(item, dict) for item in config[collection]):
            raise ValueError(f"{collection} 的条目必须是对象")
        ids = [item.get("id") for item in config[collection]]
        if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"{collection} 的 id 必须是唯一的非空字符串")
    for task in config["tasks"]:
        for field in ("template_file", "corpus_file"):
            if not isinstance(task.get(field), str):
                raise ValueError(f"任务缺少 {field}")
            if config_dir is not None and not (Path(config_dir) / task[field]).is_file():
                raise ValueError(f"任务文件不存在：{task[field]}")
    providers = None
    if 'providers' in config:
        config['providers'] = normalize_providers(config['providers'])
        providers = {p['id']: p for p in config['providers']}
    for model in config["models"]:
        if providers is not None:
            if not isinstance(model.get('provider_id'), str) or model['provider_id'] not in providers:
                raise ValueError('模型引用的连接源不存在')
            model['provider_name'] = providers[model['provider_id']]['name']
        if model.get("base_url_env"):
            if not isinstance(model["base_url_env"], str):
                raise ValueError("base_url_env 必须是环境变量名称")
            model["base_url"] = os.environ.get(model["base_url_env"], model.get("base_url", ""))
        parameters(model)
        for flag in ("enabled", "include_usage"):
            if flag in model and type(model[flag]) is not bool:
                raise ValueError(f"{flag} 必须为布尔值")
        for field in ("model", "base_url", "api_key_env"):
            if not isinstance(model.get(field), str) or not model[field].strip():
                raise ValueError(f"模型缺少 {field}")
        if not model["base_url"].startswith(("https://", "http://")):
            raise ValueError("base_url 必须是 HTTP(S) 地址")
        if not isinstance(model.get("profiles"), list):
            raise ValueError("profiles 必须是列表")
        for profile in model["profiles"]:
            if not isinstance(profile, dict):
                raise ValueError("profile 必须是对象")
            if not isinstance(profile.get("name"), str) or not profile["name"]:
                raise ValueError("profile name 必须是字符串，请为 off/on 加引号")
            if "thinking_enabled" not in profile or (profile["thinking_enabled"] is not None and type(profile["thinking_enabled"]) is not bool):
                raise ValueError("thinking_enabled 必须是布尔值或 null（未知）")
            parameters(profile)
    return config


def load_config(path):
    path = Path(path)
    return validate_config(yaml.safe_load(path.read_text(encoding="utf-8")), path.parent)


def make_group(config, task, model, profile, prompt, target, concurrency):
    parameters = {**config.get("parameters", {}), **model.get("parameters", {}), **profile.get("parameters", {})}
    group = dict(endpoint_id=model["id"], model=model["model"], base_url=model["base_url"].rstrip("/") + "/",
                 api_key_env=model["api_key_env"], profile=profile["name"], thinking_enabled=profile["thinking_enabled"],
                 parameters=parameters, task=task["id"], target_input_tokens=target,
                 benchmark_input_tokens=count(prompt), prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                 tokenizer=TOKENIZER, concurrency=concurrency, cache_state=config["cache_state"],
                 timeout_seconds=config["timeout_seconds"], warmup=config["warmup"], include_usage=model.get("include_usage", True))
    group["group_id"] = hashlib.sha256(json.dumps(group, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    # endpoint_id already identifies a source/model pair. Keep metadata outside
    # the hash so source labels and migration from legacy configs preserve work.
    if model.get('provider_id'):
        group['provider_id'] = model['provider_id']
        group['provider_name'] = model.get('provider_name', model['provider_id'])
    return group


def prepare(config, config_dir):
    prepared = []
    if config.get('visual_cases'):
        for case in config['visual_cases']:
            for concurrency in config['concurrencies']:
                for model in config['models']:
                    if model.get('enabled', True):
                        for profile in model['profiles']:
                            group = make_group(config, {'id': case['group']}, model, profile, case['prompt'], None, concurrency)
                            group.update(modality=case['modality'], media_case=case)
                            if '·' not in case['group']:
                                a = case['objects'][0]
                                group['task'] += f" · {a['width']}×{a['height']} · {case.get('duration_seconds', '')}s · {case.get('submitted_frame_count') or case.get('image_count', 1)}项 · {case['case_id'][:6]}"
                            group['group_id'] = hashlib.sha256(json.dumps(group, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
                            prepared.append((model, case['prompt'], group))
        if not prepared:
            raise ValueError('没有可执行的视觉评测组合')
        return prepared
    for task in config["tasks"]:
        for target in config["input_tokens"]:
            prompt = make_prompt(task, target, config_dir)
            for concurrency in config["concurrencies"]:
                for model in config["models"]:
                    if model.get("enabled", True):
                        for profile in model["profiles"]:
                            prepared.append((model, prompt, make_group(config, task, model, profile, prompt, target, concurrency)))
    if not prepared:
        raise ValueError("配置未生成任何测试组合")
    if len({g["group_id"] for _, _, g in prepared}) != len(prepared):
        raise ValueError("配置中存在重复测试组合")
    return prepared


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def resolve_output(config, config_dir, output_root=None, mode="incremental"):
    if mode not in {"full", "incremental"}:
        raise ValueError("update mode 必须为 full 或 incremental")
    root = (Path(output_root) if output_root else Path(os.environ.get("RESULTS_ROOT", Path(config_dir) / "results"))).resolve()
    store = get_store(root)
    if store.remote and mode == "incremental":
        restore_latest(store, config["name"], config["version"])
    version_dir = root / config["name"] / config["version"]
    latest = version_dir / "latest.json"
    if mode == "incremental" and latest.exists():
        run_id = safe_name(json.loads(latest.read_text())["run_id"])
        output = version_dir / "runs" / run_id
        if not output.is_dir():
            raise ValueError("latest.json 指向的 run 不存在，请检查数据或使用 full")
        return output, "incremental"
    return version_dir / "runs" / stamp(), "full"


def read_records(path):
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} 第 {line_number} 行损坏，请备份并检查") from exc
    return records


def plan_jobs(prepared, records, repetitions):
    successes = {r["case_id"] for r in records if r.get("phase") == "measured" and r.get("success")}
    return {group["group_id"]: [i for i in range(1, repetitions + 1) if f'{group["group_id"]}-{i:04d}' not in successes]
            for _, _, group in prepared}


def new_record(group, index, phase, session_id, attempt):
    case_id = f'{group["group_id"]}-{index:04d}' if phase == "measured" else f'{group["group_id"]}-warmup-{session_id}-{index:04d}'
    return dict(group, case_id=case_id, run_index=index, phase=phase, session_id=session_id, attempt=attempt,
                started_at=now(), success=False, error_type=None, finish_reason=None, stream_completed=False,
                status_code=None, request_id=None, first_output_seconds=None, last_output_seconds=None,
                stream_close_seconds=None, answer_chunks=[], usage=None, reasoning_observed=False)


def accept_chunk(record, chunk, elapsed, event):
    if not record["request_id"]:
        record["request_id"] = getattr(chunk, "id", None)
    if chunk.usage is not None:
        record["usage"] = chunk.usage.model_dump()
    if not chunk.choices:
        return
    choice = chunk.choices[0]
    if choice.finish_reason is not None:
        record["finish_reason"] = choice.finish_reason
    content = choice.delta.content
    reasoning = getattr(choice.delta, "reasoning_content", None)
    if content is not None and not isinstance(content, str):
        raise ValueError("content 必须为文本")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ValueError("reasoning_content 必须为文本")
    if content or reasoning:
        if record["first_output_seconds"] is None:
            record["first_output_seconds"] = elapsed
            event("first_output", case_id=record["case_id"], elapsed_seconds=elapsed)
        record["last_output_seconds"] = elapsed
    if reasoning:
        record["reasoning_observed"] = True
    if content:
        if not record["answer_chunks"]:
            event("first_answer", case_id=record["case_id"], elapsed_seconds=elapsed)
        record["answer_chunks"].append(dict(at=elapsed, text=content))


async def request(client, prompt, record, event=lambda *a, **k: None):
    event("request_started", case_id=record["case_id"], phase=record["phase"], group_id=record["group_id"])
    # Sign URLs before timing; signatures are never written to records or group identity.
    content = prompt
    if record.get('media_case'):
        from media_web import model_content
        try:
            content = await asyncio.to_thread(model_content, record['media_case'], record['timeout_seconds'])
        except Exception:
            record['error_type'] = 'media_preparation_error'
            return record
    start = time.perf_counter()
    stream = None
    try:
        async with asyncio.timeout(record["timeout_seconds"]):
            kwargs = dict(model=record["model"], messages=[dict(role="user", content=content)],
                          stream=True, extra_body=record["parameters"])
            if record["include_usage"]:
                kwargs["stream_options"] = {"include_usage": True}
            stream = await client.chat.completions.create(**kwargs)
            response = getattr(stream, "response", None)
            if response is not None:
                record["status_code"] = response.status_code
                record["request_id"] = response.headers.get("x-request-id")
            async for chunk in stream:
                accept_chunk(record, chunk, time.perf_counter() - start, event)
            record["stream_completed"] = True
            finish = record["finish_reason"]
            if finish is None:
                record["error_type"] = "stream_interrupted"
            elif finish != "stop":
                record["error_type"] = {"length": "truncated", "content_filter": "content_filter"}.get(finish, "unexpected_finish")
            elif not record["answer_chunks"]:
                record["error_type"] = "empty_answer"
            else:
                record["success"] = True
    except asyncio.CancelledError:
        record["error_type"] = "cancelled"
        raise
    except (TimeoutError, openai.APITimeoutError):
        record["error_type"] = "timeout"
    except openai.APIStatusError as exc:
        record.update(error_type="rate_limit" if exc.status_code == 429 else "http_error",
                      status_code=exc.status_code, request_id=exc.response.headers.get("x-request-id"))
    except (openai.APIConnectionError, httpx.TransportError, ConnectionError):
        record["error_type"] = "stream_interrupted" if record["first_output_seconds"] is not None else "connection_error"
    except (openai.APIError, ValueError, KeyError, TypeError):
        record["error_type"] = "invalid_stream"
    finally:
        record["stream_close_seconds"] = time.perf_counter() - start
        if stream is not None:
            try:
                await stream.close()
            except (httpx.TransportError, ConnectionError):
                if record["error_type"] is None:
                    record.update(success=False, error_type="stream_interrupted")
    return record


class BenchmarkRun:
    def __init__(self, config, config_dir, output, prepared=None, client_factory=None, credentials=None):
        if client_factory is None and openai is None:
            raise RuntimeError("缺少依赖，请先运行 uv sync")
        self.credentials = dict(credentials) if credentials is not None else None
        self.config = config
        self.config_dir = Path(config_dir)
        self.output = Path(output)
        self.prepared = prepared if prepared is not None else prepare(config, config_dir)
        self.client_factory = client_factory or openai.AsyncOpenAI
        self.session_id = stamp()
        self.started_at = now()
        self.records = []
        self.skipped = 0
        self.progress = dict(completed=0, succeeded=0, failed=0, active=0, retrying=0, retries=0, attempts=0, warmups=0)
        self.settings = dict(config, session_id=self.session_id, tokenizer=TOKENIZER,
                             environment={"python": platform.python_version(), "platform": platform.platform(),
                                          "openai": importlib.metadata.version("openai"), "tiktoken": importlib.metadata.version("tiktoken")})

    def event(self, event, **fields):
        self.events_file.write(json.dumps(dict(event=event, session_id=self.session_id, at=now(), **fields), ensure_ascii=False) + "\n")
        self.events_file.flush()

    def save(self, record):
        measure(record)
        self.records_file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        self.records_file.flush()
        self.records.append(record)
        self.event("request_finished", case_id=record["case_id"], attempt=record["attempt"], success=record["success"], error_type=record["error_type"])
        print(f'{record["case_id"]} {"OK" if record["success"] else record["error_type"]}', flush=True)

    async def run_one(self, client, prompt, group, index, phase):
        case = f'{group["group_id"]}-{index:04d}'
        measured = phase == "measured"
        # Warmups never consume the measured retry budget or progress denominator.
        budget = self.config.get("max_retries", 0) if measured else 0
        for retry in range(budget + 1):
            attempt = 1 + max((r["attempt"] for r in self.records if r["case_id"] == case), default=0)
            record = new_record(group, index, phase, self.session_id, attempt)
            again = False
            if measured:
                self.progress["active"] += 1
                self.progress["attempts"] += 1
                self.progress["retries"] += int(retry > 0)
            try:
                await request(client, prompt, record, self.event)
                transient = record["error_type"] in {"timeout", "rate_limit", "connection_error", "stream_interrupted"}
                transient |= record["error_type"] == "http_error" and (record.get("status_code") == 408 or 500 <= record.get("status_code", 0) <= 599)
                again = not record["success"] and transient and retry < budget
                record["case_complete"] = not again
            except Exception:
                record["error_type"] = record["error_type"] or "internal_error"
                raise
            finally:
                if measured:
                    self.progress["active"] -= 1
                self.save(record)
            if not again:
                if measured:
                    self.progress["completed"] += 1
                    self.progress["succeeded" if record["success"] else "failed"] += 1
                else:
                    self.progress["warmups"] += 1
                return
            delay = min(2 ** retry, 16)
            self.progress["retrying"] += 1
            self.event("retry_scheduled", case_id=case, delay_seconds=delay, retry=retry + 1)
            try:
                await asyncio.sleep(delay)
            finally:
                self.progress["retrying"] -= 1

    async def run_group(self, client, prompt, group, jobs):
        if not jobs:
            return
        for index in range(1, self.config["warmup"] + 1):
            await self.run_one(client, prompt, group, index, "warmup")
        pending = iter(jobs)
        async def worker():
            for index in pending:
                await self.run_one(client, prompt, group, index, "measured")
        async with asyncio.TaskGroup() as workers:
            for _ in range(min(group["concurrency"], len(jobs))):
                workers.create_task(worker())

    def api_key(self, name):
        return self.credentials.get(name) if self.credentials is not None else os.environ.get(name)

    async def run_model(self, entries, jobs):
        model = entries[0][0]
        async with self.client_factory(api_key=self.api_key(model["api_key_env"]), base_url=model["base_url"],
                                       timeout=self.config["timeout_seconds"], max_retries=0) as client:
            for _, prompt, group in entries:
                await self.run_group(client, prompt, group, jobs[group["group_id"]])

    def write_summary(self):
        ids = {f'{g["group_id"]}-{i:04d}' for _, _, g in self.prepared for i in range(1, self.config["repetitions"] + 1)}
        aggregate = totals(self.records, ids)
        aggregate.update(new_attempts=sum(r["session_id"] == self.session_id and r["phase"] == "measured" and r["error_type"] != "cancelled" for r in self.records),
                         skipped_successes=self.skipped, warmup_requests=sum(r["phase"] == "warmup" and r["error_type"] != "cancelled" for r in self.records))
        data = dict(started_at=min([self.started_at] + [r["started_at"] for r in self.records]), finished_at=now(),
                    data_source=self.config.get("data_source", "live_api"), status="pending" if aggregate["pending_cases"] else "complete",
                    settings=self.settings, totals=aggregate,
                    summary=[summarize(self.records, g, self.config["repetitions"]) for _, _, g in self.prepared],
                    requests=[r for r in self.records if r["phase"] == "measured"], warmups=[r for r in self.records if r["phase"] == "warmup"])
        atomic_json(self.output / "results.json", data)
        return data

    async def run(self):
        self.output.mkdir(parents=True, exist_ok=True)
        store = get_store(self.output.resolve().parents[3])
        checkpoint_task = None
        checkpoint_stop = asyncio.Event()

        async def checkpoints():
            while not checkpoint_stop.is_set():
                try:
                    await asyncio.wait_for(checkpoint_stop.wait(), timeout=CHECKPOINT_SECONDS)
                except TimeoutError:
                    self.write_summary()
                    snapshot = store.snapshot(self.output)
                    try:
                        await asyncio.to_thread(store.upload, snapshot)
                    except StorageError:
                        print("OSS 检查点上传失败，将继续重试；本地工作文件已保留", flush=True)
        lock = self.output / ".run.lock"
        try:
            with lock.open("x") as file:
                file.write(str(os.getpid()))
        except FileExistsError as exc:
            raise ValueError("该 run 已锁定；请确认原进程结束后再处理残留锁") from exc
        try:
            self.records = read_records(self.output / "requests.jsonl")
            jobs = plan_jobs(self.prepared, self.records, self.config["repetitions"])
            buckets = defaultdict(list)
            for entry in self.prepared:
                model, _, group = entry
                if jobs[group["group_id"]]:
                    if not self.api_key(model["api_key_env"]):
                        raise ValueError(f'缺少密钥环境变量：{model["api_key_env"]}')
                    buckets[model["id"]].append(entry)
            self.skipped = len(self.prepared) * self.config["repetitions"] - sum(map(len, jobs.values()))
            atomic_json(self.output.parent.parent / "latest.json", {"run_id": self.output.name})
            if store.remote:
                await asyncio.to_thread(store.put, str((self.output.parent.parent / "latest.json").relative_to(store.root)), encoded({"run_id": self.output.name}))
            with (self.output / "events.jsonl").open("a", encoding="utf-8") as events, (self.output / "requests.jsonl").open("a", encoding="utf-8") as records:
                self.events_file, self.records_file = events, records
                if store.remote:
                    checkpoint_task = asyncio.create_task(checkpoints())
                try:
                    async with asyncio.TaskGroup() as models:
                        for entries in buckets.values():
                            models.create_task(self.run_model(entries, jobs))
                finally:
                    checkpoint_stop.set()
                    if checkpoint_task:
                        await checkpoint_task
                    self.write_summary()
        finally:
            if not store.remote:
                lock.unlink(missing_ok=True)
        return self.output


def plan(config, config_dir, output_root=None, mode="incremental"):
    prepared = prepare(config, config_dir)
    output, effective = resolve_output(config, config_dir, output_root, mode)
    jobs = plan_jobs(prepared, read_records(output / "requests.jsonl"), config["repetitions"])
    description = dict(groups=len(prepared), planned_cases=len(prepared)*config["repetitions"],
                       pending_calls=sum(map(len, jobs.values())), warmup_calls=sum(bool(v) for v in jobs.values())*config["warmup"],
                       concurrencies=config["concurrencies"], output=str(output), configured_update_mode=mode,
                       effective_update_mode=effective, version=config["version"])
    return description, prepared, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--mode", choices=["full", "incremental"], default=UPDATE_MODE)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--execute", action="store_true", default=EXECUTE)
    args = parser.parse_args()
    config = load_config(args.config)
    description, prepared, output = plan(config, args.config.parent, args.output_root, args.mode)
    print(json.dumps(dict(description, execute=args.execute), ensure_ascii=False, indent=2))
    if args.execute:
        try:
            asyncio.run(BenchmarkRun(config, args.config.parent, output, prepared).run())
        finally:
            store = get_store(output.resolve().parents[3])
            if store.remote and (output / "results.json").exists():
                from report import build_report
                build_report(output / "results.json")
                store.archive(output)


if __name__ == "__main__":
    main()
