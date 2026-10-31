"""Temporary browser integration server, never the user's running instance."""
import asyncio
from contextlib import asynccontextmanager, suppress
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn


def main():
    # Isolate settings before importing modules whose paths are bound at import.
    for key in list(os.environ):
        if key.startswith(("AGENT_", "TELEGRAM_", "OPENAI_", "ANTHROPIC_")):
            os.environ.pop(key)
    os.environ["AGENT_ADMIN_TOKEN"] = "ybm-isolated-browser-test"
    with TemporaryDirectory(prefix="ybm-project-e2e-") as directory:
        os.chdir(directory)
        root = Path(directory)
        (root / "config").mkdir()
        (root / "config/config.yaml").write_text("{}", encoding="utf-8")
        from project_system import make_system
        from agent_control.scheduler import run_scheduler_forever

        app, _, _, worker, _ = make_system(root / "work", ask_first=True)

        @asynccontextmanager
        async def lifespan(app):
            running = asyncio.create_task(worker.run_forever(poll_interval_seconds=0.1))
            scheduler = asyncio.create_task(run_scheduler_forever(
                worker.repositories, worker.audit, poll_interval_seconds=0.1,
                settings_loader=lambda: worker.executor.policy.settings,
            ))
            yield
            for task in (running, scheduler):
                task.cancel()
            for task in (running, scheduler):
                with suppress(asyncio.CancelledError):
                    await task

        app.router.lifespan_context = lifespan
        uvicorn.run(app, host="127.0.0.1", port=4181, log_level="warning")


if __name__ == "__main__":
    main()
