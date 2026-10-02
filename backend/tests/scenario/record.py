"""ybm scenario record <name> - re-record a scenario fixture against a live
LLM (docs/archive/HISTORY.md N3).

Not a pytest test module (doesn't match the ``test_*.py`` discovery pattern),
so it's never auto-collected or run by ``ybm test`` / plain ``pytest``.

Finds every scenario test file that builds ``fixture_name="<name>"`` and runs
just those through pytest with ``YBM_SCENARIO_RECORD=1`` set - see
``harness.build_scenario``, which then swaps in a live provider
(``RecordingLLMProvider`` wrapping ``OpenAICompatibleProvider``) instead of
``ScriptedLLMProvider``, and the 16 scenario tests' own ``pytestmark`` flips
from skipped to running for the same reason. The real test bodies do the
recording - objective text, capability settings, and assertions all stay in
one place (the test file), not duplicated here.

This makes real, live LLM API calls against whatever profile is selected and
may cost money. It is never invoked automatically by anything else in this
repo - only by an explicit ``ybm scenario record <name>`` from the CLI.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCENARIO_DIR = Path(__file__).parent
BACKEND_DIR = SCENARIO_DIR.parent.parent
REPO_ROOT = BACKEND_DIR.parent
FIXTURES_DIR = SCENARIO_DIR / "fixtures"


def find_scenario_files(name: str) -> list[Path]:
    needle = f'fixture_name="{name}"'
    return sorted(p for p in SCENARIO_DIR.glob("test_*.py") if needle in p.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ybm scenario record")
    parser.add_argument("name", help='fixture name, e.g. "filesystem_search" (matches fixtures/<name>.json)')
    parser.add_argument(
        "--profile", default=None,
        help="llm profile name from config/config.yaml's llm.profiles (default: llm.default_profile)",
    )
    parser.add_argument(
        "--keep-existing", action="store_true",
        help="merge into the existing fixture instead of rebuilding it (leaves unreachable keys behind)",
    )
    args = parser.parse_args(argv)

    files = find_scenario_files(args.name)
    if not files:
        print(f'No scenario test under {SCENARIO_DIR} builds fixture_name="{args.name}".')
        print("Fixture names are set per test via build_scenario(..., fixture_name=...); check the test file.")
        return 1

    print(f"Recording fixture '{args.name}' against a LIVE LLM - this makes real API calls and may cost money.")
    print(f"Profile: {args.profile or '(llm.default_profile from config/config.yaml)'}")
    print("Test file(s) to run (all test functions in each, since they share this fixture):")
    for f in files:
        print(f"  {f.relative_to(BACKEND_DIR.parent)}")
    print()

    env = dict(os.environ)
    env["YBM_SCENARIO_RECORD"] = "1"
    if args.profile:
        env["YBM_SCENARIO_RECORD_PROFILE"] = args.profile

    # Rebuild rather than merge. RecordingLLMProvider writes keys as it goes and
    # never removes them, so every re-record used to layer a fresh set on top of
    # the old ones: file_find_and_read.json accumulated entries from four
    # separate runs, none of the stale ones reachable. Rebuilding is safe here
    # precisely because this command runs *every* test file that declares the
    # fixture, so one run regenerates the complete set. --keep-existing restores
    # the old merge if a fixture ever needs assembling across separate runs.
    fixture_path = FIXTURES_DIR / f"{args.name}.json"
    previous = None
    if fixture_path.exists() and not args.keep_existing:
        previous = fixture_path.read_bytes()
        fixture_path.unlink()
        print(f"Rebuilding {fixture_path.name} from scratch (use --keep-existing to merge instead).")
        print()

    cmd = [sys.executable, "-m", "pytest", "-v", "-s", *[str(f) for f in files]]
    result = subprocess.run(cmd, cwd=BACKEND_DIR, env=env)

    # Restore on ANY failed run, not just one that wrote nothing.
    # RecordingLLMProvider saves after every call, so a run that dies partway
    # leaves a truncated fixture on disk - which an "only if missing" guard
    # happily keeps. That is the case that actually happened: three fixtures
    # were cut from 15/26/34 keys down to 2/3/9 by a mid-run model failure.
    if previous is not None and result.returncode != 0:
        current = fixture_path.read_bytes() if fixture_path.exists() else b""
        if current != previous:
            fixture_path.write_bytes(previous)
            print(
                f"\nRecording failed; restored the previous {fixture_path.name} "
                "(a partial rebuild would otherwise have replaced it)."
            )

    if result.returncode == 0 and fixture_path.exists():
        print(f"\nFixture written: {fixture_path.relative_to(BACKEND_DIR.parent)}")
        print("Review the diff, then commit it.")
    elif result.returncode != 0:
        print(
            f"\npytest exited {result.returncode}. If a fixture was partially written to "
            f"{fixture_path.relative_to(BACKEND_DIR.parent)}, review it before committing - "
            "RecordingLLMProvider saves after every call, not just on success."
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
