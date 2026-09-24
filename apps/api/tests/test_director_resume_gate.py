"""Resuming a chapter must respect the storyboard review's verdict.

The live failure: an automatic run died during storyboard repair, leaving a
full-length board behind whose review had been rejected. Resume only asked
"are there shots?", saw a complete-looking board, and jumped straight to video
prompts -- skipping the repair the review asked for and never restoring the
run's real progress.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.db.models import DirectorChildStatus
from app.services import director_orchestration as orchestration


def review_child(board_id, *, approved, workflow_id="wf-old", waived=False):
    return SimpleNamespace(
        workflow_id=workflow_id,
        kind="storyboard_review",
        status=DirectorChildStatus.SUCCEEDED,
        input_refs={"storyboard_version_id": board_id},
        output_refs={
            "approved": approved,
            "waived": waived,
            "review": {
                "approved": approved,
                "waived": waived,
                "summary": "通过" if approved else "镜20-30缺少frame_layout",
                "findings": [] if approved else [
                    {"severity": "major", "location": "镜20-30", "issue": "缺少 frame_layout",
                     "suggestion": "补齐"}
                ],
            },
        },
    )


class Session:
    """Just enough of an AsyncSession to answer the review lookup."""

    def __init__(self, children):
        self.children = children

    async def scalars(self, _statement):
        return SimpleNamespace(all=lambda: self.children)


def workflow(chapter_id="chapter-1"):
    return SimpleNamespace(chapter_id=chapter_id, tenant_id="t", id="wf-new")


def lookup(children, board_id, chapter_id="chapter-1"):
    return asyncio.run(orchestration._latest_storyboard_review(
        Session(children), workflow(chapter_id), storyboard_version_id=board_id
    ))


def test_a_rejected_review_is_found_for_the_board_it_belongs_to():
    children = [
        review_child("board-current", approved=False),
        review_child("board-other", approved=True),
    ]
    child, review = lookup(children, "board-current")
    assert child is not None and child.workflow_id == "wf-old"
    assert review["approved"] is False
    assert review["findings"][0]["severity"] == "major"


def test_an_approved_review_is_found_across_runs():
    _, review = lookup([review_child("board-current", approved=True)], "board-current")
    assert review["approved"] is True


def test_a_board_that_was_never_reviewed_has_no_verdict():
    # No verdict must not be mistaken for approval, and must not inherit the
    # rejection of a different board.
    children = [review_child("board-other", approved=False)]
    assert lookup(children, "board-never-reviewed") == (None, {})


@pytest.mark.parametrize("changed", [None, "script_version_id", "repair_mode", "feedback", "review"])
def test_continue_copies_checkpoints_only_for_the_same_inputs(changed):
    payload = {"chapter_id": "ch", "script_version_id": "script", "storyboard_version_id": "board",
               "repair_mode": "partial", "feedback": "意见", "review": {"approved": False}}
    previous = SimpleNamespace(status=orchestration.TaskStatus.FAILED, request_payload={**payload,
        "storyboard_generation": {"key": "exact", "state": {"valid": {"1": {"title": "已完成"}}}}})
    if changed:
        payload[changed] = "new"

    class CheckpointSession:
        async def scalar(self, statement):
            # Query scope must include owner, project, model, and source versions.
            params = statement.compile().params.values()
            assert all(value in params for value in ("tenant", "user", "project", "model"))
            return previous

    workflow = SimpleNamespace(tenant_id="tenant", user_id="user", project_id="project")
    result = asyncio.run(orchestration._resume_storyboard_checkpoint(
        CheckpointSession(), workflow, "director_storyboard_repair", "model", payload))
    if changed:
        assert "storyboard_generation" not in result
    else:
        assert result["storyboard_generation"] == previous.request_payload["storyboard_generation"]
        result["storyboard_generation"]["state"]["valid"].clear()
        assert previous.request_payload["storyboard_generation"]["state"]["valid"]


@pytest.mark.parametrize("status", [orchestration.TaskStatus.SUCCEEDED, orchestration.TaskStatus.CANCELLED])
def test_continue_does_not_resurrect_completed_or_cancelled_work(status):
    class CheckpointSession:
        async def scalar(self, statement):
            return SimpleNamespace(status=status, request_payload={"storyboard_generation": {"state": {}}})

    workflow = SimpleNamespace(tenant_id="t", user_id="u", project_id="p")
    result = asyncio.run(orchestration._resume_storyboard_checkpoint(
        CheckpointSession(), workflow, "chapter_storyboard_generation", "model", {"chapter_id": "ch"}))
    assert "storyboard_generation" not in result


def test_exhausted_batch_retries_preserve_work_instead_of_rebuilding(monkeypatch):
    """A provider failure must not discard finished batches or rebuild a board."""
    from contextlib import asynccontextmanager

    from app.db import session as db_session
    from app.db.models import DirectorWorkflowRun, TaskStatus

    task = SimpleNamespace(
        id="t1", status=TaskStatus.FAILED, error_message="分镜输出仍被截断",
        result_payload={}, request_payload={}, tenant_id="t", user_id="u", project_id="p",
    )
    child = SimpleNamespace(
        id="c1", workflow_id="w1", kind="storyboard_generation", title="制作导演分镜表",
        status=DirectorChildStatus.RUNNING, attempt=5, max_attempts=5,
        details={}, input_refs={}, output_refs={}, summary="", parent_child_run_id=None,
    )
    workflow = SimpleNamespace(
        id="w1", status=orchestration.DirectorWorkflowStatus.RUNNING,
        stage=orchestration.DirectorWorkflowStage.STORYBOARD_REPAIRING,
        stop_requested=False, automation_mode=True, context_snapshot={},
        current_task_id="t1", chapter_id="ch1", tenant_id="t", user_id="u", project_id="p",
        last_error=None, last_message="", storyboard_version_id=None, script_version_id="s1",
        asset_extraction_id="ex1",
    )

    class Session:
        async def get(self, model, key):
            return workflow if model is DirectorWorkflowRun else task

        async def scalar(self, _statement):
            return child

        async def flush(self):
            pass

        async def commit(self):
            pass

    @asynccontextmanager
    async def sessions():
        yield Session()

    rebuilt = []

    async def queue_storyboard(_session, _workflow, parent):
        rebuilt.append(parent)
        return (SimpleNamespace(id="t2"), SimpleNamespace(id="e2"))

    async def dispatch(_session, _queued):
        pass

    monkeypatch.setattr(db_session, "SessionLocal", sessions)
    monkeypatch.setattr(orchestration, "_queue_storyboard", queue_storyboard)
    monkeypatch.setattr(orchestration, "_commit_and_dispatch", dispatch)

    asyncio.run(orchestration._advance_director_task_terminal("t1"))
    assert rebuilt == []
    assert workflow.status == orchestration.DirectorWorkflowStatus.FAILED
    assert "已保留成功批次" in workflow.last_message


def test_the_rebuild_loop_is_bounded(monkeypatch):
    """A chapter that keeps producing unusable boards must eventually stop."""
    from contextlib import asynccontextmanager

    from app.db import session as db_session
    from app.db.models import DirectorWorkflowRun, TaskStatus

    task = SimpleNamespace(
        id="t1", status=TaskStatus.FAILED, error_message="分镜输出仍被截断",
        result_payload={}, request_payload={}, tenant_id="t", user_id="u", project_id="p",
    )
    child = SimpleNamespace(
        id="c1", workflow_id="w1", kind="storyboard_generation", title="制作导演分镜表",
        status=DirectorChildStatus.RUNNING, attempt=5, max_attempts=5,
        details={}, input_refs={}, output_refs={}, summary="", parent_child_run_id=None,
    )
    workflow = SimpleNamespace(
        id="w1", status=orchestration.DirectorWorkflowStatus.RUNNING,
        stage=orchestration.DirectorWorkflowStage.STORYBOARD_REPAIRING,
        stop_requested=False, automation_mode=True,
        context_snapshot={"storyboard_rebuild_count": 2},
        current_task_id="t1", chapter_id="ch1", tenant_id="t", user_id="u", project_id="p",
        last_error=None, last_message="", storyboard_version_id=None, script_version_id="s1",
        asset_extraction_id="ex1",
    )

    class Session:
        async def get(self, model, key):
            return workflow if model is DirectorWorkflowRun else task

        async def scalar(self, _statement):
            return child

        async def commit(self):
            pass

    @asynccontextmanager
    async def sessions():
        yield Session()

    async def boom(*_args, **_kwargs):
        raise AssertionError("the rebuild budget is exhausted; nothing may be queued")

    dispatched = []

    async def dispatch(_session, queued):
        dispatched.append(queued)

    monkeypatch.setattr(db_session, "SessionLocal", sessions)
    monkeypatch.setattr(orchestration, "_queue_storyboard", boom)
    monkeypatch.setattr(orchestration, "_retry_child", boom)
    monkeypatch.setattr(orchestration, "_commit_and_dispatch", dispatch)

    asyncio.run(orchestration._advance_director_task_terminal("t1"))
    assert workflow.status == orchestration.DirectorWorkflowStatus.FAILED
    assert dispatched == [[]]
    assert "子智能体执行未完成" not in (workflow.last_message or "")


def video_stage_workflow(stage):
    return SimpleNamespace(
        id="w1", chapter_id="chapter-1", tenant_id="t",
        stage=stage, storyboard_version_id="board-current",
        current_task_id="t1", last_message="", context_snapshot={},
    )


def demote(monkeypatch, stage, *, approved, waived=False):
    refunded = []
    task = SimpleNamespace(id="t1", status=orchestration.TaskStatus.RUNNING)
    child = SimpleNamespace(
        id="c1", task_id="t1", status=DirectorChildStatus.RUNNING, summary="",
    )
    workflow = video_stage_workflow(stage)

    class Session:
        async def get(self, model, key):
            return task

        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [
                review_child("board-current", approved=approved, waived=waived)
            ])

        async def flush(self):
            pass

    async def refund(_session, cancelled_task, *, reason):
        refunded.append(reason)

    monkeypatch.setattr(orchestration, "refund_task_cost", refund)
    demoted = asyncio.run(
        orchestration._demote_unapproved_video_stage(Session(), workflow, [child])
    )
    return demoted, workflow, child, task, refunded


def test_an_unapproved_board_is_pulled_back_out_of_the_video_stage(monkeypatch):
    demoted, workflow, child, task, refunded = demote(
        monkeypatch, orchestration.DirectorWorkflowStage.VIDEO_PROMPT_GENERATING, approved=False
    )
    assert demoted is True
    assert workflow.stage == orchestration.DirectorWorkflowStage.STORYBOARD_REPAIRING
    assert workflow.current_task_id is None
    assert task.status == orchestration.TaskStatus.CANCELLED
    assert child.status == DirectorChildStatus.CANCELLED
    assert refunded, "the video-stage step must be refunded before re-driving"


def test_an_approved_board_stays_in_the_video_stage(monkeypatch):
    demoted, workflow, _child, task, refunded = demote(
        monkeypatch, orchestration.DirectorWorkflowStage.VIDEO_GENERATING, approved=True
    )
    assert demoted is False
    assert workflow.stage == orchestration.DirectorWorkflowStage.VIDEO_GENERATING
    assert task.status == orchestration.TaskStatus.RUNNING
    assert refunded == []


def test_a_run_that_is_not_in_the_video_stage_is_untouched(monkeypatch):
    demoted, _workflow, _child, task, _refunded = demote(
        monkeypatch, orchestration.DirectorWorkflowStage.STORYBOARD_REVIEWING, approved=False
    )
    assert demoted is False
    assert task.status == orchestration.TaskStatus.RUNNING


def test_a_waived_review_does_not_get_pulled_back_out_of_the_video_stage(monkeypatch):
    """Accepting advisory findings must stick across a resume.

    When the repair budget runs out with only non-blocking findings left, the
    project deliberately continues. Without recording that waiver, the next
    resume would read the still-unapproved verdict and drag the run back to
    storyboard repair every time.
    """
    demoted, workflow, _child, task, refunded = demote(
        monkeypatch, orchestration.DirectorWorkflowStage.VIDEO_PROMPT_GENERATING,
        approved=False, waived=True,
    )
    assert demoted is False
    assert workflow.stage == orchestration.DirectorWorkflowStage.VIDEO_PROMPT_GENERATING
    assert task.status == orchestration.TaskStatus.RUNNING
    assert refunded == []
def test_storyboard_review_stops_nonconvergent_hard_issues():
    from app.services.director_orchestration import storyboard_review_stalled
    snapshot = {}
    result = {"findings": [{"severity": "blocking", "issue": "缺少剧情"}]}
    assert not storyboard_review_stalled(snapshot, result)
    assert not storyboard_review_stalled(snapshot, result)
    assert storyboard_review_stalled(snapshot, result)


def test_storyboard_review_allows_measurable_progress_but_has_round_limit():
    from app.services.director_orchestration import storyboard_review_stalled
    snapshot = {}
    for count in range(10, 5, -1):
        assert not storyboard_review_stalled(snapshot, {"findings": [{"severity": "major"}] * count})
    assert storyboard_review_stalled(snapshot, {"findings": [{"severity": "major"}]})


def test_exhausted_blocking_review_creates_decision_instead_of_another_task(monkeypatch):
    workflow = SimpleNamespace(context_snapshot={"storyboard_version_count": 5},
        tenant_id="t", user_id="u", project_id="p", id="wf", current_task_id="old")
    decisions = []
    session = SimpleNamespace(add=decisions.append)
    async def forbidden(*args, **kwargs):
        pytest.fail("阻断问题达到上限后不应再排队")
    monkeypatch.setattr(orchestration, "_queue_child", forbidden)
    result = asyncio.run(orchestration._queue_automatic_repair(session, workflow,
        SimpleNamespace(id="review"), target="storyboard",
        result={"review": {"findings": [{"severity": "blocking"}]}}))
    assert result is None
    assert workflow.status == orchestration.DirectorWorkflowStatus.WAITING_USER
    assert workflow.stage == orchestration.DirectorWorkflowStage.AWAITING_STORYBOARD_DECISION
    assert workflow.current_task_id is None
    assert len(decisions) == 1
