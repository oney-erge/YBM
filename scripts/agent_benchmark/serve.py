"""Serve one benchmark candidate on a port and keep it running until interrupted.

    python serve.py --models-dir D:\\LocalModels\\gguf --name "qwen3.5-9b" --port 8090

Used to point YBM at a candidate for a live run (for example with AGENT_LLM__PROFILES__bench__BASE_URL=
http://127.0.0.1:8090/v1) and to give LocalDeploy's benchmark a llama.cpp server to measure.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import llama
from candidates import build


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--name", required=True, help="candidate name (see candidates.py), matched exactly")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--log", type=Path, default=Path("serve.log"))
    parser.add_argument("--server", default=None)
    arguments = parser.parse_args()

    configs = build(arguments.models_dir)
    if arguments.name not in configs:
        print(f"unknown candidate {arguments.name!r}; available: {', '.join(configs)}", file=sys.stderr)
        return 2
    process, load_s = llama.start(configs[arguments.name], arguments.port, arguments.log, arguments.server)
    print(f"READY {arguments.name} on http://127.0.0.1:{arguments.port}/v1 after {load_s:.0f}s", flush=True)
    try:
        while process.poll() is None:
            time.sleep(2.0)
        print(f"llama-server exited with {process.returncode}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        llama.stop(process)


if __name__ == "__main__":
    raise SystemExit(main())
