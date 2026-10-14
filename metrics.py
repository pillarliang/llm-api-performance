"""Client-observed measurements and case-based, resumable aggregates."""
from collections import Counter
from statistics import mean

from tokens import count

METRICS = ("ttft_ms", "ttfa_seconds", "output_speed_tps", "e2e_seconds",
           "max_answer_gap_seconds", "benchmark_answer_tokens", "prompt_tokens",
           "completion_tokens", "reasoning_tokens", "answer_tokens")


def measure(record):
    chunks = record.get("answer_chunks", [])
    first = record.get("first_output_seconds")
    record["ttft_ms"] = first * 1000 if first is not None else None
    record["ttfa_seconds"] = chunks[0]["at"] if chunks else None
    record["e2e_seconds"] = record.get("last_output_seconds") if record.get("success") else None
    record["benchmark_answer_tokens"] = count("".join(c["text"] for c in chunks)) if chunks else None
    record["output_speed_tps"] = None
    record["max_answer_gap_seconds"] = None
    record["speed_unavailable_reason"] = "fewer_than_two_answer_chunks"
    if len(chunks) >= 2:
        duration = chunks[-1]["at"] - chunks[0]["at"]
        delta = record["benchmark_answer_tokens"] - count(chunks[0]["text"])
        record["max_answer_gap_seconds"] = max(b["at"] - a["at"] for a, b in zip(chunks, chunks[1:]))
        if duration > 0 and delta > 0:
            record["output_speed_tps"] = delta / duration
            record["speed_unavailable_reason"] = None
        else:
            record["speed_unavailable_reason"] = "non_positive_time_or_token_delta"
    usage = record.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    record["reasoning_observed"] = bool(record.get("reasoning_observed") or (reasoning or 0) > 0)
    if reasoning is None and record.get("thinking_enabled") is False and not record["reasoning_observed"]:
        reasoning = 0
    record["prompt_tokens"] = usage.get("prompt_tokens")
    record["completion_tokens"] = usage.get("completion_tokens")
    record["reasoning_tokens"] = reasoning
    completion = record["completion_tokens"]
    record["answer_tokens"] = completion - reasoning if completion is not None and reasoning is not None else None
    return record


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def totals(records, case_ids):
    selected = [r for r in records if r.get("phase") == "measured" and r["case_id"] in case_ids and r.get("error_type") != "cancelled"]
    attempted = {r["case_id"] for r in selected}
    successful = {r["case_id"] for r in selected if r["success"]}
    planned = len(case_ids)
    return dict(planned_cases=planned, attempted_cases=len(attempted), successful_cases=len(successful),
                pending_cases=planned-len(successful), missing_cases=planned-len(attempted),
                requests=len(selected), retry_attempts=len(selected)-len(attempted),
                success_rate=len(successful)/planned if planned else 0)


def summarize(records, group, repetitions):
    ids = {f'{group["group_id"]}-{i:04d}' for i in range(1, repetitions + 1)}
    selected = [r for r in records if r.get("phase") == "measured" and r["case_id"] in ids]
    successful = [r for r in selected if r["success"]]
    distributions = {}
    for key in METRICS:
        values = [r[key] for r in successful if r.get(key) is not None]
        tail = "p5" if key == "output_speed_tps" else "p95"
        distributions[key] = dict(n=len(values), mean=mean(values) if values else None,
                                  p50=percentile(values, .5), **{tail: percentile(values, .05 if tail == "p5" else .95)})
    return dict(group, **totals(selected, ids), metrics=distributions,
                errors=dict(Counter(r["error_type"] for r in selected if not r["success"] and r.get("error_type") != "cancelled")),
                session_ids=sorted({r["session_id"] for r in selected}))
