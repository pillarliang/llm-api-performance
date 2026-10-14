import asyncio
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import benchmark as b
import metrics
from report import build_report
from tokens import count, make_prompt

ROOT = Path(__file__).resolve().parents[1]
HAS_SDK = importlib.util.find_spec("openai") is not None
HAS_TOKENS = importlib.util.find_spec("tiktoken") is not None


def config():
    return dict(name="test", version="v1", repetitions=3, warmup=0, concurrencies=[2], input_tokens=[512],
                timeout_seconds=2, cache_state="uncontrolled", parameters={},
                tasks=[dict(id="summary", template_file="prompts/summary.txt", corpus_file="prompts/corpus.txt")],
                models=[dict(id="a", model="test-model", base_url="https://example.test/v1", api_key_env="BENCHMARK_TEST_KEY",
                             profiles=[dict(name="off", thinking_enabled=False, parameters={})])])


def prepared_without_tokenizer(cfg):
    # These identity tests deliberately isolate prompt construction.
    with patch.object(b, "make_prompt", return_value="fixed input"), patch.object(b, "count", return_value=512):
        return b.prepare(cfg, ROOT)


class ConfigAndPlanTests(unittest.TestCase):
    def test_example_matrix_sizes(self):
        for filename, expected in [("config.example.yaml", (24,72,0)), ("config.matrix.example.yaml",(52,260,52))]:
            cfg=b.load_config(ROOT/filename)
            prepared=prepared_without_tokenizer(cfg)
            self.assertEqual((len(prepared),len(prepared)*cfg["repetitions"],len(prepared)*cfg["warmup"]),expected)

    def test_validation(self):
        for key,value in [("name","../escape"),("version",".."),("repetitions",True),("warmup",-1),("concurrencies",[0]),("input_tokens",[]),("timeout_seconds",float("nan"))]:
            cfg=config();cfg[key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):b.validate_config(cfg)
        cfg=config();cfg["models"].append(deepcopy(cfg["models"][0]))
        with self.assertRaises(ValueError):b.validate_config(cfg)

    def test_reserved_fields_and_yaml_booleans(self):
        for field in b.RESERVED:
            cfg=config();cfg["models"][0]["profiles"][0]["parameters"]={field:"override"}
            with self.subTest(field=field), self.assertRaises(ValueError):b.validate_config(cfg)
        cfg=config();cfg["models"][0]["profiles"][0]["name"]=False
        with self.assertRaises(ValueError):b.validate_config(cfg)

    def test_stable_identity_and_parameter_precedence(self):
        cfg=config();cfg["parameters"]={"temperature":1,"thinking":{"a":1}}
        cfg["models"][0]["parameters"]={"temperature":.5}
        cfg["models"][0]["profiles"][0]["parameters"]={"temperature":0,"thinking":{"b":2}}
        group=prepared_without_tokenizer(cfg)[0][2]
        self.assertEqual(group["parameters"],{"temperature":0,"thinking":{"b":2}})
        cfg["repetitions"]=9
        self.assertEqual(prepared_without_tokenizer(cfg)[0][2]["group_id"],group["group_id"])
        cfg["concurrencies"]=[3]
        self.assertNotEqual(prepared_without_tokenizer(cfg)[0][2]["group_id"],group["group_id"])

    def test_profile_order_and_endpoint_identity(self):
        cfg=config();cfg["models"][0]["profiles"].append(dict(name="on",thinking_enabled=True,parameters={}))
        old={g["profile"]:g["group_id"] for _,_,g in prepared_without_tokenizer(cfg)}
        cfg["models"][0]["profiles"].reverse()
        self.assertEqual(old,{g["profile"]:g["group_id"] for _,_,g in prepared_without_tokenizer(cfg)})
        cfg["models"][0]["base_url"]="https://different.test/v1"
        self.assertNotEqual(old,{g["profile"]:g["group_id"] for _,_,g in prepared_without_tokenizer(cfg)})

    def test_duplicates_and_empty_matrix(self):
        cfg=config();cfg["input_tokens"]=[512,512]
        with self.assertRaises(ValueError):prepared_without_tokenizer(cfg)
        cfg=config();cfg["models"][0]["enabled"]=False
        with self.assertRaises(ValueError):prepared_without_tokenizer(cfg)

    def test_incremental_selection_and_repetition_growth(self):
        cfg=config();prepared=prepared_without_tokenizer(cfg);gid=prepared[0][2]["group_id"]
        records=[dict(case_id=f"{gid}-0001",phase="measured",success=True),dict(case_id=f"{gid}-0002",phase="measured",success=False)]
        self.assertEqual(b.plan_jobs(prepared,records,3)[gid],[2,3])
        self.assertEqual(b.plan_jobs(prepared,records,4)[gid],[2,3,4])

    def test_directories_and_dry_run(self):
        with TemporaryDirectory() as temp:
            cfg=config();root=Path(temp)
            output,mode=b.resolve_output(cfg,ROOT,root)
            self.assertEqual(mode,"full");self.assertFalse(output.exists())
            output.mkdir(parents=True)
            b.atomic_json(output.parent.parent/"latest.json",{"run_id":output.name})
            self.assertEqual(b.resolve_output(cfg,ROOT,root),(output,"incremental"))
            new,mode=b.resolve_output(cfg,ROOT,root,"full")
            self.assertNotEqual(new,output);self.assertEqual(mode,"full")
            self.assertTrue(output.is_dir())

    def test_corrupt_logs_are_not_ignored(self):
        with TemporaryDirectory() as temp:
            path=Path(temp)/"requests.jsonl";path.write_text('{}\n{"broken":')
            with self.assertRaisesRegex(ValueError,"第 2 行"):b.read_records(path)

    def test_base_url_environment(self):
        cfg=config();cfg["models"][0]["base_url_env"]="BENCHMARK_TEST_URL"
        with patch.dict(os.environ,{"BENCHMARK_TEST_URL":"https://override.test/v1"}):
            self.assertEqual(b.validate_config(cfg)["models"][0]["base_url"],"https://override.test/v1")


