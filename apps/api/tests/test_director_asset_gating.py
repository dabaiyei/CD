"""A chapter must not deadlock between asset preparation and the storyboard gate.

The live failure: fifteen base assets were READY, two technique derivatives sat
at PROMPT_READY because asset preparation deliberately skips derivatives, and
the pre-storyboard gate then demanded the derivatives. Preparation queued
nothing, the gate refused to continue, and the automatic run stayed "running"
with no task until it was restarted.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.db.models import AssetStatus, AssetType
from app.services import director_orchestration as orchestration


def asset(name, *, asset_type=AssetType.CHARACTER, status=AssetStatus.READY,
          parent=None, media="/uploads/a.webp"):
    return SimpleNamespace(
        id=f"id-{name}",
        name=name,
        asset_type=asset_type,
        status=status,
        parent_asset_id=parent,
        media_url=media,
        asset_metadata={},
    )


def workflow(**snapshot):
    return SimpleNamespace(
        id="wf",
        asset_extraction_id="ex",
        storyboard_version_id=None,
        context_snapshot=dict(snapshot),
    )


@pytest.fixture
def base_and_derivative(monkeypatch):
    base = asset("苏郁")
    derivative = asset("苏郁·破甲", status=AssetStatus.PROMPT_READY, parent=base.id, media=None)

    async def extraction_assets(_session, _workflow):
        return [base, derivative]

    async def materialize(_key):
        return None

    monkeypatch.setattr(orchestration, "_extraction_assets", extraction_assets)
    monkeypatch.setattr(orchestration, "materialize_media_file", materialize)
    monkeypatch.setattr(orchestration, "object_key_from_media_url", lambda url: url)
    return base, derivative


def awaiting_assets(snapshot):
    async def run():
        wf = workflow(**snapshot)
        awaiting = await orchestration._assets_awaiting_generation(None, wf)
        return await orchestration._missing_ready_asset_names(awaiting)

    return asyncio.run(run())


def test_a_ready_base_asset_does_not_wait_on_its_derivative(base_and_derivative):
    # Before the storyboard only base assets are prepared, so the technique
    # derivative must not be reported as a blocker.
    assert awaiting_assets({}) == []


def test_a_derivative_is_required_once_the_storyboard_references_it(base_and_derivative):
    # After the board exists the derivative it references is genuinely needed.
    assert awaiting_assets({"assets_for_storyboard_review": True}) == ["苏郁·破甲"]


def test_a_missing_base_asset_still_blocks_early(base_and_derivative):
    base, _ = base_and_derivative
    base.status = AssetStatus.PROMPT_READY
    base.media_url = None

    assert awaiting_assets({}) == ["苏郁"]


def test_a_failed_advance_parks_the_run_instead_of_leaving_it_running(monkeypatch):
    """The step after a finished task can fail where the task itself never sees it.

    Silently swallowing that left the run "running" with no queued task, so the
    chapter looked busy forever and "continue" reported that its images were not
    finished. The failure must instead be recorded on the workflow.
    """
    parked: list[tuple[str, str, bool]] = []

    async def advance(_task_id):
        raise RuntimeError("生成分镜前必须先完成资产图片：苏郁·破甲")

    async def park(task_id, message):
        parked.append((task_id, message, True))

    async def boom(*_args, **_kwargs):
        raise AssertionError("a non-402 advance failure must not pause for credits")

    monkeypatch.setattr(orchestration, "_advance_director_task_terminal", advance)
    monkeypatch.setattr(orchestration, "_fail_workflow_after_advance_error", park)
    monkeypatch.setattr(orchestration, "_pause_workflow_for_missing_credits", boom)

    with pytest.raises(RuntimeError, match="必须先完成资产图片"):
        asyncio.run(orchestration.on_director_task_terminal("task-1"))

    assert parked == [("task-1", "生成分镜前必须先完成资产图片：苏郁·破甲", True)]
