"""Carry out a recipe: fetch each file from its written address, check its hash, run its command, then switch its capability on.

The order is the safe one. Files first, into a folder of this assistant's own; the command next; the configuration last, so a download that
fails or a hash that does not match leaves the configuration exactly as it was and the capability off. Nothing here takes an address, a
command or a path from a request: they come from the recipe, which is data in this repository.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from agent_control.abilities.recipes import Download, Recipe

#: Fetches one file to a path. Replaced in tests; the real one streams over HTTPS and follows the redirects GitHub uses.
Fetcher = Callable[[str, Path], Awaitable[None]]
#: Runs a command line and returns (exit code, the last of its output). Replaced in tests.
Runner = Callable[[tuple[str, ...], int], Awaitable[tuple[int, str]]]
#: Makes sure a service a command needs is running ("ollama"); True when it is.
Starter = Callable[[str], Awaitable[bool]]


class AcquireError(RuntimeError):
    """What went wrong, in words the user can act on."""


@dataclass
class Acquired:
    recipe: str
    files: list[str] = field(default_factory=list)
    configured: list[str] = field(default_factory=list)
    command_output: str = ""


async def fetch_https(url: str, destination: Path) -> None:
    if not url.startswith("https://"):
        raise AcquireError("only https addresses are fetched")
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0)) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise AcquireError(f"{url.rsplit('/', 1)[-1]} could not be fetched (HTTP {response.status_code})")
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 256):
                    handle.write(chunk)


async def run_command(command: tuple[str, ...], timeout_seconds: int) -> tuple[int, str]:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, creationflags=flags,
        )
    except FileNotFoundError:
        raise AcquireError(f"{command[0]} is not installed on this computer") from None
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        raise AcquireError(f"{command[0]} did not finish in {timeout_seconds // 60} minutes") from None
    return process.returncode or 0, (out or b"").decode("utf-8", errors="replace")[-600:]


async def start_service(name: str) -> bool:
    if name == "ollama":
        from agent_control.tools.image_describe import _start_ollama

        return await _start_ollama()
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        merged[key] = deep_merge(merged[key], value) if isinstance(value, dict) and isinstance(merged.get(key), dict) else value
    return merged


class Acquirer:
    def __init__(
        self,
        models_dir: Path,
        *,
        read_config: Callable[[], dict[str, Any]],
        write_config: Callable[[dict[str, Any]], None],
        fetch: Fetcher = fetch_https,
        run: Runner = run_command,
        start: Starter = start_service,
    ) -> None:
        self.models_dir = models_dir
        self._read_config = read_config
        self._write_config = write_config
        self._fetch = fetch
        self._run = run
        self._start = start

    async def install(self, recipe: Recipe) -> Acquired:
        result = Acquired(recipe=recipe.id)
        folder = recipe.folder(self.models_dir).resolve()
        for download in recipe.downloads:
            result.files.append(str(await self._download(download, folder)))
        if recipe.command:
            if recipe.needs_service and not await self._start(recipe.needs_service):
                raise AcquireError(f"{recipe.needs_service} is not running and could not be started")
            code, output = await self._run(recipe.command, recipe.command_timeout_seconds)
            result.command_output = output
            if code != 0:
                raise AcquireError(f"{recipe.command[0]} did not succeed (exit {code}): {output.strip()[-300:]}")
        if recipe.config is not None:
            updates = recipe.config(folder)
            self._write_config(deep_merge(self._read_config(), updates))
            result.configured = sorted(updates)
        return result

    async def _download(self, download: Download, folder: Path) -> Path:
        destination = folder / download.name
        if destination.exists() and _sha256(destination) == download.sha256:
            return destination
        folder.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        try:
            await self._fetch(download.url, partial)
            if partial.stat().st_size != download.size_bytes or _sha256(partial) != download.sha256:
                raise AcquireError(f"{download.name} did not match the file this was written for, so it was not kept")
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
        return destination