class MetricsTests(unittest.TestCase):
    def record(self, **fields):
        return dict(success=True,first_output_seconds=.8,last_output_seconds=4,stream_close_seconds=4.3,
                    answer_chunks=[{"at":2,"text":"a"},{"at":3,"text":"b"},{"at":4,"text":"c"}],
                    thinking_enabled=True,reasoning_observed=True,usage=None,**fields)

    def test_formula_excludes_first_chunk_and_trailing_usage(self):
        record=self.record()
        # Oracle token counts verify the formula independently of the tokenizer.
        with patch.object(metrics,"count",side_effect=lambda text:{"abc":100,"a":20}[text]):metrics.measure(record)
        self.assertEqual(record["ttft_ms"],800);self.assertEqual(record["ttfa_seconds"],2)
        self.assertEqual(record["output_speed_tps"],40);self.assertEqual(record["e2e_seconds"],4)
        self.assertEqual(record["max_answer_gap_seconds"],1)

    def test_single_chunk_and_failed_e2e(self):
        record=self.record();record.update(success=False,answer_chunks=[dict(at=2,text="answer")])
        with patch.object(metrics,"count",return_value=8):metrics.measure(record)
        self.assertIsNone(record["output_speed_tps"]);self.assertIsNone(record["e2e_seconds"])
        self.assertIsNone(record["max_answer_gap_seconds"]);self.assertEqual(record["ttfa_seconds"],2)

    def test_reasoning_unknown_and_native_input_difference(self):
        record=self.record();record["usage"]={"prompt_tokens":777,"completion_tokens":50}
        with patch.object(metrics,"count",return_value=1):metrics.measure(record)
        self.assertEqual(record["prompt_tokens"],777);self.assertIsNone(record["reasoning_tokens"]);self.assertIsNone(record["answer_tokens"])
        record.update(thinking_enabled=False,reasoning_observed=False)
        with patch.object(metrics,"count",return_value=1):metrics.measure(record)
        self.assertEqual(record["reasoning_tokens"],0);self.assertEqual(record["answer_tokens"],50)
        record.update(thinking_enabled=None,reasoning_observed=False)
        with patch.object(metrics,"count",return_value=1):metrics.measure(record)
        self.assertIsNone(record["reasoning_tokens"]);self.assertIsNone(record["answer_tokens"])

    def test_percentiles_and_missing_samples(self):
        self.assertAlmostEqual(metrics.percentile([1,2,3,4,5],.05),1.2)
        self.assertEqual(metrics.percentile([7],.95),7);self.assertIsNone(metrics.percentile([],.5))

    def test_final_case_success_preserves_failure_attempts(self):
        records=[dict(case_id="g-0001",phase="measured",success=False,error_type="rate_limit"),
                 dict(case_id="g-0001",phase="measured",success=True,error_type=None),
                 dict(case_id="g-0002",phase="measured",success=False,error_type="cancelled"),
                 dict(case_id="old-0001",phase="measured",success=True,error_type=None)]
        totals=metrics.totals(records,{"g-0001","g-0002"})
        self.assertEqual(totals,dict(planned_cases=2,attempted_cases=1,successful_cases=1,pending_cases=1,missing_cases=1,requests=2,retry_attempts=1,success_rate=.5))

    def test_success_only_distributions_and_shrinking_plan(self):
        records=[dict(case_id="g-0001",phase="measured",success=False,error_type="timeout",session_id="s1",ttft_ms=9000),
                 dict(case_id="g-0001",phase="measured",success=True,error_type=None,session_id="s2",ttft_ms=20),
                 dict(case_id="g-0002",phase="measured",success=True,error_type=None,session_id="s2",ttft_ms=40)]
        summary=metrics.summarize(records,{"group_id":"g"},1)
        self.assertEqual(summary["metrics"]["ttft_ms"]["p50"],20)
        self.assertEqual(summary["errors"],{"timeout":1});self.assertEqual(summary["requests"],2)
        self.assertEqual(summary["session_ids"],["s1","s2"])


