"""Replay recorded Operator steps against one model and score the answers (see steps.py and grade.py).

    python replay.py --steps steps.json --url http://127.0.0.1:8090/v1 --name qwen3.5-9b --out results/qwen3.5-9b.json

Run it with agent_control importable (PYTHONPATH=backend/src) and the working directory at the YBM checkout whose
config.yaml should define the tools. The request is the one YBM sends: the stored system and user prompt, the
OperatorDecision JSON schema as response_format, temperature 0.1, max_tokens 4096. One attempt per step (YBM
retries up to three times; a model that needs the retry is scored as failing here, and the retry is visible in
the cost column of a real run instead).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

from grade import grade
from steps import Step, load


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def build_payload(step: Step, schema: dict[str, Any], model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "system", "content": step.system}, {"role": "user", "content": step.user}],
        "temperature": 0.1,
        "max_tokens": 4096,
        "response_format": {"type": "json_schema", "json_schema": {"name": "OperatorDecision", "schema": schema}},
    }


def run(args: argparse.Namespace) -> None:
    from agent_control.config import load_settings
    from agent_control.llm.providers import _loads_json_object
    from agent_control.orchestration.worker import _canonical_operator_tool_call
    from agent_control.schemas import OperatorDecision
    from agent_control.tools.registry import build_tool_registry

    settings = load_settings()
    registry = build_tool_registry(settings, "http://127.0.0.1:8765")
    definitions = {definition.name: definition for definition in registry.definitions}
    schema = OperatorDecision.model_json_schema()
    steps = load(args.steps)
    if args.limit:
        steps = steps[: args.limit]
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"name": args.name, "rows": []}
    done = {row["step_id"] for row in record["rows"]}
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    with httpx.Client(timeout=args.timeout) as client:
        for number, step in enumerate(steps, 1):
            if step.id in done:
                continue
            started = time.monotonic()
            answer: dict[str, Any] | None = None
            error = ""
            timings: dict[str, Any] = {}
            usage: dict[str, Any] = {}
            try:
                response = client.post(f"{args.url.rstrip('/')}/chat/completions", headers=headers,
                                       json=build_payload(step, schema, args.model))
                response.raise_for_status()
                data = response.json()
                timings, usage = data.get("timings") or {}, data.get("usage") or {}
                content = str(data["choices"][0]["message"].get("content") or "")
                answer = OperatorDecision.model_validate(_loads_json_object(content)).model_dump(mode="json")
            except Exception as exc:  # a failed call is a failed step, not a crashed benchmark
                error = f"{type(exc).__name__}: {str(exc)[:120]}"
            latency = time.monotonic() - started
            row = grade(answer, step, definitions, _canonical_operator_tool_call)
            row.update(
                step_id=step.id, label=step.label, latency_s=round(latency, 2), error=error,
                prompt_tokens=usage.get("prompt_tokens") or timings.get("prompt_n"),
                completion_tokens=usage.get("completion_tokens") or timings.get("predicted_n"),
                prefill_tps=timings.get("prompt_per_second"), decode_tps=timings.get("predicted_per_second"),
                cached_tokens=timings.get("cache_n"),
                tool=(answer or {}).get("tool_name"), operation=((answer or {}).get("tool_input") or {}).get("operation"),
            )
            record["rows"].append(row)
            out.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            print(f"[{number}/{len(steps)}] {step.label:<4} {'PASS' if row['passed'] else 'fail'} {latency:5.1f}s "
                  f"{row['action'] or '-':<9} {row['problem'][:70]}", flush=True)
            seen = [item["latency_s"] for item in record["rows"]]
            if args.abort_after and len(seen) == args.abort_after and statistics.median(seen) > args.abort_median:
                record["aborted"] = f"median {statistics.median(seen):.0f}s over the first {args.abort_after} steps exceeds {args.abort_median:.0f}s"
                out.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                print("stopping early:", record["aborted"], flush=True)
                break


def summarize(rows: list[dict[str, Any]], latency_ids: set[str] | None = None) -> dict[str, Any]:
    def rate(selected: list[dict[str, Any]], key: str) -> tuple[int, int]:
        values = [row[key] for row in selected if row.get(key) is not None]
        return sum(1 for value in values if value), len(values)

    calls = [row for row in rows if row["action"] == "call_tool"]
    good = [row for row in rows if row["label"] == "good"]
    bad = [row for row in rows if row["label"] == "bad"]
    # Timings come only from steps run alone on a quiet machine when the caller says which those are.
    timed = [row for row in rows if latency_ids is None or row["step_id"] in latency_ids]
    latencies = sorted(row["latency_s"] for row in timed)
    decode = [row["decode_tps"] for row in timed if row.get("decode_tps")]
    summary: dict[str, Any] = {
        "steps": len(rows),
        "pass": (sum(1 for row in rows if row["passed"]), len(rows)),
        "parsed": rate(rows, "parsed"),
        "valid_call": rate(calls, "valid"),
        "grounded_call": rate(calls, "grounded"),
        "agrees_on_good": rate(good, "agrees"),
        "avoids_on_bad": rate(bad, "avoids"),
        "median_s": statistics.median(latencies) if latencies else None,
        "p95_s": latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))] if latencies else None,
        "decode_tps": round(statistics.mean(decode), 1) if decode else None,
    }
    return summary


def format_rate(pair: tuple[int, int]) -> str:
    successes, total = pair
    if not total:
        return "-"
    low, high = wilson(successes, total)
    return f"{successes / total:.0%} ({low:.0%}-{high:.0%})"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--steps", type=Path, required=True)
    parser.add_argument("--url", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8090/v1")
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", default="bench")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--abort-after", type=int, default=0, help="stop early if the median of this many steps is too slow")
    parser.add_argument("--abort-median", type=float, default=40.0)
    run(parser.parse_args())
