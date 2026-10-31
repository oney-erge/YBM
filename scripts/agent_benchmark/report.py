"""Print the benchmark results as a Markdown table (and optionally write a prompt-free JSON for publishing).

    python report.py results/ [--json summary.json]

Only scores and timings are written; recorded prompts, answers and step ids never leave the results folder.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from replay import format_rate, summarize
from steps import load


def collect(folder: Path, latency_ids: set[str] | None = None) -> list[dict]:
    rows = []
    for result_path in sorted(folder.glob("*.json")):
        if result_path.name.endswith(".meta.json"):
            continue
        meta_path = result_path.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        record = json.loads(result_path.read_text(encoding="utf-8"))
        if not record.get("rows"):
            continue
        rows.append({"name": record.get("name", result_path.stem), "meta": meta, "record": record, "summary": summarize(record["rows"], latency_ids)})
    return sorted(rows, key=lambda item: -item["summary"]["pass"][0] / max(1, item["summary"]["pass"][1]))


def table(items: list[dict]) -> str:
    header = ("| Model | Steps | Pass | Valid call | No made-up refs | Same as good step | Avoids bad repeat | "
              "Median s | p95 s | Decode tok/s | VRAM MiB |")
    lines = [header, "|" + "---|" * 11]
    for item in items:
        s, meta = item["summary"], item["meta"]
        note = " (stopped early: too slow)" if item["record"].get("aborted") else ""
        lines.append(
            f"| {item['name']}{note} | {s['steps']} | {format_rate(s['pass'])} | {format_rate(s['valid_call'])} | "
            f"{format_rate(s['grounded_call'])} | {format_rate(s['agrees_on_good'])} | {format_rate(s['avoids_on_bad'])} | "
            f"{s['median_s']:.1f} | {s['p95_s']:.1f} | {s['decode_tps'] or '-'} | {meta.get('vram_mib') or '-'} |"
        )
    return "\n".join(lines)


def publishable(items: list[dict]) -> list[dict]:
    return [
        {"name": item["name"], "gguf": item["meta"].get("gguf"), "args": item["meta"].get("args"),
         "load_s": item["meta"].get("load_s"), "vram_mib": item["meta"].get("vram_mib"),
         "aborted": item["record"].get("aborted"), **{k: v for k, v in item["summary"].items()}}
        for item in items
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--latency-from", type=Path, help="steps file whose steps alone count toward the timing columns")
    arguments = parser.parse_args()
    timed_ids = {step.id for step in load(arguments.latency_from)} if arguments.latency_from else None
    found = collect(arguments.folder, timed_ids)
    print(table(found))
    if arguments.json:
        arguments.json.write_text(json.dumps(publishable(found), indent=1), encoding="utf-8")