class ReportTests(unittest.TestCase):
    def test_report_embeds_only_summary_and_escapes_script(self):
        with TemporaryDirectory() as temp:
            source=Path(temp)/"results.json"
            source.write_text(json.dumps(dict(started_at="now",finished_at="later",status="pending",totals={},
                data_source="synthetic_fixture",summary=[dict(endpoint_id="</script><script>alert(1)</script>")],
                settings={"name":"test","environment":{"python":"test"}},requests=[{"answer":"secret answer"}],warmups=[])))
            output=build_report(source);text=output.read_text()
            self.assertNotIn("__RESULT_DATA__",text);self.assertNotIn("__REPORT_CSS__",text);self.assertNotIn("__REPORT_JS__",text)
            self.assertNotIn("secret answer",text);self.assertNotIn("</script><script>alert(1)",text)
            self.assertIn("synthetic_fixture",text);self.assertIn("renderReport",text)
            self.assertNotIn('src="http',text);self.assertNotIn('href="http',text)
            payload = text.split('<script id="report-data" type="application/json">', 1)[1].split('</script>', 1)[0]
            embedded = json.loads(payload)
            self.assertEqual(embedded["summary"][0]["endpoint_id"], "</script><script>alert(1)</script>")
            self.assertEqual(embedded["started_at"], "now")
            self.assertEqual(embedded["environment"]["name"], "test")


@unittest.skipUnless(HAS_TOKENS,"需要安装 tiktoken 后验证真实 tokenizer")
class TokenTests(unittest.TestCase):
    def test_nine_exact_input_bands_and_instruction_preservation(self):
        task=config()["tasks"][0]
        for target in [512,1024,2048,4096,8192,16384,32768,65536,131072]:
            with self.subTest(target=target):
                prompt=make_prompt(task,target,ROOT)
                self.assertEqual(count(prompt),target);self.assertTrue(prompt.startswith("你是一名服务可靠性工程师"))
                self.assertTrue(prompt.endswith("请输出：现状摘要、风险分析、改进建议。\n"))

    def test_unsized_prompt_and_cross_model_reuse(self):
        cfg=config();cfg["models"].append({**deepcopy(cfg["models"][0]),"id":"b"})
        prepared=b.prepare(cfg,ROOT);self.assertEqual(prepared[0][1],prepared[1][1])
        prompt=make_prompt(cfg["tasks"][0],None,ROOT)
        self.assertIn((ROOT/"prompts/corpus.txt").read_text(),prompt)


