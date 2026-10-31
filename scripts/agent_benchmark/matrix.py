"""Run the step replay for every candidate: start its llama-server, replay, record what it cost, stop it.

    python matrix.py --models-dir D:\\LocalModels\\gguf --steps steps.json --out results [--only qwen3.5,gemma]

Results go to <out>/<name>.json (the scored steps) and <out>/<name>.meta.json (load time, VRAM, settings).
A model whose first steps are slower than --abort-median is stopped early and recorded as too slow.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

import llama
import replay
from candidates import build


PROBE_STEPS = 12


def safe(name: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", name.lower()).strip("-")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--steps", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="", help="comma-separated substrings of candidate names")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--abort-after", type=int, default=8)
    parser.add_argument("--abort-median", type=float, default=40.0)
    parser.add_argument("--server", default=None)
    parser.add_argument("--exact", action="store_true", help="--only names must match a candidate name exactly")
    parser.add_argument("--force", action="store_true", help="run candidates that already finished")
    parser.add_argument("--full-variants", action="store_true", help="replay every step for expert-placement variants")
    arguments = parser.parse_args()

    wanted = [part.strip().lower() for part in arguments.only.split(",") if part.strip()]
    def selected(name: str) -> bool:
        if not wanted:
            return True
        return name.lower() in wanted if arguments.exact else any(part in name.lower() for part in wanted)

    configs = {name: config for name, config in build(arguments.models_dir).items() if selected(name)}
    arguments.out.mkdir(parents=True, exist_ok=True)
    print("candidates:", ", ".join(configs), flush=True)
    for name, config in configs.items():
        meta_path = arguments.out / f"{safe(name)}.meta.json"
        result_path = arguments.out / f"{safe(name)}.json"
        if not arguments.force and meta_path.exists() and json.loads(meta_path.read_text(encoding="utf-8")).get("finished"):
            print(f"skip {name}: finished", flush=True)
            continue
        # Placements of a mixture-of-experts model only get a short probe; --force --full-variants
        # reruns the best one over every step (the probe's steps are kept, not repeated).
        limit = PROBE_STEPS if "experts in RAM" in name and not arguments.full_variants else arguments.limit
        print(f"\n===== {name}", flush=True)
        meta = {"name": name, "gguf": Path(config.gguf).name, "args": config.args, "context": config.context}
        idle = llama.gpu_used_mib()
        process = None
        try:
            process, load_s = llama.start(config, arguments.port, arguments.out / f"{safe(name)}.server.log", arguments.server)
            meta["load_s"] = round(load_s, 1)
            replay.run(argparse.Namespace(
                steps=arguments.steps, url=f"http://127.0.0.1:{arguments.port}/v1", name=name, model="bench", api_key="",
                out=result_path, limit=limit, timeout=300.0,
                abort_after=arguments.abort_after, abort_median=arguments.abort_median,
            ))
            used = llama.gpu_used_mib()
            meta["vram_mib"] = (used - idle) if used is not None and idle is not None else None
            meta["finished"] = True
        except Exception as exc:  # one broken candidate must not stop the rest
            meta["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            meta["finished"] = True
            traceback.print_exc(file=sys.stdout)
        finally:
            if process is not None:
                llama.stop(process)
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
