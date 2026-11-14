"""The GPU is given to the vision model for the look and given back (tools/image_describe.py, LocalDeploySwap)."""

from __future__ import annotations

import httpx
import pytest

from agent_control.schemas import ToolResultStatus
from agent_control.tools import image_describe
from test_image_describe import _adapter, _picture, _request, _scanned_pdf  # noqa: E402 - the helpers of the neighbouring tests


class _Swap:
    def __init__(self) -> None:
        self.steps: list[tuple] = []

    async def make_room(self, root: str) -> None:
        self.steps.append(("room", root))

    async def release(self, root: str, profile: str) -> None:
        self.steps.append(("release", root, profile))


def _swapping(tmp_path, swap, *, answers=("TOTAL 1",), **vision):
    adapter, seen = _adapter(tmp_path, answers=answers, **vision)
    adapter._swap = swap if adapter.settings.vision.swap_models else None
    return adapter, seen


@pytest.mark.asyncio
async def test_the_gpu_is_freed_before_the_look_and_the_vision_model_unloaded_after(tmp_path) -> None:
    swap = _Swap()
    adapter, _ = _swapping(tmp_path, swap)
    result = await adapter.execute(_request(_picture(tmp_path)))
    assert result.status == ToolResultStatus.SUCCEEDED
    assert swap.steps == [("room", "http://127.0.0.1:8000"), ("release", "http://127.0.0.1:8000", "qwen3vl_4b_ollama")]


@pytest.mark.asyncio
async def test_the_vision_model_is_unloaded_even_when_the_look_fails(tmp_path) -> None:
    swap = _Swap()
    adapter, _ = _swapping(tmp_path, swap, answers=[RuntimeError("the model fell over")])
    result = await adapter.execute(_request(_picture(tmp_path)))
    assert result.status == ToolResultStatus.FAILED and swap.steps[-1][0] == "release"


@pytest.mark.asyncio
async def test_a_scanned_pdf_swaps_once_for_all_its_pages(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    swap = _Swap()
    adapter, seen = _swapping(tmp_path, swap, answers=("a", "b"))
    await adapter.execute(_request(_scanned_pdf(tmp_path)))
    assert [step[0] for step in swap.steps] == ["room", "release"] and len(seen["provider"].calls) == 2


@pytest.mark.asyncio
async def test_nothing_is_swapped_when_it_is_off_or_there_is_no_swapper(tmp_path) -> None:
    swap = _Swap()
    adapter, _ = _swapping(tmp_path, swap, swap_models=False)
    assert (await adapter.execute(_request(_picture(tmp_path)))).status == ToolResultStatus.SUCCEEDED and swap.steps == []
    plain, _ = _adapter(tmp_path)  # the adapter a test builds has no swapper: it never talks to a real LocalDeploy
    assert plain._swap is None and (await plain.execute(_request(_picture(tmp_path)))).status == ToolResultStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_localdeploy_is_asked_to_free_the_gpu_and_to_stop_the_vision_model_and_failures_are_not_fatal(monkeypatch) -> None:
    asked: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.path, request.content.decode()))
        return httpx.Response(200, json={"success": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(image_describe.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    swap = image_describe.LocalDeploySwap()
    await swap.make_room("http://127.0.0.1:8000")
    await swap.release("http://127.0.0.1:8000", "qwen3vl_4b_ollama")
    assert asked == [("/models/free", "{}"), ("/models/stop", '{"profile":"qwen3vl_4b_ollama"}')]

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(image_describe.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(refuse), **kw))
    await swap.make_room("http://127.0.0.1:8000")  # does not raise


def test_swapping_is_on_by_default() -> None:
    from agent_control.config import AppSettings

    assert AppSettings(_env_file=None).vision.swap_models is True