@unittest.skipUnless(HAS_SDK and HAS_TOKENS,"需要安装 openai/httpx/tiktoken 后验证真实 SDK 流管线")
class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        import openai
        self.httpx,self.openai=httpx,openai

    def stream_client(self, chunks, status=200, inspect=None):
        def handler(request):
            if inspect:inspect(json.loads(request.content))
            if status!=200:return self.httpx.Response(status,json={"error":{"message":"test failure"}},headers={"x-request-id":"req-test"})
            content="".join("data: "+json.dumps(dict(id="mock",object="chat.completion.chunk",created=1,model="test-model",**chunk))+"\n\n" for chunk in chunks)+"data: [DONE]\n\n"
            return self.httpx.Response(200,text=content,headers={"content-type":"text/event-stream","x-request-id":"req-test"})
        return self.openai.AsyncOpenAI(api_key="fake",base_url="https://example.test/v1",max_retries=0,http_client=self.httpx.AsyncClient(transport=self.httpx.MockTransport(handler)))

    def chunk(self, content=None, reasoning=None, finish=None):
        delta={}
        if content is not None:delta["content"]=content
        if reasoning is not None:delta["reasoning_content"]=reasoning
        return {"choices":[{"index":0,"delta":delta,"finish_reason":finish}]}

    def record(self):
        group=b.prepare(config(),ROOT)[0][2]
        return b.new_record(group,1,"measured","session",1)

    async def test_real_sdk_sse_and_vendor_parameters(self):
        inspected=[]
        chunks=[self.chunk(),self.chunk(reasoning="reason"),self.chunk(content="hello"),self.chunk(content=" world"),self.chunk(finish="stop"),dict(choices=[],usage=dict(prompt_tokens=777,completion_tokens=30,total_tokens=807,completion_tokens_details={"reasoning_tokens":20}))]
        record=self.record();record["parameters"]={"enable_thinking":True}
        async with self.stream_client(chunks,inspect=inspected.append) as client:await b.request(client,"prompt",record)
        metrics.measure(record)
        self.assertTrue(record["success"]);self.assertTrue(record["reasoning_observed"]);self.assertEqual(record["request_id"],"req-test")
        self.assertEqual(record["answer_tokens"],10);self.assertEqual(record["prompt_tokens"],777)
        self.assertTrue(inspected[0]["enable_thinking"]);self.assertTrue(inspected[0]["stream_options"]["include_usage"])
        self.assertLessEqual(record["e2e_seconds"],record["stream_close_seconds"])

    async def test_protocol_failures_and_rate_limit(self):
        for finish,content,error in [("length","partial","truncated"),("content_filter","","content_filter"),("stop",None,"empty_answer"),(None,"partial","stream_interrupted"),("tool_calls","partial","unexpected_finish")]:
            record=self.record();record["timeout_seconds"]=10  # Protocol assertions must tolerate cold SDK initialization.
            async with self.stream_client([self.chunk(content=content,finish=finish)]) as client:await b.request(client,"prompt",record)
            self.assertEqual(record["error_type"],error);self.assertFalse(record["success"])
        record=self.record()
        async with self.stream_client([],status=429) as client:await b.request(client,"prompt",record)
        self.assertEqual(record["error_type"],"rate_limit");self.assertEqual(record["status_code"],429)

    async def test_timeout_and_connection_failure(self):
        async def handler(request):
            await asyncio.sleep(1)
        record=self.record();record["timeout_seconds"]=.01
        async with self.openai.AsyncOpenAI(api_key="fake",base_url="https://example.test/v1",max_retries=0,http_client=self.httpx.AsyncClient(transport=self.httpx.MockTransport(handler))) as client:
            await b.request(client,"prompt",record)
        self.assertEqual(record["error_type"],"timeout")

    async def test_resume_pipeline_keeps_failed_attempt_and_skips_success(self):
        cfg=config();cfg["repetitions"]=2
        calls=[]
        def factory(**kwargs):
            client=self.stream_client([self.chunk(content="hello"),self.chunk(content=" world",finish="stop")],inspect=calls.append)
            return client
        with TemporaryDirectory() as temp, patch.dict(os.environ,{"BENCHMARK_TEST_KEY":"fake"}):
            output,_=b.resolve_output(cfg,ROOT,Path(temp));run=b.BenchmarkRun(cfg,ROOT,output,client_factory=factory)
            await run.run();self.assertEqual(len(calls),2)
            records=b.read_records(output/"requests.jsonl");records[1].update(success=False,error_type="timeout")
            (output/"requests.jsonl").write_text("".join(json.dumps(r)+"\n" for r in records))
            await b.BenchmarkRun(cfg,ROOT,output,client_factory=factory).run()
            data=json.loads((output/"results.json").read_text())
            self.assertEqual(len(calls),3);self.assertEqual(data["totals"]["requests"],3);self.assertEqual(data["totals"]["success_rate"],1)
            self.assertEqual(data["totals"]["retry_attempts"],1);self.assertEqual(data["summary"][0]["errors"],{"timeout":1})
            cfg["repetitions"]=3
            await b.BenchmarkRun(cfg,ROOT,output,client_factory=factory).run();self.assertEqual(len(calls),4)
            self.assertFalse((output/".run.lock").exists());self.assertTrue(build_report(output/"results.json").exists())

    async def test_concurrency_across_models_and_serial_groups(self):
        from types import SimpleNamespace
        cfg=config();cfg.update(repetitions=20,concurrencies=[20],input_tokens=[512,1024])
        cfg["models"].append({**deepcopy(cfg["models"][0]),"id":"b"})
        cfg["models"][1]["model"]="second-model"
        active=0;peak=0;active_by_model={};prompt_by_model={}
        async def create(**kwargs):
            nonlocal active,peak
            model=kwargs["model"];prompt=kwargs["messages"][0]["content"]
            if active_by_model.get(model,0):self.assertEqual(prompt_by_model[model],prompt)
            active+=1;peak=max(peak,active);active_by_model[model]=active_by_model.get(model,0)+1;prompt_by_model[model]=prompt
            await asyncio.sleep(.01)
            class Stream:
                async def __aiter__(self):
                    yield SimpleNamespace(id="mock",usage=None,choices=[SimpleNamespace(finish_reason="stop",delta=SimpleNamespace(content="answer"))])
                async def close(self):
                    nonlocal active
                    active-=1;active_by_model[model]-=1
            return Stream()
        class Client:
            def __init__(self,**kwargs):self.chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        with TemporaryDirectory() as temp,patch.dict(os.environ,{"BENCHMARK_TEST_KEY":"fake"}):
            output,_=b.resolve_output(cfg,ROOT,Path(temp))
            await b.BenchmarkRun(cfg,ROOT,output,client_factory=Client).run()
        self.assertEqual(peak,40);self.assertEqual(active,0)

    async def test_cancellation_is_saved_and_resumable(self):
        from types import SimpleNamespace
        started=asyncio.Event()
        async def create(**kwargs):started.set();await asyncio.sleep(60)
        class Client:
            def __init__(self,**kwargs):self.chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        with TemporaryDirectory() as temp,patch.dict(os.environ,{"BENCHMARK_TEST_KEY":"fake"}):
            cfg=config();output,_=b.resolve_output(cfg,ROOT,Path(temp))
            run=b.BenchmarkRun(cfg,ROOT,output,client_factory=Client);task=asyncio.create_task(run.run())
            await started.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            records=b.read_records(output/"requests.jsonl")
            self.assertTrue(records);self.assertTrue(all(r["error_type"]=="cancelled" for r in records))
            data=json.loads((output/"results.json").read_text());self.assertEqual(data["totals"]["requests"],0)
            self.assertEqual(data["totals"]["pending_cases"],3);self.assertFalse((output/".run.lock").exists())


if __name__ == "__main__":unittest.main()
